"""AskVault CLI — ask questions from the terminal.

Usage:
    python cli.py ask "how do I request production access"
    python cli.py ask "question" --json
    python cli.py health
    python cli.py eval [--min-score 0.95]

By default it runs against the local corpus (no server needed).
Use --base-url to query a deployed instance instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from app.core.config import settings
from app.services import answer as answer_service
from app.services.llm import get_llm
from app.services.retrieval import store


def _build_index() -> None:
    db_path = settings.db_path
    samples_dir = settings.samples_dir
    store.build(db_path=db_path, samples_dir=samples_dir)


def cmd_ask(args: argparse.Namespace) -> int:
    question = args.question
    if args.base_url:
        import httpx

        url = f"{args.base_url.rstrip('/')}/chat"
        try:
            resp = httpx.post(url, json={"question": question, "k": args.k}, timeout=30)
        except httpx.HTTPError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if not resp.is_success:
            print(f"Error: HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
            return 1
        body = resp.json()
    else:
        _build_index()
        llm = get_llm()
        passages = store.retrieve(question, k=args.k, db_path=settings.db_path)
        result = answer_service.compose(question, passages, llm)
        body = {
            "answer": result.answer,
            "citations": [{"doc": c.doc, "section": c.section} for c in result.citations],
            "passages_considered": result.passages_considered,
            "model": result.model,
        }

    if args.json:
        print(json.dumps(body, indent=2))
    else:
        print(body["answer"])
        if body.get("citations"):
            print()
            print("Sources:")
            for c in body["citations"]:
                print(f"  - {c['doc']} > {c['section']}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    if args.base_url:
        import httpx

        url = f"{args.base_url.rstrip('/')}/health"
        try:
            resp = httpx.get(url, timeout=10)
        except httpx.HTTPError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if not resp.is_success:
            print(f"Error: HTTP {resp.status_code}", file=sys.stderr)
            return 1
        body = resp.json()
    else:
        _build_index()
        body = {
            "passages": sum(n for _, n in store.manifest(db_path=settings.db_path)),
            "documents": store.manifest(db_path=settings.db_path),
        }
    if args.json:
        print(json.dumps(body, indent=2))
    else:
        print(f"Passages: {body.get('passages', '?')}")
        docs = body.get("documents", [])
        if docs:
            print(f"Documents: {len(docs)}")
            for d, n in docs:
                print(f"  - {d} ({n} sections)")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    import subprocess

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "eval.py")]
        + (["--min-score", str(args.min_score)] if args.min_score else []),
        cwd=str(REPO_ROOT),
        check=False,
    )
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="AskVault CLI")
    parser.add_argument("--base-url", default=None, help="query a deployed instance")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    ask_p = sub.add_parser("ask", help="ask a question")
    ask_p.add_argument("question", help="question text")
    ask_p.add_argument("-k", type=int, default=5, help="retrieval depth (default 5)")
    ask_p.add_argument("--base-url", default=None)
    ask_p.add_argument("--json", action="store_true")
    ask_p.set_defaults(func=cmd_ask)

    health_p = sub.add_parser("health", help="check index health")
    health_p.add_argument("--base-url", default=None)
    health_p.add_argument("--json", action="store_true")
    health_p.set_defaults(func=cmd_health)

    eval_p = sub.add_parser("eval", help="run the eval harness")
    eval_p.add_argument("--min-score", type=float, default=None)
    eval_p.set_defaults(func=cmd_eval)

    args = parser.parse_args()
    if not hasattr(args, "base_url"):
        args.base_url = None
    if not hasattr(args, "json"):
        args.json = False
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
