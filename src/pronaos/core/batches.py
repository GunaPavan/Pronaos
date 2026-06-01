"""Provider batch clients for the async batches API (Phase 59).

Both OpenAI and Anthropic ship async batch APIs that complete in
24h at 50% of synchronous pricing. This module provides:

1. ``BatchClient`` — Protocol both provider clients satisfy.
2. ``OpenAIBatchClient`` — uses Files API + Batches API.
3. ``AnthropicBatchClient`` — uses Messages Batches API (inline).
4. ``batch_cost_hcents`` — half-of-sync cost math using the
   provider catalog's per-model pricing.
5. ``normalize_status`` — provider-specific status → Pronaos's
   normalized state machine (validating | in_progress | finalizing
   | completed | failed | expired | cancelled).

v1 supports chat completions only. The ``endpoint`` field on the
batch row is a forward-compatible hook for the embeddings batches
follow-up.

Design notes
------------
- **Both clients are HTTP-direct, not via the catalog adapter.**
  The synchronous adapters (anthropic.py, openai_compat.py) speak
  the per-call chat completions wire; batches use different paths
  (``/v1/messages/batches``, ``/v1/files`` + ``/v1/batches``). We
  reuse the catalog's API keys but not the chat adapters themselves.
- **JSONL is the lingua franca.** Both providers' batch results are
  JSONL where each line is one request's outcome. We serialise our
  inline ``requests`` array to JSONL at submit time + parse it back
  at retrieve time. The DB row's ``input_payload`` / ``output_payload``
  store the JSONL verbatim for replay + audit.
- **Cost math is provider-aware.** OpenAI's batch pricing is exactly
  half of its sync rate per token. Anthropic's batch pricing is also
  exactly half. The shared ``batch_cost_hcents`` helper looks up the
  catalog's ``input_hcents_per_mtok`` / ``output_hcents_per_mtok``
  and multiplies by 0.5 before applying the per-token math.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from pronaos.providers.catalog import CATALOG

# Both OpenAI and Anthropic ship batches at half the sync rate.
# Encoded as a percentage * 100 (so 50 -> 0.50x multiplier) for
# integer math symmetry with Phase 34's cache pricing (which uses
# 125/100 for cache writes and 10/100 for cache reads).
BATCH_COST_MULTIPLIER_NUMERATOR = 50
BATCH_COST_MULTIPLIER_DENOMINATOR = 100


# Pronaos's normalized status taxonomy. Both provider clients map
# upstream status strings onto this set; downstream consumers
# (endpoint, worker, CLI) only see normalized values.
NORMALIZED_STATUSES = frozenset(
    {
        "validating",
        "in_progress",
        "finalizing",
        "completed",
        "failed",
        "expired",
        "cancelled",
    }
)


@dataclass(frozen=True, slots=True)
class BatchSubmission:
    """Result of submitting a batch to the provider."""

    provider_batch_id: str
    initial_status: str


@dataclass(frozen=True, slots=True)
class BatchStatus:
    """One snapshot of a batch's provider-side state."""

    provider_batch_id: str
    status: str
    request_count: int
    completed_count: int
    failed_count: int
    # Set on terminal completion; identifies how to fetch results.
    # Anthropic returns a results_url; OpenAI returns an output_file_id.
    results_handle: str | None = None
    error_message: str | None = None


class BatchClient(Protocol):
    """The minimal contract a provider batch client satisfies."""

    async def submit(
        self,
        *,
        requests_jsonl: str,
        endpoint: str = "/v1/chat/completions",
    ) -> BatchSubmission:
        """Submit an inline JSONL batch. Returns the provider's
        opaque batch id + the initial normalized status. ``endpoint``
        selects which API the upstream targets — only OpenAI's
        Batches API honors values other than the default."""
        ...

    async def poll(self, *, provider_batch_id: str) -> BatchStatus:
        """Fetch the current status of the batch."""
        ...

    async def retrieve_results(self, *, results_handle: str) -> str:
        """Fetch the result JSONL by the handle returned in
        ``BatchStatus.results_handle``."""
        ...

    async def cancel(self, *, provider_batch_id: str) -> None:
        """Cancel an in-flight batch. No-op if already terminal."""
        ...

    async def aclose(self) -> None:
        """Release the underlying HTTP client. Idempotent."""
        ...


