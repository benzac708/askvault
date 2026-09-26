"""The web surface at `/`: the page, the form fallback, and the shared limiter.

The two properties worth the most attention here are the ones that only fail in
ways nobody notices by clicking around:

1. Everything interpolated into the page is escaped. The answer text and every
   citation field come from a language model reading a corpus that a real
   deployment lets other people edit, so an unescaped field would be a
   stored-XSS hole with a model in the middle of it.
2. The no-JavaScript form post takes from the same limiter as `/chat`. It is a
   second route to a paid provider call; leaving it unthrottled would hand
   around D32 for free.

The template is also where two structural traps live, so both are pinned here:
`string.Template` substitutes `$name` anywhere in the file including the inline
script, and the page's own enhancement block is the one legitimate `<script>` on
the page. Tests that assert "no script tag" or "no innerHTML" without scoping
themselves to the answer region will pass vacuously or fail for the wrong
reason.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

import app.main as app_main
from app.main import app
from app.services.answer import Answer, Citation
from app.web import PLACEHOLDERS, TEMPLATE_PATH, render_page

HOSTILE = '<script>alert("xss")</script>'


def _template_source() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _script_source() -> str:
    """The inline enhancement block, and nothing else.

    Scoped deliberately: the word "innerHTML" in a sentence explaining why the
    block avoids it is not a use of innerHTML, and neither is the page's own
    legitimate script tag evidence of an injection.
    """
    source = _template_source()
    assert "<script>" in source and "</script>" in source, "the template lost its script block"
    return source.split("<script>", 1)[1].split("</script>", 1)[0]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client whose requests are NOT metered, so tests cannot starve each other.

    The limiter's global ceilings are process-wide and short (15/minute, 40/day
    by default), so leaving them on would make this file's provider calls
    compete with every other test module for one shared budget. A suite whose
    pass count depends on execution order is a suite that fails in CI and only
    in CI. The two tests that care about metering build their own client with
    the limiter live.
    """
    monkeypatch.setattr(app_main.settings, "rate_limit_enabled", False)
    with TestClient(app) as test_client:
        yield test_client


def form(question: str) -> tuple[bytes, dict[str, str]]:
    """A real urlencoded form body, the way a browser sends one."""
    return (
        urlencode({"question": question}).encode(),
        {"content-type": "application/x-www-form-urlencoded"},
    )


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# --------------------------------------------------------------------------
# the template itself
# --------------------------------------------------------------------------


def test_the_template_references_exactly_the_declared_placeholders() -> None:
    """`string.Template` reads `$name` anywhere in the file, the script included.

    The inline script lives in the same file as the markup, so a template
    literal there would be consumed as a substitution and leave a blank page --
    silently, because `safe_substitute` does not raise on an unknown name.
    """
    raw = _template_source()

    stray = [m.start() for m in re.finditer(r"\$(?!([A-Za-z_][A-Za-z0-9_]*))", raw)]
    assert not stray, f"a bare $ at offset {stray[0]} would be read as a substitution"

    found = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", raw))
    assert found == set(PLACEHOLDERS), f"template {found} vs declared {set(PLACEHOLDERS)}"


def test_every_placeholder_is_actually_substituted() -> None:
    """A placeholder that stops being filled in blanks a whole page section."""
    page = render_page(query="hello")
    for name in sorted(PLACEHOLDERS):
        assert f"${name}" not in page, f"${name} survived into the rendered page"


def test_the_page_loads_no_external_assets() -> None:
    """Nothing is fetched from anywhere else.

    A font CDN would add a render-blocking third-party request to a page whose
    whole point is that it is self-contained, and would hand a visitor's IP to
    that third party for nothing.
    """
    raw = _template_source()
    assert "https://" not in raw
    assert "<link" not in raw
    assert "@import" not in raw
    assert "src=" not in raw


def test_the_inline_script_writes_text_never_markup() -> None:
    """The same untrusted output arrives in the browser via JSON.

    Escaping on the server cannot help a value the script inserts afterwards, so
    the client-side renderer has to hold the same line. `textContent` cannot
    create elements; `innerHTML` can.
    """
    script = _script_source()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in script, f"the enhancement block uses {forbidden}"
    assert "textContent" in script, "the block should be writing text with textContent"


