from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.core.config import settings
from app.services import answer as answer_service
from app.services.llm import get_llm
from app.services.llm.base import LLMError
from app.services.retrieval import store


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The FTS5 index is derived state, so it is rebuilt from the corpus on every
    # start rather than persisted as truth. The corpus itself is baked into the
    # image, which keeps exactly one copy of it in exactly one repo; the PVC then
    # holds only a disposable cache that a fresh pod can always reconstruct.
    app.state.passages = store.build()
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


@app.get("/health")
def health() -> dict[str, str | int]:
    return {
        "status": "ok",
        "app": settings.app_name,
        "passages": getattr(app.state, "passages", 0),
    }


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    passages = store.retrieve(request.question, k=request.k)
    try:
        result = answer_service.compose(request.question, passages, get_llm())
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ChatResponse(
        answer=result.answer,
        citations=[Citation(**asdict(c)) for c in result.citations],
        model=result.model,
        passages_considered=result.passages_considered,
    )