# --------------------------------------------------------------------------- #
# Cost math                                                                   #
# --------------------------------------------------------------------------- #


def batch_cost_hcents(
    *,
    provider_key: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    endpoint: str = "/v1/chat/completions",
) -> int:
    """Compute batch cost in hundredths-of-a-cent — half the sync rate.

    Looks up the catalog's per-model ``input_hcents_per_mtok`` /
    ``output_hcents_per_mtok``, multiplies by 0.5, and applies the
    per-token math. Integer math throughout (no float drift in
    chargeback). Returns 0 when the model isn't in the catalog —
    same defensive posture as the sync chat handler.

    The ``endpoint`` discriminator routes the catalog lookup:

    - ``/v1/chat/completions`` (default) → ``entry.pricing``
    - ``/v1/embeddings`` (Phase 60) → ``entry.embedding_pricing``,
      which is keyed by bare embedding model name (no prefix) and
      uses input-only pricing (output_hcents_per_mtok = 0 by
      construction in the catalog).
    """
    entry = CATALOG.get(provider_key)
    if entry is None:
        return 0
    # Choose the right pricing dict for the endpoint.
    pricing_dict = (
        entry.embedding_pricing
        if endpoint == "/v1/embeddings"
        else entry.pricing
    )
    if not pricing_dict:
        return 0
    # Some catalog entries are keyed by short name, some by prefix.
    # The synchronous chat code does this lookup via the OpenAI-
    # compat provider wrapper; here we replicate it directly.
    pricing = pricing_dict.get(model)
    if pricing is None:
        # Anthropic-direct provider keys model by bare name (no prefix);
        # embedding entries are keyed by bare name too.
        bare = model.split("/", 1)[-1]
        pricing = pricing_dict.get(bare)
    if pricing is None:
        return 0
    # Per-token cost: tokens * hcents_per_mtok / 1_000_000.
    # Then halve via the batch multiplier.
    input_cost = (
        prompt_tokens
        * pricing.input_hcents_per_mtok
        * BATCH_COST_MULTIPLIER_NUMERATOR
        // (1_000_000 * BATCH_COST_MULTIPLIER_DENOMINATOR)
    )
    output_cost = (
        completion_tokens
        * pricing.output_hcents_per_mtok
        * BATCH_COST_MULTIPLIER_NUMERATOR
        // (1_000_000 * BATCH_COST_MULTIPLIER_DENOMINATOR)
    )
    return input_cost + output_cost


# --------------------------------------------------------------------------- #
# Status normalization                                                        #
# --------------------------------------------------------------------------- #


# OpenAI's status vocabulary -> Pronaos normalized.
_OPENAI_STATUS_MAP: dict[str, str] = {
    "validating": "validating",
    "in_progress": "in_progress",
    "finalizing": "finalizing",
    "completed": "completed",
    "failed": "failed",
    "expired": "expired",
    "cancelling": "cancelled",
    "cancelled": "cancelled",
}

# Anthropic's vocabulary -> Pronaos normalized.
_ANTHROPIC_STATUS_MAP: dict[str, str] = {
    # Anthropic uses ``processing_status`` with values:
    # in_progress | canceling | ended (with results_url on completion)
    "in_progress": "in_progress",
    "canceling": "cancelled",
    "ended": "completed",
    # Defensive: Anthropic also sometimes returns these in the result_type
    # field for individual requests; we don't map those at the batch level
    # but include for completeness if the API evolves.
    "expired": "expired",
    "errored": "failed",
}


