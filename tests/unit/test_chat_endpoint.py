"""HTTP-level tests for /v1/chat/completions.

Exercises the full stack: auth gate → FastAPI route → router → provider
registry → adapter → httpx (mocked by respx). Complements the provider-level
tests (test_anthropic.py, test_openai_compat.py) with an end-to-end check
through the real FastAPI stack.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from pronaos.providers.anthropic import ANTHROPIC_API_URL


def _anthropic_response(text: str = "hi there") -> dict:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 4},
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@respx.mock
@pytest.mark.asyncio
async def test_chat_completion_openai_shape(auth_setup) -> None:  # type: ignore[no-untyped-def]
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response("hi there"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "hi there"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    assert body["pronaos"]["provider"] == "anthropic"
    assert body["pronaos"]["cost_hcents"] >= 0


@respx.mock
@pytest.mark.asyncio
async def test_streaming_emits_openai_sse(auth_setup) -> None:  # type: ignore[no-untyped-def]
    sse = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
    )
    respx.post(ANTHROPIC_API_URL).mock(return_value=httpx.Response(200, text=sse))

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    body = resp.text
    events = [line for line in body.split("\n\n") if line.startswith("data: ")]
    assert any('"role":"assistant"' in e for e in events)
    assert any('"content":"hi"' in e for e in events)
    assert any('"finish_reason":"stop"' in e for e in events)
    assert events[-1].strip() == "data: [DONE]"


@respx.mock
@pytest.mark.asyncio
async def test_upstream_auth_error_surfaces_as_401(auth_setup) -> None:  # type: ignore[no-untyped-def]
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "type": "error",
                "error": {"type": "authentication_error", "message": "bad key"},
            },
        )
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 401
    body = resp.json()
    assert "error" in body
    assert body["error"]["type"] == "AuthError"


# --------------------------------------------------------------------------- #
# Phase 17: model allowlist gate                                              #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_model_not_in_allowlist_returns_403(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """When a team's ``allowed_models`` doesn't match the requested
    model, the chat handler must short-circuit to 403 with a clear
    ``model_not_allowed`` error type. The upstream provider must NOT
    be called — the gate runs before failover."""
    from sqlalchemy import select

    from pronaos.db.models import Team

    # Configure the team's allowlist to disallow anthropic/*.
    sm = auth_setup.sm
    async with sm() as session:
        team = (
            await session.execute(select(Team).where(Team.id == auth_setup.team_id))
        ).scalar_one()
        team.allowed_models = ["groq/*"]
        await session.commit()

    # Mock the Anthropic endpoint so we can detect if the handler
    # accidentally calls it despite the policy.
    anthropic_route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response("should not be reached"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["type"] == "model_not_allowed"
    assert detail["model"] == "anthropic/claude-opus-4-7"
    # Crucially: no upstream call was made. If this fails, the gate
    # is in the wrong place in the handler.
    assert anthropic_route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_model_in_allowlist_passes_through(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """The matching-pattern path. With ``["anthropic/*"]`` set, a
    request for ``anthropic/claude-opus-4-7`` should proceed normally
    and the upstream provider should be called exactly once."""
    from sqlalchemy import select

    from pronaos.db.models import Team

    sm = auth_setup.sm
    async with sm() as session:
        team = (
            await session.execute(select(Team).where(Team.id == auth_setup.team_id))
        ).scalar_one()
        team.allowed_models = ["anthropic/*"]
        await session.commit()

    anthropic_route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response("hello"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "hello"
    assert anthropic_route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_null_allowlist_is_unrestricted(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Regression: teams provisioned BEFORE this feature shipped have
    ``allowed_models = NULL`` and must continue to work without any
    config change. The default auth_setup fixture leaves it as NULL,
    so this is the path the existing tests exercise — pin it
    explicitly here too."""
    respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response("unblocked"))
    )
    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "unblocked"


