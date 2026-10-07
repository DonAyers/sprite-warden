"""Identity completeness: features / stills / anchors / locomotion / body class / tier, CLI + MCP, FTS update."""

import json
import sqlite3
import subprocess
import sys

import pytest
from PIL import Image

from spritewarden.judge import evaluate_still
from spritewarden.judge.views import GAME_ORDER, check_direction_set
from spritewarden.repo.catalog import AssetCatalog
from spritewarden.repo.identity import CharacterIdentity, build_identity, parse_feature

BODY = (89, 112, 128, 255)  # Luap-40 #597080
MARK = (255, 0, 255, 255)


def _run(args, db, cwd=None):
    import os

    env = {**os.environ, "SPRITEWARDEN_DB": str(db)}
    return subprocess.run([sys.executable, "-m", "spritewarden.cli", *args], capture_output=True, text=True,
                          encoding="utf-8", env=env, cwd=cwd)


def test_parse_feature_forms():
    f = parse_feature("wrench:left:#FF00FF:10,30,8,12")
    assert (f.name, f.side, f.marker_hex, f.relative_box) == ("wrench", "left", "#ff00ff", [10, 30, 8, 12])
    f = parse_feature("visor:-:6da18a")
    assert f.side is None and f.marker_hex == "#6da18a" and f.relative_box is None
    f = parse_feature({"name": "emitter", "side": "right", "marker_hex": "#00ffff", "relative_box": [1, 2, 3, 4]})
    assert f.side == "right" and f.relative_box == [1, 2, 3, 4]
    for bad in ("", "wrench:middle:#fff", "wrench:left:#fff:1,2,3"):
        with pytest.raises(ValueError):
            parse_feature(bad)


def test_build_identity_creates_and_merges():
    with pytest.raises(ValueError):
        build_identity(None, "bot")  # a new identity needs a description
    first = build_identity(None, "bot", prompt_description="a builder bot", locomotion="wheels", body_class="figure",
                           size_tier="96", features=["wrench:left:#ff00ff"], turnaround_stills={"south": "s.png"},
                           style_anchors={"spritecook": "a1"}, ground_row_offset=19)
    assert first.name == "bot" and first.locomotion == "wheels" and first.size_tier == "96" and first.ground_row_offset == 19
    second = build_identity(first, "bot", features=[{"name": "emitter", "side": "right", "marker_hex": "#00ffff"}],
                            turnaround_stills={"east": "e.png"}, style_anchors={"pixellab_character_id": "c1"})
    assert set(second.features) == {"wrench", "emitter"} and second.features["wrench"].side == "left"
    assert second.turnaround_stills == {"south": "s.png", "east": "e.png"}
    assert second.style_anchors == {"spritecook": "a1", "pixellab_character_id": "c1"}
    assert second.locomotion == "wheels" and second.prompt_description == "a builder bot"  # untouched fields stay
    with pytest.raises(ValueError):
        build_identity(first, "bot", locomotion="hover")
    # identities stored before the new fields still load
    old = CharacterIdentity.model_validate({"id": "o", "name": "O", "prompt_description": "x"})
    assert old.locomotion is None and old.body_class is None and old.size_tier is None


