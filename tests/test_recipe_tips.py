"""Tipshare bridge: recipe tips from the registry, published through a FAKE stdio server only."""

import json
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from spritewarden.learning.db import LearningDB
from spritewarden.learning.mcp_probe import McpError, StdioSession
from spritewarden.learning.recipe_tips import (
    TIPSHARE_CMD_ENV,
    build_recipe_tips,
    mark_exported,
    publish_tips,
    tipshare_command,
)

FAKE = Path(__file__).parent / "fixtures" / "fake_tipshare_server.py"
REQUIRED = {"title", "summary", "problem", "solution", "gotchas", "topic_slug"}


@pytest.fixture
def server(tmp_path, monkeypatch):
    store = tmp_path / "tipshare_store.json"
    store.write_text(json.dumps({"tips": [], "calls": []}), encoding="utf-8")
    monkeypatch.setenv("FAKE_TIPSHARE_STORE", str(store))
    monkeypatch.delenv(TIPSHARE_CMD_ENV, raising=False)

    class Server:
        command = [sys.executable, str(FAKE)]

        def data(self):
            return json.loads(store.read_text(encoding="utf-8"))

        def seed(self, tips):
            store.write_text(json.dumps({"tips": tips, "calls": []}), encoding="utf-8")

        def shared(self):
            return [c for c in self.data()["calls"] if c["tool"] == "share_tip"]

    return Server()


def test_one_tip_per_provider_from_the_registry():
    from spritewarden.enforce.core import load_raw_registry
    registry = load_raw_registry()
    tips = build_recipe_tips()
    assert [t["applicability"]["provider"] for t in tips] == [
        "pixellab", "spritefusion", "retrodiffusion", "spritecook", "openrouter_image",
    ]
    for tip in tips:
        provider = tip["applicability"]["provider"]
        projection = {"provider": registry["providers"][provider], "pipeline_rules": registry.get("pipeline_rules", []),
                      "stages": registry.get("stages", {}), "version": registry.get("version", 1)}
        digest = hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert tip["export_key"] == f"recipe:{provider}:{digest}"
        assert tip["content_hash"] == tip["applicability"]["registry_hash"] == digest
        assert f"sha256 {digest}" in tip["title"]
        assert tip["evidence_state"] == "local_policy_not_provider_verified"
        assert REQUIRED <= set(tip) and tip["topic_slug"] == "godot-pixel-art" and tip["author"] == "sprite-warden"
        assert all(isinstance(tip[k], str) and tip[k] for k in ("title", "summary", "problem", "solution"))
        assert isinstance(tip["gotchas"], list) and tip["gotchas"]
        assert not re.search(r"\{(?:count|value|operation|max|of)\}", tip["solution"])  # JSON policy is allowed, unfilled rule placeholders are not
    sc = next(t for t in tips if t["export_key"].startswith("recipe:spritecook"))
    assert sc["title"].startswith("SpriteCook generator recipes:")
    assert "animate_game_art" in sc["solution"] and "matte" in sc["solution"] and "at most 8" in sc["solution"]
    assert "gates before: start_image" in sc["solution"]
    assert any("transparent background" in g for g in sc["gotchas"])
    rd = next(t for t in tips if "retrodiffusion" in t["export_key"])
    assert "pixel art" in rd["gotchas"][0] and "multiples of 8" in rd["solution"]
    assert build_recipe_tips(topic="other")[0]["topic_slug"] == "other"


def test_stdio_session_calls_tools(server):
    with StdioSession(server.command, env={"TIPSHARE_AGENT_ID": "sprite-warden"}) as session:
        assert {t["name"] for t in session.list_tools()} == {"search", "share_tip", "get_tip"}
        assert session.call_tool("search", {"query": "nothing", "topic_slug": "t"}) == []
        with pytest.raises(McpError):
            session.call_tool("search", {"query": "broken: (query)"})
        with pytest.raises(McpError):
            session.call_tool("no_such_tool", {})
        with pytest.raises(McpError, match="tip not found"):
            session.call_tool("get_tip", {"tip_id": "missing"})
        payload = build_recipe_tips()[0]
        receipt = session.call_tool("share_tip", {k: payload[k] for k in REQUIRED})
        assert str(UUID(receipt["uuid"])) == receipt["uuid"]
        by_id = session.call_tool("get_tip", {"tip_id": receipt["id"]})
        by_uuid = session.call_tool("get_tip", {"tip_id": receipt["uuid"]})
        assert by_id == by_uuid
        assert by_id["uuid"] == receipt["uuid"] and by_id["solution"] == payload["solution"]
    assert server.data()["calls"][0]["agent"] == "sprite-warden"
    with pytest.raises(McpError):
        StdioSession(["definitely-not-a-real-binary-xyz"]).open()