# --------------------------------------------------------------------------
# escaping: the security boundary
# --------------------------------------------------------------------------


def test_the_question_is_escaped_inside_the_value_attribute() -> None:
    """The one place a quote could break out is the input's own value attribute."""
    page = render_page(query=HOSTILE, result=None, error="")
    assert HOSTILE not in page
    assert f'value="{HOSTILE}"' not in page
    assert 'value="&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;"' in page


def test_a_hostile_answer_and_citations_cannot_inject_markup() -> None:
    """The answer is model output; the citation fields come from the corpus.

    Both are attacker-influenceable in a real deployment and both land in the
    page, so neither may be interpolated raw. The page legitimately contains one
    script tag of its own, so the assertion is the *count*, not the absence.
    """
    result = Answer(
        answer=HOSTILE,
        citations=[Citation(doc=HOSTILE, section='"><b>x</b>', snippet=HOSTILE)],
        model=HOSTILE,
        passages_considered=1,
    )
    page = render_page(query="q", result=result)

    assert HOSTILE not in page
    assert "<b>x</b>" not in page
    assert page.count("<script>") == 1, "the payload created a second script tag"
    # Four fields, four escapes: answer, doc, snippet, and the model id that the
    # header prints next to the passage count.
    assert page.count("&lt;script&gt;") == 4


def test_an_error_message_is_escaped() -> None:
    page = render_page(query="q", result=None, error=HOSTILE)
    assert HOSTILE not in page
    assert "&lt;script&gt;" in page


def test_a_hostile_question_survives_the_form_post_escaped(client: TestClient) -> None:
    """End to end: the hostile input goes in raw over the wire, as a browser sends it."""
    body, headers = form(HOSTILE)
    response = client.post("/", content=body, headers=headers)

    assert response.status_code == 200
    assert HOSTILE not in response.text
    assert response.text.count("<script>") == 1


# --------------------------------------------------------------------------
# the page renders
# --------------------------------------------------------------------------


