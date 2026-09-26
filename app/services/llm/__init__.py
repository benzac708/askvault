from app.core.config import settings
from app.services.llm.base import LLM, LLMError
from app.services.llm.mock import MockLLM
from app.services.llm.openai_compat import OpenAICompatLLM

_REAL = {"openai", "openrouter", "openai-compatible", "openai_compatible"}


def get_llm() -> LLM:
    """Provider is chosen by config, never by code. The model name lives in env.

    Default is `mock` so a fresh clone, a local run and CI all work with no
    secrets configured and no network egress.
    """
    provider = settings.llm_provider.strip().lower()
    if provider == "mock":
        return MockLLM()
    if provider in _REAL:
        return OpenAICompatLLM(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
        )
    raise LLMError(f"unknown llm_provider: {provider!r}")
