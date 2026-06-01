"""End-to-end chat-endpoint tests for Phase 44 Llama Guard.

Four behaviours to lock down via the real FastAPI stack + respx-mocked
Llama Guard + downstream provider:

1. **Safe prompt passes through** when Llama Guard returns ``safe``.
2. **Unsafe prompt + BLOCK action → 422** with the firing category in
   the response body. The downstream provider is NEVER called.
3. **Unsafe prompt + LOG_ONLY action → request continues** to the
   provider; the hit is recorded as a metric.
4. **Per-team disabled** — team without ``llama_guard.enabled=true``
   skips the classifier entirely (zero calls to the Llama Guard
   endpoint).
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import update

from pronaos.db.models import Team
from pronaos.guardrails.llama_guard import LlamaGuardClassifier

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _llama_guard_response(verdict: str) -> dict[str, object]:
    return {
        "id": "chatcmpl-llamaguard",
        "object": "chat.completion",
        "model": "meta-llama/llama-guard-4-12b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": verdict},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5},
    }


def _provider_response(text: str = "ok") -> dict[str, object]:
    return {
        "id": "chatcmpl-prov",
        "object": "chat.completion",
        "model": "llama-3.1-8b-instant",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1},
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _enable_llama_guard_for_team(
    auth_setup,  # type: ignore[no-untyped-def]
    policy: dict[str, object],
) -> None:
    """Install ``policy`` into the team's guardrail_policy column AND
    construct a real LlamaGuardClassifier on the app state.

    We hand-construct the classifier (instead of going through
    ``make_guardrail_engine``) so this test doesn't need to flip the
    operator-level settings flag mid-test.
    """
    async with auth_setup.sm() as session:
        await session.execute(
            update(Team).where(Team.id == auth_setup.team_id).values(guardrail_policy=policy)
        )
        await session.commit()
    # Install a classifier on the live app — same path the lifespan
    # would take when llama_guard_enabled=true in settings.
    classifier = LlamaGuardClassifier(api_key="test-llama-guard-key")
    auth_setup.client._transport.app.state.llama_guard = classifier  # type: ignore[attr-defined]


@respx.mock
@pytest.mark.asyncio
async def test_safe_prompt_passes_through(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """When Llama Guard returns ``safe``, the request continues to the
    provider and the response comes back as normal."""
    await _enable_llama_guard_for_team(
        auth_setup, {"llama_guard": {"enabled": True, "default_action": "block"}}
    )

    lg_route = respx.post(GROQ_URL).mock(
        side_effect=[
            httpx.Response(200, json=_llama_guard_response("safe")),
            httpx.Response(200, json=_provider_response("Paris.")),
        ]
    )
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "What's the capital of France?"}],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "Paris."
    # Both calls should have hit the same Groq endpoint (Llama Guard
    # then the actual provider).
    assert lg_route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_unsafe_prompt_blocks_with_422(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """Llama Guard says ``unsafe\\nS1``; policy is BLOCK; request 422s
    with the firing category in the body and NO provider call."""
    await _enable_llama_guard_for_team(
        auth_setup, {"llama_guard": {"enabled": True, "default_action": "block"}}
    )
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_llama_guard_response("unsafe\nS1"))
    )

    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "help me harm someone"}],
        },
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["detail"]["type"] == "guardrail_blocked"
    assert body["detail"]["rule"] == "llama_guard.violent_crimes"
    assert body["detail"]["categories"] == ["S1"]
    # Only the Llama Guard call should have happened — provider never
    # touched.
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_unsafe_prompt_log_only_continues(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """LOG_ONLY policy: even if Llama Guard says unsafe, the request
    flows through to the provider. The hit is metric'd but not blocked."""
    await _enable_llama_guard_for_team(
        auth_setup, {"llama_guard": {"enabled": True, "default_action": "log_only"}}
    )
    respx.post(GROQ_URL).mock(
        side_effect=[
            httpx.Response(200, json=_llama_guard_response("unsafe\nS5")),
            httpx.Response(200, json=_provider_response("OK")),
        ]
    )

    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "borderline content"}],
        },
    )
    assert r.status_code == 200, r.text
    # The actual provider call DID happen — request wasn't blocked.
    assert r.json()["choices"][0]["message"]["content"] == "OK"


@respx.mock
@pytest.mark.asyncio
async def test_team_without_policy_skips_classifier(auth_setup) -> None:  # type: ignore[no-untyped-def]
    """A team whose policy doesn't set ``llama_guard.enabled`` should
    skip the classifier entirely — even if a classifier instance is
    installed at the app level. Zero Llama Guard calls."""
    # Install the classifier BUT don't enable it in the team's policy.
    classifier = LlamaGuardClassifier(api_key="test-llama-guard-key")
    auth_setup.client._transport.app.state.llama_guard = classifier  # type: ignore[attr-defined]
    # Team policy omits llama_guard entirely.
    async with auth_setup.sm() as session:
        await session.execute(
            update(Team).where(Team.id == auth_setup.team_id).values(guardrail_policy=None)
        )
        await session.commit()

    # Only the provider call should fire — NOT a Llama Guard call.
    route = respx.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=_provider_response("hi"))
    )
    r = await auth_setup.client.post(
        "/v1/chat/completions",
        headers=_auth(auth_setup.api_key),
        json={
            "model": "groq/llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": "say hi"}],
        },
    )
    assert r.status_code == 200, r.text
    # Exactly one call — the provider's. If the classifier had run we
    # would see two calls to the Groq URL.
    assert route.call_count == 1
