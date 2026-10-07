"""Enforcement core: tool call -> (provider, operation) -> recipe rules, gates, pipeline, override.

Fixtures are built in tmp_path; nothing here reads the game repo or calls a provider.
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from spritewarden.enforce import RULE_KINDS, check_call, find_project_root, load_config
from spritewarden.enforce.core import FACING_STEMS, load_raw_registry
from spritewarden.gates import write_record

PL = "mcp__pixellab__"
SC = "mcp__plugin_spritecook_spritecook__"
ASSET = "b4154184-638f-4fd6-8c6d-3ce4ce54cff7"
OTHER = "11111111-2222-3333-4444-555555555555"


def _png(path: Path, color=(200, 60, 60, 255), size=32, box=(8, 8, 24, 24)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            img.putpixel((x, y), color)
    img.save(path)
    return path


def _gated(path: Path, gate="start_image", **kw) -> Path:
    _png(path, **kw)
    write_record(path, {gate: True})
    return path


def _manifest(root: Path, data: dict) -> None:
    p = root / "art_source" / "spritecook-assets.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def _bash(command: str) -> dict:
    return {"command": command}


# ------------------------------------------------------------------ the game hook's --test list

def test_ported_hook_cases(tmp_path):
    root = tmp_path
    assert check_call(PL + "get_balance", {}, root).decision == "ignore"

    v = check_call(PL + "create_character", {"reference_image_base64": "AAAA"}, root)
    assert v.decision == "deny" and (v.provider, v.operation) == ("pixellab", "create_character_v3")
    assert "base64" in v.reasons[0] and "URL" in v.reasons[0]

    v = check_call(SC + "generate_game_art", {"colors": ["#000000"] * 47}, root)
    assert v.decision == "deny" and "47 entries" in v.reasons[0] and "at most 8" in v.reasons[0]

    v = check_call(SC + "animate_game_art", {"asset_id": "nope", "prompt": "rolls forward, looping",
                                             "model": "pixel-engine-v1.5", "auto_enhance_prompt": False}, root)
    assert v.decision == "deny" and v.operation == "animate_game_art"
    text = " | ".join(v.reasons)
    assert "v1.1" in text and "enhancer" in text and "not in art_source/spritecook-assets.json" in text

    assert check_call(SC + "check_job_status", {}, root).decision == "ignore"

    v = check_call("Bash", _bash('python tools/sprite_fusion.py edit --input a.png --size 64 --prompt "crisp pixel art 64x64"'), root)
    assert v.decision == "deny" and (v.provider, v.operation) == ("spritefusion", "edit")
    text = " | ".join(v.reasons)
    assert "rejects --size" in text and '"crisp pixel art"' in text and "pixel dimensions" in text

    v = check_call("Bash", _bash("python tools/sprite_fusion.py direction-set --input art_source/bots/bildbot/sc/v3/sq96/base_1.png --size 64 --out x.png"), root)
    assert v.decision == "deny" and v.operation == "direction-set"
    assert "source image not found locally: art_source/bots/bildbot/sc/v3/sq96/base_1.png" in v.reasons[0]

    v = check_call("Bash", _bash('python tools/retro_diffusion.py generate --style rd_advanced_animation__rotate --input a.png --size 90x90 --prompt "pixel art robot"'), root)
    assert v.decision == "deny" and (v.provider, v.operation) == ("retrodiffusion", "rotate")
    text = " | ".join(v.reasons)
    assert "multiples of 8" in text and '"pixel art"' in text and "not found locally: a.png" in text

    assert check_call("Bash", _bash("python tools/sprite_fusion.py credits"), root).decision == "ignore"


# ------------------------------------------------------------------ classification

@pytest.mark.parametrize("tool,inp", [
    ("Read", {"file_path": "x"}),
    ("Bash", {"command": "git status"}),
    ("Bash", {"command": "python tools/retro_diffusion.py balance"}),
    ("Bash", {"command": "python tools/retro_diffusion.py styles --json"}),
    ("Bash", {"command": "python tools/sprite_fusion.py --help"}),
    ("PowerShell", {"command": "warden recipes spritecook"}),
    ("Bash", {"command": "warden judge-still a.png"}),
    ("mcp__tipshare__search", {"query": "x"}),
    (PL + "list_characters", {}),
    (PL + "wait_for_jobs", {}),
    (PL + "delete_character", {"character_id": "c"}),
    (PL + "sandbox_bash", {"command": "ls"}),
    (SC + "get_credit_balance", {}),
    (SC + "list_recent_assets", {}),
    (SC + "update_asset_label", {"asset_id": "x"}),
    (SC + "finalize_asset_upload", {}),
    (SC + "create_asset_upload", {}),
    (SC + "check_character_animation_run", {}),
    ("Artifact", {"action": "list"}),
    ("Bash", None),
])
def test_not_spending_calls_are_ignored(tmp_path, tool, inp):
    assert check_call(tool, inp, tmp_path).decision == "ignore"


def test_tool_to_provider_operation_mapping(tmp_path):
    cases = [
        (PL + "animate_with_skeleton_v3", {}, ("pixellab", "animate_skeleton")),
        (PL + "animate_character", {}, ("pixellab", "animate_template")),
        (PL + "animate_object", {}, ("pixellab", "animate_text")),
        (PL + "inpaint_image_pro_flash", {}, ("pixellab", "inpaint")),
        (PL + "create_character_state", {}, ("pixellab", "create_character_state")),
        (PL + "create_image_pixflux", {}, ("pixellab", "create_image_pixflux")),
        (SC + "generate_game_art", {"prompt": "robot"}, ("spritecook", "generate_game_art")),
        (SC + "generate_game_art", {"prompt": "robot", "edit_asset_id": ASSET}, ("spritecook", "edit")),
        (SC + "generate_character", {}, ("spritecook", "generate_character")),
        ("mcp__spritecook__remove_background", {}, ("spritecook", "remove_background")),
        ("Bash", _bash("python tools/retro_diffusion.py generate --style rd_pro__topdown --prompt robot"), ("retrodiffusion", "rd_pro")),
        ("Bash", _bash("python tools/retro_diffusion.py generate --style rd_pro__fantasy --prompt robot"), ("retrodiffusion", "rd_pro")),
        ("Bash", _bash("python tools/sprite_fusion.py style-reference --prompt robot"), ("spritefusion", "style-reference")),
        ("PowerShell", _bash("python tools\\sprite_fusion.py generate --prompt 'a robot'"), ("spritefusion", "generate")),
        ("Bash", _bash("cd repo && python C:/x/tools/sprite_fusion.py generate --prompt robot | tee log"), ("spritefusion", "generate")),
        ("Bash", _bash('warden generate "a crate" --type props --provider sc'), ("spritecook", "generate_game_art")),
        ("Bash", _bash('sprite-warden generate "a crate"'), ("spritewarden", "generate")),
        ("Bash", _bash('.venv/Scripts/python.exe -m spritewarden.cli generate "a crate" --provider openrouter'), ("openrouter_image", "generate")),
    ]
    for tool, inp, expected in cases:
        v = check_call(tool, inp, tmp_path)
        assert v.decision == "allow", (tool, inp, v.reasons)
        assert (v.provider, v.operation) == expected


@pytest.mark.parametrize("style,operation", [
    ("rd_advanced_animation__walking", "walking"),
    ("rd_advanced_animation__idle", "idle"),
    ("rd_advanced_animation__attack", "attack"),
    ("rd_advanced_animation__custom_action", "attack"),
])
def test_retro_diffusion_styles_map_to_operations(tmp_path, style, operation):
    src = _gated(tmp_path / "a.png")
    v = check_call("Bash", _bash(f"python tools/retro_diffusion.py generate --style {style} --input a.png --size 64x64 --prompt strides"), tmp_path)
    assert (v.decision, v.provider, v.operation) == ("allow", "retrodiffusion", operation), v.reasons
    assert src.is_file()


def test_allow_context_is_the_recipe(tmp_path):
    v = check_call(SC + "generate_game_art", {"prompt": "small robot, low top-down"}, tmp_path)
    assert v.decision == "allow" and v.reasons == []
    assert "spritecook / generate_game_art" in v.context
    assert "Mention the view angle" in v.context  # operation rule
    assert "One-line prompts" in v.context  # provider rule
    assert len(v.context) < 2600
    _gated(tmp_path / "s.png")
    v = check_call("Bash", _bash("python tools/retro_diffusion.py generate --style rd_advanced_animation__rotate --input s.png --size 64"), tmp_path)
    assert v.decision == "allow" and "Start image: faces south" in v.context and "native-resolution" in v.context


# ------------------------------------------------------------------ rule kinds

def test_every_registry_rule_kind_is_implemented():
    reg = load_raw_registry()
    used = set()
    for prov in reg["providers"].values():
        blocks = [prov.get("enforce") or {}] + [(e.get("enforce") or {}) for e in prov["operations"].values()]
        for block in blocks:
            for rule in block.get("rules", []):
                used.add(rule["kind"])
    assert used == set(RULE_KINDS)


def test_max_items(tmp_path):
    ok = check_call(SC + "generate_game_art", {"colors": ["#111111"] * 8}, tmp_path)
    assert ok.decision == "allow"
    assert check_call(SC + "generate_character", {"colors": ["#111111"] * 9}, tmp_path).decision == "deny"


def test_forbid_args_and_enum(tmp_path):
    _gated(tmp_path / "a.png")
    assert check_call("Bash", _bash("python tools/sprite_fusion.py edit --input a.png --prompt 'add a hat'"), tmp_path).decision == "allow"
    v = check_call("Bash", _bash("python tools/sprite_fusion.py edit --input a.png --size=64 --prompt 'add a hat'"), tmp_path)
    assert v.decision == "deny" and "rejects --size" in v.reasons[0]
    v = check_call("Bash", _bash("python tools/sprite_fusion.py animate --input a.png --size 64 --prompt 'rolls'"), tmp_path)
    assert v.decision == "deny" and "`animate` keeps the input's size" in v.reasons[0]

    v = check_call("Bash", _bash("python tools/sprite_fusion.py direction-set --input a.png --size 96"), tmp_path)
    assert v.decision == "deny" and "16, 32 or 64" in v.reasons[0] and "96" in v.reasons[0]
    for size in ("16", "32", "64"):
        assert check_call("Bash", _bash(f"python tools/sprite_fusion.py direction-set --input a.png --size {size}"), tmp_path).decision == "allow"


def test_multiple_of(tmp_path):
    _gated(tmp_path / "a.png")
    cmd = "python tools/retro_diffusion.py generate --style rd_advanced_animation__walking --input a.png --size {}"
    assert check_call("Bash", _bash(cmd.format("96x96")), tmp_path).decision == "allow"
    v = check_call("Bash", _bash(cmd.format("96x90")), tmp_path)
    assert v.decision == "deny" and "multiples of 8" in v.reasons[0]
    # rd_pro stills are not bound to the multiple-of-8 rule
    assert check_call("Bash", _bash("python tools/retro_diffusion.py generate --style rd_pro__default --size 90x90 --prompt robot"), tmp_path).decision == "allow"


@pytest.mark.parametrize("arg", ["reference_image_base64", "custom_start_frame_base64", "end_frame_base64"])
def test_forbid_inline_image_args(tmp_path, arg):
    v = check_call(PL + "animate_with_skeleton_v3", {arg: "iVBOR"}, tmp_path)
    assert v.decision == "deny" and arg in v.reasons[0]


def test_forbidden_words_on_each_prompt_argument(tmp_path):
    v = check_call(PL + "create_image_pixflux", {"description": "a robot, transparent background, 16-bit"}, tmp_path)
    assert v.decision == "deny" and len(v.reasons) == 2
    assert '"transparent background"' in v.reasons[0] and "`description`" in v.reasons[0]
    v = check_call(PL + "animate_character", {"action_description": "walks, high quality"}, tmp_path)
    assert v.decision == "deny" and "`action_description`" in v.reasons[0]
    v = check_call(SC + "generate_game_art", {"prompt": "robot, pixel-perfect"}, tmp_path)
    assert v.decision == "deny"
    # "pixel art" is Retro Diffusion's word only
    assert check_call(SC + "generate_game_art", {"prompt": "pixel art robot"}, tmp_path).decision == "allow"
    v = check_call("Bash", _bash('python tools/retro_diffusion.py generate --style rd_pro__default --prompt "pixel art robot"'), tmp_path)
    assert v.decision == "deny" and "Retro Diffusion" in v.reasons[0]
    v = check_call("Bash", _bash('python tools/sprite_fusion.py generate --prompt "robot spritesheet"'), tmp_path)
    assert v.decision == "deny" and '"spritesheet"' in v.reasons[0]


def test_no_dimensions_in_prompt(tmp_path):
    for prompt in ("a 64x64 robot", "a robot 48 px tall"):
        v = check_call("Bash", _bash(f'python tools/sprite_fusion.py generate --prompt "{prompt}"'), tmp_path)
        assert v.decision == "deny" and "pixel dimensions" in v.reasons[0], prompt
    assert check_call("Bash", _bash('python tools/sprite_fusion.py generate --prompt "a robot with 2 arms"'), tmp_path).decision == "allow"


def _sc_project(root: Path, color=(200, 60, 60, 255)) -> Path:
    src = _gated(root / "art" / "anchor.png", color=color)
    _manifest(root, {"_about": "ids", "assets": [{"asset_id": ASSET, "file": "art/anchor.png"}]})
    return src


def test_loop_engine_and_require_arg_value(tmp_path):
    _sc_project(tmp_path)
    base = {"asset_id": ASSET, "matte_color": "#FF00FF"}
    v = check_call(SC + "animate_game_art", {**base, "prompt": "walking forward", "model": "pixel-engine-v1.5"}, tmp_path)
    assert v.decision == "deny" and "v1.1" in v.reasons[0]
    assert check_call(SC + "animate_game_art", {**base, "prompt": "walking forward", "model": "pixel-engine-v1.1"}, tmp_path).decision == "allow"
    # a one-angle action on v1.5 is the documented use; "trunk" is not "run"
    assert check_call(SC + "animate_game_art", {**base, "prompt": "swings the trunk", "model": "pixel-engine-v1.5"}, tmp_path).decision == "allow"

    v = check_call(SC + "animate_game_art", {**base, "prompt": "swings", "auto_enhance_prompt": False}, tmp_path)
    assert v.decision == "deny" and "enhancer ON" in v.reasons[0]
    assert check_call(SC + "animate_game_art", {**base, "prompt": "swings", "auto_enhance_prompt": True}, tmp_path).decision == "allow"


def test_matte_collision_default_grey_on_a_grey_sprite(tmp_path):
    _sc_project(tmp_path, color=(124, 130, 134, 255))  # within 24 of #808080
    v = check_call(SC + "animate_game_art", {"asset_id": ASSET, "prompt": "swings"}, tmp_path)
    assert v.decision == "deny" and len(v.reasons) == 1
    assert "matte colour #808080" in v.reasons[0] and "100.0%" in v.reasons[0]
    assert "pass a matte colour absent from the sprite" in v.reasons[0] and "#FF00FF" in v.reasons[0]
    # an explicit matte the sprite does not contain is fine; so is no background removal
    assert check_call(SC + "animate_game_art", {"asset_id": ASSET, "prompt": "swings", "matte_color": "#FF00FF"}, tmp_path).decision == "allow"
    assert check_call(SC + "animate_game_art", {"asset_id": ASSET, "prompt": "swings", "removebg": "None"}, tmp_path).decision == "allow"
    # an explicit matte that IS in the sprite collides too
    v = check_call(SC + "animate_game_art", {"asset_id": ASSET, "prompt": "swings", "matte_color": "#7c8286"}, tmp_path)
    assert v.decision == "deny"

    v = check_call(SC + "generate_character_animations", {"character_id": ASSET}, tmp_path)
    assert v.decision == "deny" and "use animate_game_art with an explicit matte_color" in v.reasons[0]


def test_matte_collision_threshold_is_two_percent(tmp_path):
    src = tmp_path / "art" / "anchor.png"
    _png(src, color=(200, 60, 60, 255), size=32, box=(0, 0, 20, 20))  # 400 opaque px
    img = Image.open(src).convert("RGBA")
    for i in range(8):  # 8 / 400 = 2.0% -> not MORE than 2%
        img.putpixel((i, 0), (128, 128, 128, 255))
    img.save(src)
    write_record(src, {"start_image": True})
    _manifest(tmp_path, {"assets": [{"asset_id": ASSET, "file": "art/anchor.png"}]})
    assert check_call(SC + "animate_game_art", {"asset_id": ASSET, "prompt": "swings"}, tmp_path).decision == "allow"
    img.putpixel((8, 0), (128, 128, 128, 255))  # 9 / 400 = 2.25%
    img.save(src)
    write_record(src, {"start_image": True})
    v = check_call(SC + "animate_game_art", {"asset_id": ASSET, "prompt": "swings"}, tmp_path)
    assert v.decision == "deny" and "2.2%" in v.reasons[0]


# ------------------------------------------------------------------ gates, manifest, pipeline

def test_cli_input_must_be_gated_and_current(tmp_path):
    src = _png(tmp_path / "art" / "a.png")
    cmd = _bash("python tools/sprite_fusion.py direction-set --input art/a.png --size 64")
    v = check_call("Bash", cmd, tmp_path)
    assert v.decision == "deny" and "art/a.png has no gate record" in v.reasons[0]

    write_record(src, {"start_image": False, "still": False})
    v = check_call("Bash", cmd, tmp_path)
    assert v.decision == "deny" and "no current passed gate among start_image" in v.reasons[0]

    write_record(src, {"start_image": True})
    assert check_call("Bash", cmd, tmp_path).decision == "allow"

    _png(src, color=(10, 200, 10, 255))  # the image changed after it was gated
    v = check_call("Bash", cmd, tmp_path)
    assert v.decision == "deny" and "changed since it was gated" in v.reasons[0]

    # clean pixels are not anchor readiness: `still` (or `anim`) does not stand in for start_image
    for other in ("still", "anim", "views"):
        write_record(src, {other: True})
        v = check_call("Bash", cmd, tmp_path)
        assert v.decision == "deny" and "no current passed gate among start_image" in v.reasons[0], other
    # ... but an animation may start from a gated view of the set
    write_record(src, {"views": True})
    assert check_call("Bash", _bash("python tools/retro_diffusion.py generate --style rd_advanced_animation__walking --input art/a.png --size 32"), tmp_path).decision == "allow"
    write_record(src, {"still": True})
    assert check_call("Bash", _bash("python tools/retro_diffusion.py generate --style rd_advanced_animation__walking --input art/a.png --size 32"), tmp_path).decision == "deny"
    # a person's "no" outlives every gate result
    from spritewarden.gates.records import mark_rejected

    write_record(src, {"start_image": True})
    mark_rejected(src, by="don", reason="orbs on the head")
    v = check_call("Bash", cmd, tmp_path)
    assert v.decision == "deny" and "rejected by don (orbs on the head)" in v.reasons[0]
    write_record(src, {"start_image": True})  # judging the same bytes again does not undo it
    assert check_call("Bash", cmd, tmp_path).decision == "deny"
    # edits create anchors: their input is not gated
    _png(tmp_path / "raw.png")
    assert check_call("Bash", _bash("python tools/sprite_fusion.py edit --input raw.png --prompt 'add a hat'"), tmp_path).decision == "allow"


def test_spritecook_ids_gate_only_animations(tmp_path):
    _png(tmp_path / "art" / "anchor.png")  # registered but NOT gated
    _manifest(tmp_path, {"assets": [{"asset_id": ASSET, "file": "art/anchor.png"}], "single": {"asset_id": OTHER, "file": "art/other.png"}})
    # stills and edits from an un-gated asset are allowed
    assert check_call(SC + "generate_game_art", {"prompt": "robot", "edit_asset_id": ASSET}, tmp_path).decision == "allow"
    assert check_call(SC + "generate_game_art", {"prompt": "robot", "asset_id": ASSET}, tmp_path).decision == "allow"
    # animations are not
    v = check_call(SC + "animate_game_art", {"asset_id": ASSET, "prompt": "swings", "matte_color": "#FF00FF"}, tmp_path)
    assert v.decision == "deny" and "art/anchor.png has no gate record" in v.reasons[0]
    v = check_call(SC + "generate_character_animations", {"character_id": ASSET}, tmp_path)
    assert v.decision == "deny" and any("no gate record" in r for r in v.reasons)
    # a dict-valued manifest key holds an entry as well; its file is missing
    v = check_call(SC + "animate_game_art", {"asset_id": OTHER, "prompt": "swings"}, tmp_path)
    assert v.decision == "deny" and "source image not found locally: art/other.png" in v.reasons[0]
    # no id at all
    v = check_call(SC + "animate_game_art", {"prompt": "swings"}, tmp_path)
    assert v.decision == "deny" and "no source asset id given" in v.reasons[0]

    write_record(tmp_path / "art" / "anchor.png", {"start_image": True})
    assert check_call(SC + "animate_game_art", {"asset_id": ASSET, "prompt": "swings", "matte_color": "#FF00FF"}, tmp_path).decision == "allow"


def test_pixellab_reference_urls_resolve_through_the_manifest(tmp_path):
    _gated(tmp_path / "art" / "anchor.png")
    _png(tmp_path / "art" / "ungated.png")
    _manifest(tmp_path, {
        "assets": [{"asset_id": ASSET, "file": "art/anchor.png"}],
        "hosted": [{"url": "https://cdn.example.com/x/ungated.png", "file": "art/ungated.png"}],
    })
    url = f"https://api.spritecook.ai/v1/assets/{ASSET}/download?sig=1"
    assert check_call(PL + "create_character", {"description": "robot", "reference_image_url": url}, tmp_path).decision == "allow"

    v = check_call(PL + "create_character", {"reference_image_url": f"https://api.spritecook.ai/v1/assets/{OTHER}/download"}, tmp_path)
    assert v.decision == "deny" and "register the gated file and its URL in the manifest" in v.reasons[0]
    v = check_call(PL + "animate_with_skeleton_v3", {"custom_start_frame_url": "https://elsewhere.example/a.png"}, tmp_path)
    assert v.decision == "deny" and "custom_start_frame_url" in v.reasons[0]
    # a URL registered by its exact address resolves, and that file is not gated
    v = check_call(PL + "animate_with_skeleton_v3", {"end_frame_url": "https://cdn.example.com/x/ungated.png"}, tmp_path)
    assert v.decision == "deny" and "art/ungated.png has no gate record" in v.reasons[0]
    # no reference at all: a prompt-only generation
    assert check_call(PL + "create_character", {"description": "small robot"}, tmp_path).decision == "allow"


def test_pixellab_character_views_must_be_gated(tmp_path):
    views = tmp_path / "art" / "bot" / "views"
    for facing in ("south", "east", "north", "west"):
        _gated(views / f"{facing}.png", gate="views")
    _png(views / "contact_sheet.png")  # not a facing: not required
    _manifest(tmp_path, {"pixellab_characters": [{"character_id": "char-1", "dir": "art/bot/views"},
                                                 {"character_id": "char-2", "dir": "art/empty"}]})
    for tool in ("animate_character", "animate_with_skeleton_v3", "create_character_state"):
        assert check_call(PL + tool, {"character_id": "char-1"}, tmp_path).decision == "allow", tool

    _png(views / "bot_south_east.png")  # an un-gated view joins the set
    v = check_call(PL + "animate_character", {"character_id": "char-1"}, tmp_path)
    assert v.decision == "deny" and "1 of 5 view(s)" in v.reasons[0] and "bot_south_east.png" in v.reasons[0]
    assert "warden judge-views art/bot/views" in v.reasons[0]

    v = check_call(PL + "animate_character", {"character_id": "char-2"}, tmp_path)
    assert v.decision == "deny" and "holds no <facing>.png views" in v.reasons[0]
    v = check_call(PL + "animate_character", {"character_id": "unknown"}, tmp_path)
    assert v.decision == "deny" and "pixellab_characters" in v.reasons[0]


def test_facing_stems_match_the_views_gate():
    from spritewarden.judge.views import FACING_ALIASES

    assert FACING_STEMS == set(FACING_ALIASES)


def test_pipeline_json_provider_mismatch(tmp_path):
    bot = tmp_path / "art_source" / "bots" / "bildbot"
    _gated(bot / "sc" / "deep" / "anchor.png")
    (bot / "pipeline.json").write_text(json.dumps({"rotation_provider": "PixelLab", "animation_provider": "pixellab"}), encoding="utf-8")
    rel = "art_source/bots/bildbot/sc/deep/anchor.png"

    v = check_call("Bash", _bash(f"python tools/sprite_fusion.py direction-set --input {rel} --size 64"), tmp_path)
    assert v.decision == "deny" and len(v.reasons) == 1
    assert "art_source/bots/bildbot/pipeline.json says rotation is done by PixelLab, not spritefusion" in v.reasons[0]
    assert "never mix providers inside one set" in v.reasons[0]

    v = check_call("Bash", _bash(f"python tools/retro_diffusion.py generate --style rd_advanced_animation__walking --input {rel} --size 32"), tmp_path)
    assert v.decision == "deny" and "animation is done by pixellab, not retrodiffusion" in v.reasons[0]

    _manifest(tmp_path, {"assets": [{"asset_id": ASSET, "file": rel}]})
    v = check_call(SC + "animate_game_art", {"asset_id": ASSET, "prompt": "swings", "matte_color": "#FF00FF"}, tmp_path)
    assert v.decision == "deny" and "not spritecook" in v.reasons[0]

    # the recorded provider itself passes; aliases count as the same provider
    (bot / "pipeline.json").write_text(json.dumps({"rotation_provider": "sprite_fusion", "animation_provider": "sc"}), encoding="utf-8")
    assert check_call("Bash", _bash(f"python tools/sprite_fusion.py direction-set --input {rel} --size 64"), tmp_path).decision == "allow"
    assert check_call(SC + "animate_game_art", {"asset_id": ASSET, "prompt": "swings", "matte_color": "#FF00FF"}, tmp_path).decision == "allow"
    # stages a pipeline does not record are free
    (bot / "pipeline.json").write_text(json.dumps({"notes": "undecided"}), encoding="utf-8")
    assert check_call("Bash", _bash(f"python tools/sprite_fusion.py direction-set --input {rel} --size 64"), tmp_path).decision == "allow"


# ------------------------------------------------------------------ override, config, review, fail-open

def test_override_decrements_and_reports(tmp_path):
    override = tmp_path / "tmp" / "generator_hook_override.json"
    override.parent.mkdir()
    override.write_text(json.dumps({"reason": "Don said so", "calls": 2}), encoding="utf-8")
    call = (SC + "generate_game_art", {"colors": ["#000000"] * 12})
    for left in (1, 0):
        v = check_call(*call, tmp_path)
        assert v.decision == "allow" and len(v.reasons) == 1
        assert "1 rule(s) overridden" in v.context and "Don said so" in v.context and "12 entries" in v.context
        assert json.loads(override.read_text(encoding="utf-8"))["calls"] == left
    assert check_call(*call, tmp_path).decision == "deny"
    # an allowed call never spends the override
    override.write_text(json.dumps({"reason": "r", "calls": 1}), encoding="utf-8")
    assert check_call(SC + "generate_game_art", {"prompt": "robot"}, tmp_path).decision == "allow"
    assert json.loads(override.read_text(encoding="utf-8"))["calls"] == 1


def test_project_config(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.manifest == "art_source/spritecook-assets.json" and cfg.review_specs is True
    assert cfg.cli_tools["tools/sprite_fusion.py"] == "spritefusion"

    (tmp_path / "spritewarden.json").write_text(json.dumps({
        "manifest": "assets/ids.json", "override": "ovr.json", "review_specs": False,
        "cli_tools": {"scripts\\sf.py": "sf"},
    }), encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.manifest_path == tmp_path / "assets" / "ids.json" and cfg.cli_tools == {"scripts/sf.py": "sf"}
    # the default tool path is no longer a generator CLI, the configured one is
    assert check_call("Bash", _bash("python tools/sprite_fusion.py generate --prompt spritesheet"), tmp_path).decision == "ignore"
    v = check_call("Bash", _bash("python scripts/sf.py generate --prompt spritesheet"), tmp_path)
    assert v.decision == "deny" and v.provider == "spritefusion"
    (tmp_path / "ovr.json").write_text(json.dumps({"reason": "ok", "calls": 1}), encoding="utf-8")
    assert check_call("Bash", _bash("python scripts/sf.py generate --prompt spritesheet"), tmp_path).decision == "allow"
    # manifest path comes from the config
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "ids.json").write_text(json.dumps({"a": [{"asset_id": ASSET, "file": "x.png"}]}), encoding="utf-8")
    v = check_call(SC + "animate_game_art", {"asset_id": ASSET, "prompt": "swings"}, tmp_path)
    assert "source image not found locally: x.png" in v.reasons[0]

    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert find_project_root(sub) == tmp_path


def _review_page(root: Path, paths: list[str]) -> str:
    page = root / "art" / "review.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("<html></html>", encoding="utf-8")
    page.with_suffix(".json").write_text(json.dumps({"groups": [{"items": [{"paths": paths[:1]}, {"paths": paths[1:]}]}]}), encoding="utf-8")
    return str(page)


def test_review_pages_need_gated_images(tmp_path):
    _gated(tmp_path / "art" / "a.png", gate="still")
    _gated(tmp_path / "art" / "b.png", gate="anim")
    _png(tmp_path / "art" / "c.png")
    page = _review_page(tmp_path, ["art/a.png", "art/b.png", "art/c.png", "art/missing.png"])
    v = check_call("Artifact", {"file_path": page}, tmp_path)
    assert v.decision == "deny" and (v.provider, v.operation) == ("review", "publish")
    assert "2 review page candidate(s)" in v.reasons[0] and "art/c.png, art/missing.png" in v.reasons[0]

    page = _review_page(tmp_path, ["art/a.png", "art/b.png"])
    v = check_call("Artifact", {"file_path": "art/review.html"}, tmp_path)  # relative to the project root
    assert v.decision == "allow" and v.context == ""

    # pages without a spec, or with another kind of JSON beside them, are not review pages
    (tmp_path / "art" / "plain.html").write_text("<html></html>", encoding="utf-8")
    assert check_call("Artifact", {"file_path": str(tmp_path / "art" / "plain.html")}, tmp_path).decision == "ignore"
    (tmp_path / "art" / "plain.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
    assert check_call("Artifact", {"file_path": str(tmp_path / "art" / "plain.html")}, tmp_path).decision == "ignore"
    assert check_call("Artifact", {"file_path": str(tmp_path / "notes.md")}, tmp_path).decision == "ignore"

    _review_page(tmp_path, ["art/c.png"])
    (tmp_path / "spritewarden.json").write_text(json.dumps({"review_specs": False}), encoding="utf-8")
    assert check_call("Artifact", {"file_path": page}, tmp_path).decision == "ignore"


def test_internal_errors_fail_closed(tmp_path, monkeypatch):
    # a registry with a rule kind nobody implements
    reg = copy.deepcopy(load_raw_registry())
    reg["providers"]["spritecook"]["enforce"]["rules"].append({"kind": "telepathy"})
    v = check_call(SC + "generate_game_art", {"prompt": "robot"}, tmp_path, registry=reg)
    assert v.decision == "deny" and v.reasons[0].startswith("gatekeeper error: ValueError:") and "telepathy" in v.reasons[0]
    assert "NOT checked" in v.reasons[0]
    # the override file still lets a call through on purpose
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp" / "generator_hook_override.json").write_text(json.dumps({"reason": "known bug", "calls": 1}), encoding="utf-8")
    v = check_call(SC + "generate_game_art", {"prompt": "robot"}, tmp_path, registry=reg)
    assert v.decision == "allow" and "known bug" in v.context
    assert check_call(SC + "generate_game_art", {"prompt": "robot"}, tmp_path, registry=reg).decision == "deny"
    # ... which never turns an ignored call into noise
    assert check_call(SC + "get_credit_balance", {}, tmp_path, registry=reg).decision == "ignore"

    # an unreadable source image inside the matte rule
    (tmp_path / "art").mkdir()
    (tmp_path / "art" / "anchor.png").write_bytes(b"not a png")
    write_record(tmp_path / "art" / "anchor.png", {"start_image": True})
    _manifest(tmp_path, {"assets": [{"asset_id": ASSET, "file": "art/anchor.png"}]})
    v = check_call(SC + "animate_game_art", {"asset_id": ASSET, "prompt": "swings"}, tmp_path)
    assert v.decision == "deny" and v.reasons[0].startswith("gatekeeper error: ")

    # a malformed project config
    (tmp_path / "spritewarden.json").write_text("{not json", encoding="utf-8")
    v = check_call(SC + "generate_game_art", {"prompt": "robot"}, tmp_path)
    assert v.decision == "deny" and "gatekeeper error" in v.reasons[0] and "spritewarden.json" in v.reasons[0]
    assert check_call("Bash", _bash("git status"), tmp_path).decision == "ignore"

    # anything unexpected while classifying is swallowed too
    import spritewarden.enforce.core as core

    monkeypatch.setattr(core, "_classify_mcp", lambda *a, **k: 1 / 0)
    assert check_call(SC + "generate_game_art", {}, tmp_path).decision == "ignore"


# ------------------------------------------------------------------ the CLI and the hook protocol

def _run(args: list[str], stdin: str | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "spritewarden.cli", *args], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", cwd=cwd)


def test_enforce_imports_nothing_heavy():
    code = (
        "import sys, json\n"
        "from spritewarden.enforce import check_call\n"
        "import spritewarden.enforce.cli\n"
        "from spritewarden.cli import main  # the console-script import: must not load cli_full\n"
        "check_call('Bash', {'command': 'git status'}, '.')\n"
        "check_call('mcp__pixellab__create_character', {'description': 'robot'}, '.')\n"
        "names = ('numpy', 'PIL', 'httpx', 'mcp', 'pydantic', 'rich', 'spritewarden.cli_full')\n"
        "heavy = [m for m in names if m in sys.modules]\n"
        "print(json.dumps(heavy))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == []


def test_check_call_cli(tmp_path):
    call = tmp_path / "call.json"
    call.write_text(json.dumps({"colors": ["#000000"] * 9}), encoding="utf-8")
    out = _run(["check-call", "--tool", SC + "generate_game_art", "--input-file", str(call), "--project", str(tmp_path), "--json"])
    assert out.returncode == 2, out.stderr
    data = json.loads(out.stdout)
    assert data["decision"] == "deny" and data["provider"] == "spritecook" and data["operation"] == "generate_game_art"
    assert set(data) == {"decision", "provider", "operation", "reasons", "context"}

    # a whole event file works too; plain output; exit 0 on allow and on ignore
    call.write_text(json.dumps({"tool_name": "x", "tool_input": {"prompt": "robot"}}), encoding="utf-8")
    out = _run(["check-call", "--tool", SC + "generate_game_art", "--input-file", str(call), "--project", str(tmp_path)])
    assert out.returncode == 0 and out.stdout.startswith("ALLOW (spritecook / generate_game_art)")
    call.write_text(json.dumps({"command": "git status"}), encoding="utf-8")
    out = _run(["check-call", "--tool", "Bash", "--input-file", str(call), "--project", str(tmp_path)])
    assert out.returncode == 0 and out.stdout.strip() == "IGNORE"


def test_hook_protocol(tmp_path):
    (tmp_path / "spritewarden.json").write_text(json.dumps({"override": "tmp/ovr.json"}), encoding="utf-8")
    sub = tmp_path / "deep" / "dir"
    sub.mkdir(parents=True)

    deny = {"tool_name": SC + "generate_game_art", "tool_input": {"colors": ["#000000"] * 9}, "cwd": str(sub)}
    out = _run(["hook", "--claude"], stdin=json.dumps(deny))
    assert out.returncode == 0, out.stderr
    spec = json.loads(out.stdout)["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "deny"
    assert spec["permissionDecisionReason"].startswith("BLOCKED by sprite-warden (spritecook / generate_game_art):")
    assert "9 entries" in spec["permissionDecisionReason"] and "tmp/ovr.json" in spec["permissionDecisionReason"]

    allow = {"tool_name": SC + "generate_game_art", "tool_input": {"prompt": "robot"}, "cwd": str(sub)}
    out = _run(["hook", "--claude"], stdin=json.dumps(allow))
    spec = json.loads(out.stdout)["hookSpecificOutput"]
    assert out.returncode == 0 and "permissionDecision" not in spec
    assert spec["hookEventName"] == "PreToolUse" and "spritecook / generate_game_art" in spec["additionalContext"]

    ignore = {"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": str(sub)}
    out = _run(["hook", "--claude", "--project", str(tmp_path)], stdin=json.dumps(ignore))
    assert out.returncode == 0 and out.stdout == ""

    for garbage in ("", "not json", "[1, 2]"):
        out = _run(["hook", "--claude"], stdin=garbage)
        assert out.returncode == 0 and out.stdout == ""


def test_full_cli_still_lists_the_enforcement_commands():
    out = _run(["--help"])
    assert out.returncode == 0 and "check-call" in out.stdout and "hook" in out.stdout and "judge-still" in out.stdout
