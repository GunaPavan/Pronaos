"""Unit tests for the AWS Bedrock provider adapter (Phase 42).

Five surfaces under test:

1. **SigV4 signing math**. The adapter's ``_sign()`` method must produce
   an Authorization header whose Credential scope says ``bedrock``,
   whose SignedHeaders includes ``content-type;host;x-amz-date``, and
   whose signature is the 64-hex-character HMAC-SHA256 over the
   canonical request. We don't assert exact bytes (botocore owns the
   primitive); we assert that the GATEWAY invokes SigV4Auth correctly.

2. **Per-family wire-shape translators**. Each model family
   (Anthropic, Llama, Nova, Mistral) gets the right body shape from
   the same OpenAI-compat request.

3. **Per-family response translators**. Each family's response shape
   collapses to a ChatCompletionChunk with content, finish reason,
   and token counts where the provider returns them.

4. **End-to-end non-streaming request through respx**. We mock the
   Bedrock endpoint, fire a chat completion, and verify the wire body
   + headers reaching the upstream are correct.

5. **Cost math** uses the catalog pricing for the resolved model.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from pronaos.providers.base import ChatCompletionRequest
from pronaos.providers.bedrock import (
    BedrockProvider,
    _build_anthropic_body,
    _build_llama_body,
    _build_mistral_body,
    _build_nova_body,
    _model_family,
    _parse_anthropic_response,
    _parse_llama_response,
    _parse_mistral_response,
    _parse_nova_response,
    _render_llama3_prompt,
    _render_mistral_prompt,
)

# AWS-canonical example credentials — these are the same dummy creds AWS
# uses in their published docs. Safe to commit; not real.
TEST_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
TEST_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
TEST_REGION = "us-east-1"


# --------------------------------------------------------------------------- #
# Model-family discriminator                                                  #
# --------------------------------------------------------------------------- #


class TestModelFamily:
    def test_anthropic(self) -> None:
        assert _model_family("anthropic.claude-3-5-haiku-20241022-v1:0") == "anthropic"

    def test_meta(self) -> None:
        assert _model_family("meta.llama3-70b-instruct-v1:0") == "meta"

    def test_amazon(self) -> None:
        assert _model_family("amazon.nova-pro-v1:0") == "amazon"

    def test_mistral(self) -> None:
        assert _model_family("mistral.mistral-large-2407-v1:0") == "mistral"


# --------------------------------------------------------------------------- #
# SigV4 signing                                                               #
# --------------------------------------------------------------------------- #


class TestSigV4Signing:
    def _provider(self) -> BedrockProvider:
        return BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )

    def test_authorization_header_has_bedrock_credential_scope(self) -> None:
        """The Credential field must put us in the bedrock service for the
        right region. This is the test that catches "we accidentally signed
        for sagemaker" or "we signed for the wrong region"."""
        prov = self._provider()
        signed = prov._sign(
            "POST",
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/x/invoke",
            b'{"foo":1}',
        )
        auth = signed.headers.get("Authorization") or ""
        # AWS4-HMAC-SHA256 Credential=<key>/<date>/<region>/<service>/aws4_request, ...
        m = re.search(
            r"Credential=AKIAIOSFODNN7EXAMPLE/\d{8}/us-east-1/bedrock/aws4_request",
            auth,
        )
        assert m is not None, f"unexpected Authorization header: {auth!r}"

    def test_signed_headers_include_content_type_and_host(self) -> None:
        prov = self._provider()
        signed = prov._sign(
            "POST",
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/x/invoke",
            b'{"foo":1}',
        )
        auth = signed.headers["Authorization"]
        m = re.search(r"SignedHeaders=([^,]+)", auth)
        assert m is not None
        signed_headers = m.group(1).split(";")
        # AWS requires content-type, host, x-amz-date in every Bedrock request.
        for required in ("content-type", "host", "x-amz-date"):
            assert required in signed_headers

    def test_signature_is_64_hex_chars(self) -> None:
        prov = self._provider()
        signed = prov._sign(
            "POST",
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/x/invoke",
            b'{"foo":1}',
        )
        auth = signed.headers["Authorization"]
        m = re.search(r"Signature=([0-9a-f]+)", auth)
        assert m is not None
        # SHA-256 → 32 bytes → 64 hex chars.
        assert len(m.group(1)) == 64

    def test_session_token_is_included_when_supplied(self) -> None:
        """When AWS_SESSION_TOKEN is configured (STS / role assumption),
        botocore adds X-Amz-Security-Token to the signed headers."""
        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
            session_token="FwoGZXIvYXdzEJr//////////wEaDLE_TEMP_TOKEN==",
        )
        signed = prov._sign(
            "POST",
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/x/invoke",
            b'{"foo":1}',
        )
        assert "X-Amz-Security-Token" in signed.headers

    def test_region_changes_signature(self) -> None:
        """Same request body + creds, different region -> different signature.
        Confirms the region IS in the canonical signing string."""
        body = b'{"foo":1}'
        url_template = "https://bedrock-runtime.{region}.amazonaws.com/model/x/invoke"

        p_east = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region="us-east-1",
        )
        s_east = p_east._sign("POST", url_template.format(region="us-east-1"), body)

        p_west = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region="us-west-2",
        )
        s_west = p_west._sign("POST", url_template.format(region="us-west-2"), body)

        assert s_east.headers["Authorization"] != s_west.headers["Authorization"]


