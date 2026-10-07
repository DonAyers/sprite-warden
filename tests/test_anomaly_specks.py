"""animation_anomalies: a repeating detached speck, or one over 24px, is an error (was a warning)."""

from PIL import Image

from spritewarden.judge import evaluate_animation
from spritewarden.judge.animation import check_animation_anomalies

BODY = (89, 112, 128, 255)
SPECK = (220, 200, 60, 255)


def _frame(shift=0, speck=None, body_h=20) -> Image.Image:
    """A 12px-wide body; ``speck`` = (x, y, w, h) of a detached blob."""
    img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
    for y in range(40 - body_h, 40):
        for x in range(18 + shift, 30 + shift):
            img.putpixel((x, y), BODY)
    if speck:
        x0, y0, w, h = speck
        for y in range(y0, y0 + h):
            for x in range(x0, x0 + w):
                img.putpixel((x, y), SPECK)
    return img


def test_single_small_speck_stays_a_warning_free_pass():
    frames = [_frame(), _frame(speck=(4, 6, 3, 3)), _frame(), _frame(), _frame(), _frame()]
    gate = check_animation_anomalies(frames)
    assert gate.passed is True
    assert gate.metrics["detached_specks"] == [{"frame": 1, "pixels": 9, "bbox": [4, 6, 7, 9]}]
    assert gate.metrics["repeated_specks"] == [] and gate.metrics["speck_error_frames"] == []


def test_speck_repeating_in_two_frames_is_an_error():
    frames = [_frame(), _frame(speck=(4, 6, 3, 3)), _frame(), _frame(speck=(4, 6, 3, 3)), _frame(), _frame()]
    gate = check_animation_anomalies(frames)
    assert gate.passed is False
    assert gate.metrics["repeated_specks"] == [{"frames": [1, 3], "pixels": 9, "bbox": [4, 6, 7, 9]}]
    assert "repeats in frames [1, 3]" in gate.errors[0] and "9px" in gate.errors[0]
    assert "detached speck(s) in frame(s) [1, 3]" in gate.summary
    # two different specks in two places are not "the same speck"
    frames = [_frame(), _frame(speck=(4, 6, 3, 3)), _frame(), _frame(speck=(40, 30, 3, 3)), _frame(), _frame()]
    assert check_animation_anomalies(frames).passed is True


def test_detached_island_over_24px_is_an_error():
    frames = [_frame(), _frame(speck=(4, 6, 5, 5)), _frame(), _frame(), _frame()]  # 25px
    gate = check_animation_anomalies(frames)
    assert gate.passed is False and gate.metrics["speck_error_frames"] == [1]
    assert "detached island of 25px" in gate.errors[0] and "[4, 6, 9, 11]" in gate.errors[0]
    frames = [_frame(), _frame(speck=(4, 6, 6, 4)), _frame(), _frame(), _frame()]  # 24px: at the limit
    assert check_animation_anomalies(frames).passed is True


def test_body_motion_is_not_a_speck():
    # a two-phase bob (every frame differs from both neighbours, which agree) is the body moving
    bob = [_frame(body_h=20), _frame(body_h=23), _frame(body_h=20), _frame(body_h=23)]
    gate = check_animation_anomalies(bob)
    assert gate.passed is True and gate.metrics["detached_specks"] == []
    # an arm that sticks out for one frame is attached to the body: 5x6 = 30px, not an error
    punch = _frame()
    for y in range(24, 30):
        for x in range(30, 35):
            punch.putpixel((x, y), BODY)
    gate = check_animation_anomalies([_frame(), punch, _frame(), _frame(), _frame()])
    assert gate.passed is True and gate.metrics["detached_specks"] == []


def test_evaluate_animation_fails_on_repeating_specks():
    frames = [_frame(), _frame(speck=(4, 6, 3, 3)), _frame(), _frame(speck=(4, 6, 3, 3))]
    card = evaluate_animation(frames, palette_source=None)
    assert card.gate("animation_anomalies").passed is False and card.passed is False
