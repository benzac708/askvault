import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from starlette.middleware.base import RequestResponseEndpoint

from app.core.config import settings
from app.core.limits import RateLimiter, RateLimitExceeded
from app.core.logging import configure_logging
from app.observability import metrics
from app.services import answer as answer_service
from app.services.answer import Answer
from app.services.llm import get_llm
from app.services.llm.base import LLMError
from app.services.retrieval import store
from app.web import render_page

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
    app.state.manifest = store.manifest()
    app.state.limiter = RateLimiter(
        per_ip_per_minute=settings.rate_limit_per_ip_per_minute,
        global_per_minute=settings.rate_limit_global_per_minute,
        global_per_day=settings.rate_limit_global_per_day,
    )
    logger.info(
        "index built",
        extra={
            "passages": app.state.passages,
            "documents": len(app.state.manifest),
            "provider": settings.llm_provider,
        },
    )
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

# Retrieval depth. Declared once and used by both entry points so the JSON API
# and the no-JavaScript form cannot quietly disagree about how much context a
# question gets.
DEFAULT_K = 5


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=DEFAULT_K, ge=1, le=10)


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
async def observe(request: Request, call_next: RequestResponseEndpoint) -> Response:
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


def _check_rate_limit(request: Request) -> RateLimitExceeded | None:
    """Consume one unit of the caller's budget, or report which ceiling said no.

    Returns the exception rather than raising it, because the two entry points
    report it in different shapes: `/chat` answers JSON, the form post answers
    HTML. Recording the metric here means both paths are counted once and cannot
    drift apart.
    """
    if not settings.rate_limit_enabled:
        return None
    limiter: RateLimiter = request.app.state.limiter
    try:
        limiter.check(_client_id(request))
    except RateLimitExceeded as exc:
        metrics.RATE_LIMITED.labels(scope=exc.scope).inc()
        return exc
    return None


def _enforce_rate_limit(request: Request) -> None:
    """Only the API is limited, because only the API costs money.

    Probes and metrics are never throttled: a 429 on `/readyz` reads to
    Kubernetes as a failed pod and a 429 on `/metrics` reads to Prometheus as a
    dead target, turning rate limiting into the outage.
    """
    exc = _check_rate_limit(request)
    if exc is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many requests ({exc.scope} ceiling). Retry in {exc.retry_after}s.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc


def _answer_question(request: Request, question: str, k: int = DEFAULT_K) -> Answer:
    """Retrieve, compose and record metrics. The one and only RAG path.

    `/chat` and the form post both call this, which is the entire point: one
    retrieval, one prompt, one set of metrics. Two copies of this sequence would
    be free to drift, and a drift that only affected the browser path would be
    invisible to anyone testing with curl.
    """
    passages = store.retrieve(question, k=k)
    metrics.PASSAGES.observe(len(passages))

    provider = settings.llm_provider
    try:
        result = answer_service.compose(question, passages, get_llm())
    except LLMError as exc:
        metrics.LLM_CALLS.labels(provider=provider, outcome="error").inc()
        logger.error("llm call failed", extra={"provider": provider, "error": str(exc)})
        raise

    metrics.LLM_CALLS.labels(provider=provider, outcome="ok").inc()
    return result


@app.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    _enforce_rate_limit(request)
    try:
        result = _answer_question(request, payload.question, payload.k)
    except LLMError as exc:
        # The shared path raises so each entry point can render the failure in
        # its own shape. A provider error is a bad gateway, and the detail is
        # the provider's message -- never a stack trace.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ChatResponse(
        answer=result.answer,
        citations=[Citation(**asdict(c)) for c in result.citations],
        model=result.model,
        passages_considered=result.passages_considered,
    )


def _page(
    request: Request,
    *,
    query: str = "",
    result: Answer | None = None,
    error: str = "",
) -> str:
    return render_page(
        query=query,
        result=result,
        error=error,
        index=getattr(request.app.state, "manifest", ()),
        total_passages=getattr(request.app.state, "passages", 0),
    )


def _question_from_form(body: bytes) -> str:
    """Read `question` out of an application/x-www-form-urlencoded body.

    Parsed by hand rather than through `request.form()`: this route exists to
    work with scripting disabled, so it must not depend on which optional
    parsers a given Starlette version decides to require for form bodies. A
    urlencoded body is a percent-decoded key/value list, and parsing it needs
    nothing installed. The value is clipped to the length the JSON endpoint
    accepts, so an oversized paste is truncated instead of becoming a second,
    differently sized question the limiter has never seen.
    """
    fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return (fields.get("question") or [""])[0][:1000]


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index_page(request: Request) -> HTMLResponse:
    """The whole application, as one file. No build step, no Node (D27).

    Neither cached nor throttled on purpose. Rendering it costs no provider call
    and reads no index, so a ceiling here would only make the site look broken to
    a person who has not typed anything yet.
    """
    return HTMLResponse(_page(request))


@app.post("/", response_class=HTMLResponse, include_in_schema=False)
async def ask(request: Request) -> HTMLResponse:
    """The same form with no JavaScript, rendered by the server.

    This route exists so the page works with scripting off, which makes it a
    second path to a paid provider call. It therefore draws from the same limiter
    as `/chat` rather than being exempt: an unthrottled form post would be the
    cheapest possible way around D32.
    """
    question = _question_from_form(await request.body())
    if not question.strip():
        return HTMLResponse(_page(request, error="Type a question first."), status_code=400)

    exceeded = _check_rate_limit(request)
    if exceeded is not None:
        return HTMLResponse(
            _page(
                request,
                query=question,
                error=(
                    f"Too many requests ({exceeded.scope} ceiling). "
                    f"Retry in {exceeded.retry_after}s."
                ),
            ),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(exceeded.retry_after)},
        )

    try:
        result = _answer_question(request, question)
    except LLMError as exc:
        # `/chat` answers 502 with a JSON body. The same failure has to render
        # here or a provider outage shows up as an unexplained blank page.
        return HTMLResponse(
            _page(request, query=question, error=f"The answer service failed: {exc}"),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    return HTMLResponse(_page(request, query=question, result=result))
