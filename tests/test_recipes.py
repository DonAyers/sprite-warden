"""Tests for the per-provider recipe registry and its use by the playbook."""

import json

from spritewarden.router.recipes import get_recipe, list_recipes, registry, registry_json
from spritewarden.router.playbook import determine_fallback_recipe, sanitize_prompt_for_provider

REQUIRED = {
    "pixellab": {"create_character_v3", "animate_skeleton", "animate_template", "animate_text", "inpaint", "create_8_direction_object"},
    "spritefusion": {"generate", "edit", "style-reference", "direction-set", "animate"},
    "retrodiffusion": {"rotate", "walking", "idle", "attack", "rd_pro", "pixel_tools"},
    "spritecook": {"generate_game_art", "generate_character", "generate_character_animations", "animate_game_art", "edit"},
    "openrouter_image": {"generate", "snap"},
}


def test_registry_covers_every_provider_operation():
    reg = registry()
    for provider, ops in REQUIRED.items():
        have = {r.operation for r in reg.select(provider)}
        assert ops <= have, f"{provider} missing {ops - have}"
    for rec in list_recipes():
        assert rec.sources, f"{rec.provider}/{rec.operation} has no source URL"
        assert rec.required_gates_after, f"{rec.provider}/{rec.operation} has no gates after"
        for g in rec.required_gates_before + rec.required_gates_after:
            assert g in reg.gate_names, f"unknown gate {g} in {rec.provider}/{rec.operation}"


def test_provider_and_operation_aliases_resolve():
    assert get_recipe("sprite_fusion", "direction_set").operation == "direction-set"
    assert get_recipe("sf", "8dir").operation == "direction-set"
    assert get_recipe("rd", "rd_advanced_animation__rotate").operation == "rotate"
    assert get_recipe("retro_diffusion", "rotate").arg_limits["multiple_of"] == 8
    assert get_recipe("pixellab", "animate_character_v3").operation == "animate_skeleton"
    assert get_recipe("openrouter", "pixel_snap").operation == "snap"
    assert get_recipe("nobody", "x") is None
    assert get_recipe("pixellab", "nope") is None


def test_documented_limits_are_data():
    assert get_recipe("spritecook", "generate_game_art").arg_limits["colors_max"] == 8
    assert "not accepted" in get_recipe("spritefusion", "edit").arg_limits["size"]
    assert get_recipe("spritefusion", "direction-set").arg_limits["size_values"] == [16, 32, 64]
    pl = get_recipe("pixellab", "create_character_v3")
    assert pl.arg_limits["reference_max_px"] == 256
    assert pl.arg_limits["reference_prefer_url_above_px"] == 32
    assert pl.start_image.facing == "south" and pl.start_image.symmetric is True
    assert "start_image" in pl.required_gates_before
    rd = get_recipe("retrodiffusion", "rotate")
    assert rd.start_image.multiple_of == 8 and rd.start_image.margin_px == 3
    assert "equal" in rd.arg_limits["width_height"]


def test_sanitize_reads_registry_patterns():
    # Sprite Fusion also forbids sheet/grid words and counts.
    raw = "cute robot, 32x32, transparent background, pixel-perfect, 8 frames spritesheet, grid"
    assert sanitize_prompt_for_provider(raw, "sprite_fusion") == "cute robot"
    # Retro Diffusion forbids the phrase "pixel art" itself; PixelLab does not.
    assert "pixel art" not in sanitize_prompt_for_provider("robot pixel art", "retro_diffusion")
    assert "pixel art" in sanitize_prompt_for_provider("robot pixel art", "pixellab")
    # Unknown providers are left alone.
    assert sanitize_prompt_for_provider("robot pixel-perfect", "unknown") == "robot pixel-perfect"


def test_fallback_recipe_carries_registry_rules_and_gates():
    char = determine_fallback_recipe("bot", "character")
    assert char.provider == "pixellab" and char.operation == "create_character_v3"
    assert char.required_gates_before == ["start_image"]
    assert "direction_set" in char.suggested_gates
    assert any(r.startswith("Start image: faces south") for r in char.rules)

    veh = determine_fallback_recipe("hull", "vehicle", is_vehicle_or_rigid=True)
    assert veh.required_gates_before == ["start_image"]
    assert "direction_set" in veh.suggested_gates and "clipping" in veh.suggested_gates

    walk = determine_fallback_recipe("bot", "character", is_animation=True)
    assert "walk_mechanics" in walk.suggested_gates and "loop_smoothness" in walk.suggested_gates
    assert "direction_set" in walk.required_gates_before


def test_registry_json_rendering():
    data = json.loads(registry_json())
    assert data["version"] == 1 and len(data["recipes"]) >= 24
    one = json.loads(registry_json("spritefusion", "edit"))
    assert one["operation"] == "edit"
    assert "error" in json.loads(registry_json("spritefusion", "missing"))