def test_identity_cli_set_show_list(tmp_path):
    db = tmp_path / "cat.db"
    out = _run(["identity", "set", "bildbot", "--name", "bildBot", "--desc", "one-wheel builder bot", "--game", "irm",
                "--width", "96", "--height", "96", "--ground-row-offset", "19", "--locomotion", "wheels",
                "--body-class", "figure", "--size-tier", "96", "--feature", "wrench:left:#ff00ff:10,30,8,12",
                "--feature", "emitter:right:#00ffff", "--still", "south=art/south.png",
                "--style-anchor", "spritecook=asset-1", "--prohibit", "no legs", "--json"], db)
    assert out.returncode == 0, out.stderr + out.stdout
    data = json.loads(out.stdout)
    assert data["dimensions"] == [96, 96] and data["locomotion"] == "wheels" and data["body_class"] == "figure"
    assert data["features"]["wrench"]["side"] == "left" and data["features"]["wrench"]["relative_box"] == [10, 30, 8, 12]
    assert data["turnaround_stills"] == {"south": "art/south.png"} and data["style_anchors"] == {"spritecook": "asset-1"}

    # an update touches only what it names
    out = _run(["identity", "set", "bildbot", "--still", "north=art/north.png", "--size-tier", "48-64"], db)
    assert out.returncode == 0, out.stderr + out.stdout
    shown = json.loads(_run(["identity", "show", "bildbot"], db).stdout)
    assert shown["size_tier"] == "48-64" and shown["locomotion"] == "wheels" and shown["name"] == "bildBot"
    assert shown["turnaround_stills"] == {"south": "art/south.png", "north": "art/north.png"}
    listed = json.loads(_run(["identity", "list", "--json"], db).stdout)
    assert [i["id"] for i in listed] == ["bildbot"]

    assert _run(["identity", "set", "newbot"], db).returncode == 1  # no description
    assert _run(["identity", "set", "bildbot", "--still", "nonsense"], db).returncode == 1
    assert _run(["identity", "show", "nobody"], db).returncode == 1


def test_mcp_register_exposes_the_new_fields(tmp_path, monkeypatch):
    import spritewarden.server.mcp_server as server

    monkeypatch.setattr(server, "_catalog", AssetCatalog(tmp_path / "cat.db"))
    res = server.register_character_identity(
        id="bot", name="Bot", prompt_description="a bot",
        features=[{"name": "wrench", "side": "left", "marker_hex": "#ff00ff", "relative_box": [1, 2, 3, 4]}],
        turnaround_stills={"south": "s.png"}, ground_row_offset=12, style_anchors={"spritecook": "a"},
        locomotion="treads", body_class="rigid", size_tier="48-64",
    )
    assert res["status"] == "ok"
    ident = server.get_character_identity("bot")
    assert ident["features"]["wrench"]["side"] == "left" and ident["ground_row_offset"] == 12
    assert ident["locomotion"] == "treads" and ident["body_class"] == "rigid" and ident["size_tier"] == "48-64"
    # a second call keeps what it does not mention
    server.register_character_identity(id="bot", name="Bot", prompt_description="a bot", turnaround_stills={"east": "e.png"})
    ident = server.get_character_identity("bot")
    assert ident["locomotion"] == "treads" and ident["turnaround_stills"] == {"south": "s.png", "east": "e.png"}
    bad = server.register_character_identity(id="bot", name="Bot", prompt_description="a bot", locomotion="hover")
    assert bad["status"] == "error"


def _wheel_frames():
    frames = []
    for i in range(4):
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        for y in range(9, 21):
            for x in range(10, 22):
                img.putpixel((x, y), BODY)
        for y in range(22, 30):
            for x in range(11, 21):
                img.putpixel((x, y), BODY)
        sx, sy = [(11, 22), (20, 22), (20, 29), (11, 29)][i]
        img.putpixel((sx, sy), (30, 30, 40, 255))
        frames.append(img)
    return frames


def test_judge_anim_reads_locomotion_from_the_identity(tmp_path):
    db = tmp_path / "cat.db"
    AssetCatalog(db).save_identity(build_identity(None, "roller", prompt_description="a wheel bot", locomotion="wheels"))
    sheet = Image.new("RGBA", (32 * 4, 32), (0, 0, 0, 0))
    for i, f in enumerate(_wheel_frames()):
        sheet.paste(f, (i * 32, 0))
    path = tmp_path / "roll.png"
    sheet.save(path)

    base = ["judge-anim", str(path), "--cell", "32", "--walk", "--no-record", "--json", "--palette", "luap-40"]
    with_id = json.loads(_run([*base, "--char-id", "roller"], db).stdout)
    walk = next(g for g in with_id["gates"] if g["name"] == "walk_mechanics")
    assert walk["metrics"]["locomotion"] == "wheels" and walk["passed"] is True
    without = json.loads(_run(base, db).stdout)
    assert next(g for g in without["gates"] if g["name"] == "walk_mechanics")["metrics"]["locomotion"] == "legs"
    forced = json.loads(_run([*base, "--char-id", "roller", "--locomotion", "legs"], db).stdout)
    assert next(g for g in forced["gates"] if g["name"] == "walk_mechanics")["metrics"]["locomotion"] == "legs"


