import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.limits import RateLimiter, RateLimitExceeded
from app.core.logging import configure_logging
from app.observability import metrics
from app.services import answer as answer_service
from app.services.llm import get_llm
from app.services.llm.base import LLMError
from app.services.retrieval import store

# Configured at import, not in the lifespan hook. uvicorn emits "Started server
# process" and "Waiting for application startup" before any startup hook runs, so
# configuring there is too late: the container's log stream would open as plain
# text and turn into JSON a moment later, and those first lines are exactly the
# ones you read when a pod is crash-looping. Importing the ASGI entrypoint
# happens before uvicorn logs anything, which makes this the earliest point that
# is still ours. `app.main` is the entrypoint, not a library, so a side effect at
# import is the intended contract rather than a surprise.
configure_logging(settings.log_level)

logger = logging.getLogger("askvault")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The FTS5 index is derived state, so it is rebuilt from the corpus on every
    # start rather than persisted as truth. The corpus is baked into the image,
    # which keeps exactly one copy of it in exactly one repo; the emptyDir mount
    # then holds only a disposable cache a fresh pod can always reconstruct.
    # Nothing survives a restart on purpose -- see D29.
    app.state.passages = store.build()
    app.state.limiter = RateLimiter(
        per_ip_per_minute=settings.rate_limit_per_ip_per_minute,
        global_per_minute=settings.rate_limit_global_per_minute,
        global_per_day=settings.rate_limit_global_per_day,
    )
    logger.info(
        "index built", extra={"passages": app.state.passages, "provider": settings.llm_provider}
    )
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=3, ge=1, le=10)


class Citation(BaseModel):
    doc: str
    section: str
    snippet: str


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation]
    model: str
    passages_considered: int


def _route_template(request: Request) -> str:
    """The matched path template, or a single collapsed label if nothing matched.

    Unmatched paths are scanners, typo'd probes and crawlers. Labelling them by
    raw path is a cheap way for anyone on the internet to inflate the series
    count until Prometheus suffers, so they all share one bucket and the raw
    path is kept as a log field instead where it costs nothing.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "unmatched"


def _client_id(request: Request) -> str:
    """Identify the caller for the per-IP ceiling.

    Behind Traefik every peer address is the same node, so the leftmost
    X-Forwarded-For entry is the only usable client identity. It is spoofable by
    design -- see the `trust_forwarded_for` note in config. The global ceilings
    are what protect the quota; this one only smooths out a single visitor.
    """
    peer = request.client.host if request.client else "unknown"
    if not settings.trust_forwarded_for:
        return peer
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return peer


@app.middleware("http")
async def observe(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Count and time every request, then log it as one structured line.

    The route template is read *after* the call, because Starlette populates
    `scope["route"]` during routing, which happens inside `call_next`. Reading
    it beforehand would label every request `unmatched`.
    """
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = _route_template(request)
        method = request.method
        elapsed = time.perf_counter() - started
        metrics.REQUESTS.labels(route=route, method=method, status=str(status_code)).inc()
        metrics.LATENCY.labels(route=route, method=method).observe(elapsed)
        logger.info(
            "request",
            extra={
                "route": route,
                "method": method,
                "status": status_code,
                "duration_ms": round(elapsed * 1000, 2),
                "path": request.url.path if route == "unmatched" else None,
            },
        )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness. Deliberately independent of the index.

    If liveness depended on a successful index build, an empty or slow build
    would have Kubernetes restart the pod on a timer forever, which cannot fix
    a bad corpus -- it just burns restarts. Restart is the correct response to a
    crashed process, not to a slow one. Readiness is the endpoint that carries
    the index state.
    """
    return {"status": "ok", "app": settings.app_name}


@app.get("/readyz")
def readyz(response: Response) -> dict[str, str | int]:
    """Readiness. 503 until the index exists and is non-empty.

    503 is what keeps a pod out of the Service endpoints during startup, so
    Traefik never routes to a process that would answer `no context` to
    everything it is asked.
    """
    count = getattr(app.state, "passages", 0)
    if count <= 0:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "indexing", "app": settings.app_name, "passages": 0}
    return {"status": "ready", "app": settings.app_name, "passages": count}


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _enforce_rate_limit(request: Request) -> None:
    """Only `/chat` is limited, because only `/chat` costs money.

    Probes and metrics are never throttled: a 429 on `/readyz` reads to
    Kubernetes as a failed pod and a 429 on `/metrics` reads to Prometheus as a
    dead target, turning rate limiting into an outage.
    """
    if not settings.rate_limit_enabled:
        return
    limiter: RateLimiter = request.app.state.limiter
    try:
        limiter.check(_client_id(request))
    except RateLimitExceeded as exc:
        metrics.RATE_LIMITED.labels(scope=exc.scope).inc()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many requests ({exc.scope} ceiling). Retry in {exc.retry_after}s.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc


@app.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    _enforce_rate_limit(request)
    passages = store.retrieve(payload.question, k=payload.k)
    metrics.PASSAGES.observe(len(passages))

    provider = settings.llm_provider
    try:
        result = answer_service.compose(payload.question, passages, get_llm())
    except LLMError as exc:
        metrics.LLM_CALLS.labels(provider=provider, outcome="error").inc()
        logger.error("llm call failed", extra={"provider": provider, "error": str(exc)})
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    metrics.LLM_CALLS.labels(provider=provider, outcome="ok").inc()
    return ChatResponse(
        answer=result.answer,
        citations=[Citation(**asdict(c)) for c in result.citations],
        model=result.model,
        passages_considered=result.passages_considered,
    )
