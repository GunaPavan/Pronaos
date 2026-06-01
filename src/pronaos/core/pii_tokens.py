"""Reversible PII tokenization (Phase 38).

Existing guardrails REDACT — replace matched PII with ``[REDACTED-EMAIL]``
and similar generic markers. The substitution is one-way and lossy:
the upstream LLM can't reason about an entity it never sees, and the
client never gets the original back. Claim #3 already showed this
breaks topically-relevant cases (where the PII *is* the question).

This module ships the reversible alternative: TOKENIZE. Matched PII is
replaced with a deterministic, per-tenant-salted token like
``[EMAIL_a3f7c2e1b890]``. The mapping ``token -> original`` is stored
in Redis with a per-team TTL. After the upstream responds, the chat
handler scans the response for ``[TYPE_HASH]`` patterns and reverses
the substitution before returning to the client.

Three properties this guarantees, by design:

1. **The upstream LLM never sees the original PII.** Compliance perimeter
   (HIPAA / GDPR pseudonymization) preserved: the gateway is the
   "key holder," the LLM is the "data processor" that only sees
   pseudonyms.
2. **Entity tracking is preserved.** Two mentions of "John Doe" produce
   the same token (deterministic per (tenant, value)). The LLM can
   correctly answer "is the email address in turn 1 the same as in
   turn 3?" without ever seeing the actual email.
3. **The client gets real data back.** The response is detokenized
   gateway-side. Application code doesn't have to write its own
   reversal logic, and there's no round-trip to re-fetch originals.

Token format
------------
``[TYPE_HASH]`` where:

- ``TYPE`` is an uppercase short name matching the rule's category
  (``EMAIL``, ``PHONE``, ``SSN``, ``IPV4``, ``CC``, ``NAME`` for
  Presidio-detected proper nouns).
- ``HASH`` is the 12-character lowercase-hex prefix of
  ``sha256(tenant_id || ':' || original_value)``. Salted by tenant_id
  so the same value across two tenants produces different tokens —
  no inference leak across tenants. 12 chars = 48 bits of entropy,
  more than enough for per-tenant uniqueness at any realistic scale.

Storage
-------
Redis key ``pronaos:pii_token:{tenant_id}:{token}`` -> original value,
TTL = team's ``pii_token_ttl_seconds`` (default 3600s). The mapping
is the only place the original is held while the request is in
flight — once the TTL expires, the original is gone, and any future
LLM-emitted token that points at it surfaces as orphaned (the
detokenizer leaves it in place and increments a metric).

Failure modes
-------------
- **Redis outage during ingress write**: tokenization fails -> the
  caller (chat handler) falls back to redaction so the request still
  goes through. PII still doesn't reach the upstream, but the client
  also can't get the original back. Documented in the chat handler.
- **Redis outage during egress reverse**: tokens stay in the response.
  The client sees ``[EMAIL_a3f7...]`` instead of ``john@example.com``.
  Operationally visible via the ``orphaned`` metric. Better than 5xx-ing
  a response we already got from the upstream.
- **LLM hallucinates a token that doesn't exist**: ``GET`` returns
  ``None``; the token stays in the response, the orphaned counter
  ticks. Logging + dashboards surface this so operators can tell
  apart "Redis ate our mapping" from "the model made up a token."
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from dataclasses import dataclass
from typing import Final

from redis.asyncio import Redis

from pronaos.logging import get_logger

log = get_logger(__name__)


# 1 hour default. Matches the agent-turn TTL default — most tool-using
# agent loops finish within minutes, but a generous default avoids
# surprising operators when a multi-turn conversation continues past
# the original message.
DEFAULT_TTL_SECONDS: Final = 3600

# ``[TYPE_HASH]`` capture. TYPE is uppercase letters; HASH is 12
# lowercase hex chars. The strict shape lets us scan response text
# safely without false positives on Markdown ``[link][id]`` or LaTeX
# ``[Eq. 1]`` style brackets.
_TOKEN_RE: Final = re.compile(r"\[([A-Z]{1,16})_([a-f0-9]{12})\]")

# Tokens are at most ``1 + 16 + 1 + 12 + 1`` = 31 chars (``[TYPE_HASH]``).
# StreamingDetokenizer uses this as the worst-case partial-token buffer
# size at chunk boundaries — anything shorter than this at the tail of
# a chunk MIGHT be the start of a token; we hold it until either the
# next chunk arrives (concatenate + rescan) or the stream ends (flush).
_MAX_TOKEN_LEN: Final = 1 + 16 + 1 + 12 + 1


# --------------------------------------------------------------------------- #
# Token derivation                                                            #
# --------------------------------------------------------------------------- #


def make_token(*, tenant_id: str, rule_name: str, value: str) -> str:
    """Produce the deterministic token for one PII value.

    Same inputs always produce the same output — the LLM sees
    consistent pseudonyms across the prompt and across follow-up
    turns within the TTL window. The tenant_id salt prevents an
    attacker who learns one tenant's tokens from inferring another
    tenant's tokens for the same value.

    ``rule_name`` is expected in the form ``pii.email`` /
    ``pii.phone`` / ``pii.ipv4`` etc. We extract the suffix after
    the last dot and upper-case it for the token TYPE. Falls back
    to ``PII`` for rules without the dotted prefix.
    """
    suffix = rule_name.rsplit(".", 1)[-1] if "." in rule_name else "PII"
    type_label = _type_label_for(suffix)
    digest = hashlib.sha256(f"{tenant_id}:{value}".encode()).hexdigest()
    return f"[{type_label}_{digest[:12]}]"


def _type_label_for(suffix: str) -> str:
    """Map rule suffix to the short token label.

    A small alias map keeps the most operationally-common token types
    short (``CC`` not ``CREDIT_CARD``, ``IPV4`` not ``IPV4_ADDRESS``).
    Anything not in the map gets uppercased verbatim.
    """
    aliases = {
        "credit_card": "CC",
        "ipv4": "IPV4",
        "email": "EMAIL",
        "phone": "PHONE",
        "ssn": "SSN",
        # Presidio rule families surface long names; normalise the
        # operationally-common ones. Other Presidio entities pass
        # through uppercased.
        "person": "NAME",
        "location": "LOC",
        "date_time": "DATE",
    }
    return aliases.get(suffix.lower(), suffix.upper())


# --------------------------------------------------------------------------- #
# TokenStore — Redis-backed mapping                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DetokenizeOutcome:
    """Result of scanning a string for tokens and reversing them.

    ``text`` is the reversed string (tokens that resolved are replaced
    with their original values; tokens that didn't resolve are left
    in place). ``reversed_count`` and ``orphaned_count`` feed the
    metrics so we can distinguish "Redis flaked" from "the model
    made up a token."
    """

    text: str
    reversed_count: int = 0
    orphaned_count: int = 0
    # Per-rule breakdown for the metric. Keyed by the rule short name
    # (``email``, ``phone``, etc.) so the metric label matches the
    # ingress side.
    reversed_by_type: dict[str, int] | None = None
    orphaned_by_type: dict[str, int] | None = None


class TokenStore:
    """Redis-backed mapping ``token -> original``, scoped per tenant.

    All keys are namespaced ``pronaos:pii_token:{tenant_id}:{token}`` so
    a tenant operator deleting one tenant's data can do so with a
    single ``SCAN+DEL`` over the tenant prefix. Cross-tenant reads
    are impossible by construction — even if a malicious client
    guessed another tenant's hash, the lookup goes against THEIR
    own tenant prefix.
    """

    def __init__(self, redis: Redis[bytes]) -> None:
        self._redis = redis

    @staticmethod
    def _key(tenant_id: str, token: str) -> str:
        # The token already carries the brackets. Strip them in the
        # Redis key — keeps the namespace cleaner and avoids edge
        # cases where Redis CLI tools mishandle ``[`` / ``]``.
        return f"pronaos:pii_token:{tenant_id}:{token.strip('[]')}"

    async def store_many(
        self,
        *,
        tenant_id: str,
        mappings: list[tuple[str, str]],
        ttl_seconds: int,
    ) -> int:
        """Persist ``(token, original)`` pairs with a per-key TTL.

        Returns the number of writes that succeeded. Uses a pipeline
        so N writes are one round-trip. Failures are logged and the
        method returns the partial count — the caller (chat handler)
        decides whether to fall back to redaction.

        Same pair written twice overwrites with the same value (the
        token IS deterministic, so the second value is identical). The
        TTL refreshes on each write — keeps long-running agent loops
        from losing tokens mid-conversation.
        """
        if not mappings:
            return 0
        try:
            pipe = self._redis.pipeline(transaction=False)
            for token, original in mappings:
                pipe.set(self._key(tenant_id, token), original, ex=ttl_seconds)
            results = await pipe.execute()
            return sum(1 for r in results if r)
        except Exception as e:
            log.warning(
                "pii_tokens.store_failed",
                tenant_id=tenant_id,
                count=len(mappings),
                error=str(e),
            )
            return 0

    async def reverse_text(self, *, tenant_id: str, text: str) -> DetokenizeOutcome:
        """Scan ``text`` for ``[TYPE_HASH]`` tokens and reverse them.

        One ``MGET`` for every unique token in the text — O(1)
        round-trips regardless of token count. Tokens that resolve
        get substituted; tokens that don't (Redis outage / TTL
        expiry / model-hallucinated tokens) stay in place.
        """
        matches = list(_TOKEN_RE.finditer(text))
        if not matches:
            return DetokenizeOutcome(text=text)

        # Deduplicate — the same token can appear multiple times in
        # the response (entity tracking working as designed). One
        # MGET handles the lot.
        unique_tokens: list[str] = []
        seen: set[str] = set()
        for m in matches:
            tok = m.group(0)
            if tok not in seen:
                seen.add(tok)
                unique_tokens.append(tok)

        keys = [self._key(tenant_id, t) for t in unique_tokens]
        try:
            raw_values = await self._redis.mget(keys)
        except Exception as e:
            log.warning(
                "pii_tokens.reverse_failed",
                tenant_id=tenant_id,
                token_count=len(unique_tokens),
                error=str(e),
            )
            # Fail-open: return the text as-is. Operationally visible
            # via the absence of the reversed counter incrementing.
            return DetokenizeOutcome(
                text=text,
                orphaned_count=len(matches),
                orphaned_by_type=_count_by_type(matches),
            )

        # Build the lookup map.
        resolved: dict[str, str] = {}
        for tok, raw in zip(unique_tokens, raw_values, strict=False):
            if raw is None:
                continue
            value = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            resolved[tok] = value

        # Replace right-to-left so earlier spans aren't invalidated by
        # length changes. Same pattern as ``apply_redactions`` in the
        # guardrails module.
        out = text
        reversed_count = 0
        orphaned_count = 0
        reversed_by_type: dict[str, int] = {}
        orphaned_by_type: dict[str, int] = {}
        for m in reversed(matches):
            tok = m.group(0)
            type_label = m.group(1).lower()
            original = resolved.get(tok)
            if original is None:
                orphaned_count += 1
                orphaned_by_type[type_label] = orphaned_by_type.get(type_label, 0) + 1
                continue
            out = out[: m.start()] + original + out[m.end() :]
            reversed_count += 1
            reversed_by_type[type_label] = reversed_by_type.get(type_label, 0) + 1

        return DetokenizeOutcome(
            text=out,
            reversed_count=reversed_count,
            orphaned_count=orphaned_count,
            reversed_by_type=reversed_by_type or None,
            orphaned_by_type=orphaned_by_type or None,
        )


def _count_by_type(matches: list[re.Match[str]]) -> dict[str, int]:
    """Count token occurrences grouped by type label (lowercase)."""
    out: dict[str, int] = {}
    for m in matches:
        label = m.group(1).lower()
        out[label] = out.get(label, 0) + 1
    return out


# --------------------------------------------------------------------------- #
# Streaming detokenizer — handles tokens that span chunk boundaries           #
# --------------------------------------------------------------------------- #


class StreamingDetokenizer:
    """Per-stream stateful detokenizer for the SSE generator.

    The naive approach of running ``reverse_text`` on each chunk
    would corrupt tokens that span chunk boundaries — the LLM might
    emit ``"... [EMAIL_a3"`` in one chunk and ``"f7c2e1b890] sent..."``
    in the next. Neither chunk on its own contains a parseable token,
    but the concatenation does.

    The fix: maintain a *tail buffer*. After scanning each chunk, hold
    back the last ``_MAX_TOKEN_LEN - 1`` characters (the worst-case
    partial token) and emit only what comes before. Next chunk arrives,
    concatenate buffer + chunk, scan again, emit safe prefix, hold new
    tail. On stream end, flush the buffer (any partial token at that
    point can't resolve — emit as-is).

    The buffer is small (31 chars) so memory cost is negligible and
    latency overhead is at most one chunk's wait time — usually a
    few milliseconds per token-containing region.
    """

    def __init__(self, store: TokenStore, *, tenant_id: str) -> None:
        self._store = store
        self._tenant_id = tenant_id
        self._buffer = ""
        self._reversed_total = 0
        self._orphaned_total = 0
        self._reversed_by_type: dict[str, int] = {}
        self._orphaned_by_type: dict[str, int] = {}

    async def feed(self, chunk: str) -> str:
        """Process one streaming text fragment.

        Returns the part of ``self._buffer + chunk`` that is safe to
        emit to the client (no partial tokens at the tail). Any
        complete tokens in that safe part are reversed.
        """
        if not chunk:
            return ""
        combined = self._buffer + chunk

        # Find the split point: the latest position past which we
        # CANNOT guarantee no token starts. Strategy: scan from the
        # tail backward for a ``[`` character within the last
        # ``_MAX_TOKEN_LEN - 1`` chars. If found, hold from that ``[``
        # onward in the buffer. If no ``[`` in the tail window,
        # everything is safe to emit.
        safe_end = self._find_safe_split(combined)
        safe_part = combined[:safe_end]
        self._buffer = combined[safe_end:]

        if not safe_part:
            return ""

        outcome = await self._store.reverse_text(tenant_id=self._tenant_id, text=safe_part)
        self._reversed_total += outcome.reversed_count
        self._orphaned_total += outcome.orphaned_count
        if outcome.reversed_by_type:
            for k, v in outcome.reversed_by_type.items():
                self._reversed_by_type[k] = self._reversed_by_type.get(k, 0) + v
        if outcome.orphaned_by_type:
            for k, v in outcome.orphaned_by_type.items():
                self._orphaned_by_type[k] = self._orphaned_by_type.get(k, 0) + v
        return outcome.text

    async def flush(self) -> str:
        """Emit whatever's left in the buffer at stream end.

        The trailing buffer can still contain a complete token (it
        just happened to land at the very end of the stream). Run
        one final ``reverse_text`` pass to catch that case before
        emitting.
        """
        if not self._buffer:
            return ""
        outcome = await self._store.reverse_text(tenant_id=self._tenant_id, text=self._buffer)
        self._reversed_total += outcome.reversed_count
        self._orphaned_total += outcome.orphaned_count
        if outcome.reversed_by_type:
            for k, v in outcome.reversed_by_type.items():
                self._reversed_by_type[k] = self._reversed_by_type.get(k, 0) + v
        if outcome.orphaned_by_type:
            for k, v in outcome.orphaned_by_type.items():
                self._orphaned_by_type[k] = self._orphaned_by_type.get(k, 0) + v
        self._buffer = ""
        return outcome.text

    @property
    def reversed_total(self) -> int:
        return self._reversed_total

    @property
    def orphaned_total(self) -> int:
        return self._orphaned_total

    @property
    def reversed_by_type(self) -> dict[str, int]:
        return dict(self._reversed_by_type)

    @property
    def orphaned_by_type(self) -> dict[str, int]:
        return dict(self._orphaned_by_type)

    @staticmethod
    def _find_safe_split(text: str) -> int:
        """Return the index past which it's UNSAFE to emit immediately.

        We look at the trailing ``_MAX_TOKEN_LEN - 1`` characters for
        the latest ``[`` that *could* be the start of a token. If
        present, everything from that ``[`` onward might be a partial
        token — hold it. Otherwise the whole string is safe to emit.

        Edge case: if a ``[`` is followed by a complete ``]`` within
        the tail window, that's a complete token — we DON'T hold it.
        We only hold ``[`` that's followed by characters that haven't
        closed the bracket yet.
        """
        tail_start = 0 if len(text) <= _MAX_TOKEN_LEN - 1 else len(text) - (_MAX_TOKEN_LEN - 1)
        # Find the latest ``[`` whose matching ``]`` we haven't seen.
        # Scan from the END to find the last unclosed ``[`` in the
        # tail. If the tail has a ``[`` that's already closed, the
        # closing ``]`` makes the whole thing safe.
        last_open = text.rfind("[", tail_start)
        if last_open == -1:
            return len(text)
        # Is the ``[`` closed within the rest of the text?
        if text.find("]", last_open) != -1:
            return len(text)
        # Unclosed ``[`` in the tail — hold from there.
        return last_open


# --------------------------------------------------------------------------- #
# Convenience: ingress-side helper used by the engine                         #
# --------------------------------------------------------------------------- #


def tokenize_hits(
    *,
    tenant_id: str,
    text: str,
    hits: list[tuple[str, tuple[int, int], str]],
) -> tuple[str, list[tuple[str, str]]]:
    """Build the tokenized text + mapping list from a verdict's hits.

    Pure function — no I/O. The caller (chat handler) does the Redis
    write afterward. Hits are ``(rule_name, span, matched_text)``;
    we deduplicate by ``matched_text`` so identical values across the
    text use the same token (entity tracking).

    Right-to-left substitution so earlier spans' indices stay valid.
    """
    if not hits:
        return text, []

    # Deterministic token per (rule, value).
    value_to_token: dict[tuple[str, str], str] = {}
    for rule_name, _span, value in hits:
        key = (rule_name, value)
        if key not in value_to_token:
            value_to_token[key] = make_token(tenant_id=tenant_id, rule_name=rule_name, value=value)

    # Apply right-to-left.
    ordered = sorted(hits, key=lambda h: h[1][0], reverse=True)
    out = text
    last_start: int | None = None
    for rule_name, (start, end), value in ordered:
        # Same overlap-protection logic as ``apply_redactions``.
        if last_start is not None and end > last_start:
            continue
        token = value_to_token[(rule_name, value)]
        out = out[:start] + token + out[end:]
        last_start = start

    # Build the mappings list in stable order (by token) — keeps tests
    # deterministic and makes Redis writes idempotent.
    mappings = sorted(
        {(token, value) for (_rule, value), token in value_to_token.items()},
        key=lambda kv: kv[0],
    )
    return out, mappings


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "DetokenizeOutcome",
    "StreamingDetokenizer",
    "TokenStore",
    "make_token",
    "tokenize_hits",
]


# Silences "imported but unused" — kept available for callers that want
# to suppress Redis errors symmetrically.
_ = contextlib