def test_judges_read_size_tier_and_sided_features_from_the_identity():
    ident = build_identity(None, "bot", prompt_description="a bot", size_tier="16-32", dimensions=[64, 64],
                           features=[{"name": "tool", "side": "left", "marker_hex": "#ff00ff"}])
    tall = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    for y in range(8, 58):  # 50px tall: outside the 16-32 tier (12-32px)
        for x in range(24, 40):
            tall.putpixel((x, y), BODY)
    gate = evaluate_still(tall, "luap-40", start_image=True, identity=ident).gate("start_image")
    assert gate.details["checks"]["content_height"]["passed"] is False
    assert "tier 16-32" in gate.details["checks"]["content_height"]["detail"]
    # an explicit tier wins over the identity's
    gate = evaluate_still(tall, "luap-40", start_image=True, identity=ident, size_tier="48-64").gate("start_image")
    assert gate.details["checks"]["content_height"]["passed"] is True

    # `side` alone (no "left"/"right" in the name or description) drives the tool-side check
    def view(marker_x):
        img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
        for y in range(14, 44):
            for x in range(16, 32):
                img.putpixel((x, y), BODY)
        img.putpixel((marker_x, 33), MARK)
        return img

    views = {f: view(40) for f in GAME_ORDER}  # screen right everywhere: wrong in the back views
    gate = check_direction_set(views, identity=ident)
    assert gate.details["checks"]["tool_side"]["passed"] is False
    assert "north" in gate.details["checks"]["tool_side"]["detail"]


def test_assets_fts_follows_updates_and_deletes(tmp_path):
    cat = AssetCatalog(tmp_path / "cat.db")
    cat.register_asset("a1", "Old Name", "x/old.png", tags=["rusty", "crate"])
    assert [h["id"] for h in cat.search("rusty")] == ["a1"]
    cat.register_asset("a1", "Shiny Barrel", "x/new.png", tags=["polished"])
    assert cat.search("rusty") == [] and cat.search("crate") == []
    assert [h["id"] for h in cat.search("polished")] == ["a1"]
    assert [h["id"] for h in cat.search("barrel")] == ["a1"]
    assert cat.get_asset("a1")["file_path"] == "x/new.png"
    assert cat.delete_asset("a1") is True and cat.search("polished") == [] and cat.get_asset("a1") is None


def test_database_from_before_the_triggers_is_rebuilt(tmp_path):
    db = tmp_path / "old.db"
    cat = AssetCatalog(db)
    cat.register_asset("a1", "Old Name", "x/old.png", tags=["rusty"])
    with sqlite3.connect(db) as conn:  # simulate the old schema: no update trigger, a stale index row
        conn.execute("DROP TRIGGER assets_au")
        conn.execute("DROP TRIGGER assets_ad")
        conn.execute("UPDATE assets SET name = 'Renamed', tags = 'fresh' WHERE id = 'a1'")
    cat = AssetCatalog(db)  # reopening adds the triggers and rebuilds the index
    assert [h["id"] for h in cat.search("fresh")] == ["a1"] and cat.search("rusty") == []
    cat.register_asset("a1", "Again", "x/old.png", tags=["third"])
    assert [h["id"] for h in cat.search("third")] == ["a1"] and cat.search("fresh") == []
