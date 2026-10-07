"""frame_integrity: frames must not lose parts the source still has (the keyed-out head)."""

import json
import subprocess
import sys

from PIL import Image

from spritewarden.judge import check_frame_integrity, enclosed_holes, evaluate_animation

BODY = (200, 60, 60, 255)
OUTLINE = (30, 30, 40, 255)


def _robot(size=48, head_fill=True, shift=0, ring=False) -> Image.Image:
    """A body block with an outlined head; ``head_fill=False`` leaves only the head's outline."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    for y in range(22, 42):  # body, 20x20 = 400 px
        for x in range(14 + shift, 34 + shift):
            px[x, y] = BODY
    for y in range(8, 22):  # head, 14x14 with a 1px outline
        for x in range(17 + shift, 31 + shift):
            edge = y in (8, 21) or x in (17 + shift, 30 + shift)
            if edge:
                px[x, y] = OUTLINE
            elif head_fill:
                px[x, y] = BODY
    if ring:  # a designed-in hole (a handle): 4x4 transparent inside the body
        for y in range(30, 34):
            for x in range(22 + shift, 26 + shift):
                px[x, y] = (0, 0, 0, 0)
    return img


def test_enclosed_holes_ignore_the_outside():
    assert enclosed_holes(_robot()) == []
    assert enclosed_holes(_robot(head_fill=False)) == [144]  # the 12x12 interior of the head
    assert enclosed_holes(_robot(ring=True)) == [16]
    assert enclosed_holes(Image.new("RGBA", (8, 8), (0, 0, 0, 0))) == []
    # a notch open to the outside is not enclosed
    notch = _robot()
    for y in range(8, 20):
        notch.putpixel((24, y), (0, 0, 0, 0))
    assert enclosed_holes(notch) == []


def test_keyed_out_head_in_every_frame_fails_against_the_source():
    still = _robot()
    frames = [_robot(head_fill=False, shift=s) for s in (0, 1, 0, -1)]
    gate = check_frame_integrity(frames, source=still)
    assert gate.name == "frame_integrity" and gate.passed is False
    assert gate.metrics["holed_frames"] == [0, 1, 2, 3]
    assert "frame 0" in gate.errors[0] and "144 px" in gate.errors[0] and "source still has 0 px" in gate.errors[0]
    # the fill is 144 of 596 px (24%): the area check names the frames too
    assert gate.metrics["shrunk_frames"] == [0, 1, 2, 3]
    assert any("opaque area" in e and "below the source still" in e for e in gate.errors)
    # without the still every frame agrees with the others: the gate cannot see it, and says so
    blind = check_frame_integrity(frames)
    assert blind.passed is True and any("no source still" in w for w in blind.warnings)


def test_one_holed_frame_fails_without_a_source():
    frames = [_robot(), _robot(shift=1), _robot(head_fill=False), _robot(shift=-1)]
    gate = check_frame_integrity(frames)
    assert gate.passed is False and gate.metrics["holed_frames"] == [2]
    assert "frame 2" in gate.errors[0] and "144" in gate.errors[0] and "median of the other frames" in gate.errors[0]


def test_holes_the_design_has_and_small_ones_pass():
    still = _robot(ring=True)
    frames = [_robot(ring=True, shift=s) for s in (0, 1, 0, -1)]
    assert check_frame_integrity(frames, source=still).passed is True

    # limit = max(12, 1.5% of the frame's opaque area): 596 px -> 12 px
    small = _robot()
    for i in range(12):
        small.putpixel((20 + i % 6, 30 + i // 6), (0, 0, 0, 0))
    assert check_frame_integrity([small] * 3, source=_robot()).passed is True
    small.putpixel((20, 32), (0, 0, 0, 0))  # 13 px
    gate = check_frame_integrity([small] * 3, source=_robot())
    assert gate.passed is False and gate.metrics["hole_limits"][0] == 12


def test_area_drop_alone_fails():
    still = _robot()
    armless = _robot()
    for y in range(22, 42):  # cut 4 columns off the body: 80 of 596 px (13%) -> fine
        for x in range(30, 34):
            armless.putpixel((x, y), (0, 0, 0, 0))
    assert check_frame_integrity([armless] * 3, source=still).passed is True
    for y in range(22, 42):  # 100 px (17%) -> fails, no enclosed hole involved
        armless.putpixel((29, y), (0, 0, 0, 0))
    gate = check_frame_integrity([still, armless, still], source=still)
    assert gate.passed is False and gate.metrics["shrunk_frames"] == [1] and gate.metrics["holed_frames"] == []


def test_evaluate_animation_and_cli_carry_the_gate(tmp_path):
    still = _robot()
    still_path = tmp_path / "still.png"
    still.save(still_path)
    frames = [_robot(head_fill=False, shift=s) for s in (0, 1, 0, -1)]
    card = evaluate_animation(frames, source=still_path)
    gate = card.gate("frame_integrity")
    assert gate is not None and gate.passed is False and card.passed is False
    assert any("matte" in a for a in card.suggested_actions)
    assert evaluate_animation([_robot(shift=s) for s in (0, 1, 0, -1)], source=still).gate("frame_integrity").passed is True

    sheet = Image.new("RGBA", (48 * 4, 48), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        sheet.paste(f, (i * 48, 0))
    sheet_path = tmp_path / "sheet.png"
    sheet.save(sheet_path)
    out = subprocess.run([sys.executable, "-m", "spritewarden.cli", "judge-anim", str(sheet_path), "--cell", "48",
                          "--source", str(still_path), "--json"], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 1, out.stderr
    data = json.loads(out.stdout)
    integrity = next(g for g in data["gates"] if g["name"] == "frame_integrity")
    assert integrity["passed"] is False and integrity["metrics"]["reference"] == "source"
    record = json.loads((tmp_path / "sheet.png.gate.json").read_text(encoding="utf-8"))
    assert record["gates"]["frame_integrity"]["passed"] is False and record["gates"]["anim"]["passed"] is False
    assert record["params"]["source"] == str(still_path)
