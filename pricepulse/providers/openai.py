from __future__ import annotations

import httpx

from pricepulse.providers.base import Provider, ProviderConfig, ProviderError


class OpenAIProvider(Provider):
    provider_id = "openai"

    def __init__(self, config: ProviderConfig, client: httpx.Client | None = None) -> None:
        super().__init__(config)
        self._client = client

    def _request(self, prompt: str) -> tuple[str, dict[str, int]]:
        endpoint = self.config.endpoint or "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": "Return strict JSON with products."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self.config.max_tokens,
            "response_format": {"type": "json_object"},
        }
        client = self._client or httpx.Client(timeout=self.config.timeout_seconds)
        try:
            response = client.post(endpoint, json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.TimeoutException as exc:
            raise TimeoutError("OpenAI request timed out") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("OpenAI request failed") from exc
        finally:
            if self._client is None:
                client.close()
        try:
            text = body["choices"][0]["message"]["content"]
            usage = body["usage"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("OpenAI response is malformed") from exc
        return text, usage

