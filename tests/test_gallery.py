"""Tests for the local asset gallery: catalog listing, sync, data layer and HTTP server."""

from __future__ import annotations

import json
import threading

import pytest
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from spritewarden.gallery.data import GalleryData, animation_info
from spritewarden.gallery.server import make_server
from spritewarden.gallery.sync import sync_paths
from spritewarden.gates.records import write_record
from spritewarden.generator import get_driver
from spritewarden.repo.catalog import AssetCatalog
from spritewarden.repo.identity import CharacterIdentity


def _png(path: Path, size: int = 16, color: tuple[int, int, int, int] = (255, 0, 0, 255)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (size, size), color).save(path)
    return path


def test_catalog_list_assets_facets_summary(tmp_path: Path):
    cat = AssetCatalog(tmp_path / "cat.db")
    cat.register_asset("gen-spritecook-aaa", "hero", tmp_path / "a.png", asset_type="sprite",
                       game="gameA", character_id="hero", perspective="low top-down",
                       tags=["generated", "hero"], provenance={"kind": "generation", "provider": "spritecook"})
    cat.register_asset("local-bbb", "tile", tmp_path / "b.png", asset_type="tile",
                       game="gameB", tags=["local"], provenance={"kind": "local"})

    page = cat.list_assets(limit=10)
    assert page["total"] == 2 and len(page["assets"]) == 2

    filtered = cat.list_assets(provider="spritecook")
    assert filtered["total"] == 1 and filtered["assets"][0]["id"] == "gen-spritecook-aaa"
    # locals match on provenance.kind, generated rows on provenance.provider
    assert cat.list_assets(provider="local")["total"] == 1

    assert cat.list_assets(game="gameB")["total"] == 1
    assert cat.list_assets(query="hero")["total"] == 1
    # pagination
    first = cat.list_assets(limit=1, offset=0)
    second = cat.list_assets(limit=1, offset=1)
    assert first["total"] == 2 and second["total"] == 2
    assert {first["assets"][0]["id"], second["assets"][0]["id"]} == {"gen-spritecook-aaa", "local-bbb"}

    facets = cat.facets()
    assert "spritecook" in facets["providers"] and "local" in facets["providers"]
    assert facets["game"] == ["gameA", "gameB"]

    summary = cat.summary()
    assert summary["assets"] == 2
    assert summary["by_type"] == {"sprite": 1, "tile": 1}
    assert summary["by_provider"]["spritecook"] == 1


def test_catalog_projects_and_asset_metadata_updates(tmp_path: Path):
    cat = AssetCatalog(tmp_path / "cat.db")
    cat.register_asset("asset-a", "hero", tmp_path / "hero.png", game="old-game", tags=["old"])
    cat.save_identity(CharacterIdentity(id="hero-id", name="Hero", game="new-game", prompt_description="bot"))

    projects = cat.list_projects()
    assert any(p["slug"] == "old-game" and p["asset_count"] == 1 for p in projects)
    assert any(p["slug"] == "new-game" and p["name"] == "New Game" for p in projects)
    created = cat.create_project("curated-project", "Curated Project", "prototype")
    assert created["slug"] == "curated-project"
    assert cat.create_project("curated-project", "Curated Project", "prototype")["name"] == "Curated Project"

    assert cat.update_asset_metadata("asset-a", tags=["Blue Robot", "blue-robot", "sprite"],
                                     game="curated-project", character_id="hero-id", category="characters")
    updated = cat.get_asset("asset-a")
    assert updated["game"] == "curated-project" and updated["character_id"] == "hero-id"
    assert updated["category"] == "characters" and updated["tags"] == "blue-robot sprite"
    assert cat.search("blue-robot", game="curated-project")[0]["id"] == "asset-a"
    assert next(p for p in cat.list_projects() if p["slug"] == "curated-project")["asset_count"] == 1

    with pytest.raises(ValueError, match="unknown project"):
        cat.update_asset_metadata("asset-a", tags=[], game="missing", character_id=None, category="x")
    with pytest.raises(ValueError, match="slug"):
        cat.create_project("Not A Slug", "Bad")


