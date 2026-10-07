"""The MCP server must always handshake: lazy catalog/router, no learning side effects at import."""

import os
import sys

import pytest

from spritewarden.learning.mcp_probe import probe_stdio


def test_import_creates_nothing_eagerly():
    import spritewarden.server.mcp_server as server

    # Importing the module must not open the catalog DB or build the router.
    assert server._catalog is None
    assert server._router is None


def test_no_learn_flag(monkeypatch):
    import spritewarden.server.mcp_server as server

    monkeypatch.setenv("SPRITEWARDEN_NO_LEARN", "1")
    assert server.learning_disabled() is True
    assert server.start_learning_scheduler() is False
    monkeypatch.setenv("SPRITEWARDEN_NO_LEARN", "")
    assert server.learning_disabled() is False


def test_server_answers_initialize_and_tools_list_over_stdio(monkeypatch, tmp_path):
    monkeypatch.setenv("SPRITEWARDEN_NO_LEARN", "1")
    monkeypatch.setenv("SPRITEWARDEN_LEARNING_DB", str(tmp_path / "learning.db"))
    tools = probe_stdio([sys.executable, "-m", "spritewarden.cli", "mcp"], timeout=40.0)
    assert tools is not None, "server did not answer initialize/tools/list"
    names = {t["name"] for t in tools}
    assert {"judge_still", "judge_animation", "route_generation", "search_art"} <= names