@respx.mock
@pytest.mark.asyncio
async def test_empty_allowlist_denies_everything(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """``allowed_models = []`` is the deny-all state. Even a previously
    allowed model is rejected; the team is effectively paused without
    revoking its keys."""
    from sqlalchemy import select

    from pronaos.db.models import Team

    sm = auth_setup.sm
    async with sm() as session:
        team = (
            await session.execute(select(Team).where(Team.id == auth_setup.team_id))
        ).scalar_one()
        team.allowed_models = []
        await session.commit()

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["type"] == "model_not_allowed"


# --------------------------------------------------------------------------- #
# Phase 20: pre-flight token-budget gate                                       #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_preflight_denies_when_estimate_exceeds_budget(
    auth_setup,  # type: ignore[no-untyped-def]
) -> None:
    """A team with a tight token budget should get a 429 BEFORE the
    upstream call when the estimator predicts the request can't fit.
    Mocking Anthropic so we can assert it was NEVER called — that
    proves the preflight gate ran before failover.

    Header ``X-Pronaos-Preflight-Estimate`` carries the estimator's
    output so clients can decide whether to retry with smaller
    max_tokens."""
    from sqlalchemy import select

    from pronaos.db.models import Team

    # Set a tight 50-token budget on the seeded team.
    sm = auth_setup.sm
    async with sm() as session:
        team = (
            await session.execute(select(Team).where(Team.id == auth_setup.team_id))
        ).scalar_one()
        team.monthly_token_budget = 50
        team.current_period_tokens = 0
        await session.commit()

    # Mock so any upstream call would be visible if it leaked through.
    anthropic_route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response("should not be reached"))
    )

    # 500-token max_tokens alone blows the 50-token budget — no
    # ambiguity, the estimator's output should clearly exceed.
    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "Write me a long essay"}],
            "max_tokens": 500,
        },
    )

    assert resp.status_code == 429
    body = resp.json()
    assert body["detail"]["type"] == "monthly_token_budget_exhausted"
    # Header carries the estimate the gate used.
    assert "x-pronaos-preflight-estimate" in {h.lower() for h in resp.headers}
    estimate = int(resp.headers["x-pronaos-preflight-estimate"])
    assert estimate > 50  # the whole point — must exceed budget
    # Upstream was NEVER called: that's the cost savings.
    assert anthropic_route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_preflight_allows_when_estimate_fits_budget(
    auth_setup,  # type: ignore[no-untyped-def]
) -> None:
    """The matching case: a request with a small estimate against a
    generous budget should proceed through preflight to the upstream
    call. Confirms the gate doesn't deny everything."""
    from sqlalchemy import select

    from pronaos.db.models import Team

    sm = auth_setup.sm
    async with sm() as session:
        team = (
            await session.execute(select(Team).where(Team.id == auth_setup.team_id))
        ).scalar_one()
        team.monthly_token_budget = 100_000
        team.current_period_tokens = 0
        await session.commit()

    anthropic_route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response("ok"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        },
    )

    assert resp.status_code == 200
    assert anthropic_route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_preflight_skipped_for_unlimited_team(
    auth_setup,  # type: ignore[no-untyped-def]
) -> None:
    """A team with no token budget (NULL = unlimited) skips the
    preflight check entirely — even with a giant max_tokens request,
    the upstream is contacted because there's no budget to enforce."""
    anthropic_route = respx.post(ANTHROPIC_API_URL).mock(
        return_value=httpx.Response(200, json=_anthropic_response("ok"))
    )

    # auth_setup leaves monthly_token_budget at NULL (unlimited) by default.
    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "anthropic/claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 100_000,  # would deny if budget were finite
        },
    )
    assert resp.status_code == 200
    assert anthropic_route.call_count == 1


# --------------------------------------------------------------------------- #
# Phase 21: cost-aware auto-routing                                           #
# --------------------------------------------------------------------------- #


_GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