def test_sync_indexes_project(tmp_path: Path):
    project = tmp_path / "proj"
    bot = project / "art" / "bildbot"
    south = _png(bot / "south.png")
    _png(bot / "_raw" / "east.png", color=(0, 0, 255, 255))
    (project / "spritewarden.json").write_text(json.dumps({"manifest": "art/ids.json"}), encoding="utf-8")
    (bot / "pipeline.json").write_text(json.dumps({
        "bot": "bildbot", "rotation_provider": "spritefusion", "anchor": "art/bildbot/south.png",
    }), encoding="utf-8")
    write_record(south, {"start_image": {"passed": True, "summary": "ok"}, "pixel_grid": {"passed": True}})

    cat = AssetCatalog(tmp_path / "cat.db")
    report = sync_paths([project], catalog=cat, project_root=project, recursive=True, category="characters")

    assert report.scanned == 2
    assert report.registered == 2
    assert report.with_record == 1
    assert {f["bot"] for f in report.files} == {"bildbot"}
    gated = [f for f in report.files if f["path"].endswith("south.png")][0]
    assert "start_image" in gated["gates"] and not gated["rejected"]

    page = cat.list_assets(limit=10)
    assert page["total"] == 2
    south_asset = next(a for a in page["assets"] if a["name"] == "south")
    assert south_asset["provenance"]["gates"] == ["pixel_grid", "start_image"]
    assert south_asset["provenance"]["pipeline"]["rotation_provider"] == "spritefusion"

    # idempotent: a second run updates, does not duplicate
    again = sync_paths([project], catalog=cat, project_root=project, recursive=True)
    assert again.registered == 0 and again.updated == 2
    assert cat.list_assets(limit=10)["total"] == 2

    cat.create_project("curated", "Curated")
    cat.update_asset_metadata(south_asset["id"], tags=["favorite", "needs-review"],
                              game="curated", character_id=None, category="reference")
    sync_paths([project], catalog=cat, project_root=project, recursive=True)
    saved = cat.get_asset(south_asset["id"])
    assert saved["game"] == "curated" and saved["category"] == "reference"
    assert saved["tags"] == "favorite needs-review"
    assert saved["provenance"]["gallery_metadata"]["edited"] is True


@pytest.mark.parametrize("explicit_paths", [False, True])
def test_sync_preserves_cleared_gallery_organization(tmp_path: Path, explicit_paths):
    project = tmp_path / "proj"
    bot = project / "bot"
    _png(bot / "south.png")
    (bot / "pipeline.json").write_text(json.dumps({"bot": "hero"}), encoding="utf-8")
    cat = AssetCatalog(tmp_path / "cat.db")
    cat.save_identity(CharacterIdentity(id="hero", name="Hero", game="inferred", prompt_description="bot"))
    paths = [project] if explicit_paths else None
    first = sync_paths(paths, catalog=cat, project_root=project)
    asset_id = first.files[0]["asset_id"]
    assert cat.get_asset(asset_id)["character_id"] == "hero"
    assert cat.get_asset(asset_id)["game"] == "inferred"
    # An unedited asset continues to pick up inference on resync.
    cat.save_identity(CharacterIdentity(id="hero", name="Hero", game="changed", prompt_description="bot"))
    sync_paths(paths, catalog=cat, project_root=project)
    assert cat.get_asset(asset_id)["game"] == "changed"
    cat.create_project("curated", "Curated")
    cat.update_asset_metadata(asset_id, tags=["favorite"], game="curated", character_id="hero", category="reference")
    sync_paths(paths, catalog=cat, project_root=project)
    assert cat.get_asset(asset_id)["game"] == "curated"
    cat.update_asset_metadata(asset_id, tags=[], game="", character_id=None, category="reference")
    for _ in range(2):
        sync_paths(paths, catalog=cat, project_root=project)
        saved = cat.get_asset(asset_id)
        assert saved["game"] == ""
        assert saved["character_id"] is None
        assert saved["tags"] == ""
        assert saved["category"] == "reference"
        assert saved["provenance"]["gallery_metadata"]["edited"] is True


