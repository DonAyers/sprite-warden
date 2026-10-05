"""Tests for the spritewarden.learning L3 (scheduler, doctor, tipshare, project watch, MCP)."""

from pathlib import Path

import pytest

from spritewarden.learning.acquisition import FetchResult, check_source
from spritewarden.learning.db import LearningDB
from spritewarden.learning.doctor import report as doctor_report
from spritewarden.learning.episodes import record_from_still
from spritewarden.learning.models import Proposal, Source, Strategy, utcnow
from spritewarden.learning.project_watch import register_project
from spritewarden.learning.scheduler import LearningScheduler
from spritewarden.learning.tipshare_bridge import export_tips, outcome_payload


@pytest.fixture(autouse=True)
def _deterministic_llm(monkeypatch):
    monkeypatch.setenv("SPRITEWARDEN_LEARN_LLM", "off")


def _offline(url, etag=None, last_modified=None, timeout=15.0):
    return FetchResult(url=url, status=0, error="offline")


def test_scheduler_due_and_run_once(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    doc = tmp_path / "doc.md"
    doc.write_text("v1", encoding="utf-8")
    db.upsert_source(Source(id="d", provider="acme", kind="md", url=str(doc)))
    scheduler = LearningScheduler(db=db)

    assert [s.id for s in scheduler.due_sources()] == ["d"]
    events = scheduler.run_once()
    assert len(events) == 1 and events[0].change_kind == "added"
    assert scheduler.due_sources() == []  # just checked within cadence


def test_scheduler_seeds_when_empty(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    scheduler = LearningScheduler(db=db, fetch_fn=_offline)
    scheduler.run_once()
    assert len(db.list_sources()) >= 5


def test_project_watch_then_change_detected(tmp_path: Path):
    project = tmp_path / "game"
    (project / "assets").mkdir(parents=True)
    theme = project / "assets" / "theme.tres"
    theme.write_text("PackedColorArray(1, 0, 0)", encoding="utf-8")

    db = LearningDB(tmp_path / "l.db")
    count = register_project(db, provider="mygame", root=project)
    assert count == 1
    src = db.get_source("mygame:assets/theme.tres")
    assert src is not None and src.kind == "file"

    check_source(src, db)
    theme.write_text("PackedColorArray(0, 0, 1)", encoding="utf-8")
    events = check_source(db.get_source(src.id), db)
    assert len(events) == 1 and events[0].change_kind == "changed"


def test_doctor_report_surfaces_warnings(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    db.upsert_source(Source(id="d", provider="acme", kind="md", url="file://x"))
    data = doctor_report(db)
    assert data["sources"]["never_checked"] == 1
    assert any("never checked" in w for w in data["warnings"])
    assert data["db_path"].endswith("l.db")


def test_tipshare_export_and_mark(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    db.upsert_strategy(Strategy(id="s1", scope_key="p|o|a|top-down", what="Prefer X", how="Do Y", why="Because Z", status="active"))
    payloads = export_tips(db, topic="godot-pixel-art")
    assert len(payloads) == 1 and payloads[0]["title"] == "Prefer X"

    export_tips(db, topic="godot-pixel-art", mark=True)
    assert export_tips(db) == []
    assert len(export_tips(db, include_exported=True)) == 1
    assert db.list_tip_exports()[0]["strategy_id"] == "s1"

    assert outcome_payload(42, worked=True, note="confirmed")["tip_id"] == 42


def test_mcp_module_registers_learning_surface():
    import spritewarden.server.mcp_server as mcp_server

    assert hasattr(mcp_server, "mcp")
    for name in ("learn_status", "list_learned_proposals", "approve_learned_proposal",
                 "record_learning_episode", "resource_playbook", "resource_learn_status"):
        assert hasattr(mcp_server, name), name
