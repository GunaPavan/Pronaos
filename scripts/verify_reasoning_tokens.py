"""Verify reasoning-token FinOps surface across five provider paths
(Claim #43, Phase 56).

The empirical question
----------------------
Reasoning models (Anthropic extended thinking, OpenAI o1/o3, DeepSeek
R1, Gemini 2.0/2.5 thinking) are becoming the default for hard
agentic + math + code tasks. Their cost profile is very different
from non-reasoning chat: a single request can burn thousands of
"reasoning tokens" that the user never sees but the operator pays
for.

Every provider exposes the count differently:

- **Anthropic extended thinking** (direct + Bedrock + Vertex) emits
  thinking as a separate ``type: "thinking"`` content block. The
  token count is rolled into ``usage.output_tokens`` — Anthropic
  does NOT expose a separate count. Pronaos extracts the thinking
  text into ``reasoning_content`` and estimates ``reasoning_tokens``
  from char-length (~4 chars/token, ceil-divide) for visibility.
- **OpenAI o1/o3 + DeepSeek R1** (OpenAI-compat path) expose
  ``usage.completion_tokens_details.reasoning_tokens`` — already
  included in ``completion_tokens`` (cost math unchanged). DeepSeek
  additionally ships the CoT text as ``message.reasoning_content``.
  OpenAI does NOT expose CoT text intentionally.
- **Gemini thinking** (Vertex) exposes ``usageMetadata.thoughtsTokenCount``
  as a SEPARATE billable count EXCLUDED from ``candidatesTokenCount``.
  This is the only material **correctness fix** in Phase 56: Pronaos
  ADDS thoughts to ``completion_tokens`` so cost math is accurate.
  Without the fix, Gemini thinking-mode requests were under-billed
  by 100% of the thinking portion.

What this verify exercises
--------------------------
Each of the five paths drives synthesized inputs through the adapter
and asserts:
1. ``reasoning_tokens`` surfaces on the chunk.
2. ``reasoning_content`` is present where the upstream ships it
   (Anthropic, DeepSeek) and None where it doesn't (OpenAI o-series).
3. ``completion_tokens`` is correct: untouched for Anthropic /
   OpenAI / DeepSeek (already includes reasoning), augmented by
   ``thoughtsTokenCount`` for Gemini (correctness fix).
4. Plain non-reasoning calls leave the reasoning fields unset
   (no behavioural change for the common case).

VERDICT holds when all five paths produce the expected reasoning
shape + the Gemini cost-fix delta materialises empirically.

Honesty disclosure
------------------
No real provider endpoints are hit — every adapter sees a synthesized
response shaped like what the upstream would send. The parser logic,
char-length estimator, and Gemini correctness fix are exercised end-
to-end; only the network hop is substituted.
"""

from __future__ import annotations

import argparse
import sys

from pronaos.providers.anthropic import AnthropicProvider
from pronaos.providers.bedrock import _parse_anthropic_response
from pronaos.providers.vertex import (
    _parse_anthropic_on_vertex_response,
    _parse_gemini_response,
)


def _verify_anthropic_direct() -> dict[str, object]:
    """Path 1: direct Anthropic. Thinking block + text block; estimate
    via char-length."""
    body = {
        "id": "msg_path1",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "thinking",
                "thinking": (
                    "Step 1: parse the request. Step 2: derive the answer "
                    "from first principles. Step 3: explain succinctly."
                ),
                "signature": "opaque",
            },
            {"type": "text", "text": "The result is 42."},
        ],
        "model": "claude-opus-4-7",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 20, "output_tokens": 60},
    }
    chunk = AnthropicProvider._build_response_chunk(body)
    return {
        "content_delta": chunk.content_delta,
        "reasoning_tokens": chunk.reasoning_tokens,
        "reasoning_content": chunk.reasoning_content,
        "completion_tokens": chunk.completion_tokens,
    }