def test_gallery_data_layer(tmp_path: Path):
    project = tmp_path / "proj"
    bot = project / "art" / "bildbot"
    south = _png(bot / "south.png")
    (project / "spritewarden.json").write_text(json.dumps({"rejections": "art/rejections.jsonl"}), encoding="utf-8")
    (bot / "pipeline.json").write_text(json.dumps({"bot": "bildbot"}), encoding="utf-8")
    (project / "art" / "rejections.jsonl").write_text(
        json.dumps({"time": "2026-01-01T00:00:00+00:00", "by": "gate", "image": "art/bildbot/east.png",
                    "provider": "spritefusion", "stage": "rotation",
                    "failed": [{"name": "vision_reference", "errors": ["invented: hat"]}]}) + "\n",
        encoding="utf-8",
    )

    cat = AssetCatalog(tmp_path / "cat.db")
    cat.save_identity(CharacterIdentity(
        id="bildbot", name="BildBot", game="gameA", prompt_description="a bot"))
    sync_paths([project], catalog=cat, project_root=project)

    data = GalleryData(catalog=cat, project_root=project)
    assert data.health()["ok"] is True
    assert data.summary()["assets"] == 1
    assert data.facets()["providers"]

    page = data.assets(limit=10)
    assert page["total"] == 1 and page["assets"][0]["exists"] is True
    assert "relative_path" in page["assets"][0]

    identity = data.identities()
    assert identity and identity[0]["id"] == "bildbot"

    pipelines = data.pipelines()
    assert pipelines and pipelines[0]["bot"] == "bildbot" and pipelines[0]["status"] is not None

    rejections = data.rejections()
    assert rejections["count"] == 1
    assert rejections["groups"][0]["what"] == "vision_reference"

    providers = data.providers()
    assert any(p["name"] == "pixellab" for p in providers["providers"])
    assert providers["pipeline_rules"]


def test_animation_metadata_and_filtered_sequence(tmp_path: Path):
    cat = AssetCatalog(tmp_path / "cat.db")
    cat.register_asset(
        "anim-walk", "walking cycle", tmp_path / "walk.png", category="animations",
        asset_type="spritesheet", game="game-a", frame_size=[240, 59], frame_count=8,
        provenance={"kind": "import", "provider": "sprite_fusion", "asset_type": "animation",
                    "dimensions": [30, 59], "frame_count": 8, "fps": 8},
    )
    cat.register_asset(
        "still-idle", "idle pose", tmp_path / "idle.png", game="game-a",
        provenance={"kind": "import", "provider": "sprite_fusion"},
    )
    cat.register_asset(
        "anim-other", "other walk", tmp_path / "other.png", asset_type="spritesheet",
        game="game-b", frame_size=[64, 64], frame_count=10,
        provenance={"kind": "sheet", "columns": 4, "rows": 3},
    )

    sf_animation = animation_info(cat.get_asset("anim-walk"))
    assert sf_animation == {
        "frame_count": 8, "frame_width": 30, "frame_height": 59,
        "columns": 8, "rows": 1, "fps": 8,
    }
    grid_animation = animation_info(cat.get_asset("anim-other"))
    assert grid_animation["columns"] == 4 and grid_animation["rows"] == 3
    assert animation_info(cat.get_asset("still-idle")) is None

    sequence = GalleryData(catalog=cat, project_root=tmp_path).asset_sequence(query="walk", game="game-a")
    assert sequence["total"] == 1 and not sequence["truncated"]
    assert sequence["assets"][0]["id"] == "anim-walk"
    assert sequence["assets"][0]["animation"]["frame_count"] == 8


