"""Tests for the SpriteCook generator driver and pipeline wiring (offline)."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from spritewarden.generator import DRIVER_CLASSES, available_providers, dispatch, get_driver
from spritewarden.generator import spritecook as sc
from spritewarden.generator.spritecook import SpriteCookDriver
from spritewarden.router.playbook import determine_fallback_recipe


def _png_bytes(size: int = 8) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGBA", (size, size), (200, 40, 40, 255))
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    """Minimal httpx.Client stand-in keyed off the request URL."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        if url.endswith("/v1/api/generate-sync"):
            return _FakeResponse(
                200,
                {
                    "assets": [{"asset_id": "asset-1", "sprite_url": "https://cdn.test/a1.png"}],
                    "credits_used": 3,
                    "credits_remaining": 97,
                },
            )
        if url.endswith("/v1/api/animate-sync"):
            return _FakeResponse(
                200,
                {
                    "assets": [
                        {
                            "asset_id": "anim-1",
                            "spritesheet_url": "https://cdn.test/anim1.png",
                            "sprite_url": "https://cdn.test/anim1_preview.png",
                        }
                    ],
                    "credits_used": 5,
                },
            )
        if url.endswith("/v1/api/assets/import"):
            return _FakeResponse(200, {"asset_id": "imported-1"})
        return _FakeResponse(404, text="not found")

    def get(self, url, headers=None):
        if url.startswith("https://cdn.test/"):
            return _FakeResponse(200, content=_png_bytes())
        return _FakeResponse(404, text="missing")


@pytest.fixture
def fake_http(monkeypatch):
    monkeypatch.setattr(sc, "httpx", SimpleNamespace(Client=_FakeClient))


def _recipe(**overrides):
    from spritewarden.router.playbook import GenerationRecipe

    base = dict(
        provider="spritecook",
        operation="generate",
        prompt="a medieval iron sword, side view",
        size=64,
        canvas_size=64,
        perspective="low top-down",
        palette_id="sweetie-16",
    )
    base.update(overrides)
    return GenerationRecipe(**base)


def test_driver_registered():
    assert "spritecook" in available_providers()
    assert DRIVER_CLASSES["spritecook"] is SpriteCookDriver
    assert isinstance(get_driver("spritecook", api_key="k"), SpriteCookDriver)


def test_is_configured(monkeypatch):
    monkeypatch.setattr(sc, "_get_sprite_cook_key", lambda: "")
    assert SpriteCookDriver(api_key="").is_configured() is False
    assert SpriteCookDriver(api_key="sc_live_x").is_configured() is True


def test_apply_palette_hex_list_and_size():
    driver = SpriteCookDriver(api_key="k")
    body: dict = {}
    driver._apply_palette(body, _recipe())
    assert isinstance(body["colors"], list)
    assert len(body["colors"]) == 8  # reviewed API cap, not the local snap palette
    assert all(c.startswith("#") for c in body["colors"])

    anim_body: dict = {}
    driver._apply_palette(anim_body, _recipe(), palette_size=True)
    assert anim_body["colors"] == 8


def test_common_param_mapping():
    driver = SpriteCookDriver(api_key="k")
    recipe = _recipe(
        params={
            "variations": 2,
            "bg_mode": "transparent",
            "smart_crop_mode": "tightest",
            "model": "gpt-image-2",
            "quality": "high",
            "reference_asset_id": "ref-1",
            "style_asset_ids": ["s1", "s2"],
        }
    )
    body = driver._common_params(recipe)
    assert body["variations"] == 2
    assert body["bg_mode"] == "transparent"
    assert body["smart_crop_mode"] == "tightest"
    assert body["model"] == "gpt-image-2"
    assert body["quality"] == "high"
    assert body["reference_asset_id"] == "ref-1"
    assert body["style_asset_ids"] == ["s1", "s2"]


def test_extract_assets_shapes():
    assert sc._extract_assets({"assets": [{"asset_id": "a"}]}) == [{"asset_id": "a"}]
    assert sc._extract_assets({"output": [{"url": "http://x"}]}) == [{"url": "http://x"}]
    assert sc._extract_assets({"data": {"assets": [{"asset_id": "b"}]}}) == [{"asset_id": "b"}]
    assert sc._extract_assets({"asset_id": "c", "sprite_url": "http://y"}) == [
        {"asset_id": "c", "sprite_url": "http://y"}
    ]
    assert sc._extract_assets({"status": "queued"}) == []


def test_asset_url_preference():
    asset = {"sprite_url": "https://a/sprite.png", "spritesheet_url": "https://a/sheet.png"}
    assert sc._asset_url(asset, prefer_sheet=False) == "https://a/sprite.png"
    assert sc._asset_url(asset, prefer_sheet=True) == "https://a/sheet.png"


def test_generate_still_end_to_end(tmp_path, fake_http):
    driver = SpriteCookDriver(api_key="k")
    out = driver.generate(_recipe(), tmp_path)
    assert out.success is True
    assert out.provider == "spritecook"
    assert out.metadata["asset_ids"] == ["asset-1"]
    assert out.metadata["credits_used"] == 3
    assert out.output_files
    assert (tmp_path / "spritecook_events.json").exists()
    for path in out.output_files:
        assert path.endswith("_snapped.png")


def test_generate_animation_prefers_spritesheet(tmp_path, fake_http):
    driver = SpriteCookDriver(api_key="k")
    recipe = _recipe(
        operation="animate_game_art",
        params={"asset_id": "asset-1", "frames": 8},
    )
    out = driver.generate(recipe, tmp_path)
    assert out.success is True
    assert out.metadata["asset_id"] == "asset-1"
    assert out.output_files
    assert all("spritecook_anim" in p for p in out.output_files)


def test_animation_requires_asset_id(tmp_path, fake_http):
    driver = SpriteCookDriver(api_key="k")
    out = driver.generate(_recipe(operation="animate_game_art", params={}), tmp_path)
    assert out.success is False
    assert "asset_id" in out.error


def test_unconfigured_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_get_sprite_cook_key", lambda: "")
    driver = SpriteCookDriver(api_key="")
    out = driver.generate(_recipe(), tmp_path)
    assert out.success is False
    assert "SPRITE_COOK_API_KEY" in out.error


def test_dispatch_unknown_provider(tmp_path):
    out = dispatch(_recipe(provider="does_not_exist"), tmp_path)
    assert out.success is False
    assert "No generator driver" in out.error


def test_icon_routes_to_spritecook():
    recipe = determine_fallback_recipe(
        subject="health potion icon",
        asset_type="icon",
        size=32,
    )
    assert recipe.provider == "spritecook"
    assert recipe.operation == "generate"
    assert "palette_adherence" in recipe.suggested_gates
