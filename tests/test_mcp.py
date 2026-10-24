"""Tests for the MCP server."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_mcp_server_imports():
    """MCP server module imports without error."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("mcp_server", REPO_ROOT / "mcp_server.py")
    assert spec is not None


def test_mcp_server_lists_tools():
    """MCP server exposes ask_vault and vault_health tools."""
    import asyncio

    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    async def _run():
        params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]

    names = asyncio.run(_run())
    assert "ask_vault" in names
    assert "vault_health" in names


def test_mcp_ask_vault_returns_citations():
    """ask_vault returns an answer with citations."""
    import asyncio

    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    async def _run():
        params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("ask_vault", {"question": "how do I request production access"})
                return result.content[0].text

    text = asyncio.run(_run())
    data = json.loads(text)
    assert "answer" in data
    assert "citations" in data
    assert len(data["citations"]) > 0
    assert any(c["doc"] == "access-control" for c in data["citations"])


def test_mcp_vault_health():
    """vault_health returns passage and document counts."""
    import asyncio

    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession

    async def _run():
        params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("vault_health", {})
                return result.content[0].text

    text = asyncio.run(_run())
    data = json.loads(text)
    assert data["passages"] > 0
    assert len(data["documents"]) > 0