def test_dry_run_is_the_default_and_sends_nothing(server, tmp_path):
    db = LearningDB(tmp_path / "l.db")
    tips = build_recipe_tips()
    # no server configured: unchecked
    report = publish_tips(tips, db)
    assert [r["action"] for r in report] == ["would_publish"] * 5 and report[0]["checked"] is False
    # with a server: searches only
    report = publish_tips(tips, db, command=server.command)
    assert [r["action"] for r in report] == ["would_publish"] * 5 and report[0]["checked"] is True
    assert server.shared() == [] and len(server.data()["calls"]) == 5
    assert db.list_tip_exports() == []
    with pytest.raises(ValueError):
        publish_tips(tips, db, dry_run=False)  # a real run needs the command


def test_publish_skips_existing_titles_and_records_exports(server, tmp_path):
    db = LearningDB(tmp_path / "l.db")
    tips = build_recipe_tips()
    server.seed([{**tips[0], "id": 7, "uuid": "a1111111-1111-4111-8111-111111111111",
                  "title": tips[0]["title"].upper()}])  # same content and title, other case

    report = publish_tips(tips, db, command=server.command, dry_run=False)
    assert [r["action"] for r in report] == ["skipped_existing", "published", "published", "published", "published"]
    sent = server.shared()
    assert len(sent) == 4 and all(c["agent"] == "sprite-warden" for c in sent)
    first = sent[0]["arguments"]
    assert set(first) <= {"topic_slug", "title", "summary", "problem", "solution", "gotchas", "author", "applicability", "evidence_state"}
    assert first["applicability"] == tips[1]["applicability"]
    assert first["evidence_state"] == "local_policy_not_provider_verified"
    assert "export_key" not in first and first["title"] == tips[1]["title"]
    # searches use plain words only (the title's punctuation would break full-text search)
    searches = [c["arguments"]["query"] for c in server.data()["calls"] if c["tool"] == "search"]
    assert all(q.replace(" ", "").isalnum() for q in searches)

    exports = {e["strategy_id"]: e for e in db.list_tip_exports()}
    assert set(exports) == {t["export_key"] for t in tips}
    stored = {t["uuid"]: t for t in server.data()["tips"]}
    for tip, row in zip(tips, report):
        ref = row["tip_ref"]
        assert str(UUID(ref)) == ref
        assert exports[tip["export_key"]]["tip_ref"] == ref
        assert stored[ref]["solution"] == tip["solution"]
    assert len([c for c in server.data()["calls"] if c["tool"] == "get_tip"]) == 5

    # a second run sends nothing: all verified receipts, including the existing tip, are recorded
    report = publish_tips(tips, db, command=server.command, dry_run=False)
    assert [r["action"] for r in report] == ["skipped_exported"] * 5
    assert len(server.shared()) == 4
    # --all re-checks them against the server, which now has every title
    report = publish_tips(tips, db, command=server.command, dry_run=False, include_exported=True)
    assert {r["action"] for r in report} == {"skipped_existing"} and len(server.shared()) == 4


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("override, error", [
    ({"solution": "conflicting content"}, "conflicting content"),
    ({"uuid": "7"}, "UUID"),
])
def test_existing_tip_requires_content_and_uuid_readback(server, tmp_path, dry_run, override, error):
    db = LearningDB(tmp_path / "l.db")
    tip = build_recipe_tips()[0]
    server.seed([{**tip, "id": 7, "uuid": "a1111111-1111-4111-8111-111111111111", **override}])
    report = publish_tips([tip], db, command=server.command, dry_run=dry_run)
    assert report[0]["action"] == "error" and error in report[0]["error"]
    assert db.list_tip_exports() == [] and server.shared() == []
    assert [c["tool"] for c in server.data()["calls"]] == ["search", "get_tip"]


def test_dry_run_verifies_existing_tip_without_recording(server, tmp_path):
    db = LearningDB(tmp_path / "l.db")
    tip = build_recipe_tips()[0]
    ref = "a1111111-1111-4111-8111-111111111111"
    server.seed([{**tip, "id": 7, "uuid": ref}])
    report = publish_tips([tip], db, command=server.command)
    assert report[0]["action"] == "skipped_existing" and report[0]["tip_ref"] == ref
    assert db.list_tip_exports() == [] and server.shared() == []
    assert [c["tool"] for c in server.data()["calls"]] == ["search", "get_tip"]


