from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field


class ProviderError(Exception):
    pass


class ProviderTimeout(ProviderError):
    pass


class InvalidExtraction(ProviderError):
    pass


class ExtractionProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    product_id: str | None = None
    canonical_url: str | None = None
    sku: str | None = None
    title: str
    price: str
    currency: str
    availability: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    observed_at: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    products: tuple[ExtractionProduct, ...]


@dataclass(frozen=True)
class ProviderConfig:
    model: str
    timeout_seconds: float = 10.0
    max_calls: int = 2
    max_tokens: int = 2000
    endpoint: str | None = None

    def __post_init__(self) -> None:
        if not self.model or not 0.1 <= self.timeout_seconds <= 120:
            raise ProviderError("provider timeout or model is invalid")
        if not 1 <= self.max_calls <= 4 or not 1 <= self.max_tokens <= 100_000:
            raise ProviderError("provider call or token limit is out of range")


@dataclass
class ProviderUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fallback_calls: int = 0


def parse_response_text(text: Any, usage: Mapping[str, Any]) -> tuple[ExtractionResponse, dict[str, int]]:
    if not isinstance(text, str):
        raise InvalidExtraction("provider did not return text")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidExtraction("provider returned invalid JSON") from exc
    try:
        parsed = ExtractionResponse.model_validate(raw)
    except ValueError as exc:
        raise InvalidExtraction("provider response does not match the extraction contract") from exc
    if not parsed.products:
        raise InvalidExtraction("provider response contains no products")
    prompt_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    completion_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ProviderError("provider reported negative token usage")
    return parsed, {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}


class Provider(ABC):
    provider_id: str

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.usage = ProviderUsage()

    @abstractmethod
    def _request(self, prompt: str) -> tuple[str, Mapping[str, Any]]:
        raise NotImplementedError

    def complete(self, prompt: str) -> ExtractionResponse:
        if self.usage.calls >= self.config.max_calls:
            raise ProviderError("provider call budget exhausted")
        self.usage.calls += 1
        try:
            text, usage = self._request(prompt)
        except TimeoutError as exc:
            raise ProviderTimeout(str(exc)) from exc
        response, token_usage = parse_response_text(text, usage)
        self.usage.prompt_tokens += token_usage["prompt_tokens"]
        self.usage.completion_tokens += token_usage["completion_tokens"]
        if self.usage.prompt_tokens + self.usage.completion_tokens > self.config.max_tokens:
            raise ProviderError("provider token budget exhausted")
        return response


def complete_with_fallback(
    primary: Provider,
    fallback: Provider,
    prompt: str,
    *,
    recover_from: tuple[type[ProviderError], ...] = (ProviderTimeout, ProviderError),
) -> ExtractionResponse:
    try:
        return primary.complete(prompt)
    except recover_from:
        if fallback.usage.calls >= fallback.config.max_calls:
            raise
        fallback.usage.fallback_calls += 1
        return fallback.complete(prompt)
