"""Tests for the eval harness."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval import load_questions, run_eval

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_eval_questions_load():
    questions = load_questions(REPO_ROOT / "eval_questions.jsonl")
    assert len(questions) >= 20
    for q in questions:
        assert "question" in q
        assert "should_refuse" in q


def test_eval_all_pass():
    questions = load_questions(REPO_ROOT / "eval_questions.jsonl")
    report = run_eval(questions)
    assert report["retrieval"]["accuracy"] == 1.0
    assert report["refusal"]["accuracy"] == 1.0
    assert report["citation"]["accuracy"] == 1.0


def test_eval_refusal_questions_have_no_expected_doc():
    questions = load_questions(REPO_ROOT / "eval_questions.jsonl")
    for q in questions:
        if q["should_refuse"]:
            assert q["expected_doc"] is None
            assert q["expected_citation"] is None