def test_a_failing_tip_is_reported_and_not_recorded(server, tmp_path):
    db = LearningDB(tmp_path / "l.db")
    tips = build_recipe_tips()[:2]
    tips[0] = {**tips[0], "title": "FAIL this one"}
    report = publish_tips(tips, db, command=server.command, dry_run=False)
    assert report[0]["action"] == "error" and "rejected" in report[0]["error"]
    assert report[1]["action"] == "published"
    assert [e["strategy_id"] for e in db.list_tip_exports()] == [tips[1]["export_key"]]


def test_mark_exported_and_command_env(tmp_path, monkeypatch):
    db = LearningDB(tmp_path / "l.db")
    tips = build_recipe_tips()
    monkeypatch.delenv(TIPSHARE_CMD_ENV, raising=False)
    for ref in (None, "7", "a1111111-1111-4111-8111-111111111111"):
        with pytest.raises(ValueError, match="verified get_tip receipt"):
            mark_exported(tips, db, tip_ref=ref)
    assert db.list_tip_exports() == []
    assert [r["action"] for r in publish_tips(tips, db)] == ["would_publish"] * 5

    monkeypatch.setenv(TIPSHARE_CMD_ENV, '["node", "C:/x/tipshare/dist/server.js"]')
    assert tipshare_command() == ["node", "C:/x/tipshare/dist/server.js"]
    monkeypatch.setenv(TIPSHARE_CMD_ENV, "node server.js")
    with pytest.raises(ValueError):
        tipshare_command()
    monkeypatch.delenv(TIPSHARE_CMD_ENV)
    assert tipshare_command() is None


def _cli(args, tmp_path, server=None):
    env = {**os.environ, "SPRITEWARDEN_LEARNING_DB": str(tmp_path / "learning.db")}
    env.pop(TIPSHARE_CMD_ENV, None)
    if server is not None:
        env[TIPSHARE_CMD_ENV] = json.dumps(server.command)
    return subprocess.run([sys.executable, "-m", "spritewarden.cli", *args], capture_output=True, text=True,
                          encoding="utf-8", env=env)


def test_recipes_export_tips_cli(server, tmp_path):
    out_file = tmp_path / "out" / "tips.json"
    out = _cli(["recipes", "--export-tips", str(out_file), "--json"], tmp_path)
    assert out.returncode == 0, out.stderr + out.stdout
    tips = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(tips) == 5 and all(REQUIRED <= set(t) for t in tips)
    data = json.loads(out.stdout)
    assert data["dry_run"] is True and {r["action"] for r in data["report"]} == {"would_publish"}

    # one provider; publishing without a server command fails loudly
    out = _cli(["recipes", "sc", "--export-tips", str(out_file), "--json"], tmp_path)
    assert len(json.loads(out_file.read_text(encoding="utf-8"))) == 1
    assert _cli(["recipes", "--publish-tips"], tmp_path).returncode == 1
    assert server.shared() == []

    # against the fake server
    out = _cli(["recipes", "sc", "--publish-tips", "--json"], tmp_path, server)
    assert out.returncode == 0, out.stderr + out.stdout
    assert [r["action"] for r in json.loads(out.stdout)["report"]] == ["published"]
    assert len(server.shared()) == 1 and server.shared()[0]["arguments"]["title"].startswith("SpriteCook")
    out = _cli(["recipes", "sc", "--publish-tips", "--json"], tmp_path, server)
    assert [r["action"] for r in json.loads(out.stdout)["report"]] == ["skipped_exported"]

    out = _cli(["recipes", "--mark-tips-exported", "--json"], tmp_path)
    assert out.returncode == 1 and "verified" in out.stdout + out.stderr
    assert len(LearningDB(tmp_path / "learning.db").list_tip_exports()) == 1
    out = _cli(["learn", "export-tips", "--recipes", "--publish", "--json"], tmp_path, server)
    assert out.returncode == 0, out.stderr + out.stdout
    assert [r["action"] for r in json.loads(out.stdout)["report"]] == ["skipped_exported"] + ["published"] * 4
    assert len(server.shared()) == 5
    out = _cli(["learn", "export-tips", "--recipes", "--publish", "--json"], tmp_path, server)
    assert out.returncode == 0, out.stderr + out.stdout
    assert {r["action"] for r in json.loads(out.stdout)["report"]} == {"skipped_exported"}
    assert len(server.shared()) == 5