def _verify_openai_compat_openai_o1() -> dict[str, object]:
    """Path 2: OpenAI o-series. reasoning_tokens via
    completion_tokens_details; NO reasoning_content."""
    # We exercise the parser directly rather than spinning up an HTTP
    # mock — the parser is the unit under test for this verify.
    from pronaos.providers.openai_compat import OpenAICompatibleProvider

    data = {
        "id": "chatcmpl_o1",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "Hello.", "role": "assistant"},
            }
        ],
        "usage": {
            "prompt_tokens": 15,
            "completion_tokens": 250,
            "completion_tokens_details": {"reasoning_tokens": 200},
        },
    }
    chunk = OpenAICompatibleProvider._chunk_from_response(data)
    return {
        "reasoning_tokens": chunk.reasoning_tokens,
        "reasoning_content": chunk.reasoning_content,
        "completion_tokens": chunk.completion_tokens,
    }


def _verify_openai_compat_deepseek() -> dict[str, object]:
    """Path 3: DeepSeek R1. Same usage shape as OpenAI o-series PLUS
    ``message.reasoning_content`` carrying the CoT text."""
    from pronaos.providers.openai_compat import OpenAICompatibleProvider

    data = {
        "id": "chatcmpl_r1",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": "Final answer.",
                    "reasoning_content": (
                        "Let me think step by step. First, ..."
                    ),
                    "role": "assistant",
                },
            }
        ],
        "usage": {
            "prompt_tokens": 15,
            "completion_tokens": 75,
            "completion_tokens_details": {"reasoning_tokens": 40},
        },
    }
    chunk = OpenAICompatibleProvider._chunk_from_response(data)
    return {
        "content_delta": chunk.content_delta,
        "reasoning_tokens": chunk.reasoning_tokens,
        "reasoning_content": chunk.reasoning_content,
        "completion_tokens": chunk.completion_tokens,
    }


def _verify_vertex_gemini() -> dict[str, object]:
    """Path 4: Vertex Gemini thinking. The correctness fix.
    thoughtsTokenCount must land on BOTH reasoning_tokens AND get
    ADDED to completion_tokens."""
    data = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": "Answer."}],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 30,
            "candidatesTokenCount": 20,
            "thoughtsTokenCount": 500,
            "totalTokenCount": 550,
        },
    }
    chunk = _parse_gemini_response(data)
    return {
        "completion_tokens": chunk.completion_tokens,
        "candidates_only": data["usageMetadata"]["candidatesTokenCount"],
        "thoughts": data["usageMetadata"]["thoughtsTokenCount"],
        "reasoning_tokens": chunk.reasoning_tokens,
    }


def _verify_anthropic_on_bedrock() -> dict[str, object]:
    """Path 5a: Anthropic-on-Bedrock thinking. Same wire shape as
    direct Anthropic — thinking block parses identically."""
    data = {
        "content": [
            {
                "type": "thinking",
                "thinking": "AWS-side reasoning on this call.",
                "signature": "opaque",
            },
            {"type": "text", "text": "Bedrock answer."},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 25, "output_tokens": 35},
    }
    chunk = _parse_anthropic_response(data)
    return {
        "content_delta": chunk.content_delta,
        "reasoning_tokens": chunk.reasoning_tokens,
        "reasoning_content": chunk.reasoning_content,
        "completion_tokens": chunk.completion_tokens,
    }


def _verify_anthropic_on_vertex() -> dict[str, object]:
    """Path 5b: Anthropic-on-Vertex thinking. Same wire shape again."""
    data = {
        "content": [
            {
                "type": "thinking",
                "thinking": "GCP-side reasoning on this call.",
                "signature": "opaque",
            },
            {"type": "text", "text": "Vertex answer."},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 25, "output_tokens": 35},
    }
    chunk = _parse_anthropic_on_vertex_response(data)
    return {
        "content_delta": chunk.content_delta,
        "reasoning_tokens": chunk.reasoning_tokens,
        "reasoning_content": chunk.reasoning_content,
        "completion_tokens": chunk.completion_tokens,
    }


def _verify_non_reasoning_unaffected() -> dict[str, object]:
    """Regression: a plain Groq Llama response (no reasoning fields)
    leaves the chunk's reasoning surface at 0/None."""
    from pronaos.providers.openai_compat import OpenAICompatibleProvider

    data = {
        "id": "chatcmpl_plain",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "Plain text.", "role": "assistant"},
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    chunk = OpenAICompatibleProvider._chunk_from_response(data)
    return {
        "reasoning_tokens": chunk.reasoning_tokens,
        "reasoning_content": chunk.reasoning_content,
        "completion_tokens": chunk.completion_tokens,
    }