def test_the_index_page_renders_without_javascript(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # A real form to a real route: this is what makes the no-JS path work.
    assert 'method="post"' in response.text
    assert 'action="/"' in response.text
    assert 'name="question"' in response.text


def test_the_index_lists_the_documents_that_were_actually_indexed(client: TestClient) -> None:
    """The rail is read from the index, so it cannot advertise a dead document."""
    page = client.get("/").text
    for doc in ("onboarding", "faq-it", "access-control", "incident-response"):
        assert doc in page, f"{doc} is indexed but missing from the page"
    assert "13 sections indexed" in page


def test_a_form_post_renders_the_answer_with_its_citations(client: TestClient) -> None:
    body, headers = form("how do I request production access?")
    page = client.post("/", content=body, headers=headers).text

    assert "Answer" in page
    assert "Cited passages" in page
    assert "access-control" in page
    # The question comes back in the input so a refresh does not blank the page.
    assert 'value="how do I request production access?"' in page


def test_a_question_the_corpus_never_mentions_is_marked_ungrounded(client: TestClient) -> None:
    """Zero passages means the model saw nothing, and the page must say so.

    Without the distinct treatment a canned "I could not find anything" string
    sits in the same typographic slot as a grounded answer and reads as equally
    authoritative, which is the exact failure a RAG demo must not have.

    The trigger here is a total term miss, and that is not a coincidence --
    retrieval is an FTS5 OR-join with no relevance floor (FINDING 12), so a
    plausible-but-unrelated question like "how do I price a banana?" still
    matches a few passages on stray words and takes the *grounded* path with
    three irrelevant citations. This branch is therefore a term-miss guard, not
    a semantic-relevance guard, and closing the second gap needs vector
    retrieval, which D3 defers to P1. Asserting the banana case renders as
    ungrounded would be asserting a behaviour this build does not have.
    """
    body, headers = form("xylophone quokka telemetry")
    page = client.post("/", content=body, headers=headers).text

    # Scoped to the server-rendered markup, not the whole file: the stylesheet
    # carries an `.ungrounded` class rule and the enhancement block builds a
    # "Cited passages" heading, so bare substring searches for either word would
    # pass vacuously no matter what the server emitted.
    assert 'class="result ungrounded"' in page
    assert "0 passages" in page
    assert 'class="kicker">Cited passages' not in page


def test_an_empty_question_is_rejected_rather_than_answered(client: TestClient) -> None:
    body, headers = form("   ")
    response = client.post("/", content=body, headers=headers)

    assert response.status_code == 400
    assert "Type a question first" in response.text


def test_a_form_body_is_parsed_from_urlencoding(client: TestClient) -> None:
    """The field name and encoding are the whole contract with the no-JS form."""
    body, headers = form("who approves access?")
    assert client.post("/", content=body, headers=headers).status_code == 200

    # And a body with no `question` key at all is a 400, not a crash.
    response = client.post(
        "/",
        content=b"q=access",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400


def test_the_page_does_not_leak_internals(client: TestClient) -> None:
    page = client.get("/").text
    assert "askvault_llm_calls_total" not in page
    assert "DB_PATH" not in page
    assert str(app_main.settings.db_path) not in page


# --------------------------------------------------------------------------
# the limiter: the form post is not a free way to call the provider
# --------------------------------------------------------------------------


def test_the_form_post_is_rate_limited_like_the_json_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """A form post reaches the same paid provider call, so it draws the same bucket.

    The limiter is constructed in the lifespan hook from settings, so this test
    needs its own client built *after* the ceiling is lowered. An unthrottled
    form post would be the cheapest possible way around D32.
    """
    monkeypatch.setattr(app_main.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(app_main.settings, "rate_limit_per_ip_per_minute", 2)

    with TestClient(app) as client:
        body, headers = form("access")
        headers["x-forwarded-for"] = "198.51.100.42"

        assert client.post("/", content=body, headers=headers).status_code == 200
        assert client.post("/", content=body, headers=headers).status_code == 200

        refused = client.post("/", content=body, headers=headers)

    assert refused.status_code == 429
    assert "Retry-After" in refused.headers
    # HTML, not JSON: the JSON path returns a body this page cannot render.
    assert refused.headers["content-type"].startswith("text/html")
    assert "per-ip ceiling" in refused.text


def test_a_refused_form_post_is_counted_under_the_scope_that_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_main.settings, "rate_limit_enabled", True)
    monkeypatch.setattr(app_main.settings, "rate_limit_per_ip_per_minute", 1)

    with TestClient(app) as client:
        body, headers = form("access")
        headers["x-forwarded-for"] = "198.51.100.7"
        assert client.post("/", content=body, headers=headers).status_code == 200

        before = sample("askvault_rate_limited_total", scope="per-ip")
        assert client.post("/", content=body, headers=headers).status_code == 429
        after = sample("askvault_rate_limited_total", scope="per-ip")

    assert after == before + 1


def test_the_two_entry_points_share_one_measurement_path(client: TestClient) -> None:
    """A question asked in the browser must be visible in the metrics.

    If the form post bypassed the shared path it would be an unmonitored route
    into a paid call, and the Grafana panels in D35 would under-report real
    usage by exactly the amount a browser user generates.

    The counter chosen is the provider-call one, not the request one: the request
    counter is labelled by route, so by design the two entry points land on
    different series and comparing them proves nothing. `llm_calls_total` has no
    route label precisely because the spend is the thing being measured, and that
    is the series a browser user must move.
    """
    question = "what do I do if a service is down?"
    before = sample("askvault_llm_calls_total", provider="mock", outcome="ok")

    body, headers = form(question)
    assert client.post("/", content=body, headers=headers).status_code == 200
    after_form = sample("askvault_llm_calls_total", provider="mock", outcome="ok")

    assert client.post("/chat", json={"question": question}).status_code == 200
    after_api = sample("askvault_llm_calls_total", provider="mock", outcome="ok")

    assert after_form == before + 1
    assert after_api == after_form + 1

    # And each entry point is still recorded under its own route label, so the
    # traffic panel can still tell a browser from an API client.
    assert sample("askvault_requests_total", route="/", method="POST", status="200") >= 1
    assert sample("askvault_requests_total", route="/chat", method="POST", status="200") >= 1
