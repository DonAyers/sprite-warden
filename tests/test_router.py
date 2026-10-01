"""Tests for spritewarden.router (playbook sanitization, Jev router, PipelineRouter)."""

import pytest
from pathlib import Path
from spritewarden.router.playbook import sanitize_prompt_for_provider, determine_fallback_recipe
from spritewarden.router.jev import JevClient
from spritewarden.router import PipelineRouter
from spritewarden.repo.catalog import AssetCatalog
from spritewarden.repo.identity import CharacterIdentity


def test_sanitize_prompt_sprite_fusion():
    raw = "cute robot, 32x32, transparent background, pixel-perfect, crisp pixel art"
    cleaned = sanitize_prompt_for_provider(raw, "sprite_fusion")
    assert "transparent background" not in cleaned
    assert "pixel-perfect" not in cleaned
    assert "32x32" not in cleaned
    assert "cute robot" in cleaned


def test_determine_fallback_recipe_vehicle():
    recipe = determine_fallback_recipe(
        subject="tank hull with dual treads",
        asset_type="vehicle",
        size=64,
        is_vehicle_or_rigid=True,
    )
    assert recipe.provider == "sprite_fusion"
    assert recipe.operation == "direction-set"
    assert "clipping" in recipe.suggested_gates


def test_determine_fallback_recipe_walk():
    recipe = determine_fallback_recipe(
        subject="SmolBot",
        asset_type="character",
        is_animation=True,
    )
    assert recipe.provider == "pixellab"
    assert "walk_mechanics" in recipe.suggested_gates
    assert "loop_smoothness" in recipe.suggested_gates


def test_pipeline_router_deduplication(tmp_path: Path):
    db_file = tmp_path / "router_test.db"
    cat = AssetCatalog(db_file)
    cat.register_asset(
        asset_id="smolbot_idle_south",
        name="SmolBot Idle South",
        file_path="assets/sprites/smolbot.png",
        game="i-require-maintenance",
        character_id="smolbot",
        tags=["idle", "south"],
    )

    router = PipelineRouter(catalog=cat)
    # Search for something already in catalog
    result = router.route_request(
        prompt="smolbot idle south",
        game="i-require-maintenance",
        character_id="smolbot",
    )
    assert result.action == "reuse_existing"
    assert result.existing_asset_match is not None

    # Search for something new
    result_new = router.route_request(
        prompt="giant volcanic dragon boss",
        game="i-require-maintenance",
    )
    assert result_new.action == "generate_new"
    assert result_new.recipe is not None
