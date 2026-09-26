import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

CORPUS_SECTIONS = 13


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "api.db"))
    monkeypatch.setattr(settings, "samples_dir", "samples")
    with TestClient(app) as c:
        yield c


def test_health_reports_the_indexed_passage_count(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["passages"] == CORPUS_SECTIONS


def test_chat_answers_with_citations(client):
    response = client.post("/chat", json={"question": "how do I request production access"})
    assert response.status_code == 200
    body = response.json()
    assert body["passages_considered"] > 0
    assert any(c["doc"] == "access-control" for c in body["citations"])
    assert all(c["doc"] and c["section"] for c in body["citations"])


def test_chat_is_byte_identical_across_calls(client):
    payload = {"question": "incident severity levels and escalation", "k": 2}
    assert client.post("/chat", json=payload).json() == client.post("/chat", json=payload).json()


@pytest.mark.parametrize(
    "payload",
    [
        {"question": ""},
        {"question": "access", "k": 0},
        {"question": "access", "k": 99},
        {},
    ],
)
def test_chat_validates_its_input(client, payload):
    assert client.post("/chat", json=payload).status_code == 422


def test_no_context_when_nothing_in_the_corpus_matches(client):
    """The no-context guard fires on out-of-vocabulary questions.

    Retrieval ORs every query token, so `k` passages come back if ANY word lands
    anywhere in the corpus. A genuinely unrelated question must therefore use
    words that appear in no document at all.
    """
    body = client.post("/chat", json={"question": "zzzqqq xyzzy frobnicate"}).json()
    assert body["passages_considered"] == 0
    assert body["citations"] == []
    assert "could not find" in body["answer"]


def test_a_nonsense_question_still_returns_citations(client):
    """Documents OR-recall honestly: 'price a banana' matches 'how/do/I/a'.

    High recall is the deliberate tradeoff for having no embeddings, but it means
    the no-context branch is a cheap pre-filter, not the primary guard. Rejecting
    an unsupported question is the LLM's job, via the system prompt.
    """
    body = client.post("/chat", json={"question": "how do I price a banana"}).json()
    assert body["passages_considered"] > 0
    assert len(body["citations"]) == body["passages_considered"]


def test_provider_failure_is_a_502_not_a_stack_trace(client, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "nope")
    response = client.post("/chat", json={"question": "access control"})
    assert response.status_code == 502
    assert "Traceback" not in response.text
