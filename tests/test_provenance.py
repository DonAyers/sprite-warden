"""Provenance: dispatch and `warden sheet --register` record outputs in the catalog (offline, fake driver)."""

import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image

from spritewarden.generator import registry as gen_registry
from spritewarden.generator.base import BaseGeneratorDriver, GenerationOutput
from spritewarden.repo.catalog import AssetCatalog
from spritewarden.router.playbook import GenerationRecipe


def _png(path: Path, color=(200, 60, 60, 255)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    for y in range(4, 12):
        for x in range(4, 12):
            img.putpixel((x, y), color)
    img.save(path)
    return path


class _WritingDriver(BaseGeneratorDriver):
    def is_configured(self) -> bool:
        return True

    def generate(self, recipe, output_dir):
        out = Path(output_dir)
        # Post-gates execute now: provenance fixtures must be valid on-palette outputs.
        from spritewarden.judge.palette import load_palette
        colors = load_palette(recipe.palette_id)
        files = [str(_png(out / "gear_1.png", tuple(colors[0]) + (255,))),
                 str(_png(out / "gear_2.png", tuple(colors[1]) + (255,)))]
        return GenerationOutput(success=True, provider="spritecook", output_files=files,
                                metadata={"asset_ids": ["sc-asset-1", "sc-asset-2"], "credits_used": 3})


def _recipe(**params) -> GenerationRecipe:
    return GenerationRecipe(provider="spritecook", operation="generate", prompt="brass gear icon", size=32,
                            canvas_size=32, perspective="top-down", params={"mode": "assets", **params})


def test_dispatch_registers_outputs_with_provider_ids_and_params(tmp_path, monkeypatch):
    monkeypatch.setitem(gen_registry.DRIVER_CLASSES, "spritecook", _WritingDriver)
    cat = AssetCatalog(tmp_path / "cat.db")
    recipe = _recipe(blob="A" * 5000)
    out = gen_registry.dispatch(recipe, tmp_path / "out", project_root=tmp_path, catalog=cat, game="irm", character_id="gearbot")
    assert out.success is True
    ids = out.metadata["catalog_asset_ids"]
    assert len(ids) == 2 and all(i.startswith("gen-spritecook-") for i in ids)

    row = cat.get_asset(ids[0])
    assert row["game"] == "irm" and row["character_id"] == "gearbot" and row["asset_type"] == "sprite"
    assert row["frame_w"] == 16 and row["file_path"].endswith("gear_1.png")
    prov = row["provenance"]
    assert prov["provider"] == "spritecook" and prov["operation"] == "generate_game_art" and prov["prompt"] == "brass gear icon"
    assert prov["provider_asset_ids"] == ["sc-asset-1", "sc-asset-2"] and prov["provider_asset_id"] == "sc-asset-1"
    assert cat.get_asset(ids[1])["provenance"]["provider_asset_id"] == "sc-asset-2"
    assert prov["params"]["mode"] == "assets" and len(prov["params"]["blob"]) < 100  # long values are cut
    assert prov["credits_used"] == 3 and len(prov["sha256"]) == 64
    # searchable by provider and by prompt words
    assert {h["id"] for h in cat.search("spritecook gear", game="irm")} == set(ids)

    # the same bytes again update the rows instead of duplicating them
    again = gen_registry.dispatch(recipe, tmp_path / "out", project_root=tmp_path, catalog=cat, game="irm")
    assert again.metadata["catalog_asset_ids"] == ids and len(cat.search("", game="irm", limit=50)) == 2

    # opt out
    silent = gen_registry.dispatch(recipe, tmp_path / "out2", project_root=tmp_path, catalog=cat, register=False)
    assert "catalog_asset_ids" not in silent.metadata


def test_a_broken_catalog_never_fails_the_generation(tmp_path, monkeypatch):
    monkeypatch.setitem(gen_registry.DRIVER_CLASSES, "spritecook", _WritingDriver)

    class _Broken:
        def register_asset(self, **kwargs):
            raise RuntimeError("disk full")

    out = gen_registry.dispatch(_recipe(), tmp_path / "out", project_root=tmp_path, catalog=_Broken())
    assert out.success is True and out.metadata["catalog_error"] == "RuntimeError: disk full"


def test_sheet_register_cli(tmp_path):
    frames = tmp_path / "frames"
    for i in range(4):
        _png(frames / f"walk_{i}.png")
    db = tmp_path / "cat.db"
    sheet = tmp_path / "bot_walk.png"
    env = {**os.environ, "SPRITEWARDEN_DB": str(db)}
    cmd = [sys.executable, "-m", "spritewarden.cli", "sheet", str(frames), "--out", str(sheet), "--no-palette",
           "--engine", "python", "--register", "--game", "irm", "--char-id", "bot", "--json"]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    assert out.returncode == 0, out.stderr + out.stdout
    report = json.loads(out.stdout)[0]
    assert report["catalog_asset_id"] == "sheet-irm-bot-walk"

    row = AssetCatalog(db).get_asset("sheet-irm-bot-walk")
    assert row["asset_type"] == "spritesheet" and row["frame_count"] == 4 and row["character_id"] == "bot"
    assert row["frame_w"] == report["cell_size"] and row["game"] == "irm"
    prov = row["provenance"]
    assert prov["kind"] == "sheet" and len(prov["sources"]) == 4 and prov["manifest"] == report["manifest_path"]

    # without --register nothing is written to the catalog
    plain = [sys.executable, "-m", "spritewarden.cli", "sheet", str(frames), "--out", str(tmp_path / "other.png"),
             "--no-palette", "--engine", "python", "--json"]
    out = subprocess.run(plain, capture_output=True, text=True, encoding="utf-8", env=env)
    assert out.returncode == 0, out.stderr + out.stdout
    assert "catalog_asset_id" not in json.loads(out.stdout)[0]
    assert len(AssetCatalog(db).search("", limit=50)) == 1
