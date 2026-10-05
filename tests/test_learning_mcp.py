"""Tests for the MCP tools/list drift probe."""

import sys
from pathlib import Path

from spritewarden.learning.acquisition import check_source
from spritewarden.learning.db import LearningDB
from spritewarden.learning.mcp_probe import normalize_tools, probe_source, probe_stdio
from spritewarden.learning.models import Source

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"

TOOLS_V1 = [
    {"name": "make_sprite", "description": "make a sprite",
     "inputSchema": {"type": "object", "properties": {"size": {"type": "integer"}}}},
]
TOOLS_V2 = TOOLS_V1 + [
    {"name": "animate", "description": "animate a sprite",
     "inputSchema": {"type": "object", "properties": {"frames": {"type": "integer"}}}},
    {"name": "deprecated_op", "description": "old and deprecated"},
]


def test_probe_stdio_reads_tools_from_server():
    tools = probe_stdio([sys.executable, str(FIXTURE)], timeout=20.0)
    assert tools is not None
    names = {t["name"] for t in tools}
    assert {"make_sprite", "deprecated_op"} <= names


def test_probe_source_unconfigured_returns_none():
    source = Source(id="p:mcp", provider="p", kind="mcp_tools", url="mcp://p")
    assert probe_source(source, config={}) is None


def test_normalize_tools_stable_and_sorted():
    a = normalize_tools(TOOLS_V2)
    b = normalize_tools(list(reversed(TOOLS_V2)))
    assert a == b
    assert a.index("animate") < a.index("make_sprite")


def test_check_mcp_source_baseline_then_change(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    db.upsert_source(Source(id="p:mcp", provider="p", kind="mcp_tools", url="mcp://p"))

    state = {"tools": TOOLS_V1}

    def probe(source):
        return state["tools"]

    # Baseline: first probe records tools but emits no false "added" event.
    assert check_source(db.get_source("p:mcp"), db, mcp_probe_fn=probe) == []
    tool = db.get_tool("p", "make_sprite")
    assert tool is not None and "size" in tool.params_json
    # Unchanged -> no event.
    assert check_source(db.get_source("p:mcp"), db, mcp_probe_fn=probe) == []

    state["tools"] = TOOLS_V2
    events = check_source(db.get_source("p:mcp"), db, mcp_probe_fn=probe)
    assert len(events) == 1 and events[0].change_kind == "changed"


def test_check_mcp_source_detects_schema_change_and_deprecations(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    db.upsert_source(Source(id="p:mcp", provider="p", kind="mcp_tools", url="mcp://p"))

    state = {"tools": TOOLS_V1}

    def probe(source):
        return state["tools"]

    check_source(db.get_source("p:mcp"), db, mcp_probe_fn=probe)
    state["tools"] = TOOLS_V2
    events = check_source(db.get_source("p:mcp"), db, mcp_probe_fn=probe)
    assert len(events) == 1 and events[0].change_kind == "changed"
    assert "new_capability" in events[0].signals or "addition" in events[0].signals
    assert db.get_tool("p", "animate") is not None
    dep = db.get_tool("p", "deprecated_op")
    assert dep is not None and "deprecated" in dep.deprecations_json
