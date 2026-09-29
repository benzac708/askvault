"""D26's health split and D35's metric surface.

The interesting assertions here are the negative ones: that liveness stays green
when readiness cannot be, and that unmatched paths collapse into one series.
Both protect the cluster from a failure mode that looks like a network problem.
"""

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.logging import JsonFormatter, configure_logging
from app.main import app

CORPUS_SECTIONS = sum(
    1
    for md in Path(__file__).resolve().parents[1].joinpath("samples").rglob("*.md")
    for line in md.read_text(encoding="utf-8").splitlines()
    if line.startswith("## ")
)


def build_client(monkeypatch, tmp_path, **overrides):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "obs.db"))
    monkeypatch.setattr(settings, "samples_dir", "samples")
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)
    return TestClient(app)


def test_healthz_is_liveness_only_and_never_mentions_the_index(monkeypatch, tmp_path):
    with build_client(monkeypatch, tmp_path) as c:
        body = c.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["app"] == settings.app_name
    assert "passages" not in body


def test_readyz_reports_the_indexed_passage_count(monkeypatch, tmp_path):
    with build_client(monkeypatch, tmp_path) as c:
        response = c.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "app": settings.app_name,
        "passages": CORPUS_SECTIONS,
    }


def test_readyz_is_503_while_the_index_is_empty_and_healthz_stays_green(monkeypatch, tmp_path):
    """The split earns its keep here.

    A pod with no index must be pulled out of the Service endpoints, which is
    what 503 does. It must NOT be restarted, because restarting cannot conjure a
    corpus that is not in the image. If these two endpoints reported the same
    condition, one of those two corrections would be impossible.
    """
    with build_client(monkeypatch, tmp_path, samples_dir=str(tmp_path / "no-corpus-here")) as c:
        ready = c.get("/readyz")
        live = c.get("/healthz")
    assert ready.status_code == 503
    assert ready.json()["status"] == "indexing"
    assert ready.json()["passages"] == 0
    assert live.status_code == 200