# --------------------------------------------------------------------------- #
# Per-family request translators                                              #
# --------------------------------------------------------------------------- #


class TestAnthropicBodyShape:
    def test_basic_request_includes_anthropic_version(self) -> None:
        req = ChatCompletionRequest(
            model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
        )
        body = _build_anthropic_body(req, "anthropic.claude-3-5-haiku-20241022-v1:0")
        assert body["anthropic_version"] == "bedrock-2023-05-31"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert body["max_tokens"] == 10
        # CRITICAL: no ``model`` field (model is in URL).
        assert "model" not in body

    def test_system_hoisted(self) -> None:
        req = ChatCompletionRequest(
            model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
            messages=[
                {"role": "system", "content": "be helpful"},
                {"role": "user", "content": "hi"},
            ],
        )
        body = _build_anthropic_body(req, "anthropic.claude-3-5-haiku-20241022-v1:0")
        assert body["system"] == "be helpful"
        # system message removed from the message list.
        assert all(m["role"] != "system" for m in body["messages"])

    def test_tools_translated(self) -> None:
        req = ChatCompletionRequest(
            model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
            messages=[{"role": "user", "content": "look up weather"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "lookup",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )
        body = _build_anthropic_body(req, "anthropic.claude-3-5-haiku-20241022-v1:0")
        assert body["tools"][0]["name"] == "get_weather"
        # OpenAI calls it ``parameters``; Anthropic-on-Bedrock calls it ``input_schema``.
        assert body["tools"][0]["input_schema"] == {"type": "object", "properties": {}}


class TestLlamaBodyShape:
    def test_basic_request_renders_template(self) -> None:
        req = ChatCompletionRequest(
            model="bedrock/meta.llama3-70b-instruct-v1:0",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=50,
            temperature=0.5,
        )
        body = _build_llama_body(req, "meta.llama3-70b-instruct-v1:0")
        assert "prompt" in body
        # Llama 3 begin-of-text + assistant-header should both appear.
        assert "<|begin_of_text|>" in body["prompt"]
        assert "<|start_header_id|>user<|end_header_id|>" in body["prompt"]
        assert "<|start_header_id|>assistant<|end_header_id|>" in body["prompt"]
        assert body["max_gen_len"] == 50
        assert body["temperature"] == 0.5

    def test_multi_turn_template(self) -> None:
        prompt = _render_llama3_prompt(
            [
                {"role": "system", "content": "be concise"},
                {"role": "user", "content": "What's 2+2?"},
                {"role": "assistant", "content": "4"},
                {"role": "user", "content": "Are you sure?"},
            ]
        )
        # Each turn gets a header pair; assistant turn ends in <|eot_id|>;
        # last assistant header is OPEN for the model to continue.
        assert prompt.count("<|eot_id|>") == 4  # system + user + assistant + user
        assert prompt.endswith("<|start_header_id|>assistant<|end_header_id|>\n\n")


class TestNovaBodyShape:
    def test_basic_request(self) -> None:
        req = ChatCompletionRequest(
            model="bedrock/amazon.nova-pro-v1:0",
            messages=[{"role": "user", "content": "describe a cat"}],
            max_tokens=20,
            temperature=0.7,
        )
        body = _build_nova_body(req, "amazon.nova-pro-v1:0")
        assert body["inferenceConfig"] == {"maxTokens": 20, "temperature": 0.7}
        # Nova wraps content in {"text": "..."} parts even for plain strings.
        assert body["messages"][0] == {"role": "user", "content": [{"text": "describe a cat"}]}

    def test_system_separated(self) -> None:
        req = ChatCompletionRequest(
            model="bedrock/amazon.nova-pro-v1:0",
            messages=[
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ],
        )
        body = _build_nova_body(req, "amazon.nova-pro-v1:0")
        assert body["system"] == [{"text": "be terse"}]
        # system DOES NOT leak into messages.
        assert all(m["role"] != "system" for m in body["messages"])

    def test_image_data_uri_translated(self) -> None:
        req = ChatCompletionRequest(
            model="bedrock/amazon.nova-pro-v1:0",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what's in this image?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,iVBORw=="},
                        },
                    ],
                }
            ],
        )
        body = _build_nova_body(req, "amazon.nova-pro-v1:0")
        parts = body["messages"][0]["content"]
        assert parts[0] == {"text": "what's in this image?"}
        # Image part uses Nova's {"image": {"format": "png", "source": {"bytes": "..."}}} shape.
        assert parts[1] == {"image": {"format": "png", "source": {"bytes": "iVBORw=="}}}


