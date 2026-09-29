import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

CORPUS_SECTIONS = 13


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "api.db"))
    monkeypatch.setattr(settings, "samples_dir", "samples")
    # Off by default so the limiter never masks a functional assertion. D32 gets
    # its own tests in test_limits.py and its own API-level tests at the bottom.
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    with TestClient(app) as c:
        yield c


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


def test_out_of_vocabulary_question_is_refused_cleanly(client):
    """Stopword-stripped OR-recall: a fully out-of-vocabulary question is gone.

    The matcher drops functional words, so 'how do I price a banana' reduces to
    content words that appear nowhere in the corpus and the no-context branch
    refuses cleanly. High recall now applies to *content* words only; rejecting
    an unsupported question is still the LLM's job for near-misses, but the
    cheap pre-filter is a real guard for total term misses.
    """
    body = client.post("/chat", json={"question": "how do I price a banana"}).json()
    assert body["passages_considered"] == 0
    assert body["citations"] == []
    assert "could not find" in body["answer"]


def test_provider_failure_is_a_502_not_a_stack_trace(client, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "nope")
    response = client.post("/chat", json={"question": "access control"})
    assert response.status_code == 502
    assert "Traceback" not in response.text


# --- D32 at the API seam ---


def limited(monkeypatch, tmp_path, **overrides):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "rl.db"))
    monkeypatch.setattr(settings, "samples_dir", "samples")
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_per_ip_per_minute", 2)
    monkeypatch.setattr(settings, "rate_limit_global_per_minute", 100)
    monkeypatch.setattr(settings, "rate_limit_global_per_day", 1000)
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)
    return TestClient(app)


def test_chat_returns_429_with_a_retry_hint_when_the_ceiling_is_hit(monkeypatch, tmp_path):
    with limited(monkeypatch, tmp_path) as c:
        for _ in range(2):
            assert c.post("/chat", json={"question": "access"}).status_code == 200
        blocked = c.post("/chat", json={"question": "access"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    # The detail names the ceiling, which is the only diagnostic an operator has.
    assert "per-ip" in blocked.json()["detail"]


def test_validation_errors_never_consume_the_rate_limit_budget(monkeypatch, tmp_path):
    """Pydantic rejects the body before the handler runs, so a 422 costs nothing.

    Charging rejected requests against the budget would let a client lock itself
    out with malformed JSON and look like an outage rather than a bug.
    """
    with limited(monkeypatch, tmp_path) as c:
        for _ in range(5):
            assert c.post("/chat", json={"question": ""}).status_code == 422
        assert c.post("/chat", json={"question": "access"}).status_code == 200


def test_probes_and_metrics_are_never_rate_limited(monkeypatch, tmp_path):
    """A 429 on a probe reads as a dead pod, and on /metrics as a dead target.

    Throttling the observability surface turns rate limiting into the outage it
    was meant to prevent, so /healthz, /readyz and /metrics stay outside it.
    """
    with limited(monkeypatch, tmp_path, rate_limit_per_ip_per_minute=1) as c:
        assert c.post("/chat", json={"question": "access"}).status_code == 200
        assert c.post("/chat", json={"question": "access"}).status_code == 429
        for _ in range(3):
            assert c.get("/healthz").status_code == 200
            assert c.get("/readyz").status_code == 200
            assert c.get("/metrics").status_code == 200


def test_the_per_ip_ceiling_keys_off_forwarded_for(monkeypatch, tmp_path):
    """Behind Traefik every peer is the node, so XFF is the only client identity.

    The ceiling is spoofable by design; the global ones are not. This test pins
    the mechanism, and the spoofability is a documented accepted trade, not an
    accident.
    """
    with limited(monkeypatch, tmp_path, rate_limit_per_ip_per_minute=1) as c:
        assert (
            c.post(
                "/chat", json={"question": "access"}, headers={"x-forwarded-for": "9.9.9.1"}
            ).status_code
            == 200
        )
        # A different claimed client identity gets its own budget.
        assert (
            c.post(
                "/chat", json={"question": "access"}, headers={"x-forwarded-for": "9.9.9.2"}
            ).status_code
            == 200
        )
        # The same one is now spent.
        assert (
            c.post(
                "/chat", json={"question": "access"}, headers={"x-forwarded-for": "9.9.9.1"}
            ).status_code
            == 429
        )


def test_trusting_nothing_collapses_every_client_into_one_bucket(monkeypatch, tmp_path):
    """The `trust_forwarded_for=false` escape hatch: no spoofing, no per-user limit."""
    with limited(monkeypatch, tmp_path, trust_forwarded_for=False) as c:
        for forwarded in ("9.9.9.1", "9.9.9.2", "9.9.9.3"):
            c.post("/chat", json={"question": "access"}, headers={"x-forwarded-for": forwarded})
        blocked = c.post(
            "/chat", json={"question": "access"}, headers={"x-forwarded-for": "9.9.9.1"}
        )
    assert blocked.status_code == 429