def test_metrics_serves_the_prometheus_content_type(monkeypatch, tmp_path):
    with build_client(monkeypatch, tmp_path) as c:
        response = c.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def counter_value(body: str, series: str) -> float:
    """Read one counter sample out of a scrape.

    A counter child that has never been touched does not appear at all, which
    means zero -- so absence is not an error. Absolute values are deliberately
    never asserted against: the default registry is process-global, so every
    counter accumulates across the whole test session and any hard-coded total
    would depend on test execution order. Deltas are the only stable assertion.
    """
    for line in body.splitlines():
        if line.startswith(f"{series} "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_every_declared_metric_is_registered_under_its_exact_name(monkeypatch, tmp_path):
    """The dashboard in D35 is written against these exact names.

    Asserting the `# HELP` line rather than a bare substring: a `# HELP` proves
    the metric is actually registered, whereas a substring would also match the
    name inside an unrelated metric's documentation.
    """
    with build_client(monkeypatch, tmp_path) as c:
        c.post("/chat", json={"question": "how do I request production access"})
        body = c.get("/metrics").text
    for metric in (
        "askvault_requests_total",
        "askvault_request_duration_seconds",
        "askvault_llm_calls_total",
        "askvault_passages_considered",
        "askvault_rate_limited_total",
    ):
        assert f"# HELP {metric} " in body, f"{metric} is not registered"


def test_chat_increments_the_retrieval_histogram(monkeypatch, tmp_path):
    with build_client(monkeypatch, tmp_path) as c:
        c.post("/chat", json={"question": "how do I request production access"})
        body = c.get("/metrics").text
    assert "askvault_passages_considered_count" in body


def test_unmatched_paths_collapse_into_one_metric_series(monkeypatch, tmp_path):
    """A public endpoint labelling raw paths is a cardinality attack.

    Without this, a scanner hitting /a /b /c ... grows the series count without
    limit, and Prometheus is the thing that falls over. The raw path survives as
    a log field, where it is free.
    """
    with build_client(monkeypatch, tmp_path) as c:
        for path in ("/nope-1", "/nope-2", "/../etc/passwd", "/nope-3"):
            c.get(path)
        body = c.get("/metrics").text
    assert 'askvault_requests_total{method="GET",route="unmatched",status="404"}' in body
    assert "nope-1" not in body
    assert "nope-2" not in body


def test_matched_routes_are_labelled_by_template(monkeypatch, tmp_path):
    with build_client(monkeypatch, tmp_path) as c:
        c.get("/healthz")
        body = c.get("/metrics").text
    assert 'askvault_requests_total{method="GET",route="/healthz",status="200"}' in body


def test_the_rate_limited_counter_records_which_ceiling_fired(monkeypatch, tmp_path):
    """The label is the diagnostic, not decoration.

    A spike in `per-ip` is one visitor misbehaving. A spike in `daily` means the
    day's budget is gone and no code change will bring it back. Same 429, two
    completely different responses, so the label has to be there.
    """
    series = 'askvault_rate_limited_total{scope="per-ip"}'
    with build_client(
        monkeypatch,
        tmp_path,
        rate_limit_enabled=True,
        rate_limit_per_ip_per_minute=1,
        rate_limit_global_per_minute=100,
        rate_limit_global_per_day=100,
    ) as c:
        before = counter_value(c.get("/metrics").text, series)
        assert c.post("/chat", json={"question": "access"}).status_code == 200
        assert c.post("/chat", json={"question": "access"}).status_code == 429
        after = counter_value(c.get("/metrics").text, series)
    assert after - before == 1.0


def test_the_llm_call_counter_separates_ok_from_error(monkeypatch, tmp_path):
    """502 and 200 must be distinguishable, or the panel shows a single number."""
    ok = 'askvault_llm_calls_total{outcome="ok",provider="mock"}'
    err = 'askvault_llm_calls_total{outcome="error",provider="nope"}'
    with build_client(monkeypatch, tmp_path) as c:
        c.post("/chat", json={"question": "access"})
        ok_before = counter_value(c.get("/metrics").text, ok)
        err_before = counter_value(c.get("/metrics").text, err)

        # The counter is labelled with the *configured* provider, so a typo in
        # config becomes its own series rather than polluting a real one. That is
        # the intended behaviour: a pod wired to a provider that does not exist
        # is a thing you want to see on a dashboard, not something to average away.
        monkeypatch.setattr(settings, "llm_provider", "nope")
        assert c.post("/chat", json={"question": "access"}).status_code == 502
        body = c.get("/metrics").text

    assert counter_value(body, ok) - ok_before == 0.0
    assert counter_value(body, err) - err_before == 1.0


# --- structured logging (L4) ---


def test_the_json_formatter_emits_one_flat_object_per_record(capsys):
    configure_logging("INFO")
    logging.getLogger("test").info("served", extra={"route": "/chat", "status": 200})
    record = json.loads(capsys.readouterr().out.strip())
    assert record["msg"] == "served"
    assert record["level"] == "INFO"
    assert record["logger"] == "test"
    assert record["route"] == "/chat"
    assert record["status"] == 200
    assert record["ts"].endswith("+00:00")


def test_the_json_formatter_never_leaks_logrecord_internals(capsys):
    """`filename` and `args` are machinery. If they surface as top-level keys,
    the shape of every log line stops being predictable to the consumer."""
    configure_logging("INFO")
    logging.getLogger("test").info("hello %s", "world")
    record = json.loads(capsys.readouterr().out.strip())
    assert record["msg"] == "hello world"
    for leaked in ("filename", "args", "levelno", "lineno", "msecs", "created"):
        assert leaked not in record


def test_the_json_formatter_includes_the_traceback(capsys):
    configure_logging("INFO")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("test").exception("failed")
    record = json.loads(capsys.readouterr().out.strip())
    assert "ValueError: boom" in record["exc"]


def test_the_entrypoint_configures_logging_at_import_not_just_in_lifespan():
    """uvicorn logs two lines before any startup hook runs.

    Configuring inside the lifespan is structurally too late for them, so the
    container's log stream would open as plain text and turn into JSON a moment
    later. A subprocess is used deliberately: the property under test is a
    side effect of *importing* the entrypoint, which cannot be observed from
    inside a process that already imported it.
    """
    probe = (
        "import logging, app.main;"
        "handlers = logging.getLogger().handlers;"
        "print(type(handlers[0].formatter).__name__ if handlers else 'NONE')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "JsonFormatter"


def test_uvicorn_loggers_are_folded_into_the_json_root_handler():
    """A container log stream that changes format halfway is unparseable.

    uvicorn installs its own handlers with propagate=False, so without this the
    first lines of a pod's output are plain text and the rest are JSON. The
    startup lines are exactly the ones needed when a pod crash-loops.
    """
    configure_logging("INFO")
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).propagate is True, name
        assert not logging.getLogger(name).handlers, name


def test_uvicorn_access_log_is_disabled_because_the_middleware_supersedes_it():
    """One request must not produce two log lines that can disagree.

    `app.main.observe` already emits route, status and duration per request.
    Leaving uvicorn's access log on duplicates every line and invites the two
    copies to drift.
    """
    configure_logging("INFO")
    assert logging.getLogger("uvicorn.access").disabled is True


def test_configure_logging_is_idempotent():
    """`logging.basicConfig` silently no-ops when handlers already exist, so a
    second call would keep the first format. Clearing explicitly is the fix."""
    configure_logging("INFO")
    configure_logging("INFO")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)


@pytest.fixture(autouse=True)
def _restore_logging():
    """Undo the logging surgery so these tests cannot leak into others.

    Restores the uvicorn loggers as well as the root: `configure_logging`
    mutates their handlers, `propagate` and `disabled` flags, and a suite that
    permanently folds uvicorn into a JSON handler it never asked for is a
    confusing failure to debug in whatever runs next.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    uvicorn_names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved_uvicorn = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
        )
        for name in uvicorn_names
    }
    yield
    root.handlers.clear()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    for name, (handlers, propagate, disabled) in saved_uvicorn.items():
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        for handler in handlers:
            uvicorn_logger.addHandler(handler)
        uvicorn_logger.propagate = propagate
        uvicorn_logger.disabled = disabled