def normalize_openai_status(raw: str) -> str:
    """Map OpenAI's batch status string to Pronaos normalized."""
    return _OPENAI_STATUS_MAP.get(raw, "failed")


def normalize_anthropic_status(raw: str) -> str:
    """Map Anthropic's processing_status to Pronaos normalized."""
    return _ANTHROPIC_STATUS_MAP.get(raw, "failed")


# --------------------------------------------------------------------------- #
# OpenAI Batch Client                                                         #
# --------------------------------------------------------------------------- #


class OpenAIBatchClient:
    """Batch client for OpenAI's Batches API.

    Submission requires uploading a JSONL file via ``POST /v1/files``
    with ``purpose=batch``, then creating the batch via
    ``POST /v1/batches`` referencing the file_id. v1 wraps both
    steps transparently — callers see a single ``submit()``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        # Caller may share an httpx client across providers; if not
        # supplied we create a fresh one. Closed via ``aclose()``.
        self._http = http or httpx.AsyncClient(timeout=60.0)
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def submit(
        self,
        *,
        requests_jsonl: str,
        endpoint: str = "/v1/chat/completions",
    ) -> BatchSubmission:
        # Step 1: upload the JSONL as a file with purpose=batch.
        # httpx accepts a Mapping[str, tuple[filename, content, type]]
        # for multi-part uploads; we mix the file with the form field
        # ``purpose`` via the same dict (httpx treats tuples with
        # filename=None as plain form fields).
        files: dict[str, tuple[str | None, bytes | str, str | None]] = {
            "file": (
                "batch.jsonl",
                requests_jsonl.encode("utf-8"),
                "application/jsonl",
            ),
            "purpose": (None, "batch", None),
        }
        r = await self._http.post(
            f"{self._base_url}/v1/files", headers=self._headers(), files=files
        )
        r.raise_for_status()
        file_id = r.json()["id"]

        # Step 2: create the batch. ``endpoint`` is the upstream's
        # target API path — OpenAI's batches API supports both
        # /v1/chat/completions and /v1/embeddings (Phase 60).
        body = {
            "input_file_id": file_id,
            "endpoint": endpoint,
            "completion_window": "24h",
        }
        r = await self._http.post(
            f"{self._base_url}/v1/batches",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        return BatchSubmission(
            provider_batch_id=data["id"],
            initial_status=normalize_openai_status(data.get("status", "validating")),
        )

    async def poll(self, *, provider_batch_id: str) -> BatchStatus:
        r = await self._http.get(
            f"{self._base_url}/v1/batches/{provider_batch_id}",
            headers=self._headers(),
        )
        r.raise_for_status()
        data = r.json()
        counts = data.get("request_counts") or {}
        # output_file_id is set on completion; we surface it as
        # results_handle so downstream callers don't care which
        # provider produced it.
        results_handle = data.get("output_file_id")
        errors = data.get("errors") or {}
        error_msg: str | None = None
        if isinstance(errors, dict) and errors.get("data"):
            # Errors are a list of {code, message, ...} dicts.
            first = errors["data"][0]
            error_msg = f"{first.get('code')}: {first.get('message')}"
        return BatchStatus(
            provider_batch_id=provider_batch_id,
            status=normalize_openai_status(data.get("status", "failed")),
            request_count=int(counts.get("total") or 0),
            completed_count=int(counts.get("completed") or 0),
            failed_count=int(counts.get("failed") or 0),
            results_handle=results_handle,
            error_message=error_msg,
        )

    async def retrieve_results(self, *, results_handle: str) -> str:
        # OpenAI's output_file_id → GET /v1/files/{id}/content
        r = await self._http.get(
            f"{self._base_url}/v1/files/{results_handle}/content",
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.text

    async def cancel(self, *, provider_batch_id: str) -> None:
        r = await self._http.post(
            f"{self._base_url}/v1/batches/{provider_batch_id}/cancel",
            headers=self._headers(),
        )
        # 200 OK or 4xx if already terminal — both are fine.
        if r.status_code >= 500:
            r.raise_for_status()


# --------------------------------------------------------------------------- #
# Anthropic Batch Client                                                      #
# --------------------------------------------------------------------------- #


class AnthropicBatchClient:
    """Batch client for Anthropic's Message Batches API.

    Anthropic accepts inline requests in the submission body — no
    separate file upload step. We translate the OpenAI-shape JSONL
    (one ``{custom_id, body: {model, messages, ...}}`` per line)
    into Anthropic's ``{custom_id, params: {model, messages, ...}}``
    shape at submit time.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        anthropic_version: str = "2023-06-01",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._version = anthropic_version
        self._http = http or httpx.AsyncClient(timeout=60.0)
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._version,
            "content-type": "application/json",
        }

    async def submit(
        self,
        *,
        requests_jsonl: str,
        endpoint: str = "/v1/chat/completions",
    ) -> BatchSubmission:
        # Anthropic's batches API serves /v1/messages only — no
        # embeddings, no other endpoints. The caller (api/v1/batches.py)
        # rejects non-chat endpoints on Anthropic before reaching here;
        # this assertion catches misuse from internal code paths.
        if endpoint != "/v1/chat/completions":
            raise ValueError(
                f"Anthropic batches API only supports /v1/chat/completions; "
                f"got {endpoint!r}"
            )
        # Translate OpenAI-shape JSONL into Anthropic's inline shape.
        # Each input line: {"custom_id": "...", "body": {...chat body...}}
        # Each output:     {"custom_id": "...", "params": {...messages body...}}
        requests = []
        for line in requests_jsonl.splitlines():
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            requests.append(
                {
                    "custom_id": entry["custom_id"],
                    "params": entry["body"],
                }
            )
        body = {"requests": requests}
        r = await self._http.post(
            f"{self._base_url}/v1/messages/batches",
            headers=self._headers(),
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        return BatchSubmission(
            provider_batch_id=data["id"],
            initial_status=normalize_anthropic_status(
                data.get("processing_status", "in_progress")
            ),
        )

    async def poll(self, *, provider_batch_id: str) -> BatchStatus:
        r = await self._http.get(
            f"{self._base_url}/v1/messages/batches/{provider_batch_id}",
            headers=self._headers(),
        )
        r.raise_for_status()
        data = r.json()
        counts = data.get("request_counts") or {}
        # Anthropic surfaces results_url when processing_status == ended.
        results_handle = data.get("results_url")
        return BatchStatus(
            provider_batch_id=provider_batch_id,
            status=normalize_anthropic_status(
                data.get("processing_status", "in_progress")
            ),
            request_count=int(counts.get("processing", 0) or 0)
            + int(counts.get("succeeded", 0) or 0)
            + int(counts.get("errored", 0) or 0)
            + int(counts.get("canceled", 0) or 0)
            + int(counts.get("expired", 0) or 0),
            completed_count=int(counts.get("succeeded", 0) or 0),
            failed_count=int(counts.get("errored", 0) or 0)
            + int(counts.get("canceled", 0) or 0)
            + int(counts.get("expired", 0) or 0),
            results_handle=results_handle,
        )

    async def retrieve_results(self, *, results_handle: str) -> str:
        # Anthropic's results_url is an absolute URL, not a path —
        # we hit it directly with the same auth headers.
        r = await self._http.get(results_handle, headers=self._headers())
        r.raise_for_status()
        return r.text

    async def cancel(self, *, provider_batch_id: str) -> None:
        r = await self._http.post(
            f"{self._base_url}/v1/messages/batches/{provider_batch_id}/cancel",
            headers=self._headers(),
        )
        if r.status_code >= 500:
            r.raise_for_status()


# --------------------------------------------------------------------------- #
# Result parsing                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BatchResultRow:
    """One row of a parsed result JSONL — provider-normalized."""

    custom_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    is_error: bool
    error_message: str | None = None


def parse_openai_result_jsonl(jsonl: str) -> list[BatchResultRow]:
    """Each OpenAI result line:
        {"id": "...", "custom_id": "...", "response": {...completion...},
         "error": null|{...}}
    """
    rows: list[BatchResultRow] = []
    for line in jsonl.splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        custom_id = entry.get("custom_id", "")
        err = entry.get("error")
        if err:
            rows.append(
                BatchResultRow(
                    custom_id=custom_id,
                    model="",
                    prompt_tokens=0,
                    completion_tokens=0,
                    is_error=True,
                    error_message=str(err.get("message") or err),
                )
            )
            continue
        resp = entry.get("response") or {}
        body = resp.get("body") or {}
        usage = body.get("usage") or {}
        rows.append(
            BatchResultRow(
                custom_id=custom_id,
                model=body.get("model", ""),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                is_error=False,
            )
        )
    return rows


def parse_anthropic_result_jsonl(jsonl: str) -> list[BatchResultRow]:
    """Each Anthropic result line:
        {"custom_id": "...", "result": {"type": "succeeded"|"errored",
         "message": {...} | "error": {...}}}
    """
    rows: list[BatchResultRow] = []
    for line in jsonl.splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        custom_id = entry.get("custom_id", "")
        result = entry.get("result") or {}
        result_type = result.get("type")
        if result_type != "succeeded":
            err = result.get("error") or {}
            rows.append(
                BatchResultRow(
                    custom_id=custom_id,
                    model="",
                    prompt_tokens=0,
                    completion_tokens=0,
                    is_error=True,
                    error_message=str(err.get("message") or result_type),
                )
            )
            continue
        msg = result.get("message") or {}
        usage = msg.get("usage") or {}
        rows.append(
            BatchResultRow(
                custom_id=custom_id,
                model=msg.get("model", ""),
                prompt_tokens=int(usage.get("input_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or 0),
                is_error=False,
            )
        )
    return rows


def summarize_results(rows: list[BatchResultRow]) -> dict[str, int]:
    """Compute aggregate counts + token totals across a result set."""
    completed = sum(1 for r in rows if not r.is_error)
    failed = sum(1 for r in rows if r.is_error)
    prompt = sum(r.prompt_tokens for r in rows)
    completion = sum(r.completion_tokens for r in rows)
    return {
        "completed_count": completed,
        "failed_count": failed,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
    }


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def provider_from_model(model: str) -> str:
    """Determine which batch provider serves this model.

    OpenAI batch supports OpenAI-shape models (gpt-*, o1, o3).
    Anthropic batch supports Claude models. Pronaos's model prefix
    convention (``openai/gpt-4o-mini``, ``anthropic/claude-sonnet-4-5``)
    makes the routing trivial; the bare model name (``gpt-4o-mini``)
    falls back to OpenAI for OpenAI-style names, Anthropic for
    Claude names.
    """
    if "/" in model:
        prefix = model.split("/", 1)[0]
        if prefix == "openai":
            return "openai"
        if prefix == "anthropic":
            return "anthropic"
    # OpenAI chat models: gpt-*, o1*, o3*.
    if model.startswith(("gpt-", "o1", "o3")):
        return "openai"
    # OpenAI embedding models — Phase 60. Anthropic doesn't expose
    # an embeddings API at all, so any bare embedding-flavoured name
    # routes to OpenAI unambiguously.
    if model.startswith("text-embedding-"):
        return "openai"
    if model.startswith("claude"):
        return "anthropic"
    raise ValueError(
        f"cannot determine batch provider for model {model!r}; use "
        "an explicit prefix (openai/* or anthropic/*) or a model "
        "name beginning with gpt-/o1/o3/text-embedding- (OpenAI) "
        "or claude (Anthropic)"
    )
