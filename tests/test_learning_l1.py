"""Tests for the spritewarden.learning L1 foundations (offline)."""

import json
from pathlib import Path

from spritewarden.learning.acquisition import FetchResult, check_all, check_source, fetch
from spritewarden.learning.db import LearningDB
from spritewarden.learning.diffing import analyze_change, content_hash, json_structural_diff
from spritewarden.learning.models import Episode, Proposal, Source, Strategy
from spritewarden.learning.registry import seed_sources
from spritewarden.learning.taxonomy import normalize_perspective, normalize_style


# --- taxonomy ---

def test_normalize_perspective_maps_synonyms():
    assert normalize_perspective("Overhead / Bird's-Eye").canonical == "top-down"
    assert normalize_perspective("side-scrolling").canonical == "side-on"
    assert normalize_perspective("isometric 2.5D").canonical == "isometric"
    assert normalize_perspective("three-quarter").ambiguous is True
    assert normalize_perspective("something weird").canonical is None


def test_normalize_style_dedupes_and_slugs():
    tags = normalize_style(["Luap-40", "1px Outline", "luap 40", ""])
    assert tags == ["luap 40", "1px outline"]


# --- diffing ---

def test_content_hash_stable():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


def test_analyze_change_identical_returns_none():
    assert analyze_change("same", "same") is None


def test_analyze_change_keywords_raise_significance():
    old = "model list: a, b"
    new = "model list: a, b, c\nnote: x is now deprecated and will be removed"
    rec = analyze_change(old, new)
    assert rec["change_kind"] == "changed"
    assert "deprecation" in rec["signals"] or "removal" in rec["signals"]
    assert rec["significance"] >= 0.35
    assert "+note:" in rec["diff_text"] or "deprecated" in rec["diff_text"]


def test_analyze_change_added_from_empty():
    rec = analyze_change("", "hello world")
    assert rec["change_kind"] == "added"


def test_json_structural_diff():
    old = {"models": {"a": {"cost": 1}}, "version": 1}
    new = {"models": {"b": {"cost": 2}}, "version": 2}
    diff = json_structural_diff(old, new)
    assert any(d.startswith("removed models.a") for d in diff)
    assert any(d.startswith("added models.b") for d in diff)
    assert any(d.startswith("changed version") for d in diff)


# --- db ---

def test_db_sources_and_snapshots(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    src = Source(id="s1", provider="acme", kind="md", url="file://x")
    db.upsert_source(src)
    assert db.get_source("s1").provider == "acme"
    assert [s.id for s in db.list_sources(provider="acme")] == ["s1"]

    db.update_source_fetch_state("s1", "etag1", "lm1", "hash1", "2026-01-01T00:00:00+00:00")
    fetched = db.get_source("s1")
    assert fetched.etag == "etag1" and fetched.last_hash == "hash1"


def test_db_strategy_fts_and_proposals(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    strat = Strategy(id="st1", scope_key="pixellab|walk|character|top-down", what="Mid-stride start",
                     how="Start and end on a real mid-walk frame", why="Idle start twists the body")
    db.upsert_strategy(strat)
    assert db.get_strategy("st1").what == "Mid-stride start"
    hits = db.search_strategies("mid-stride")
    assert any(h.id == "st1" for h in hits)

    pid = db.insert_proposal(Proposal(kind="new_strategy", target="st1", what="promote", how="x", why="y"))
    assert db.get_proposal(pid).status == "open"
    db.set_proposal_status(pid, "approved", "2026-01-01T00:00:00+00:00")
    assert db.list_proposals(status="approved")[0].id == pid


def test_db_episodes_and_budget(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    db.insert_episode(Episode(provider="pixellab", operation="create_character_v3",
                              perspective="top-down", outcome="rejected", reason="off-palette"))
    assert db.count_episodes(provider="pixellab") == 1
    assert db.list_episodes(outcome="rejected")[0].reason == "off-palette"

    budget = db.get_budget("2026-01", cap_usd=1.0, cap_calls=10)
    assert budget.allows(0.5)
    db.add_spend("2026-01", 0.75, calls=2)
    after = db.get_budget("2026-01")
    assert after.spend_usd == 0.75 and after.calls == 2
    assert not after.allows(0.5)


# --- acquisition ---

def test_check_source_file_added_then_unchanged(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    doc = tmp_path / "doc.md"
    doc.write_text("# Docs\nversion 1\n", encoding="utf-8")
    src = Source(id="docs", provider="acme", kind="md", url=str(doc))
    db.upsert_source(src)

    events = check_source(db.get_source("docs"), db)
    assert len(events) == 1
    assert events[0].change_kind == "added"

    again = check_source(db.get_source("docs"), db)
    assert again == []


def test_check_source_detects_modification(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    doc = tmp_path / "doc.md"
    doc.write_text("plain text\n", encoding="utf-8")
    src = Source(id="docs", provider="acme", kind="md", url=str(doc))
    db.upsert_source(src)
    check_source(db.get_source("docs"), db)

    doc.write_text("plain text\nNEW: feature added. old thing deprecated\n", encoding="utf-8")
    events = check_source(db.get_source("docs"), db)
    assert len(events) == 1
    assert events[0].change_kind == "changed"
    assert events[0].significance > 0.2


def test_check_source_not_modified_short_circuits(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    db.upsert_source(Source(id="s", provider="p", kind="md", url="https://example.invalid"))
    calls = {"n": 0}

    def fake_fetch(url, etag=None, last_modified=None, timeout=15.0):
        calls["n"] += 1
        return FetchResult(url=url, status=304, not_modified=True)

    events = check_source(db.get_source("s"), db, fetch_fn=fake_fetch)
    assert events == [] and calls["n"] == 1 and db.latest_snapshot("s") is None


def test_check_source_error_is_graceful(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    src = Source(id="s", provider="p", kind="md", url="https://example.invalid")
    db.upsert_source(src)

    def boom(url, etag=None, last_modified=None, timeout=15.0):
        return FetchResult(url=url, status=0, error="network down")

    assert check_source(db.get_source("s"), db, fetch_fn=boom) == []


def test_check_all_filters_by_provider(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    a = tmp_path / "a.md"
    a.write_text("a", encoding="utf-8")
    b = tmp_path / "b.md"
    b.write_text("b", encoding="utf-8")
    db.upsert_source(Source(id="a", provider="acme", kind="md", url=str(a)))
    db.upsert_source(Source(id="b", provider="other", kind="md", url=str(b)))
    events = check_all(db, provider="acme")
    assert {e.source_id for e in events} == {"a"}


def test_fetch_file_url(tmp_path: Path):
    doc = tmp_path / "x.txt"
    doc.write_text("payload", encoding="utf-8")
    res = fetch(doc.as_uri())
    assert res.status == 200 and res.body == "payload"


# --- registry ---

def test_seed_sources_idempotent(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    n = seed_sources(db)
    assert n >= 5
    seed_sources(db)
    assert len(db.list_sources()) == n
