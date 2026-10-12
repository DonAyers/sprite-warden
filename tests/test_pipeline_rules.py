"""The standing pipeline rules: chained commands, the one anchor, provider identity, review
completeness, the rejection log, `warden rules` / `pipeline` / `stage`.

Everything is built in tmp_path; no provider and no vision model is called.
"""

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

from spritewarden.enforce import check_call
from spritewarden.enforce.pipeline import check_pipeline, load_pipeline, rules_text, set_values, stage_status
from spritewarden.enforce.config import load_config
from spritewarden.gates import require_passed, review_problem, write_record
from spritewarden.gates.records import mark_rejected, passed_gates
from spritewarden.gates.rejections import log_rejection, read_rejections, recent_rejections, summarize

PL = "mcp__pixellab__"
SC = "mcp__plugin_spritecook_spritecook__"
ASSET = "b4154184-638f-4fd6-8c6d-3ce4ce54cff7"
OTHER = "11111111-2222-3333-4444-555555555555"
RD_ROTATE = "python tools/retro_diffusion.py generate --style rd_advanced_animation__rotate --input {} --size 32x32"
RD_WALK = "python tools/retro_diffusion.py generate --style rd_advanced_animation__walking --input {} --size 32x32"


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


def _bash(command: str) -> dict:
    return {"command": command}


def _strict(root: Path) -> None:
    (root / "spritewarden.json").write_text(json.dumps({"require_pipeline": True}), encoding="utf-8")


def _pipeline(bot: Path, **values) -> Path:
    bot.mkdir(parents=True, exist_ok=True)
    data = {"bot": bot.name, **values}
    (bot / "pipeline.json").write_text(json.dumps(data), encoding="utf-8")
    return bot / "pipeline.json"


FACES = {"back": "plain grey panel", "left": "plain", "right": "plain"}


# ------------------------------------------------------------------ chained commands

def test_every_generator_call_in_a_chain_is_checked(tmp_path):
    # a read-only first call must not hide the spending one behind it
    v = check_call("Bash", _bash('python tools/sprite_fusion.py credits && python tools/sprite_fusion.py generate --prompt "robot spritesheet"'), tmp_path)
    assert v.decision == "deny" and (v.provider, v.operation) == ("spritefusion", "generate")
    # two spending calls: both are held to their recipes, and the reasons say whose they are
    v = check_call("Bash", _bash('python tools/sprite_fusion.py generate --prompt robot; '
                                 'python tools/retro_diffusion.py generate --style rd_pro__default --prompt "pixel art robot"'), tmp_path)
    assert v.decision == "deny" and (v.provider, v.operation) == ("retrodiffusion", "rd_pro")
    assert v.reasons[0].startswith("[retrodiffusion / rd_pro] ")
    v = check_call("Bash", _bash("python tools/sprite_fusion.py generate --prompt robot && python tools/sprite_fusion.py generate --prompt crate"), tmp_path)
    assert v.decision == "allow" and v.context.count("GENERATOR CALL") == 2


def test_force_ungated_needs_the_override_file(tmp_path):
    v = check_call("Bash", _bash('warden generate "a crate" --provider sc --force-ungated'), tmp_path)
    assert v.decision == "deny" and "override file" in v.reasons[0]


# ------------------------------------------------------------------ the recorded route

def test_strict_mode_requires_a_recorded_route(tmp_path):
    _strict(tmp_path)
    bot = tmp_path / "art" / "bot"
    anchor = _gated(bot / "anchor.png")
    rel = "art/bot/anchor.png"
    v = check_call("Bash", _bash(RD_ROTATE.format(rel)), tmp_path)
    assert v.decision == "deny" and "no pipeline.json above art/bot/anchor.png" in v.reasons[0]

    _pipeline(bot)
    v = check_call("Bash", _bash(RD_ROTATE.format(rel)), tmp_path)
    text = " | ".join(v.reasons)
    assert v.decision == "deny"
    assert "records no rotation_provider" in text and "records no anchor" in text and "back, left, right face(s)" in text

    _pipeline(bot, rotation_provider="rd", anchor=rel, faces=FACES)
    assert check_call("Bash", _bash(RD_ROTATE.format(rel)), tmp_path).decision == "allow"
    # stills are never held to the route
    assert check_call("Bash", _bash("python tools/retro_diffusion.py generate --style rd_pro__default --prompt robot"), tmp_path).decision == "allow"
    assert anchor.is_file()


