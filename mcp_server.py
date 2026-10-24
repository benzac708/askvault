"""AskVault MCP server — expose the RAG corpus as agent tools over stdio.

Usage:
    python mcp_server.py
    # or: uv run mcp_server.py

Tools:
    ask_vault(question, k=5)  — answer a question from the corpus with citations
    vault_health()            — index stats (passage count, document list)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from app.core.config import settings
from app.services import answer as answer_service
from app.services.llm import get_llm
from app.services.retrieval import store

# Build the index at import time (same as FastAPI lifespan).
_db_path = settings.db_path
_samples_dir = settings.samples_dir
_passage_count = store.build(db_path=_db_path, samples_dir=_samples_dir)
_manifest = store.manifest(db_path=_db_path)
_llm = get_llm()

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("askvault")


@mcp.tool()
def ask_vault(question: str, k: int = 5) -> str:
    """Ask the AskVault knowledge base a question.

    Returns an answer with cited source documents. The corpus covers
    onboarding, access control, incident response, IT and AskVault's own
    documentation.
    """
    passages = store.retrieve(question, k=k, db_path=_db_path)
    result = answer_service.compose(question, passages, _llm)
    citations = [{"doc": c.doc, "section": c.section} for c in result.citations]
    return json.dumps({
        "answer": result.answer,
        "citations": citations,
        "passages_considered": result.passages_considered,
        "model": result.model,
    }, indent=2)


@mcp.tool()
def vault_health() -> str:
    """Check AskVault index health: passage count and document list."""
    return json.dumps({
        "passages": _passage_count,
        "documents": [{"doc": d, "sections": n} for d, n in _manifest],
        "llm_provider": settings.llm_provider,
    }, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
