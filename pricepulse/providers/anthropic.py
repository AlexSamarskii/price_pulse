from __future__ import annotations

import httpx

from pricepulse.providers.base import Provider, ProviderConfig, ProviderError


class AnthropicProvider(Provider):
    provider_id = "anthropic"

    def __init__(self, config: ProviderConfig, client: httpx.Client | None = None) -> None:
        super().__init__(config)
        self._client = client

    def _request(self, prompt: str) -> tuple[str, dict[str, int]]:
        endpoint = self.config.endpoint or "https://api.anthropic.com/v1/messages"
        payload = {
            "model": self.config.model,
            "system": "Return strict JSON with products.",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.config.max_tokens,
        }
        client = self._client or httpx.Client(timeout=self.config.timeout_seconds)
        try:
            response = client.post(endpoint, json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.TimeoutException as exc:
            raise TimeoutError("Anthropic request timed out") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("Anthropic request failed") from exc
        finally:
            if self._client is None:
                client.close()
        try:
            text = body["content"][0]["text"]
            usage = body["usage"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Anthropic response is malformed") from exc
        return text, usage

