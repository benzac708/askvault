import json

import pytest

from app.core.config import settings
from app.services import answer as answer_service
from app.services.llm import get_llm
from app.services.llm.base import LLMError
from app.services.llm.mock import MockLLM
from app.services.llm.openai_compat import OpenAICompatLLM
from app.services.retrieval.models import Passage

BASE = "https://example.test/v1"


@pytest.fixture
def passages() -> list[Passage]:
    return [
        Passage("access-control#1", "access-control", "Secrets", "body one", -1.9, "snip one"),
        Passage("onboarding#0", "onboarding", "First day", "body two", -0.4, "snip two"),
    ]


class RudeLLM:
    """Ignores the numbering entirely. Citations must survive this."""

    model = "rude-model"

    def complete(self, system: str, user: str) -> str:
        return "I have no idea, honestly."


def test_no_passages_never_calls_the_model():
    llm = MockLLM()
    result = answer_service.compose("how do I price a banana", [], llm)
    assert result.passages_considered == 0
    assert result.citations == []
    assert result.answer == answer_service.NO_CONTEXT
    assert llm.calls == []


def test_numbered_context_reaches_the_provider(passages):
    llm = MockLLM()
    answer_service.compose("how are secrets handled", passages, llm)
    assert len(llm.calls) == 1
    system, user = llm.calls[0]
    assert "ONLY" in system
    assert "[1] access-control > Secrets" in user
    assert "[2] onboarding > First day" in user


def test_citations_survive_a_model_that_ignores_them(passages):
    result = answer_service.compose("q", passages, RudeLLM())
    assert [(c.doc, c.section) for c in result.citations] == [
        ("access-control", "Secrets"),
        ("onboarding", "First day"),
    ]
    assert result.model == "rude-model"


def test_every_citation_carries_a_snippet(passages):
    for c in answer_service.compose("q", passages, MockLLM()).citations:
        assert c.doc and c.section and c.snippet


def test_openai_compat_calls_chat_completions(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": "rotated within the hour"}}]},
    )
    llm = OpenAICompatLLM(model="vendor/model", base_url=BASE, api_key="k")
    assert llm.complete("sys", "user") == "rotated within the hour"

    sent = httpx_mock.get_requests()[0]
    body = json.loads(sent.content)
    assert sent.headers["Authorization"] == "Bearer k"
    assert body["model"] == "vendor/model"
    assert body["temperature"] == 0
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_openai_compat_wraps_upstream_failure(httpx_mock):
    httpx_mock.add_response(status_code=500, json={"error": "boom"})
    llm = OpenAICompatLLM(model="m", base_url=BASE, api_key="k")
    with pytest.raises(LLMError):
        llm.complete("sys", "user")


def test_openai_compat_wraps_a_malformed_payload(httpx_mock):
    httpx_mock.add_response(url=f"{BASE}/chat/completions", json={"nope": 1})
    llm = OpenAICompatLLM(model="m", base_url=BASE, api_key="k")
    with pytest.raises(LLMError):
        llm.complete("sys", "user")


@pytest.mark.parametrize("base_url,api_key", [("", "k"), (BASE, ""), ("", "")])
def test_openai_compat_refuses_to_start_without_credentials(base_url, api_key):
    with pytest.raises(LLMError):
        OpenAICompatLLM(model="m", base_url=base_url, api_key=api_key)


def test_default_provider_is_the_offline_mock(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    assert isinstance(get_llm(), MockLLM)


def test_real_provider_aliases_all_resolve(monkeypatch):
    for name in ("openai", "openrouter", "openai-compatible"):
        monkeypatch.setattr(settings, "llm_provider", name)
        monkeypatch.setattr(settings, "llm_base_url", BASE)
        monkeypatch.setattr(settings, "llm_api_key", "k")
        assert isinstance(get_llm(), OpenAICompatLLM)


def test_unknown_provider_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gpt-9-ultra")
    with pytest.raises(LLMError):
        get_llm()