class TestMistralBodyShape:
    def test_basic_request(self) -> None:
        req = ChatCompletionRequest(
            model="bedrock/mistral.mistral-large-2407-v1:0",
            messages=[{"role": "user", "content": "what's the capital of france?"}],
            max_tokens=30,
        )
        body = _build_mistral_body(req, "mistral.mistral-large-2407-v1:0")
        assert "[INST]" in body["prompt"]
        assert "[/INST]" in body["prompt"]
        assert body["max_tokens"] == 30

    def test_system_inlined_in_first_inst(self) -> None:
        prompt = _render_mistral_prompt(
            [
                {"role": "system", "content": "be concise"},
                {"role": "user", "content": "hi"},
            ]
        )
        # System prompt should be inside the first [INST] block.
        assert "[INST] be concise" in prompt
        # The user's content follows.
        assert "hi [/INST]" in prompt


# --------------------------------------------------------------------------- #
# Per-family response translators                                             #
# --------------------------------------------------------------------------- #


class TestAnthropicResponseParse:
    def test_text_block(self) -> None:
        chunk = _parse_anthropic_response(
            {
                "content": [{"type": "text", "text": "hello there"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 3},
            }
        )
        assert chunk.content_delta == "hello there"
        assert chunk.finish_reason == "stop"
        assert chunk.prompt_tokens == 10
        assert chunk.completion_tokens == 3
        assert chunk.tool_calls is None

    def test_tool_use_block(self) -> None:
        chunk = _parse_anthropic_response(
            {
                "content": [
                    {"type": "text", "text": "calling"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 25, "output_tokens": 8},
            }
        )
        assert chunk.finish_reason == "tool_calls"
        assert chunk.tool_calls is not None
        tc = chunk.tool_calls[0]
        assert tc["id"] == "toolu_1"
        assert tc["function"]["name"] == "get_weather"
        # arguments must be a JSON-encoded STRING (matching OpenAI shape exactly).
        assert json.loads(tc["function"]["arguments"]) == {"city": "Paris"}

    def test_thinking_block_surfaces_on_bedrock(self) -> None:
        """Phase 56: Anthropic-on-Bedrock returns the same
        ``type: "thinking"`` content blocks as direct Anthropic.
        The parser must extract thinking text into reasoning_content
        and estimate the count via the same ~4-chars-per-token
        heuristic."""
        chunk = _parse_anthropic_response(
            {
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Reasoning about the AWS call here.",
                        "signature": "opaque",
                    },
                    {"type": "text", "text": "Done."},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 15, "output_tokens": 25},
            }
        )
        assert chunk.content_delta == "Done."
        assert chunk.reasoning_content == "Reasoning about the AWS call here."
        # 35 chars → ceil(35/4) = 9 tokens.
        assert chunk.reasoning_tokens == 9
        # completion_tokens unchanged — Anthropic already includes
        # thinking in output_tokens.
        assert chunk.completion_tokens == 25

    def test_no_thinking_block_leaves_reasoning_unset(self) -> None:
        """Plain-text Anthropic-on-Bedrock response: reasoning fields
        stay at 0/None (no behavioural change for non-thinking models)."""
        chunk = _parse_anthropic_response(
            {
                "content": [{"type": "text", "text": "Hi."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        )
        assert chunk.reasoning_tokens == 0
        assert chunk.reasoning_content is None

    def test_prompt_cache_fields_extracted(self) -> None:
        """Phase 55: Anthropic-on-Bedrock surfaces the same
        cache_creation_input_tokens + cache_read_input_tokens fields
        as direct Anthropic. The parser must capture them so the
        chat handler's downstream FinOps headers + weighted cost
        math apply uniformly."""
        chunk = _parse_anthropic_response(
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 1000,
                    "cache_read_input_tokens": 4000,
                },
            }
        )
        # input_tokens per Anthropic spec is the NON-cached portion.
        assert chunk.prompt_tokens == 100
        assert chunk.cache_creation_tokens == 1000
        assert chunk.cache_read_tokens == 4000

    def test_prompt_cache_fields_default_to_zero_when_absent(self) -> None:
        """Requests that don't use cache_control get cache_*_tokens=0,
        not None — keeps the downstream math + headers branch-free."""
        chunk = _parse_anthropic_response(
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            }
        )
        assert chunk.cache_creation_tokens == 0
        assert chunk.cache_read_tokens == 0


