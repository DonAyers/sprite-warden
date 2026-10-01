"""Tests for spritewarden.repo (identities, palettes, catalog)."""

import pytest
from pathlib import Path
from spritewarden.repo.identity import CharacterIdentity, FeatureLandmark
from spritewarden.repo.palette import PaletteRegistry
from spritewarden.repo.catalog import AssetCatalog


def test_character_identity_format_prompt():
    ident = CharacterIdentity(
        id="smolbot",
        name="SmolBot",
        game="i-require-maintenance",
        prompt_description="small gray cuboid robot with green visor and antenna",
        prohibitions=["no toes", "no organic parts"],
        dimensions=[64, 64],
        ground_row_offset=7,
        features={
            "visor": FeatureLandmark(name="visor", description="glowing green rectangle", rigid=True)
        }
    )
    prompt = ident.format_generation_prompt("mid-stride walk facing south")
    assert "[no toes]" in prompt
    assert "[no organic parts]" in prompt
    assert "small gray cuboid robot" in prompt
    assert "mid-stride walk facing south" in prompt


def test_palette_registry():
    reg = PaletteRegistry()
    assert "luap-40" in reg.list_palettes()
    colors = reg.get("luap-40")
    assert len(colors) >= 40
    assert isinstance(colors[0], tuple)
    assert len(colors[0]) == 3


def test_asset_catalog_search(tmp_path: Path):
    db_file = tmp_path / "test_catalog.db"
    cat = AssetCatalog(db_file)

    ident = CharacterIdentity(
        id="tredbot",
        name="TredBot",
        game="i-require-maintenance",
        prompt_description="heavy tank-tread bot with cannon",
        prohibitions=["no legs"],
    )
    cat.save_identity(ident)

    fetched = cat.get_identity("tredbot")
    assert fetched is not None
    assert fetched.name == "TredBot"

    # Register an asset
    cat.register_asset(
        asset_id="tredbot_walk_down",
        name="TredBot Walk Down",
        file_path="assets/sprites/tredbot_walk.png",
        category="characters",
        asset_type="spritesheet",
        game="i-require-maintenance",
        character_id="tredbot",
        perspective="low top-down",
        frame_size=[96, 96],
        tags=["treads", "heavy", "walk"],
    )

    # Search by query
    hits = cat.search("treads", character_id="tredbot")
    assert len(hits) == 1
    assert hits[0]["id"] == "tredbot_walk_down"

    # Search with no match
    no_hits = cat.search("laser hover", character_id="tredbot")
    assert len(no_hits) == 0
