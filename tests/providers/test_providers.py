from __future__ import annotations

import json

import httpx
import pytest


from pricepulse.providers.anthropic import AnthropicProvider
from pricepulse.providers.base import (
    InvalidExtraction,
    ProviderConfig,
    ProviderError,
    ProviderTimeout,
    complete_with_fallback,
)
from pricepulse.providers.ollama import OllamaProvider
from pricepulse.providers.openai import OpenAIProvider


def dumps(value: object) -> str:
    return json.dumps(value)


PRODUCT = {
    "source": "protocol_double",
    "product_id": "p1",
    "sku": "s1",
    "title": "Coffee",
    "price": "12.99",
    "currency": "USD",
    "availability": "in_stock",
    "attributes": {},
    "observed_at": "2026-10-25T10:00:00Z",
    "evidence": {"selector": "article"},
}


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeTimeoutClient:
    def post(self, endpoint: str, json: dict) -> None:
        raise httpx.ReadTimeout("timed out")


def openai_body() -> dict:
    return {
        "choices": [{"message": {"content": json.dumps({"products": [PRODUCT]})}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }


def anthropic_body() -> dict:
    return {
        "content": [{"text": json.dumps({"products": [PRODUCT]})}],
        "usage": {"input_tokens": 11, "output_tokens": 21},
    }


def ollama_body() -> dict:
    return {
        "response": json.dumps({"products": [PRODUCT]}),
        "prompt_eval_count": 12,
        "eval_count": 22,
    }


def test_openai_adapter_normalizes_response_and_usage() -> None:
    provider = OpenAIProvider(
        ProviderConfig(model="local", max_calls=1, max_tokens=100),
    )
    class Client:
        def post(self, endpoint: str, json: dict, **kwargs) -> FakeResponse:
            assert json["max_tokens"] == 100
            return FakeResponse(openai_body())

    provider._client = Client()
    result = provider.complete('{"products":[]}')
    assert len(result.products) == 1
    assert result.products[0].price == "12.99"
    assert provider.usage.calls == 1
    assert provider.usage.prompt_tokens == 10
    assert provider.usage.completion_tokens == 20


def test_anthropic_adapter_uses_native_response_contract() -> None:
    provider = AnthropicProvider(ProviderConfig(model="local", max_calls=1, max_tokens=100))
    class Client:
        def post(self, endpoint: str, json: dict, **kwargs) -> FakeResponse:
            assert json["messages"][0]["role"] == "user"
            return FakeResponse(anthropic_body())

    provider._client = Client()
    result = provider.complete("data")
    assert result.products[0].title == "Coffee"
    assert provider.usage.prompt_tokens == 11
    assert provider.usage.completion_tokens == 21


def test_ollama_adapter_uses_local_protocol_usage_fields() -> None:
    provider = OllamaProvider(ProviderConfig(model="local", max_calls=1, max_tokens=100))
    class Client:
        def post(self, endpoint: str, json: dict, **kwargs) -> FakeResponse:
            return FakeResponse(ollama_body())

    provider._client = Client()
    result = provider.complete("data")
    assert result.products[0].product_id == "p1"
    assert provider.usage.calls == 1
    assert provider.usage.prompt_tokens == 12
    assert provider.usage.completion_tokens == 22


@pytest.mark.parametrize("provider_type", [OpenAIProvider, AnthropicProvider, OllamaProvider])
def test_adapters_reject_invalid_schema(provider_type) -> None:
    provider = provider_type(ProviderConfig(model="local", max_calls=1, max_tokens=100))
    class Client:
        def post(self, endpoint: str, json: dict, **kwargs) -> FakeResponse:
            payload = {"products": [{"source": "x", "title": "invalid"}]}
            if provider_type is OpenAIProvider:
                return FakeResponse({"choices": [{"message": {"content": dumps(payload)}}], "usage": {}})
            if provider_type is AnthropicProvider:
                return FakeResponse({"content": [{"text": dumps(payload)}], "usage": {}})
            return FakeResponse({"response": dumps(payload), "prompt_eval_count": 0, "eval_count": 0})

    provider._client = Client()
    with pytest.raises(InvalidExtraction):
        provider.complete("data")
    assert provider.usage.calls == 1


def test_fallback_is_single_call_and_preserves_contract() -> None:
    primary = OpenAIProvider(ProviderConfig(model="primary", max_calls=1, max_tokens=100))
    fallback = AnthropicProvider(ProviderConfig(model="fallback", max_calls=1, max_tokens=100))

    class TimeoutClient:
        def post(self, endpoint: str, json: dict, **kwargs) -> None:
            raise httpx.ReadTimeout("timeout")

    class FallbackClient:
        def post(self, endpoint: str, json: dict, **kwargs) -> FakeResponse:
            return FakeResponse(anthropic_body())

    primary._client = TimeoutClient()
    fallback._client = FallbackClient()
    result = complete_with_fallback(primary, fallback, "data")
    assert result.products[0].title == "Coffee"
    assert primary.usage.calls == 1
    assert fallback.usage.calls == 1
    assert fallback.usage.fallback_calls == 1


def test_timeout_and_budget_are_bounded() -> None:
    provider = OllamaProvider(ProviderConfig(model="local", timeout_seconds=0.2, max_calls=2, max_tokens=100))
    provider._client = FakeTimeoutClient()
    with pytest.raises(ProviderTimeout):
        provider.complete("data")
    with pytest.raises(ProviderTimeout):
        provider.complete("data")
    with pytest.raises(ProviderError):
        provider.complete("data")
    assert provider.usage.calls == 2