class TestLlamaResponseParse:
    def test_completed_generation(self) -> None:
        chunk = _parse_llama_response(
            {
                "generation": "The answer is 4.",
                "prompt_token_count": 8,
                "generation_token_count": 5,
                "stop_reason": "stop",
            }
        )
        assert chunk.content_delta == "The answer is 4."
        assert chunk.finish_reason == "stop"
        assert chunk.prompt_tokens == 8
        assert chunk.completion_tokens == 5

    def test_length_hit(self) -> None:
        chunk = _parse_llama_response(
            {
                "generation": "truncated",
                "prompt_token_count": 100,
                "generation_token_count": 50,
                "stop_reason": "length",
            }
        )
        assert chunk.finish_reason == "length"


class TestNovaResponseParse:
    def test_text_only(self) -> None:
        chunk = _parse_nova_response(
            {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [{"text": "A grey cat sitting on a chair."}],
                    }
                },
                "stopReason": "end_turn",
                "usage": {"inputTokens": 50, "outputTokens": 8, "totalTokens": 58},
            }
        )
        assert chunk.content_delta == "A grey cat sitting on a chair."
        assert chunk.finish_reason == "stop"
        assert chunk.prompt_tokens == 50
        assert chunk.completion_tokens == 8


class TestMistralResponseParse:
    def test_text(self) -> None:
        chunk = _parse_mistral_response(
            {
                "outputs": [{"text": "Paris.", "stop_reason": "stop"}],
            }
        )
        assert chunk.content_delta == "Paris."
        assert chunk.finish_reason == "stop"

    def test_empty_outputs(self) -> None:
        chunk = _parse_mistral_response({"outputs": []})
        assert chunk.content_delta == ""
        assert chunk.finish_reason is None


# --------------------------------------------------------------------------- #
# End-to-end (adapter-level, respx-mocked)                                    #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_adapter_signs_and_posts_anthropic_on_bedrock() -> None:
    """End-to-end through ``chat_completion``: the wire body to Bedrock
    is correctly shaped for Anthropic-on-Bedrock AND the Authorization
    header is a SigV4 signature for the bedrock service."""
    url = (
        "https://bedrock-runtime.us-east-1.amazonaws.com/"
        "model/anthropic.claude-3-5-haiku-20241022-v1:0/invoke"
    )
    route = respx.post(url).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Paris is the capital."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 6},
            },
        )
    )

    prov = BedrockProvider(
        access_key_id=TEST_ACCESS_KEY,
        secret_access_key=TEST_SECRET_KEY,
        region=TEST_REGION,
    )
    req = ChatCompletionRequest(
        model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
        messages=[{"role": "user", "content": "capital of france?"}],
        max_tokens=20,
    )
    chunks = []
    async for c in await prov.chat_completion(req):
        chunks.append(c)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.content_delta == "Paris is the capital."
    assert chunk.finish_reason == "stop"

    # Inspect the actual outbound request that hit the mock.
    sent = route.calls[0].request
    auth = sent.headers.get("authorization") or sent.headers.get("Authorization") or ""
    assert "AWS4-HMAC-SHA256" in auth
    assert "/bedrock/aws4_request" in auth

    body = json.loads(sent.content)
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert body["messages"] == [{"role": "user", "content": "capital of france?"}]
    # No ``model`` field in the body — it's in the URL.
    assert "model" not in body


@respx.mock
@pytest.mark.asyncio
async def test_adapter_handles_llama_response() -> None:
    """Llama-on-Bedrock returns its own response shape; the adapter
    translates to the canonical ChatCompletionChunk."""
    url = (
        "https://bedrock-runtime.us-east-1.amazonaws.com/model/meta.llama3-70b-instruct-v1:0/invoke"
    )
    route = respx.post(url).mock(
        return_value=httpx.Response(
            200,
            json={
                "generation": "Paris.",
                "prompt_token_count": 15,
                "generation_token_count": 1,
                "stop_reason": "stop",
            },
        )
    )
    prov = BedrockProvider(
        access_key_id=TEST_ACCESS_KEY,
        secret_access_key=TEST_SECRET_KEY,
        region=TEST_REGION,
    )
    req = ChatCompletionRequest(
        model="bedrock/meta.llama3-70b-instruct-v1:0",
        messages=[{"role": "user", "content": "What is the capital of France?"}],
    )
    chunks = []
    async for c in await prov.chat_completion(req):
        chunks.append(c)

    assert chunks[0].content_delta == "Paris."
    assert chunks[0].prompt_tokens == 15
    # The outbound body should be the Llama prompt shape, NOT the
    # Anthropic messages shape.
    body = json.loads(route.calls[0].request.content)
    assert "prompt" in body
    assert "messages" not in body


