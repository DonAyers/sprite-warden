"""Tipshare bridge: recipe tips from the registry, published through a FAKE stdio server only."""

import json
import os
import subprocess
import sys
from pathlib import Path

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
    tips = build_recipe_tips()
    assert [t["export_key"] for t in tips] == [
        "recipe:pixellab:v1", "recipe:spritefusion:v1", "recipe:retrodiffusion:v1", "recipe:spritecook:v1",
        "recipe:openrouter_image:v1",
    ]
    for tip in tips:
        assert REQUIRED <= set(tip) and tip["topic_slug"] == "godot-pixel-art" and tip["author"] == "sprite-warden"
        assert all(isinstance(tip[k], str) and tip[k] for k in ("title", "summary", "problem", "solution"))
        assert isinstance(tip["gotchas"], list) and tip["gotchas"]
        assert "{" not in tip["solution"]  # no unfilled message placeholders
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
        assert {t["name"] for t in session.list_tools()} == {"search", "share_tip"}
        assert session.call_tool("search", {"query": "nothing", "topic_slug": "t"}) == []
        with pytest.raises(McpError):
            session.call_tool("search", {"query": "broken: (query)"})
        with pytest.raises(McpError):
            session.call_tool("no_such_tool", {})
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
    server.seed([{"id": 7, "title": tips[0]["title"].upper(), "topic_slug": "godot-pixel-art"}])  # same title, other case

    report = publish_tips(tips, db, command=server.command, dry_run=False)
    assert [r["action"] for r in report] == ["skipped_existing", "published", "published", "published", "published"]
    sent = server.shared()
    assert len(sent) == 4 and all(c["agent"] == "sprite-warden" for c in sent)
    first = sent[0]["arguments"]
    assert set(first) <= {"topic_slug", "title", "summary", "problem", "solution", "gotchas", "author"}
    assert "export_key" not in first and first["title"] == tips[1]["title"]
    # searches use plain words only (the title's punctuation would break full-text search)
    searches = [c["arguments"]["query"] for c in server.data()["calls"] if c["tool"] == "search"]
    assert all(q.replace(" ", "").isalnum() for q in searches)

    exports = {e["strategy_id"]: e for e in db.list_tip_exports()}
    assert set(exports) == {t["export_key"] for t in tips[1:]}
    assert exports["recipe:spritecook:v1"]["tip_ref"] == report[3]["tip_ref"] and report[3]["tip_ref"].isdigit()

    # a second run sends nothing: recorded exports are skipped, the first tip still exists on the server
    report = publish_tips(tips, db, command=server.command, dry_run=False)
    assert [r["action"] for r in report] == ["skipped_exported"] * 4 + ["skipped_existing"]
    assert len(server.shared()) == 4
    # --all re-checks them against the server, which now has every title
    report = publish_tips(tips, db, command=server.command, dry_run=False, include_exported=True)
    assert {r["action"] for r in report} == {"skipped_existing"} and len(server.shared()) == 4


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
    assert mark_exported(tips, db) == 5
    assert [r["action"] for r in publish_tips(tips, db)] == ["skipped_exported"] * 5

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
    assert {r["action"] for r in json.loads(out.stdout)["report"]} == {"marked_exported"}
    out = _cli(["learn", "export-tips", "--recipes", "--publish", "--json"], tmp_path, server)
    assert out.returncode == 0, out.stderr + out.stdout
    assert {r["action"] for r in json.loads(out.stdout)["report"]} == {"skipped_exported"}
    assert len(server.shared()) == 1
