from dataclasses import dataclass

from app.services.llm.base import LLM
from app.services.retrieval.models import Passage

SYSTEM = (
    "You answer questions about an internal documentation corpus. "
    "Use ONLY the numbered context passages provided. "
    "Answer directly from the passages: if a passage addresses the question, "
    "even in different wording, answer from it. Do not refuse when the "
    "passages cover the question. If no passage addresses it, say so plainly. "
    "Never invent policy, names, numbers, or people."
)

NO_CONTEXT = (
    "I could not find anything in the indexed corpus that answers this. "
    "The corpus covers onboarding, access control, incident response, IT "
    "and AskVault's own documentation."
)


@dataclass(frozen=True, slots=True)
class Citation:
    doc: str
    section: str
    snippet: str


@dataclass(frozen=True, slots=True)
class Answer:
    answer: str
    citations: list[Citation]
    model: str
    passages_considered: int


def compose(question: str, passages: list[Passage], llm: LLM) -> Answer:
    """Answer strictly from retrieved passages.

    Citations are derived from `passages`, never parsed out of the model's reply.
    The model is told to cite by number, but a model that ignores the numbers,
    hallucinates, or returns empty prose still yields correct citations.
    """
    if not passages:
        return Answer(NO_CONTEXT, [], llm.model, 0)

    context = "\n\n".join(
        f"[{i}] {p.doc} > {p.section}\n{p.text}" for i, p in enumerate(passages, start=1)
    )
    text = llm.complete(SYSTEM, f"Context passages:\n\n{context}\n\nQuestion: {question}").strip()
    return Answer(
        answer=text or NO_CONTEXT,
        citations=[Citation(p.doc, p.section, p.snippet) for p in passages],
        model=llm.model,
        passages_considered=len(passages),
    )