@respx.mock
@pytest.mark.asyncio
async def test_adapter_raises_on_400() -> None:
    """A 4xx upstream response surfaces as ProviderError so the chat
    handler can map it to HTTP."""
    from pronaos.providers.base import ProviderError

    url = (
        "https://bedrock-runtime.us-east-1.amazonaws.com/"
        "model/anthropic.claude-3-5-haiku-20241022-v1:0/invoke"
    )
    respx.post(url).mock(
        return_value=httpx.Response(
            400,
            json={"message": "Invalid request body"},
        )
    )
    prov = BedrockProvider(
        access_key_id=TEST_ACCESS_KEY,
        secret_access_key=TEST_SECRET_KEY,
        region=TEST_REGION,
    )
    req = ChatCompletionRequest(
        model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
        messages=[{"role": "user", "content": "x"}],
    )
    with pytest.raises(ProviderError) as exc_info:
        async for _ in await prov.chat_completion(req):
            pass
    assert "Invalid request body" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Cost math                                                                   #
# --------------------------------------------------------------------------- #


class TestCostMath:
    def test_haiku_pricing(self) -> None:
        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        # Haiku: $0.80/Mtok input, $4.00/Mtok output → 80_000 / 400_000 hcents.
        # 1000 input tokens → 80_000 * 1000 / 1_000_000 = 80 hcents
        # 500 output tokens → 400_000 * 500 / 1_000_000 = 200 hcents
        # total = 280 hcents
        cost = prov.cost_cents(
            prompt_tokens=1000,
            completion_tokens=500,
            model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
        )
        assert cost == 280

    def test_unknown_model_returns_zero(self) -> None:
        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        # Not in the catalog → 0 (Pronaos won't guess at prices).
        cost = prov.cost_cents(
            prompt_tokens=100,
            completion_tokens=50,
            model="bedrock/unknown.model-v1:0",
        )
        assert cost == 0

    def test_cache_args_ignored_for_zero_tokens(self) -> None:
        """Cache args at zero produce the same cost as omitting them
        (matches the plain math branch — no weighted math when there
        are no cache tokens to weight)."""
        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        cost_with = prov.cost_cents(
            prompt_tokens=1000,
            completion_tokens=500,
            model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
            cache_creation_tokens=0,
            cache_read_tokens=0,
        )
        cost_without = prov.cost_cents(
            prompt_tokens=1000,
            completion_tokens=500,
            model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
        )
        assert cost_with == cost_without

    def test_anthropic_cache_weighted_math(self) -> None:
        """Phase 55: Anthropic-on-Bedrock applies the same weighted
        prompt-cache pricing as direct Anthropic — 1.25x for cache
        creation, 0.10x for cache reads."""
        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        # Haiku: input 80_000 hcents/Mtok, output 400_000 hcents/Mtok.
        # 1000 non-cached input → 80_000 * 1000 / 1_000_000 = 80 hcents
        # 500 cache_creation @ 1.25x → 80_000 * 500 * 125 / 100_000_000 = 50 hcents
        # 200 cache_read @ 0.10x → 80_000 * 200 * 10 / 100_000_000 = 1 hcent (integer truncation)
        # 500 output → 400_000 * 500 / 1_000_000 = 200 hcents
        # Total = 331 hcents
        cost = prov.cost_cents(
            prompt_tokens=1000,
            completion_tokens=500,
            model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
            cache_creation_tokens=500,
            cache_read_tokens=200,
        )
        assert cost == 80 + 50 + 1 + 200  # 331

    def test_anthropic_cache_heavy_read_workload(self) -> None:
        """A workload dominated by cache reads (RAG re-prompt) should
        cost dramatically less than the same workload without the cache."""
        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        # Sonnet: input 300_000 hcents/Mtok, output 1_500_000 hcents/Mtok.
        # Scenario: 10_000 token prefix (now cached) + 100 token query + 200 output.
        # Without cache: 10_100 input * 300_000 / 1_000_000 = 3_030 hcents
        # With cache (prefix hit): 100 non-cached + 10_000 cache_read @ 0.10x
        #   = 100 * 300_000 / 1_000_000 = 30 hcents
        #   + 10_000 * 300_000 * 10 / 100_000_000 = 300 hcents
        #   = 330 hcents
        # → 89% cost reduction on the input portion.
        with_cache = prov.cost_cents(
            prompt_tokens=100,  # non-cached part
            completion_tokens=200,
            model="bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
            cache_creation_tokens=0,
            cache_read_tokens=10_000,
        )
        # 30 (non-cached input) + 300 (cache reads) + 300 (output) = 630
        assert with_cache == 30 + 300 + 300

    def test_non_anthropic_family_ignores_cache_args(self) -> None:
        """Llama / Nova / Mistral on Bedrock don't support prompt
        caching — cache args fall through to the plain math (matches
        the documented behaviour in cost_cents docstring)."""
        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        cost_with = prov.cost_cents(
            prompt_tokens=1000,
            completion_tokens=500,
            model="bedrock/meta.llama3-70b-instruct-v1:0",
            cache_creation_tokens=999,
            cache_read_tokens=999,
        )
        cost_without = prov.cost_cents(
            prompt_tokens=1000,
            completion_tokens=500,
            model="bedrock/meta.llama3-70b-instruct-v1:0",
        )
        assert cost_with == cost_without


