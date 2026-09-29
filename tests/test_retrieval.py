from pathlib import Path

import pytest

from app.services.retrieval import store
from app.services.retrieval.models import Passage

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "samples"

# The indexed section count is derived from the corpus, not hard-coded:
# a new document adds sections and this stays true.
EXPECTED_PASSAGES = sum(
    1
    for md in SAMPLES.rglob("*.md")
    for line in md.read_text(encoding="utf-8").splitlines()
    if line.startswith("## ")
)


@pytest.fixture
def indexed(tmp_path):
    db = tmp_path / "askvault.db"
    count = store.build(db_path=str(db), samples_dir=str(SAMPLES))
    return db, count


def test_index_builds_every_section(indexed):
    _, count = indexed
    assert count == EXPECTED_PASSAGES


def test_service_down_question_lands_on_the_incident_runbook(indexed):
    """The user's natural phrasing must reach the incident doc, not the FAQ noise."""
    db, _ = indexed
    hits = store.retrieve("what do I do if a service is down?", k=5, db_path=str(db))
    assert hits, "expected at least one hit"
    assert hits[0].doc == "incident-response"


def test_rebuild_is_idempotent(indexed):
    """A rebuild must replace, not append. Missing the DELETE doubles the index."""
    db, first = indexed
    second = store.build(db_path=str(db), samples_dir=str(SAMPLES))
    assert second == first


def test_retrieve_finds_the_right_document(indexed):
    db, _ = indexed
    hits = store.retrieve("how do I request production access", k=3, db_path=str(db))
    assert hits
    assert all(isinstance(h, Passage) for h in hits)
    assert any(h.doc == "access-control" for h in hits)


def test_every_hit_carries_a_citation(indexed):
    """doc + section are the citation. An empty one would produce uncited answers."""
    db, _ = indexed
    for hit in store.retrieve("incident severity levels and escalation", k=5, db_path=str(db)):
        assert hit.doc
        assert hit.section
        assert hit.passage_id


def test_stemming_matches_across_word_forms(indexed):
    """'policies' must reach the policy doc: this is what porter tokenizer buys."""
    db, _ = indexed
    hits = store.retrieve("secret rotation policies", k=5, db_path=str(db))
    assert any(h.doc == "access-control" for h in hits)


def test_respects_k(indexed):
    db, _ = indexed
    assert len(store.retrieve("access", k=2, db_path=str(db))) <= 2
    assert store.retrieve("access", k=0, db_path=str(db)) == []


def test_is_deterministic(indexed):
    """CI depends on this: same question, same order, every run."""
    db, _ = indexed
    a = store.retrieve("onboarding first week checklist", k=3, db_path=str(db))
    b = store.retrieve("onboarding first week checklist", k=3, db_path=str(db))
    assert [h.passage_id for h in a] == [h.passage_id for h in b]


@pytest.mark.parametrize(
    "question",
    ["", "   ", "?!*", '- " : (', "^foo", "a" * 500, "AND OR NOT", "\\", "%"],
)
def test_hostile_questions_do_not_raise(indexed, question):
    """FTS5 reads - " * : ( ^ \\ as query syntax. These must not 500 the app."""
    db, _ = indexed
    assert isinstance(store.retrieve(question, k=3, db_path=str(db)), list)


def test_missing_index_returns_empty(tmp_path):
    assert store.retrieve("anything", k=3, db_path=str(tmp_path / "absent.db")) == []


def test_empty_corpus_yields_zero(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    db = tmp_path / "empty.db"
    assert store.build(db_path=str(db), samples_dir=str(empty)) == 0
    assert store.retrieve("anything", k=3, db_path=str(db)) == []


def test_unindexed_file_is_ignored(tmp_path):
    """A stray non-markdown file must not become a passage."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "notes.txt").write_text("this should never be indexed", encoding="utf-8")
    db = tmp_path / "corpus.db"
    assert store.build(db_path=str(db), samples_dir=str(corpus)) == 0


def test_manifest_counts_sections_per_document(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    # A leading H1 that has body text under it becomes its own "Overview"
    # passage, so alpha yields Overview + One + Two and beta Overview + Only.
    (corpus / "alpha.md").write_text("# Alpha\n\nintro\n\n## One\n\na\n\n## Two\n\nb\n", "utf-8")
    (corpus / "beta.md").write_text("# Beta\n\nintro\n\n## Only\n\nb\n", "utf-8")
    db = tmp_path / "corpus.db"
    assert store.build(db_path=str(db), samples_dir=str(corpus)) == 5

    assert store.manifest(db_path=str(db)) == [("alpha", 3), ("beta", 2)]


def test_manifest_is_empty_without_an_index(tmp_path):
    """The web page falls back to its own placeholder rather than failing."""
    assert store.manifest(db_path=str(tmp_path / "absent.db")) == []


def test_manifest_reports_only_what_was_indexed(tmp_path):
    """It reads the index, not the directory.

    A document that failed to parse is absent from the index, so listing the
    directory instead would advertise a document no query can return a passage
    from -- the page would promise retrieval it cannot deliver.
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "good.md").write_text("# Good\n\nintro\n\n## S\n\nbody\n", "utf-8")
    db = tmp_path / "corpus.db"
    store.build(db_path=str(db), samples_dir=str(corpus))

    # A file appears in the corpus directory after the index was built.
    (corpus / "added-later.md").write_text("# Later\n\n## S\n\nbody\n", "utf-8")

    assert [doc for doc, _ in store.manifest(db_path=str(db))] == ["good"]