def _groq_response(text: str = "auto-routed reply") -> dict:
    """OpenAI-compat chat completion response shape that Groq returns."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "llama-3.1-8b-instant",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


@respx.mock
@pytest.mark.asyncio
async def test_auto_routing_picks_cheapest_groq_model(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """model='auto' with allowed=['groq/*'] and strategy=cheapest must
    resolve to ``groq/llama-3.1-8b-instant`` (the lowest-cost Groq entry
    in the catalog). The X-Pronaos-Routed-Model header surfaces the
    decision so clients can see what the gateway picked."""
    from sqlalchemy import select

    from pronaos.db.models import Team

    sm = auth_setup.sm
    async with sm() as session:
        team = (
            await session.execute(select(Team).where(Team.id == auth_setup.team_id))
        ).scalar_one()
        team.allowed_models = ["groq/*"]
        team.routing_strategy = "cheapest"
        await session.commit()

    groq_route = respx.post(_GROQ_CHAT_URL).mock(
        return_value=httpx.Response(200, json=_groq_response("ok"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50,
        },
    )

    assert resp.status_code == 200
    assert resp.headers["X-Pronaos-Routed-Model"] == "groq/llama-3.1-8b-instant"
    assert resp.headers["X-Pronaos-Routing-Strategy"] == "cheapest"
    # Confirm the upstream call actually happened to the resolved model.
    assert groq_route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_auto_routing_with_no_eligible_model_returns_422(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """An empty allowlist (deny-all) combined with model='auto' must
    short-circuit to 422 with a clear ``no_eligible_model`` error.
    No upstream provider must be contacted."""
    from sqlalchemy import select

    from pronaos.db.models import Team

    sm = auth_setup.sm
    async with sm() as session:
        team = (
            await session.execute(select(Team).where(Team.id == auth_setup.team_id))
        ).scalar_one()
        team.allowed_models = []  # explicit deny-all
        await session.commit()

    # Mock to detect if any upstream was reached anyway.
    groq_route = respx.post(_GROQ_CHAT_URL).mock(
        return_value=httpx.Response(200, json=_groq_response("should not be reached"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["type"] == "no_eligible_model"
    assert groq_route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_auto_routing_defaults_to_cheapest_when_strategy_unset(  # type: ignore[no-untyped-def]
    auth_setup,
) -> None:
    """A team with NULL routing_strategy must default to 'cheapest'
    when an auto-routed request arrives. This is the backwards-compat
    path for teams provisioned before Phase 21."""
    from sqlalchemy import select

    from pronaos.db.models import Team

    sm = auth_setup.sm
    async with sm() as session:
        team = (
            await session.execute(select(Team).where(Team.id == auth_setup.team_id))
        ).scalar_one()
        team.allowed_models = ["groq/*"]
        # Deliberately leave routing_strategy = NULL.
        await session.commit()

    respx.post(_GROQ_CHAT_URL).mock(return_value=httpx.Response(200, json=_groq_response("ok")))

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50,
        },
    )

    assert resp.status_code == 200
    assert resp.headers["X-Pronaos-Routing-Strategy"] == "cheapest"
    # Cheapest groq model is the 8b-instant.
    assert resp.headers["X-Pronaos-Routed-Model"] == "groq/llama-3.1-8b-instant"


# --------------------------------------------------------------------------- #
# Phase 24: quality-aware routing                                             #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_quality_aware_routing_picks_expensive_when_cheap_fails_threshold(  # type: ignore[no-untyped-def]
    auth_setup,
) -> None:
    """Headline behaviour: team has stored eval scores. 8B-instant
    scored 0.4 (below 0.7 threshold) → filtered out. 70B-versatile
    scored 0.9 → clears the bar → wins on cost-among-eligible.

    The cheap-but-failing 8B-instant is the model plain ``cheapest``
    would pick; quality-aware routing skips it because the team's
    eval data says it under-performs."""
    from sqlalchemy import select

    from pronaos.db.models import Team

    sm = auth_setup.sm
    async with sm() as session:
        team = (
            await session.execute(select(Team).where(Team.id == auth_setup.team_id))
        ).scalar_one()
        team.allowed_models = [
            "groq/llama-3.1-8b-instant",
            "groq/llama-3.3-70b-versatile",
        ]
        team.routing_strategy = "quality-aware-cheapest"
        team.quality_threshold = 0.7
        team.quality_scores = {
            "groq/llama-3.1-8b-instant": {"score": 0.4, "n_samples": 8},
            "groq/llama-3.3-70b-versatile": {"score": 0.9, "n_samples": 8},
        }
        await session.commit()

    # Mock both possible upstreams. Only the 70B URL should be hit.
    groq_70b = respx.post(_GROQ_CHAT_URL).mock(
        return_value=httpx.Response(200, json=_groq_response("70b reply"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 20,
        },
    )

    assert resp.status_code == 200
    assert resp.headers["X-Pronaos-Routed-Model"] == "groq/llama-3.3-70b-versatile"
    assert resp.headers["X-Pronaos-Routing-Strategy"] == "quality-aware-cheapest"
    # The stored score shows up in the response header so clients
    # can audit the quality-aware decision.
    assert resp.headers["X-Pronaos-Quality-Score"] == "0.900"
    assert groq_70b.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_quality_aware_routing_degrades_to_cheapest_with_no_scores(  # type: ignore[no-untyped-def]
    auth_setup,
) -> None:
    """A team that switched to quality-aware-cheapest before running
    any eval must not 500. The scorer degrades to plain ``cheapest``
    over the capability-eligible pool."""
    from sqlalchemy import select

    from pronaos.db.models import Team

    sm = auth_setup.sm
    async with sm() as session:
        team = (
            await session.execute(select(Team).where(Team.id == auth_setup.team_id))
        ).scalar_one()
        team.allowed_models = [
            "groq/llama-3.1-8b-instant",
            "groq/llama-3.3-70b-versatile",
        ]
        team.routing_strategy = "quality-aware-cheapest"
        team.quality_scores = None  # No eval data on file.
        await session.commit()

    respx.post(_GROQ_CHAT_URL).mock(return_value=httpx.Response(200, json=_groq_response("ok")))

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 20,
        },
    )

    assert resp.status_code == 200
    # Same answer plain cheapest would give.
    assert resp.headers["X-Pronaos-Routed-Model"] == "groq/llama-3.1-8b-instant"
    # No score on record for the picked model → no header.
    assert "X-Pronaos-Quality-Score" not in resp.headers


@respx.mock
@pytest.mark.asyncio
async def test_quality_aware_routing_422_when_nothing_clears_bar(  # type: ignore[no-untyped-def]
    auth_setup,
) -> None:
    """If every evaluated model is below the bar, surface 422 with the
    ``no_eligible_model`` shape. Operator should either lower the
    threshold or widen the allowlist."""
    from sqlalchemy import select

    from pronaos.db.models import Team

    sm = auth_setup.sm
    async with sm() as session:
        team = (
            await session.execute(select(Team).where(Team.id == auth_setup.team_id))
        ).scalar_one()
        team.allowed_models = [
            "groq/llama-3.1-8b-instant",
            "groq/llama-3.3-70b-versatile",
        ]
        team.routing_strategy = "quality-aware-cheapest"
        team.quality_threshold = 0.99  # Impossibly high.
        team.quality_scores = {
            "groq/llama-3.1-8b-instant": {"score": 0.4, "n_samples": 8},
            "groq/llama-3.3-70b-versatile": {"score": 0.9, "n_samples": 8},
        }
        await session.commit()

    groq_route = respx.post(_GROQ_CHAT_URL).mock(
        return_value=httpx.Response(200, json=_groq_response("should not reach"))
    )

    resp = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["type"] == "no_eligible_model"
    # No upstream call made — the quality gate short-circuited.
    assert groq_route.call_count == 0