# --------------------------------------------------------------------------- #
# Phase 52 — streaming via AWS event-stream binary protocol                   #
# --------------------------------------------------------------------------- #
#
# These tests build a stream of real event-stream binary frames (real
# CRC32s via the parser-module encoder) and feed them through respx
# back to the adapter. The chunks emerging from ``chat_completion``
# carry the per-family deltas in OpenAI-compat ChatCompletionChunk
# shape.


def _make_bedrock_stream_body(payloads: list[dict[str, object]]) -> bytes:
    """Build a sequence of event-stream frames for Bedrock streaming.

    Each input ``payload`` becomes one frame whose binary payload
    is ``{"bytes": "<base64-of-utf8-json>"}`` — the wrapping Bedrock
    uses around every streamed event.
    """
    import base64

    from pronaos.providers.bedrock_eventstream import encode_frame

    frames = []
    for p in payloads:
        inner = json.dumps(p).encode("utf-8")
        wrapped = json.dumps({"bytes": base64.b64encode(inner).decode()}).encode("utf-8")
        frames.append(
            encode_frame(
                headers={
                    ":message-type": "event",
                    ":event-type": "chunk",
                    ":content-type": "application/json",
                },
                payload=wrapped,
            )
        )
    return b"".join(frames)


class TestBedrockStreamingURL:
    def test_streaming_endpoint_url_uses_invoke_with_response_stream(
        self,
    ) -> None:
        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        url = prov._streaming_endpoint_url("anthropic.claude-3-5-haiku-20241022-v1:0")
        assert "/invoke-with-response-stream" in url
        assert "bedrock-runtime.us-east-1.amazonaws.com" in url


