from __future__ import annotations

import httpx

from pricepulse.providers.base import Provider, ProviderConfig, ProviderError


class OllamaProvider(Provider):
    provider_id = "ollama"

    def __init__(self, config: ProviderConfig, client: httpx.Client | None = None) -> None:
        super().__init__(config)
        self._client = client

    def _request(self, prompt: str) -> tuple[str, dict[str, int]]:
        endpoint = self.config.endpoint or "http://127.0.0.1:11434/api/generate"
        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {"num_predict": self.config.max_tokens},
        }
        client = self._client or httpx.Client(timeout=self.config.timeout_seconds)
        try:
            response = client.post(endpoint, json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.TimeoutException as exc:
            raise TimeoutError("Ollama request timed out") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("Ollama request failed") from exc
        finally:
            if self._client is None:
                client.close()
        if not isinstance(body, dict) or not isinstance(body.get("response"), str):
            raise ProviderError("Ollama response is malformed")
        usage = {
            "prompt_tokens": body.get("prompt_eval_count", 0),
            "completion_tokens": body.get("eval_count", 0),
        }
        return body["response"], usage
