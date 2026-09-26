"""L4: structured logs from the standard library alone.

No logging dependency, deliberately. `structlog` or `python-json-logger` would
buy fancier features this app has no use for, and every added dependency is one
more thing to audit, pin and eventually patch. The stdlib formatter below is
twenty lines and emits what a log shipper actually needs.

The `extra` pass-through is deliberately narrow: only attributes a caller
attached are merged, never LogRecord internals. A logger that spreads its own
internals into the JSON is how `filename` and `args` end up as top-level keys
in production and nobody can tell a field from a variable.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes LogRecord sets on itself. Anything in here is machinery, not data.
_STANDARD = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _STANDARD or key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger. Idempotent.

    `logging.basicConfig` is a no-op once the root logger has handlers, so an
    app that configures logging twice would silently keep the first format.
    Clearing explicitly is the only way to make this honest.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())

    _adopt_uvicorn()


def _adopt_uvicorn() -> None:
    """Fold uvicorn's own loggers into the root handler.

    uvicorn installs handlers with `propagate = False`, so out of the box the
    container emits `INFO: Started server process [1140630]` as plain text and
    every line after it as JSON. A stream that changes format halfway through
    defeats every downstream parser, and the lines it breaks are precisely the
    startup ones you need when a pod is crash-looping.

    uvicorn's *access* log is disabled rather than adopted: `app.main` already
    emits one structured line per request carrying route, status and duration,
    and a second access line per request is duplication that costs tokens and
    invites the two to drift apart.
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