class TestAnthropicOnBedrockStreaming:
    @pytest.mark.asyncio
    async def test_streams_text_delta_chunks_then_finish(self) -> None:
        payloads = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_X",
                    "usage": {"input_tokens": 12, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hello"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": ", "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "world!"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 4},
            },
            {"type": "message_stop"},
        ]
        body = _make_bedrock_stream_body(payloads)

        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        with respx.mock(base_url="https://bedrock-runtime.us-east-1.amazonaws.com") as mock:
            route = mock.post(re.compile(r".*/invoke-with-response-stream$")).mock(
                return_value=httpx.Response(
                    200,
                    content=body,
                    headers={"content-type": "application/vnd.amazon.eventstream"},
                )
            )
            req = ChatCompletionRequest(
                model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
                messages=[{"role": "user", "content": "say hi"}],
                stream=True,
            )
            chunks = [c async for c in await prov.chat_completion(req)]
        assert route.called
        # Three content-bearing chunks + one terminal chunk
        text_chunks = [c for c in chunks if c.content_delta]
        assert [c.content_delta for c in text_chunks] == ["Hello", ", ", "world!"]
        # Terminal chunk carries finish_reason + token counts
        terminal = chunks[-1]
        assert terminal.finish_reason == "stop"
        assert terminal.prompt_tokens == 12
        assert terminal.completion_tokens == 4
        await prov.aclose()

    @pytest.mark.asyncio
    async def test_tool_use_assembled_on_final_chunk(self) -> None:
        """Anthropic streams a tool_use block as a content_block_start
        with type=tool_use followed by input_json_delta deltas. The
        adapter must accumulate the JSON args and emit them as a
        single OpenAI-shape tool_call on the terminal chunk."""
        payloads = [
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 5, "output_tokens": 0}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_X",
                    "name": "get_weather",
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '"Tokyo"}'},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 7},
            },
            {"type": "message_stop"},
        ]
        body = _make_bedrock_stream_body(payloads)

        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        with respx.mock(base_url="https://bedrock-runtime.us-east-1.amazonaws.com") as mock:
            mock.post(re.compile(r".*/invoke-with-response-stream$")).mock(
                return_value=httpx.Response(
                    200,
                    content=body,
                    headers={"content-type": "application/vnd.amazon.eventstream"},
                )
            )
            req = ChatCompletionRequest(
                model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
                messages=[{"role": "user", "content": "weather?"}],
                stream=True,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
            chunks = [c async for c in await prov.chat_completion(req)]
        terminal = chunks[-1]
        assert terminal.finish_reason == "tool_calls"
        assert terminal.tool_calls is not None
        assert len(terminal.tool_calls) == 1
        tc = terminal.tool_calls[0]
        assert tc["id"] == "toolu_X"
        assert tc["function"]["name"] == "get_weather"
        # Args accumulated from the two input_json_delta frames
        assert json.loads(tc["function"]["arguments"]) == {"city": "Tokyo"}
        await prov.aclose()

    @pytest.mark.asyncio
    async def test_cache_tokens_surface_on_terminal_chunk(self) -> None:
        """Phase 55: Anthropic-on-Bedrock streams carry
        cache_creation_input_tokens + cache_read_input_tokens
        on the message_start usage block. The translator must
        capture them and emit them on the terminal chunk so
        downstream FinOps + weighted cost math fire."""
        payloads = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_C",
                    "usage": {
                        "input_tokens": 50,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 1000,
                        "cache_read_input_tokens": 4000,
                    },
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "cached"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 6},
            },
            {"type": "message_stop"},
        ]
        body = _make_bedrock_stream_body(payloads)
        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        with respx.mock(base_url="https://bedrock-runtime.us-east-1.amazonaws.com") as mock:
            mock.post(re.compile(r".*/invoke-with-response-stream$")).mock(
                return_value=httpx.Response(
                    200,
                    content=body,
                    headers={"content-type": "application/vnd.amazon.eventstream"},
                )
            )
            req = ChatCompletionRequest(
                model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
                messages=[{"role": "user", "content": "cached?"}],
                stream=True,
            )
            chunks = [c async for c in await prov.chat_completion(req)]
        terminal = chunks[-1]
        assert terminal.finish_reason == "stop"
        assert terminal.prompt_tokens == 50
        assert terminal.completion_tokens == 6
        assert terminal.cache_creation_tokens == 1000
        assert terminal.cache_read_tokens == 4000
        await prov.aclose()


class TestLlamaOnBedrockStreaming:
    @pytest.mark.asyncio
    async def test_per_frame_generation_chunks(self) -> None:
        payloads = [
            {
                "generation": "Hello",
                "prompt_token_count": 10,
                "generation_token_count": 1,
                "stop_reason": None,
            },
            {
                "generation": " world",
                "prompt_token_count": 10,
                "generation_token_count": 2,
                "stop_reason": None,
            },
            {
                "generation": "",
                "prompt_token_count": 10,
                "generation_token_count": 2,
                "stop_reason": "stop",
            },
        ]
        body = _make_bedrock_stream_body(payloads)

        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        with respx.mock(base_url="https://bedrock-runtime.us-east-1.amazonaws.com") as mock:
            mock.post(re.compile(r".*/invoke-with-response-stream$")).mock(
                return_value=httpx.Response(
                    200,
                    content=body,
                    headers={"content-type": "application/vnd.amazon.eventstream"},
                )
            )
            req = ChatCompletionRequest(
                model="bedrock/meta.llama3-1-8b-instruct-v1:0",
                messages=[{"role": "user", "content": "say hi"}],
                stream=True,
            )
            chunks = [c async for c in await prov.chat_completion(req)]
        text_chunks = [c.content_delta for c in chunks if c.content_delta]
        assert text_chunks == ["Hello", " world"]
        terminal = chunks[-1]
        assert terminal.finish_reason == "stop"
        assert terminal.prompt_tokens == 10
        assert terminal.completion_tokens == 2
        await prov.aclose()


