"""dispatch refuses a recipe whose input image has not passed its required gates (offline, fake driver)."""

import json
from pathlib import Path

from PIL import Image

from spritewarden.gates import write_record
from spritewarden.generator import registry as gen_registry
from spritewarden.generator.base import BaseGeneratorDriver, GenerationOutput
from spritewarden.router.playbook import GenerationRecipe, determine_fallback_recipe

ASSET = "b4154184-638f-4fd6-8c6d-3ce4ce54cff7"


class _FakeDriver(BaseGeneratorDriver):
    calls: list[str] = []

    def is_configured(self) -> bool:
        return True

    def generate(self, recipe, output_dir):
        _FakeDriver.calls.append(f"{recipe.provider}/{recipe.operation}")
        return GenerationOutput(success=True, provider=recipe.provider, output_files=[])


def _fake_drivers(monkeypatch):
    _FakeDriver.calls = []
    for name in ("pixellab", "sprite_fusion", "spritefusion", "spritecook"):
        monkeypatch.setitem(gen_registry.DRIVER_CLASSES, name, _FakeDriver)


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    for y in range(8, 24):
        for x in range(8, 24):
            img.putpixel((x, y), (200, 60, 60, 255))
    img.save(path)
    return path


def _recipe(provider, operation, **params) -> GenerationRecipe:
    return GenerationRecipe(provider=provider, operation=operation, prompt="p", size=64, canvas_size=64,
                            perspective="low top-down", params=params)


def test_rotation_needs_a_gated_input(tmp_path, monkeypatch):
    _fake_drivers(monkeypatch)
    recipe = determine_fallback_recipe("hull", "vehicle", is_vehicle_or_rigid=True)  # spritefusion direction-set
    assert recipe.required_gates_before == ["start_image"]

    out = gen_registry.dispatch(recipe, tmp_path / "out", project_root=tmp_path)
    assert out.success is False and out.metadata["refused"] == "ungated_input"
    assert "requires a gated input image" in out.error and "--force-ungated" in out.error
    assert _FakeDriver.calls == []

    src = _png(tmp_path / "art" / "hull.png")
    recipe.params["input_image"] = "art/hull.png"
    out = gen_registry.dispatch(recipe, tmp_path / "out", project_root=tmp_path)
    assert out.success is False and "art/hull.png has no gate record" in out.error
    assert "start_image" in out.error and _FakeDriver.calls == []

    write_record(src, {"start_image": True})
    out = gen_registry.dispatch(recipe, tmp_path / "out", project_root=tmp_path)
    assert out.success is True and _FakeDriver.calls == ["sprite_fusion/direction-set"]
    assert "forced_ungated" not in out.metadata


def test_force_ungated_runs_and_records_why(tmp_path, monkeypatch):
    _fake_drivers(monkeypatch)
    _png(tmp_path / "art" / "hull.png")
    recipe = _recipe("sprite_fusion", "direction-set", input_image=str(tmp_path / "art" / "hull.png"))
    out = gen_registry.dispatch(recipe, tmp_path / "out", project_root=tmp_path, force_ungated=True)
    assert out.success is True and _FakeDriver.calls == ["sprite_fusion/direction-set"]
    assert any("no gate record" in r for r in out.metadata["forced_ungated"])


def test_asset_ids_and_pipeline_resolve_like_the_hook(tmp_path, monkeypatch):
    _fake_drivers(monkeypatch)
    bot = tmp_path / "art_source" / "bots" / "b"
    src = _png(bot / "anchor.png")
    manifest = tmp_path / "art_source" / "spritecook-assets.json"
    manifest.write_text(json.dumps({"assets": [{"asset_id": ASSET, "file": "art_source/bots/b/anchor.png"}]}), encoding="utf-8")
    recipe = _recipe("spritecook", "animate", asset_id=ASSET)

    out = gen_registry.dispatch(recipe, tmp_path / "out", project_root=tmp_path)
    assert out.success is False and "anchor.png has no gate record" in out.error

    write_record(src, {"still": True})  # an umbrella gate satisfies it
    assert gen_registry.dispatch(recipe, tmp_path / "out", project_root=tmp_path).success is True

    (bot / "pipeline.json").write_text(json.dumps({"animation_provider": "pixellab"}), encoding="utf-8")
    out = gen_registry.dispatch(recipe, tmp_path / "out", project_root=tmp_path)
    assert out.success is False and "never mix providers inside one set" in out.error

    unknown = _recipe("spritecook", "animate", asset_id="not-registered")
    out = gen_registry.dispatch(unknown, tmp_path / "out", project_root=tmp_path)
    assert out.success is False and "not in art_source/spritecook-assets.json" in out.error


def test_stills_edits_and_unknown_recipes_are_not_gated(tmp_path, monkeypatch):
    _fake_drivers(monkeypatch)
    for recipe in (
        determine_fallback_recipe("gear icon", "icon"),  # spritecook generate
        _recipe("spritecook", "edit", edit_asset_id=ASSET),  # edits create anchors
        _recipe("sprite_fusion", "edit", inputs=["nope.png"]),
        _recipe("pixellab", "create_topdown_tileset"),  # no registry entry
    ):
        out = gen_registry.dispatch(recipe, tmp_path / "out", project_root=tmp_path)
        assert out.success is True, (recipe.operation, out.error)
    assert gen_registry.ungated_reasons(_recipe("spritecook", "generate"), tmp_path) == []
