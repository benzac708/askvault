"""AskVault eval harness.

Scores retrieval accuracy, refusal correctness, and citation correctness
against eval_questions.jsonl. Runs against the in-process retrieval store
(no network, no LLM calls) so it is deterministic and fast.

Usage:
    python eval.py                    # run all questions, print report
    python eval.py --json             # machine-readable output
    python eval.py --min-score 0.9    # exit 1 if any metric below threshold
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from app.core.config import settings
from app.services.retrieval import store

K = 5  # retrieval depth for scoring


def load_questions(path: Path) -> list[dict]:
    questions = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                questions.append(json.loads(line))
    return questions


def run_eval(questions: list[dict]) -> dict:
    db_path = str(REPO_ROOT / "data" / "eval.db")
    samples_dir = str(REPO_ROOT / "samples")
    store.build(db_path=db_path, samples_dir=samples_dir)

    results = []
    retrieval_hits = 0
    retrieval_total = 0
    refusal_correct = 0
    refusal_total = 0
    citation_correct = 0
    citation_total = 0

    for q in questions:
        question = q["question"]
        expected_doc = q.get("expected_doc")
        should_refuse = q.get("should_refuse", False)
        expected_citation = q.get("expected_citation")

        hits = store.retrieve(question, k=K, db_path=db_path)
        retrieved_docs = [h.doc for h in hits]

        # Retrieval accuracy: expected doc in top-k (or empty if should_refuse)
        if should_refuse:
            retrieval_ok = len(hits) == 0
            refusal_total += 1
            if retrieval_ok:
                refusal_correct += 1
        else:
            retrieval_ok = expected_doc in retrieved_docs
            retrieval_total += 1
            if retrieval_ok:
                retrieval_hits += 1

        # Citation correctness: if citations expected, check they match retrieved
        if expected_citation is not None and not should_refuse:
            citation_total += 1
            # Citations are derived from passages; check the expected doc is in retrieved
            if expected_citation in retrieved_docs:
                citation_correct += 1
            citation_ok = expected_citation in retrieved_docs
        else:
            citation_ok = None

        results.append({
            "question": question,
            "expected_doc": expected_doc,
            "should_refuse": should_refuse,
            "retrieved_docs": retrieved_docs,
            "retrieval_ok": retrieval_ok,
            "citation_ok": citation_ok,
        })

    n = len(questions)
    retrieval_accuracy = retrieval_hits / retrieval_total if retrieval_total else 0.0
    refusal_accuracy = refusal_correct / refusal_total if refusal_total else 0.0
    citation_accuracy = citation_correct / citation_total if citation_total else 0.0

    return {
        "total": n,
        "retrieval": {"hits": retrieval_hits, "total": retrieval_total, "accuracy": retrieval_accuracy},
        "refusal": {"correct": refusal_correct, "total": refusal_total, "accuracy": refusal_accuracy},
        "citation": {"correct": citation_correct, "total": citation_total, "accuracy": citation_accuracy},
        "results": results,
    }


def print_report(report: dict) -> None:
    print(f"AskVault Eval — {report['total']} questions")
    print("=" * 60)
    r = report["retrieval"]
    print(f"Retrieval accuracy:  {r['hits']}/{r['total']} = {r['accuracy']:.1%}")
    rf = report["refusal"]
    print(f"Refusal accuracy:    {rf['correct']}/{rf['total']} = {rf['accuracy']:.1%}")
    c = report["citation"]
    print(f"Citation accuracy:   {c['correct']}/{c['total']} = {c['accuracy']:.1%}")
    print("=" * 60)

    failures = [r for r in report["results"] if not r["retrieval_ok"] or (r["citation_ok"] is False)]
    if failures:
        print(f"\n{len(failures)} failures:")
        for f in failures:
            print(f"  Q: {f['question']}")
            print(f"     expected: {f['expected_doc'] or '(refuse)'}")
            print(f"     got:      {f['retrieved_docs']}")
    else:
        print("\nAll questions passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="AskVault eval harness")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--min-score", type=float, default=None, help="exit 1 if any metric below threshold")
    args = parser.parse_args()

    questions = load_questions(REPO_ROOT / "eval_questions.jsonl")
    report = run_eval(questions)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)

    if args.min_score is not None:
        scores = [report["retrieval"]["accuracy"], report["refusal"]["accuracy"], report["citation"]["accuracy"]]
        if any(s < args.min_score for s in scores):
            sys.exit(1)


if __name__ == "__main__":
    main()
