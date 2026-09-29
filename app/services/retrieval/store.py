import re
import sqlite3
from pathlib import Path

from app.core.config import settings
from app.services.retrieval.models import Passage

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
    passage_id UNINDEXED,
    doc UNINDEXED,
    section UNINDEXED,
    body,
    tokenize = 'porter unicode61'
);
"""

_WORDS = re.compile(r"\w+", re.UNICODE)


def _sections(markdown: str) -> list[tuple[str, str]]:
    """Split markdown on H2. A doc with only an H1 yields one Overview passage."""
    parts = re.split(r"\n## ", "\n" + markdown.strip())
    out: list[tuple[str, str]] = []
    for part in parts:
        lines = part.strip().splitlines()
        heading = lines[0].lstrip("#").strip() if lines else ""
        body = "\n".join(lines[1:]).strip()
        if body:
            out.append((heading or "Overview", body))
    return out


def build(db_path: str | None = None, samples_dir: str | None = None) -> int:
    """(Re)build the FTS5 index from the markdown corpus. Returns passage count."""
    db = Path(db_path or settings.db_path)
    root = Path(samples_dir or settings.samples_dir)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM passages")
        count = 0
        for md in sorted(root.rglob("*.md")):
            doc = md.stem
            for i, (section, body) in enumerate(_sections(md.read_text(encoding="utf-8"))):
                conn.execute(
                    "INSERT INTO passages (passage_id, doc, section, body) VALUES (?,?,?,?)",
                    (f"{doc}#{i}", doc, section, body),
                )
                count += 1
        conn.commit()
        return count
    finally:
        conn.close()


def manifest(db_path: str | None = None) -> list[tuple[str, int]]:
    """(doc, section count) for every indexed document, ordered by name.

    Read from the index rather than by walking the corpus directory, because the
    index is the only thing the app can actually answer from. A file that failed
    to parse is simply absent here, so the web page cannot advertise a document
    that no query will ever return a passage from.
    """
    db = Path(db_path or settings.db_path)
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [
            (row[0], row[1])
            for row in conn.execute("SELECT doc, COUNT(*) FROM passages GROUP BY doc ORDER BY doc")
        ]
    finally:
        conn.close()


# Functional words carry no retrieval signal; OR-ing them dilutes every
# ranking (a question about a down service out-ranks nothing - the filler
# words match everything). They are stripped before the expression is built.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "do",
        "does",
        "did",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "so",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "too",
        "up",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
    ]
)


def _match_expr(question: str) -> str:
    """Build a safe FTS5 MATCH expression.

    The raw question must not be passed through: FTS5 reads - " * : ( and
    friends as query syntax and raises sqlite3.OperationalError. Reducing to
    content words (stopwords stripped) and OR-ing them is both safe and gives
    OR semantics, which suits recall for short questions.
    """
    words = [w for w in _WORDS.findall(question.lower()) if w not in _STOPWORDS]
    if not words:
        words = _WORDS.findall(question.lower())
    return " OR ".join(f'"{w}"' for w in words)


def retrieve(question: str, k: int = 3, db_path: str | None = None) -> list[Passage]:
    """Keyword search over the corpus, ranked, each hit carrying its citation.

    Returns at most k passages, best first. A missing index, an empty question
    or an unusable query yields [] rather than raising, so callers can report
    degraded state instead of 500-ing.
    """
    if k <= 0:
        return []
    expr = _match_expr(question)
    if not expr:
        return []
    db = Path(db_path or settings.db_path)
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT passage_id, doc, section, body,
                   snippet(passages, 3, '', '', '…', 12),
                   bm25(passages) AS rank
            FROM passages
            WHERE passages MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (expr, k),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [
        Passage(passage_id=r[0], doc=r[1], section=r[2], text=r[3], score=r[5], snippet=r[4])
        for r in rows
    ]
