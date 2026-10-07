"""Reference-aware vision: structured diff gate, offline = FAIL, anomalies hard-fail. No network."""

from PIL import Image

from spritewarden.judge import evaluate_still
from spritewarden.judge.vision import (
    ReferenceComparison,
    ReferenceDifference,
    VisionJudge,
    VisionObservation,
    prepare_pair_for_vision,
    reference_gate,
)


def _sprite(color=(89, 112, 128, 255), size=32) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for y in range(6, 28):
        for x in range(10, 22):
            img.putpixel((x, y), color)
    return img


def test_offline_comparison_is_unavailable_and_fails():
    judge = VisionJudge(offline=True)
    assert judge.is_available() is False
    comp = judge.compare_to_reference(_sprite(), _sprite())
    assert comp.status == "unavailable"
    gate = reference_gate(comp)
    assert gate.passed is False and gate.name == "vision_reference"
    assert "unavailable" in gate.errors[0]


def test_offline_env_var(monkeypatch):
    monkeypatch.setenv("SPRITEWARDEN_OFFLINE", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-used")
    assert VisionJudge().is_available() is False


def test_reference_gate_verdicts():
    clean = ReferenceComparison(status="match", model_used="m", perspective_match=True, same_character=True,
                                reference_hands={"left": "wrench", "right": "none"},
                                candidate_hands={"left": "wrench", "right": "none"}, notes="identical")
    assert reference_gate(clean).passed is True

    orbs = ReferenceComparison(status="mismatch", model_used="m", perspective_match=True, same_character=True,
                               differences=[ReferenceDifference(kind="added", part="two orbs beside the head")])
    g = reference_gate(orbs, facing="north_east")
    assert g.passed is False and "added" in g.errors[0] and "north_east" in g.summary

    swapped = ReferenceComparison(status="match", model_used="m", perspective_match=True,
                                  reference_hands={"left": "wrench", "right": "emitter"},
                                  candidate_hands={"left": "emitter", "right": "wrench"})
    g = reference_gate(swapped)
    assert g.passed is False and any("hands" in e for e in g.errors)

    camera = ReferenceComparison(status="match", model_used="m", perspective_match=False,
                                 reference_camera_elevation_degrees=25, candidate_camera_elevation_degrees=70)
    assert reference_gate(camera).passed is False

    recolor_only = ReferenceComparison(status="match", model_used="m", perspective_match=True,
                                       differences=[ReferenceDifference(kind="recolored", part="visor", detail="slightly lighter")])
    g = reference_gate(recolor_only)
    assert g.passed is True and g.warnings  # non-structural differences are warnings


# --- the facing-aware comparison: canned model JSON, no API calls -------------------------------

OCCLUSION_ONLY = {
    "invented_parts": [],
    "missing_visible_parts": [],
    "changed_parts": [],
    "expected_hidden_parts": ["visor", "chest panel"],
    "tool_sides": {"anchor": {"wrench": "left"}, "candidate": {"wrench": "left"}},
    "anchor_elevation": 25,
    "candidate_elevation": 30,
    "same_character": True,
    "notes": "Back view of the same robot; the visor is on the far side.",
}


def test_occlusion_only_difference_passes():
    from spritewarden.judge.vision import parse_reference_response

    comp = parse_reference_response(OCCLUSION_ONLY, model="fake", facing="north")
    assert comp.status == "match" and comp.failures() == []
    gate = reference_gate(comp, facing="north")
    assert gate.passed is True and gate.errors == []
    assert gate.metrics["expected_hidden_parts"] == ["visor", "chest panel"]

    # The live failure: the model filed the turned-away visor under "missing" with its own excuse.
    # An occlusion-explained difference never fails; it is moved to expected_hidden_parts.
    excused = {**OCCLUSION_ONLY, "expected_hidden_parts": [],
               "missing_visible_parts": [{"part": "visor", "detail": "absent as it is turned away"}]}
    comp = parse_reference_response(excused, model="fake", facing="north")
    assert comp.missing_visible_parts == [] and [p.part for p in comp.expected_hidden_parts] == ["visor"]
    assert reference_gate(comp, facing="north").passed is True


def test_invented_bolt_fails():
    from spritewarden.judge.vision import parse_reference_response

    bolt = {**OCCLUSION_ONLY, "invented_parts": [
        {"part": "round boss / bolt", "location": "back and sides of the head", "size": "4x4 px"}]}
    comp = parse_reference_response(bolt, model="fake", facing="north")
    assert comp.status == "mismatch"
    gate = reference_gate(comp, facing="north")
    assert gate.passed is False and len(gate.errors) == 1
    assert gate.errors[0].startswith("invented: round boss / bolt at back and sides of the head (~4x4 px)")
    assert "north" in gate.summary and gate.metrics["invented_parts"] == 1
    # the model saying "match" does not matter: the verdict is ours
    assert parse_reference_response({**bolt, "verdict": "match"}, facing="north").status == "mismatch"
    # plain strings are accepted for parts
    assert parse_reference_response({"invented_parts": ["antenna"]}).failures()[0].startswith("invented: antenna")


def test_the_other_verdict_rules():
    from spritewarden.judge.vision import parse_reference_response

    missing = {**OCCLUSION_ONLY, "missing_visible_parts": [{"part": "left arm", "location": "left shoulder"}]}
    gate = reference_gate(parse_reference_response(missing, facing="south_east"), facing="south_east")
    assert gate.passed is False and "missing: left arm" in gate.errors[0] and "should be visible from south_east" in gate.errors[0]

    # a bare "not the same character" with nothing concrete named is a verdict, not evidence: a warning
    other = {**OCCLUSION_ONLY, "same_character": False}
    bare = reference_gate(parse_reference_response(other))
    assert bare.passed is True and any(w.startswith("identity:") for w in bare.warnings)
    # ... with a concrete difference beside it, it fails as before
    named = {**other, "changed_parts": [{"part": "body", "detail": "blue instead of grey"}]}
    gate = reference_gate(parse_reference_response(named))
    assert gate.passed is False and any(e.startswith("identity:") for e in gate.errors)

    # elevation: 15 degrees apart is tolerated, more is not
    assert reference_gate(parse_reference_response({**OCCLUSION_ONLY, "candidate_elevation": 40})).passed is True
    steep = parse_reference_response({**OCCLUSION_ONLY, "candidate_elevation": 41})
    assert steep.perspective_match is False and reference_gate(steep).passed is False
    assert any(e.startswith("perspective:") for e in reference_gate(steep).errors)

    # changed parts and a tool that reads on the other side are warnings, never failures
    soft = {**OCCLUSION_ONLY, "changed_parts": [{"part": "antenna", "detail": "2px shorter"}],
            "tool_sides": {"anchor": {"wrench": "left"}, "candidate": {"wrench": "right"}}}
    gate = reference_gate(parse_reference_response(soft, facing="north"))
    assert gate.passed is True and len(gate.warnings) == 2
    assert gate.warnings[0].startswith("changed: antenna") and gate.warnings[1].startswith("tool side: wrench")

    # an empty or sloppy answer is a match with nothing to say, not a crash
    assert parse_reference_response({"invented_parts": None, "anchor_elevation": "n/a"}).status == "match"


def test_prompt_states_facings_and_asks_for_invented_parts_first(monkeypatch):
    from spritewarden.judge.vision import reference_prompt

    prompt = reference_prompt(candidate_facing="north", anchor_facing="south", description="grey robot")
    assert "facing south" in prompt and "face north" in prompt
    assert "EXPECTED to be" in prompt and "Do NOT report them as missing" in prompt
    assert prompt.index("INVENTED PARTS") < prompt.index("MISSING PARTS") < prompt.index("CAMERA")
    for key in ("invented_parts", "missing_visible_parts", "changed_parts", "expected_hidden_parts", "tool_sides",
                "anchor_elevation", "candidate_elevation", "same_character", "notes"):
        assert f'"{key}"' in prompt
    same = reference_prompt(candidate_facing="south", anchor_facing="south")
    assert "same camera" in same and "turned to" not in same

    # compare_to_reference sends that prompt and applies the verdict to the canned answer
    judge = VisionJudge(offline=True)
    monkeypatch.setattr(judge, "is_available", lambda: True)
    seen = {}

    def fake_chat(content):
        seen["prompt"] = content[0]["text"]
        return {**OCCLUSION_ONLY, "invented_parts": [{"part": "bolt", "location": "head", "size": "3px"}]}, 0.001, None

    monkeypatch.setattr(judge, "_chat_json", fake_chat)
    comp = judge.compare_to_reference(_sprite(), _sprite(), facing="north_east", anchor_facing="south")
    assert "face north_east" in seen["prompt"] and comp.status == "mismatch" and comp.candidate_facing == "north_east"
    assert comp.cost_usd == 0.001


def test_judge_views_vision_runs_every_view_and_records_each(tmp_path, monkeypatch):
    import json
    import subprocess
    import sys

    from spritewarden.judge.vision import parse_reference_response
    from spritewarden.judge.views import compare_views_to_anchor, vision_summary_gate

    frames = {f: _sprite() for f in ("south", "east", "north", "west")}
    calls = []

    class FakeJudge:
        def compare_to_reference(self, candidate, reference, *, facing, anchor_facing, description="", expected_perspective=""):
            calls.append((facing, anchor_facing))
            answer = dict(OCCLUSION_ONLY)
            if facing == "north":
                answer["invented_parts"] = [{"part": "bolt", "location": "back of the head", "size": "4px"}]
            return parse_reference_response(answer, model="fake", facing=facing, anchor_facing=anchor_facing)

    gates = compare_views_to_anchor(frames, frames["south"], FakeJudge(), skip_anchor_view=True)
    assert calls == [("east", "south"), ("north", "south"), ("west", "south")]
    assert {f: g.passed for f, g in gates.items()} == {"east": True, "north": False, "west": True}
    summary = vision_summary_gate(gates)
    assert summary.passed is False and "north: invented: bolt" in summary.errors[0]
    assert vision_summary_gate({}).passed is False

    # CLI, offline: the comparison is unavailable, so every compared view FAILS and says so in its record
    views = tmp_path / "views"
    views.mkdir()
    for facing, img in frames.items():
        img.save(views / f"{facing}.png")
    import os

    env = {**os.environ, "SPRITEWARDEN_OFFLINE": "1"}
    out = subprocess.run([sys.executable, "-m", "spritewarden.cli", "judge-views", str(views), "--vision", "--json"],
                         capture_output=True, text=True, encoding="utf-8", env=env)
    assert out.returncode == 1, out.stderr
    data = json.loads(out.stdout)
    assert data["passed"] is False and set(data["vision_reference"]["views"]) == {"east", "north", "west"}
    north = json.loads((views / "north.png.gate.json").read_text(encoding="utf-8"))
    assert north["gates"]["vision_reference"]["passed"] is False and north["gates"]["views"]["passed"] is False
    assert north["gates"]["direction_set"]["passed"] is True and north["params"]["facing"] == "north"
    south = json.loads((views / "south.png.gate.json").read_text(encoding="utf-8"))
    assert "vision_reference" not in south["gates"]  # the anchor is not compared with itself
    # without --vision nothing changes: the set passes and no model is asked
    out = subprocess.run([sys.executable, "-m", "spritewarden.cli", "judge-views", str(views), "--json"],
                         capture_output=True, text=True, encoding="utf-8", env=env)
    assert out.returncode == 0 and "vision_reference" not in json.loads(out.stdout)


def test_pair_board_layout():
    board = prepare_pair_for_vision(_sprite(), _sprite((200, 50, 50, 255)), zoom=4)
    assert board.width > 2 * 12 * 4 and board.height > 22 * 4
    px = board.load()
    # left half shows the reference colour, right half the candidate colour
    assert any(px[x, board.height // 2][:3] == (89, 112, 128) for x in range(board.width // 2))
    assert any(px[x, board.height // 2][:3] == (200, 50, 50) for x in range(board.width // 2, board.width))


def test_orchestrated_verdict_hard_fails_on_anomalies(monkeypatch):
    judge = VisionJudge(offline=True)
    card = evaluate_still(_sprite(), "luap-40")
    assert card.passed

    def fake_inspect(**kwargs):
        return VisionObservation(model_used="fake", perspective_match=True,
                                 anomalies=["floating orb beside the head"], visual_smell_test_notes="orb")

    monkeypatch.setattr(judge, "view_and_inspect", fake_inspect)
    verdict = judge.orchestrate_judgment(card, _sprite())
    assert verdict.passed is False
    assert verdict.recommended_action != "accept"
    assert "anomalies" in verdict.reasoning_summary

    def fake_perspective(**kwargs):
        return VisionObservation(model_used="fake", perspective_match=False, camera_elevation_degrees=80)

    monkeypatch.setattr(judge, "view_and_inspect", fake_perspective)
    verdict = judge.orchestrate_judgment(card, _sprite())
    assert verdict.passed is False and "perspective" in verdict.reasoning_summary

    # The offline stub never looked, so it cannot hard-fail; the deterministic verdict stands.
    monkeypatch.setattr(judge, "view_and_inspect", lambda **kw: VisionObservation(model_used="offline_stub"))
    assert judge.orchestrate_judgment(card, _sprite()).passed is True
