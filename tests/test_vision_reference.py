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