def test_rotation_starts_from_the_one_anchor(tmp_path):
    bot = tmp_path / "art" / "bot"
    _gated(bot / "anchor.png")
    _gated(bot / "other.png")
    _pipeline(bot, rotation_provider="retrodiffusion", anchor="art/bot/anchor.png", faces=FACES)
    assert check_call("Bash", _bash(RD_ROTATE.format("art/bot/anchor.png")), tmp_path).decision == "allow"
    v = check_call("Bash", _bash(RD_ROTATE.format("art/bot/other.png")), tmp_path)  # enforced without strict mode too
    assert v.decision == "deny" and "one anchor per set" in v.reasons[0] and "art/bot/other.png" in v.reasons[0]
    # an animation may start from another gated image of the set (a turned view)
    write_record(bot / "other.png", {"views": True})
    assert check_call("Bash", _bash(RD_WALK.format("art/bot/other.png")), tmp_path).decision == "allow"


def test_entity_providers_animate_through_their_character_id(tmp_path):
    bot = tmp_path / "art" / "bot"
    _gated(bot / "anchor.png")
    manifest = tmp_path / "art_source" / "spritecook-assets.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"assets": [{"asset_id": ASSET, "file": "art/bot/anchor.png"},
                                               {"asset_id": OTHER, "file": "art/bot/anchor.png"}]}), encoding="utf-8")
    call = {"prompt": "swings", "matte_color": "#FF00FF"}

    # recorded id: the call must use it (or an id listed under "also")
    _pipeline(bot, animation_provider="spritecook", identity={"sc": {"id": ASSET}})
    assert check_call(SC + "animate_game_art", {**call, "asset_id": ASSET}, tmp_path).decision == "allow"
    v = check_call(SC + "animate_game_art", {**call, "asset_id": OTHER}, tmp_path)
    assert v.decision == "deny" and f"SpriteCook character is {ASSET}" in v.reasons[0]
    _pipeline(bot, animation_provider="spritecook", identity={"spritecook": {"id": ASSET, "also": [OTHER]}})
    assert check_call(SC + "animate_game_art", {**call, "asset_id": OTHER}, tmp_path).decision == "allow"

    # strict mode: an entity provider without a recorded id cannot animate
    _strict(tmp_path)
    _pipeline(bot, animation_provider="spritecook")
    v = check_call(SC + "animate_game_art", {**call, "asset_id": ASSET}, tmp_path)
    assert v.decision == "deny" and "records no SpriteCook character id" in v.reasons[0]
    # a provider without a character entity has nothing to record
    write_record(bot / "anchor.png", {"start_image": True})
    _pipeline(bot, animation_provider="retrodiffusion")
    # Route revision invalidates the prior evidence; refresh it for the newly recorded provider.
    assert check_call("Bash", _bash(RD_WALK.format("art/bot/anchor.png")), tmp_path).decision == "deny"
    write_record(bot / "anchor.png", {"start_image": True})
    assert check_call("Bash", _bash(RD_WALK.format("art/bot/anchor.png")), tmp_path).decision == "allow"


def test_pixellab_loose_image_animation_is_refused_when_a_character_is_recorded(tmp_path):
    bot = tmp_path / "art" / "bot"
    _gated(bot / "anchor.png")
    manifest = tmp_path / "art_source" / "spritecook-assets.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"hosted": [{"url": "https://cdn.example.com/a.png", "file": "art/bot/anchor.png"}]}), encoding="utf-8")
    _pipeline(bot, animation_provider="pixellab", identity={"pixellab": {"id": "char-1"}})
    v = check_call(PL + "animate_with_skeleton_v3", {"custom_start_frame_url": "https://cdn.example.com/a.png"}, tmp_path)
    assert v.decision == "deny" and "animate THROUGH the character PixelLab already holds (char-1)" in v.reasons[0]


def test_allow_context_names_the_providers_identity_method(tmp_path):
    v = check_call(SC + "generate_game_art", {"prompt": "small robot, low top-down"}, tmp_path)
    assert v.decision == "allow" and "Character identity, SpriteCook's way" in v.context


# ------------------------------------------------------------------ review completeness

