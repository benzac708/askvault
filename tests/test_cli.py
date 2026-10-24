"""Tests for the CLI."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_cli_ask():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "cli.py"), "ask", "how do I request production access"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert result.returncode == 0
    assert "Sources:" in result.stdout
    assert "access-control" in result.stdout


def test_cli_ask_json():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "cli.py"), "ask", "how do I request production access", "--json"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert "answer" in data
    assert "citations" in data


def test_cli_health():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "cli.py"), "health"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert result.returncode == 0
    assert "Passages:" in result.stdout
    assert "Documents:" in result.stdout


def test_cli_eval():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "cli.py"), "eval"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert result.returncode == 0
    assert "All questions passed" in result.stdout
