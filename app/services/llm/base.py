from typing import Protocol


class LLMError(RuntimeError):
    """Provider misconfiguration or upstream failure. Surfaces as HTTP 502."""


class LLM(Protocol):
    """The entire LLM surface this app needs: one prompt in, one string out.

    Deliberately synchronous. FastAPI runs sync endpoints in a threadpool, so a
    blocking HTTP call is correct here, and it keeps the provider swappable
    without pulling an async test plugin into CI.
    """

    model: str

    def complete(self, system: str, user: str) -> str: ...