def _get(url: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def _json_request(url: str, method: str, payload: dict, base: str, token: str | None = None):
    headers = {"Content-Type": "application/json", "Origin": base}
    if token:
        headers["X-CSRF-Token"] = token
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_server_serves_api_and_images(tmp_path: Path):
    project = tmp_path / "proj"
    bot = project / "art" / "bildbot"
    _png(bot / "south.png", size=32)
    (project / "spritewarden.json").write_text("{}", encoding="utf-8")
    (bot / "pipeline.json").write_text(json.dumps({"bot": "bildbot"}), encoding="utf-8")

    cat = AssetCatalog(tmp_path / "cat.db")
    sync_paths([project], catalog=cat, project_root=project)
    cat.save_identity(CharacterIdentity(id="south-id", name="South", game="global", prompt_description="bot"))
    data = GalleryData(catalog=cat, project_root=project)
    httpd = make_server(data, "127.0.0.1", 0, quiet=True)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        status, body, ctype = _get(base + "/")
        assert status == 200 and b"sprite-warden gallery" in body and "text/html" in ctype

        status, body, _ = _get(base + "/static/app.js")
        assert status == 200 and b"loadAssets" in body
        assert b'onerror=' not in body
        assert b'addEventListener("error"' in body
        assert b"startFramePlayback" in body and b"startSequence" in body
        assert b"initViewSettings" in body and b"match-pixels" in body
        home = _get(base + "/")[1]
        assert b"grid-columns" in home and b"grid-rows" in home and b"image-preview" in home
        sequence = json.loads(_get(base + "/api/sequence?game=global")[1])
        assert sequence["total"] == 1 and sequence["assets"][0]["animation"] is None

        summary = json.loads(_get(base + "/api/summary")[1])
        assert summary["assets"] == 1

        assets = json.loads(_get(base + "/api/assets?limit=10")[1])
        assert assets["total"] == 1
        asset_id = assets["assets"][0]["id"]

        session = json.loads(_get(base + "/api/session")[1])
        token = session["csrf_token"]
        status, denied = _json_request(base + "/api/projects", "POST",
                                       {"name": "Denied", "slug": "denied"}, base)
        assert status == 403 and "CSRF" in denied["error"]

        status, project = _json_request(base + "/api/projects", "POST",
                                        {"name": "QA Project", "slug": "qa-project"}, base, token)
        assert status == 201 and project["slug"] == "qa-project"
        assert any(p["slug"] == "qa-project" for p in json.loads(_get(base + "/api/projects")[1]))

        status, updated = _json_request(base + f"/api/assets/{asset_id}", "PATCH",
                                        {"tags": ["blue bot", "approved"], "project": "qa-project",
                                         "character_id": "south-id", "category": "characters"}, base, token)
        assert status == 200
        assert updated["game"] == "qa-project" and updated["character_id"] == "south-id"
        assert updated["category"] == "characters" and updated["tags"] == "blue-bot approved"
        assert json.loads(_get(base + "/api/assets?q=blue-bot")[1])["total"] == 1

        one = json.loads(_get(base + f"/api/assets/{asset_id}")[1])
        assert one["name"] == "south"

        status, body, ctype = _get(base + f"/img/{asset_id}")
        assert status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n" and "image/png" in ctype

        status, body, ctype = _get(base + f"/thumb/{asset_id}?size=64")
        assert status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n"

        assert json.loads(_get(base + "/api/pipelines")[1])[0]["bot"] == "bildbot"
        assert _get(base + "/api/nope")[0] == 404
        assert _get(base + "/static/../server.py")[0] in (403, 404)
        assert _get(base + "/img/does-not-exist")[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_fetch_unsupported_provider(tmp_path: Path):
    driver = get_driver("openrouter_image")
    assert driver is not None
    out = driver.fetch_asset("some-id", tmp_path)
    assert out.success is False
    assert "does not support fetch-by-id" in (out.error or "")
    all_out = driver.fetch_all(tmp_path)
    assert all_out.success is False
    assert "does not expose an account asset listing" in (all_out.error or "")