class TestNovaStreaming:
    @pytest.mark.asyncio
    async def test_content_block_delta_then_message_stop_then_metadata(
        self,
    ) -> None:
        payloads = [
            {"messageStart": {"role": "assistant"}},
            {
                "contentBlockDelta": {
                    "delta": {"text": "Hi"},
                    "contentBlockIndex": 0,
                }
            },
            {
                "contentBlockDelta": {
                    "delta": {"text": " there"},
                    "contentBlockIndex": 0,
                }
            },
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "end_turn"}},
            {
                "metadata": {
                    "usage": {
                        "inputTokens": 3,
                        "outputTokens": 2,
                        "totalTokens": 5,
                    }
                }
            },
        ]
        body = _make_bedrock_stream_body(payloads)
        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        with respx.mock(base_url="https://bedrock-runtime.us-east-1.amazonaws.com") as mock:
            mock.post(re.compile(r".*/invoke-with-response-stream$")).mock(
                return_value=httpx.Response(
                    200,
                    content=body,
                    headers={"content-type": "application/vnd.amazon.eventstream"},
                )
            )
            req = ChatCompletionRequest(
                model="bedrock/amazon.nova-lite-v1:0",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
            chunks = [c async for c in await prov.chat_completion(req)]
        text_chunks = [c.content_delta for c in chunks if c.content_delta]
        assert text_chunks == ["Hi", " there"]
        # messageStop produces a finish-reason chunk
        finish_chunks = [c for c in chunks if c.finish_reason]
        assert finish_chunks
        assert finish_chunks[0].finish_reason == "stop"
        # metadata frame surfaces the usage on its own chunk
        usage_chunks = [
            c for c in chunks if c.prompt_tokens is not None or c.completion_tokens is not None
        ]
        assert usage_chunks
        usage = usage_chunks[-1]
        assert usage.prompt_tokens == 3
        assert usage.completion_tokens == 2
        await prov.aclose()


class TestMistralOnBedrockStreaming:
    @pytest.mark.asyncio
    async def test_outputs_chunks(self) -> None:
        payloads = [
            {"outputs": [{"text": "ab", "stop_reason": None}]},
            {"outputs": [{"text": "cd", "stop_reason": None}]},
            {"outputs": [{"text": "", "stop_reason": "stop"}]},
        ]
        body = _make_bedrock_stream_body(payloads)
        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        with respx.mock(base_url="https://bedrock-runtime.us-east-1.amazonaws.com") as mock:
            mock.post(re.compile(r".*/invoke-with-response-stream$")).mock(
                return_value=httpx.Response(
                    200,
                    content=body,
                    headers={"content-type": "application/vnd.amazon.eventstream"},
                )
            )
            req = ChatCompletionRequest(
                model="bedrock/mistral.mistral-large-2407-v1:0",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
            chunks = [c async for c in await prov.chat_completion(req)]
        text_chunks = [c.content_delta for c in chunks if c.content_delta]
        assert text_chunks == ["ab", "cd"]
        terminal = chunks[-1]
        assert terminal.finish_reason == "stop"
        await prov.aclose()


class TestBedrockStreamingErrors:
    @pytest.mark.asyncio
    async def test_upstream_4xx_raises_typed_error(self) -> None:
        """Bedrock returns plain JSON on error status even on the
        streaming endpoint. The adapter must raise the right typed
        error before yielding any chunks."""
        from pronaos.providers.base import ProviderError

        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        with respx.mock(base_url="https://bedrock-runtime.us-east-1.amazonaws.com") as mock:
            mock.post(re.compile(r".*/invoke-with-response-stream$")).mock(
                return_value=httpx.Response(
                    400,
                    json={"message": "ValidationException"},
                )
            )
            req = ChatCompletionRequest(
                model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
            with pytest.raises(ProviderError):
                gen = await prov.chat_completion(req)
                [c async for c in gen]
        await prov.aclose()

    @pytest.mark.asyncio
    async def test_exception_frame_translates_to_provider_error(self) -> None:
        """When Bedrock surfaces an upstream-model error mid-stream
        via an event-stream ``:message-type=exception`` frame, the
        adapter must raise ``ProviderError`` (with retryable=True so
        the failover layer treats it like a 502)."""
        from pronaos.providers.base import ProviderError
        from pronaos.providers.bedrock_eventstream import encode_frame

        prov = BedrockProvider(
            access_key_id=TEST_ACCESS_KEY,
            secret_access_key=TEST_SECRET_KEY,
            region=TEST_REGION,
        )
        body = encode_frame(
            headers={":message-type": "exception"},
            payload=b'{"errorMessage":"throttled"}',
        )
        with respx.mock(base_url="https://bedrock-runtime.us-east-1.amazonaws.com") as mock:
            mock.post(re.compile(r".*/invoke-with-response-stream$")).mock(
                return_value=httpx.Response(
                    200,
                    content=body,
                    headers={"content-type": "application/vnd.amazon.eventstream"},
                )
            )
            req = ChatCompletionRequest(
                model="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
            with pytest.raises(ProviderError, match="stream exception"):
                gen = await prov.chat_completion(req)
                [c async for c in gen]
        await prov.aclose()