def test_review_needs_the_look_checks_not_just_the_pixel_gates(tmp_path):
    view = _png(tmp_path / "views" / "east.png")
    write_record(view, {"direction_set": True, "views": True}, params={"command": "judge-views", "facing": "east"})
    assert review_problem(view) == "never compared with the anchor by vision (no vision_reference gate)"
    write_record(view, {"views": True, "vision_reference": True}, params={"command": "judge-views", "facing": "east"})
    assert review_problem(view) is None
    # the view that IS the anchor has nothing to be compared with
    south = _png(tmp_path / "views" / "south.png")
    write_record(south, {"views": True}, params={"command": "judge-views", "facing": "south", "anchor_facing": "south"})
    assert review_problem(south) is None
    # ... unless a separate anchor was given
    write_record(south, {"views": True}, params={"command": "judge-views", "facing": "south", "anchor": "a.png"})
    assert review_problem(south) is not None

    anim = _png(tmp_path / "walk.png")
    write_record(anim, {"anim": True}, params={"command": "judge-anim"})
    assert "without --source" in review_problem(anim)
    write_record(anim, {"anim": True, "frame_integrity": {"passed": True}}, params={"command": "judge-anim", "source": "s.png"})
    assert review_problem(anim) is None

    write_record(view, {"views": True}, params={"command": "judge-views", "facing": "east"})
    assert require_passed([view], ["views"])["ok"] is False
    assert require_passed([view], ["views"], strict=False)["ok"] is True

    page = tmp_path / "review.html"
    page.write_text("<html></html>", encoding="utf-8")
    page.with_suffix(".json").write_text(json.dumps({"groups": [{"items": [{"paths": ["views/east.png"]}]}]}), encoding="utf-8")
    v = check_call("Artifact", {"file_path": str(page)}, tmp_path)
    assert v.decision == "deny" and "not the look check" in v.reasons[0] and "views/east.png" in v.reasons[0]


def test_a_rejected_image_is_gated_no_more(tmp_path):
    img = _gated(tmp_path / "a.png", gate="still")
    assert "still" in passed_gates(img)
    mark_rejected(img, by="don", reason="looks terrible blown up")
    assert passed_gates(img) == set()
    _png(img, color=(10, 200, 10, 255))  # a changed file is a new candidate
    write_record(img, {"still": True})
    assert "still" in passed_gates(img)


# ------------------------------------------------------------------ the rejection log

def test_rejection_log_and_hook_context(tmp_path):
    (tmp_path / "spritewarden.json").write_text("{}", encoding="utf-8")  # marks the project root
    bot = tmp_path / "art" / "bot"
    _pipeline(bot, rotation_provider="retrodiffusion")
    view = _png(bot / "views" / "east.png")
    failed = [{"name": "vision_reference", "errors": ["invented: round boss on the side of the head"]}]
    log = log_rejection(view, by="gate", command="judge-views", failed=failed)
    assert log == tmp_path / "art_source" / "rejections.jsonl" or log.name == "rejections.jsonl"
    log_rejection(view, by="gate", command="judge-views", failed=failed)  # the same fact twice is one line
    entries = read_rejections(log)
    assert len(entries) == 1
    e = entries[0]
    assert (e["provider"], e["stage"], e["bot"], e["image"]) == ("retrodiffusion", "rotation", "bot", "art/bot/views/east.png")

    other = _png(bot / "views" / "west.png")
    log_rejection(other, by="gate", command="judge-views", failed=failed)
    log_rejection(other, by="don", reason="weird orbs", provider="retrodiffusion", log_path=log)
    assert len(recent_rejections(log, provider="rd-not-an-alias")) == 0
    assert len(recent_rejections(log, provider="retrodiffusion")) == 3
    top = summarize(read_rejections(log))[0]
    assert top["count"] == 2 and top["what"] == "vision_reference" and "round boss" in top["example"]

    # the next allowed call to that provider is told what was already rejected
    cfg = load_config(tmp_path)
    assert cfg.rejections_path == log
    v = check_call("Bash", _bash("python tools/retro_diffusion.py generate --style rd_pro__default --prompt robot"), tmp_path)
    assert v.decision == "allow" and "Already rejected for this provider" in v.context and "weird orbs" in v.context
    assert "round boss" in rules_text(tmp_path)  # repeated rejections surface at session start


# ------------------------------------------------------------------ pipeline.json helpers, rules, CLI

