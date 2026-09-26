import httpx

from app.services.llm.base import LLMError


class OpenAICompatLLM:
    """Any endpoint speaking OpenAI's /chat/completions. OpenRouter included.

    One client per call, closed on the way out. The factory builds a fresh
    instance per request, so there is no pool to leak and no lifespan to manage;
    a real high-traffic service would hoist a client to module level and reuse
    the connection pool.
    """

    def __init__(self, model: str, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        if not base_url or not api_key:
            raise LLMError("llm_base_url and llm_api_key must be set for a real provider")
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            with httpx.Client(base_url=self._base_url, timeout=self._timeout) as client:
                response = client.post("/chat/completions", headers=headers, json=payload)
                response.raise_for_status()
                return str(response.json()["choices"][0]["message"]["content"])
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
