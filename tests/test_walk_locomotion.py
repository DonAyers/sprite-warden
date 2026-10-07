"""Walk gate locomotion modes: wheels/treads check a steady turning base instead of cadence."""

import pytest
from PIL import Image

from spritewarden.judge import evaluate_animation
from spritewarden.judge.walk import check_walk_cycle

BODY = (89, 112, 128, 255)
RIM = (200, 200, 200, 255)
SPOKE = (40, 40, 40, 255)


def _wheel_bot(frame: int, *, ground: int = 29, hub_x: int = 16, hub_w: int = 10, bob: int = 0) -> Image.Image:
    """A box body on a wheel whose spoke turns with the frame index."""
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    g = ground - bob
    # body
    for y in range(g - 20, g - 8):
        for x in range(10, 22):
            img.putpixel((x, y), BODY)
    # wheel rim (8 rows tall)
    x0 = hub_x - hub_w // 2
    for y in range(g - 7, g + 1):
        for x in range(x0, x0 + hub_w):
            img.putpixel((x, y), RIM)
    # a turning spoke: one dark pixel walks around the rim
    spots = [(x0, g - 7), (x0 + hub_w - 1, g - 7), (x0 + hub_w - 1, g), (x0, g)]
    sx, sy = spots[frame % 4]
    img.putpixel((sx, sy), SPOKE)
    return img


def test_steady_turning_wheel_passes():
    frames = [_wheel_bot(i) for i in range(4)]
    res = check_walk_cycle(frames, locomotion="wheels", band=8)
    assert res.passed, res.errors
    assert res.metrics["locomotion"] == "wheels" and res.metrics["bob_frames"] == []
    assert not res.warnings


def test_legs_cadence_rejects_a_wheel_but_wheels_mode_accepts_it():
    frames = [_wheel_bot(i) for i in range(4)]
    assert check_walk_cycle(frames, locomotion="legs").passed is False
    assert check_walk_cycle(frames, locomotion="wheels", band=8).passed is True
    assert check_walk_cycle(frames, locomotion="treads", band=8).passed is True


def test_bobbing_base_fails():
    frames = [_wheel_bot(i, bob=(2 if i % 2 else 0)) for i in range(4)]
    res = check_walk_cycle(frames, locomotion="wheels", band=8)
    assert res.passed is False and "Ground row wanders" in res.errors[0]


def test_hub_width_and_drift_fail():
    frames = [_wheel_bot(i, hub_w=(14 if i == 2 else 10)) for i in range(4)]
    res = check_walk_cycle(frames, locomotion="wheels", band=8)
    assert res.passed is False and any("width" in e for e in res.errors)

    frames = [_wheel_bot(i, hub_x=(20 if i == 1 else 16)) for i in range(4)]
    res = check_walk_cycle(frames, locomotion="treads", band=8)
    assert res.passed is False and any("drifts" in e for e in res.errors)


def test_frozen_wheel_warns():
    frames = [_wheel_bot(0) for _ in range(4)]
    res = check_walk_cycle(frames, locomotion="wheels", band=8)
    assert res.passed is True
    assert any("does not visibly turn" in w for w in res.warnings)


def test_unknown_mode_and_evaluate_animation_wiring():
    frames = [_wheel_bot(i) for i in range(4)]
    with pytest.raises(ValueError):
        check_walk_cycle(frames, locomotion="hover")
    card = evaluate_animation(frames, is_walk=True, locomotion="wheels", palette_source=None)
    gate = card.gate("walk_mechanics")
    assert gate is not None and gate.metrics["locomotion"] == "wheels"