def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args()

    print("=" * 72)
    print("Phase 56 — Reasoning-token FinOps across five paths")
    print("=" * 72)
    print()

    # Path 1
    print("Path 1: Anthropic direct (extended thinking)")
    p1 = _verify_anthropic_direct()
    print(f"  content_delta: {p1['content_delta']!r}")
    print(f"  reasoning_content (first 60c): {str(p1['reasoning_content'])[:60]!r}")
    print(
        f"  reasoning_tokens (estimated): {p1['reasoning_tokens']} "
        f"(completion_tokens unchanged at {p1['completion_tokens']})"
    )
    print()

    # Path 2
    print("Path 2: OpenAI o-series (reasoning_tokens, no CoT text)")
    p2 = _verify_openai_compat_openai_o1()
    print(
        f"  reasoning_tokens: {p2['reasoning_tokens']} "
        f"reasoning_content: {p2['reasoning_content']!r} "
        f"completion_tokens: {p2['completion_tokens']}"
    )
    print()

    # Path 3
    print("Path 3: DeepSeek R1 (reasoning_tokens + reasoning_content)")
    p3 = _verify_openai_compat_deepseek()
    print(
        f"  content_delta: {p3['content_delta']!r} "
        f"reasoning_tokens: {p3['reasoning_tokens']}"
    )
    print(f"  reasoning_content: {p3['reasoning_content']!r}")
    print()

    # Path 4 — the correctness fix
    print("Path 4: Vertex Gemini thinking (CORRECTNESS FIX)")
    p4 = _verify_vertex_gemini()
    print(
        f"  candidatesTokenCount (Gemini wire): {p4['candidates_only']}, "
        f"thoughtsTokenCount: {p4['thoughts']}"
    )
    print(
        f"  Pronaos completion_tokens (billable output, post-fix): "
        f"{p4['completion_tokens']} "
        f"= candidates ({p4['candidates_only']}) + thoughts ({p4['thoughts']})"
    )
    print(
        f"  reasoning_tokens surfaced: {p4['reasoning_tokens']}"
    )
    print()

    # Path 5
    print("Path 5a: Anthropic-on-Bedrock thinking")
    p5a = _verify_anthropic_on_bedrock()
    print(
        f"  content_delta: {p5a['content_delta']!r} "
        f"reasoning_tokens (estimated): {p5a['reasoning_tokens']}"
    )
    print(f"  reasoning_content: {p5a['reasoning_content']!r}")
    print()

    print("Path 5b: Anthropic-on-Vertex thinking")
    p5b = _verify_anthropic_on_vertex()
    print(
        f"  content_delta: {p5b['content_delta']!r} "
        f"reasoning_tokens (estimated): {p5b['reasoning_tokens']}"
    )
    print(f"  reasoning_content: {p5b['reasoning_content']!r}")
    print()

    # Regression
    print("Regression: non-reasoning Groq Llama response")
    reg = _verify_non_reasoning_unaffected()
    print(
        f"  reasoning_tokens: {reg['reasoning_tokens']} "
        f"reasoning_content: {reg['reasoning_content']!r} "
        f"completion_tokens (unaffected): {reg['completion_tokens']}"
    )
    print()

    # ---- Verdict ------------------------------------------------------
    print("=" * 72)
    failures: list[str] = []

    # Path 1 gates
    if p1["content_delta"] != "The result is 42.":
        failures.append(f"Anthropic content_delta wrong: {p1['content_delta']!r}")
    if not p1["reasoning_content"] or "first principles" not in str(p1["reasoning_content"]):
        failures.append("Anthropic reasoning_content missing or wrong")
    # 103 chars / 4 = 26 (ceil) — char count is exact for the
    # concatenated string literal above.
    if p1["reasoning_tokens"] != 26:
        failures.append(
            f"Anthropic reasoning_tokens estimate wrong: "
            f"{p1['reasoning_tokens']} (expected 26)"
        )
    if p1["completion_tokens"] != 60:
        failures.append(
            f"Anthropic completion_tokens changed: {p1['completion_tokens']} "
            "(expected 60, untouched — thinking already in output_tokens)"
        )

    # Path 2 gates
    if p2["reasoning_tokens"] != 200:
        failures.append(
            f"OpenAI o1 reasoning_tokens wrong: {p2['reasoning_tokens']}"
        )
    if p2["reasoning_content"] is not None:
        failures.append(
            f"OpenAI o1 reasoning_content should be None: {p2['reasoning_content']!r}"
        )
    if p2["completion_tokens"] != 250:
        failures.append(
            f"OpenAI o1 completion_tokens wrong: {p2['completion_tokens']} "
            "(expected 250 — reasoning already included)"
        )

    # Path 3 gates
    if p3["reasoning_tokens"] != 40:
        failures.append(f"DeepSeek reasoning_tokens wrong: {p3['reasoning_tokens']}")
    if not p3["reasoning_content"] or "step by step" not in str(p3["reasoning_content"]):
        failures.append("DeepSeek reasoning_content missing")
    if p3["content_delta"] != "Final answer.":
        failures.append(
            f"DeepSeek content_delta wrong: {p3['content_delta']!r} "
            "(CoT should NOT leak into content)"
        )

    # Path 4 gates — the correctness fix
    if p4["completion_tokens"] != 520:
        failures.append(
            f"Gemini completion_tokens NOT post-fix: {p4['completion_tokens']} "
            "(expected candidates 20 + thoughts 500 = 520)"
        )
    if p4["reasoning_tokens"] != 500:
        failures.append(
            f"Gemini reasoning_tokens wrong: {p4['reasoning_tokens']}"
        )

    # Path 5 gates
    if p5a["reasoning_tokens"] != 8:
        failures.append(
            f"Bedrock Anthropic reasoning_tokens wrong: {p5a['reasoning_tokens']} "
            "(expected 8 = ceil(32/4))"
        )
    if p5a["completion_tokens"] != 35:
        failures.append(
            f"Bedrock Anthropic completion_tokens changed: {p5a['completion_tokens']}"
        )
    if p5b["reasoning_tokens"] != 8:
        failures.append(
            f"Vertex Anthropic reasoning_tokens wrong: {p5b['reasoning_tokens']}"
        )
    if p5b["completion_tokens"] != 35:
        failures.append(
            f"Vertex Anthropic completion_tokens changed: {p5b['completion_tokens']}"
        )

    # Regression gates
    if reg["reasoning_tokens"] != 0:
        failures.append(
            f"Plain Llama reasoning_tokens should be 0: {reg['reasoning_tokens']}"
        )
    if reg["reasoning_content"] is not None:
        failures.append(
            f"Plain Llama reasoning_content should be None: {reg['reasoning_content']!r}"
        )

    if failures:
        print("VERDICT: claim fails")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    delta = int(p4["completion_tokens"]) - int(p4["candidates_only"])  # type: ignore[arg-type]
    print(
        "VERDICT: claim holds — reasoning-token FinOps surface now "
        "covers five deployment paths uniformly. Anthropic direct + "
        "Bedrock + Vertex extract extended-thinking content into "
        "reasoning_content + estimate token count from char-length "
        "(26, 8, 8 tokens respectively for the synthesized inputs). "
        "OpenAI o-series surfaces reasoning_tokens=200 with no CoT "
        "text; DeepSeek R1 surfaces both reasoning_tokens=40 AND "
        f"the CoT text. Gemini correctness fix: completion_tokens "
        f"went from 20 (candidates only) to 520 (candidates + "
        f"thoughts), closing the under-billing gap by {delta} tokens "
        "on the synthesized example. Regression gate passes: a "
        "plain Llama response leaves reasoning_tokens=0 and "
        "reasoning_content=None. Substitution disclosure: parser-"
        "level direct invocation; no network hop. The same code "
        "paths fire on every real chat completion."
    )
    sys.exit(0)


if __name__ == "__main__":
    _main()