def test_pipeline_check_status_and_rules_text(tmp_path):
    bot = tmp_path / "art" / "bot"
    path = _pipeline(bot)
    cfg = load_config(tmp_path)
    data = load_pipeline(path)
    todo = {p["text"].split(":")[0].split(" ")[0] for p in check_pipeline(data, cfg, path)}
    assert {"no", "faces.back", "faces.left", "faces.right", "rotation_provider", "animation_provider"} <= todo
    assert stage_status(data, cfg)["next"].startswith("1 anchor")

    _gated(bot / "anchor.png")
    set_values(data, ["anchor=art/bot/anchor.png", "faces.back=plain", "faces.left=plain", "faces.right=plain",
                      "rotation_provider=rd", "animation_provider=pixellab", "canvas=96"])
    assert data["canvas"] == 96 and data["faces"]["back"] == "plain"
    problems = check_pipeline(data, cfg, path)
    assert [p for p in problems if p["level"] == "error"] == []
    assert any("identity.pixellab.id is not recorded" in p["text"] for p in problems)
    assert stage_status(data, cfg)["next"].startswith("2 views: turn the anchor")

    set_values(data, ["rotation_provider=nobody"])
    assert any(p["level"] == "error" and "not a provider" in p["text"] for p in check_pipeline(data, cfg, path))

    path.write_text(json.dumps({**data, "rotation_provider": "rd"}), encoding="utf-8")
    text = rules_text(tmp_path)
    assert "One anchor" in text and "PixelLab: a managed Character (character_id)" in text
    assert "warden stage views" in text and "bot (art/bot): rotation by rd" in text


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "spritewarden.cli", *args], capture_output=True, text=True,
                          encoding="utf-8", cwd=cwd)


def test_rules_and_pipeline_cli(tmp_path):
    (tmp_path / "spritewarden.json").write_text("{}", encoding="utf-8")
    out = _run(["rules", "--project", str(tmp_path)])
    assert out.returncode == 0 and "ART PIPELINE RULES" in out.stdout and "Retro Diffusion" in out.stdout

    bot = tmp_path / "art" / "bot"
    out = _run(["pipeline", "init", str(bot), "locomotion=wheels", "--project", str(tmp_path)])
    assert out.returncode == 0, out.stderr
    assert json.loads((bot / "pipeline.json").read_text(encoding="utf-8"))["locomotion"] == "wheels"
    assert _run(["pipeline", "init", str(bot), "--project", str(tmp_path)]).returncode == 1  # never overwrites silently
    out = _run(["pipeline", "set", str(bot), "faces.back=plain", "anchor=art/bot/missing.png", "--project", str(tmp_path)])
    assert out.returncode == 0
    out = _run(["pipeline", "check", str(bot), "--project", str(tmp_path), "--json"])
    assert out.returncode == 1
    report = json.loads(out.stdout)
    assert report["pipeline"]["faces"]["back"] == "plain"
    assert any(p["level"] == "error" and "does not exist" in p["text"] for p in report["problems"])


def test_stage_anchor_runs_the_gate_and_writes_the_game_scale_view(tmp_path):
    (tmp_path / "spritewarden.json").write_text("{}", encoding="utf-8")
    bot = tmp_path / "art" / "bot"
    _pipeline(bot, canvas=64, palette="luap-40")
    raw = _png(bot / "south.png", size=40, box=(8, 6, 32, 36))
    before = raw.read_bytes()
    out = _run(["stage", "anchor", str(raw)])
    assert "Stage anchor" in out.stdout
    # normalized onto the pipeline's canvas, the provider's file kept beside it
    assert Image.open(raw).size == (64, 64)
    assert (bot / "_raw" / "south.png").read_bytes() == before
    record = json.loads((bot / "south.png.gate.json").read_text(encoding="utf-8"))
    assert "start_image" in record["gates"]
    if out.returncode == 0:
        assert (bot / "_stage" / "south_x3.png").is_file()
        assert Image.open(bot / "_stage" / "south_x3.png").width == (64 + 8) * 3
    else:  # a failed gate is logged without anyone writing it down
        assert read_rejections(tmp_path / "art_source" / "rejections.jsonl")[0]["command"] == "judge-still"

    # views without a gated anchor stop before any gate runs
    views = bot / "views"
    for facing in ("south", "east", "north", "west"):
        _png(views / f"{facing}.png", size=64)
    out = _run(["stage", "views", str(views), "--no-vision"])
    assert out.returncode == 1 and "No anchor" in out.stdout
    # anim without the still it came from is refused
    out = _run(["stage", "anim", str(raw)])
    assert out.returncode == 1 and "--source" in out.stdout
