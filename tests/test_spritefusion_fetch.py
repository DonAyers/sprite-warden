"""Tests for the Sprite Fusion account-library import (offline, fake HTTP)."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from spritewarden.gallery.sync import sync_paths
from spritewarden.generator import spritefusion as sf
from spritewarden.generator.base import GenerationOutput
from spritewarden.generator.spritefusion import SpriteFusionDriver
from spritewarden.repo.catalog import AssetCatalog
from spritewarden.repo.provenance import register_imports


def _png_bytes(size: int = 16, color: tuple[int, int, int, int] = (20, 200, 90, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


_ASSETS = {
    "asset_1": {"id": "asset_1", "type": "image", "prompt": "bronze sword icon",
                "assetUrl": "https://media.test/sword.png", "width": 32, "height": 32,
                "createdAt": "2026-01-01T00:00:00Z"},
    "asset_2": {"id": "asset_2", "type": "animation", "prompt": "walk cycle",
                "assetUrl": "https://media.test/walk_preview.png",
                "spritesheetUrl": "https://media.test/walk_sheet.png",
                "frameCount": 6, "fps": 8},
    "asset_3": {"id": "asset_3", "type": "image", "prompt": "wooden shield",
                "assetUrl": "https://media.test/shield.png", "dimensions": [16, 16]},
}


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    """Serves two listing pages, single-asset lookups, and the media downloads."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, params=None):
        if url.endswith("/assets"):
            cursor = (params or {}).get("cursor")
            if cursor == "c2":
                return _FakeResponse(200, {"assets": [_ASSETS["asset_3"]], "hasMore": False, "nextCursor": None})
            return _FakeResponse(200, {"assets": [_ASSETS["asset_1"], _ASSETS["asset_2"]],
                                       "hasMore": True, "nextCursor": "c2"})
        if "/assets/" in url:
            asset_id = url.rstrip("/").split("/")[-1]
            asset = _ASSETS.get(asset_id)
            if asset is None:
                return _FakeResponse(404, text="not found")
            return _FakeResponse(200, {"asset": asset})
        if url.startswith("https://media.test/"):
            seed = sum(ord(c) for c in url)
            color = ((seed * 37) % 255, (seed * 91) % 255, (seed * 53) % 255, 255)
            return _FakeResponse(200, content=_png_bytes(color=color))
        return _FakeResponse(404, text="missing")


@pytest.fixture
def fake_http(monkeypatch):
    monkeypatch.setattr(sf, "httpx", SimpleNamespace(Client=_FakeClient))


def test_fetch_all_paginates_and_downloads(tmp_path: Path, fake_http):
    driver = SpriteFusionDriver(api_key="sf_live_x")
    pages: list[tuple[int, int]] = []
    out = driver.fetch_all(tmp_path / "lib", palette=None, on_page=lambda n, t: pages.append((n, t)))

    assert out.success is True
    assert out.metadata["listed"] == 3
    assert len(out.output_files) == 3
    assert pages == [(2, 2), (1, 3)]

    names = {Path(f).name for f in out.output_files}
    assert "sf_bronze-sword-icon.png" in names
    assert "sf_walk-cycle_sheet.png" in names  # animation prefers the spritesheet
    assert "sf_wooden-shield.png" in names
    for f in out.output_files:
        assert Path(f).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    records = out.metadata["assets"]
    assert [r["id"] for r in records] == ["asset_1", "asset_2", "asset_3"]
    assert records[1]["frame_count"] == 6 and records[1]["fps"] == 8
    assert records[0]["dimensions"] == [32, 32]
    assert records[2]["dimensions"] == [16, 16]


def test_fetch_all_max_items(tmp_path: Path, fake_http):
    driver = SpriteFusionDriver(api_key="sf_live_x")
    out = driver.fetch_all(tmp_path / "lib", max_items=1)
    assert out.metadata["listed"] == 1
    assert len(out.output_files) == 1


def test_fetch_single_asset(tmp_path: Path, fake_http):
    driver = SpriteFusionDriver(api_key="sf_live_x")
    out = driver.fetch_asset("asset_1", tmp_path / "one", palette=None)
    assert out.success is True
    assert out.metadata["asset_ids"] == ["asset_1"]
    assert Path(out.output_files[0]).name == "sf_bronze-sword-icon.png"


def test_unconfigured_fetch_all(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SPRITE_FUSION_API_KEY", raising=False)
    out = SpriteFusionDriver(api_key="").fetch_all(tmp_path)
    assert out.success is False and "SPRITE_FUSION_API_KEY" in out.error


def test_imports_keep_distinct_provider_ids_for_duplicate_bytes(tmp_path: Path):
    image = tmp_path / "same.png"
    image.write_bytes(_png_bytes())
    output = GenerationOutput(
        success=True,
        provider="sprite_fusion",
        output_files=[str(image), str(image)],
        metadata={"assets": [{"id": "asset-a", "type": "image", "name": "first"},
                            {"id": "asset-b", "type": "image", "name": "second"}]},
    )
    cat = AssetCatalog(tmp_path / "cat.db")
    first = register_imports(cat, provider="sprite_fusion", output=output)
    assert len(set(first)) == 2
    assert cat.list_assets(provider="sprite_fusion", limit=10)["total"] == 2

    again = register_imports(cat, provider="sprite_fusion", output=output)
    assert again == first
    assert cat.list_assets(provider="sprite_fusion", limit=10)["total"] == 2


def test_register_imports_and_sync_does_not_duplicate(tmp_path: Path, fake_http):
    driver = SpriteFusionDriver(api_key="sf_live_x")
    out = driver.fetch_all(tmp_path / "lib", palette=None)
    cat = AssetCatalog(tmp_path / "cat.db")
    ids = register_imports(cat, provider="sprite_fusion", output=out, game="gameA")
    assert len(ids) == 3

    page = cat.list_assets(limit=10)
    assert page["total"] == 3
    kinds = {a["provenance"]["kind"] for a in page["assets"]}
    assert kinds == {"import"}
    providers = {a["provenance"]["provider"] for a in page["assets"]}
    assert providers == {"sprite_fusion"}
    assert any(a["asset_type"] == "spritesheet" for a in page["assets"])

    # syncing the same downloaded files must not mint a second local-<sha> row
    report = sync_paths([tmp_path / "lib"], catalog=cat, project_root=tmp_path)
    assert report.already == 3 and report.registered == 0
    assert cat.list_assets(limit=10)["total"] == 3
