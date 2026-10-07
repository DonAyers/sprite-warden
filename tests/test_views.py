"""Tests for the cross-view (direction set) gate."""

from pathlib import Path

from PIL import Image

from spritewarden.judge.views import (
    GAME_ORDER,
    canonical_facing,
    check_direction_set,
    load_direction_frames,
)
from spritewarden.repo.identity import CharacterIdentity, FeatureLandmark

BODY = (89, 112, 128, 255)  # Luap-40 #597080
MARK = (255, 0, 255, 255)


def _view(canvas: int = 48, body_w: int = 16, body_h: int = 30, ground: int = 43, extra=None, marker_x=None) -> Image.Image:
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    x0 = (canvas - body_w) // 2
    y0 = ground - body_h + 1
    for y in range(y0, ground + 1):
        for x in range(x0, x0 + body_w):
            img.putpixel((x, y), BODY)
    for (x, y) in extra or []:
        img.putpixel((x, y), BODY)
    if marker_x is not None:
        img.putpixel((marker_x, ground - 10), MARK)
    return img


def _set(**overrides) -> dict[str, Image.Image]:
    frames = {f: _view() for f in GAME_ORDER}
    frames.update(overrides)
    return frames


def test_facing_aliases():
    assert canonical_facing("down_right") == "south_east"
    assert canonical_facing("SE") == "south_east"
    assert canonical_facing("south-east") == "south_east"
    assert canonical_facing("Up") == "north"
    assert canonical_facing("sideways") is None


def test_consistent_set_passes():
    gate = check_direction_set(_set(), palette="luap-40")
    assert gate.passed, gate.errors
    assert gate.metrics["anchor"]["source"] == "south view"
    assert len(gate.metrics["views"]) == 8


def test_orbs_in_some_views_are_named():
    frames = _set(south_east=_view(extra=[(6, 20), (7, 20), (6, 21)]), north=_view(extra=[(40, 18), (41, 18)]))
    gate = check_direction_set(frames)
    assert gate.passed is False
    detail = gate.details["checks"]["components"]["detail"]
    assert "south_east has 1 extra part" in detail and "north has 1 extra part" in detail
    assert "[6, 20, 8, 22]" in detail


def test_ground_height_and_area_tolerances():
    frames = _set(east=_view(ground=41))  # ground row 2px off
    gate = check_direction_set(frames)
    assert gate.details["checks"]["ground_row"]["passed"] is False
    assert "east" in gate.details["checks"]["ground_row"]["detail"]

    frames = _set(west=_view(body_h=25))  # 5px shorter
    gate = check_direction_set(frames)
    assert gate.details["checks"]["height"]["passed"] is False

    frames = _set(north_west=_view(body_w=10))  # 37% less area, same height
    gate = check_direction_set(frames)
    assert gate.details["checks"]["area"]["passed"] is False
    assert gate.details["checks"]["height"]["passed"] is True

    frames = _set(north_east=_view(body_w=14))  # 12% less area: fine
    assert check_direction_set(frames).details["checks"]["area"]["passed"] is True


def test_four_views_and_anchor_override():
    frames = {f: _view() for f in ("south", "east", "north", "west")}
    gate = check_direction_set(frames, anchor=_view(body_h=30))
    assert gate.passed and gate.metrics["anchor"]["source"] == "given anchor"
    too_few = check_direction_set({"south": _view(), "north": _view()})
    assert too_few.passed is False and "Need 4-8" in too_few.summary


def test_tool_side_expectation_table():
    ident = CharacterIdentity(
        id="t", name="T", prompt_description="bot",
        features={"wrench": FeatureLandmark(name="wrench", description="wrench in the left hand", marker_hex="#ff00ff")},
    )
    # Character's left hand: screen right in S/SE/SW, screen left in N/NE/NW, unknown in E/W.
    right_x, left_x = 40, 8
    good = {
        "south": _view(marker_x=right_x), "south_east": _view(marker_x=right_x), "south_west": _view(marker_x=right_x),
        "north": _view(marker_x=left_x), "north_east": _view(marker_x=left_x), "north_west": _view(marker_x=left_x),
        "east": _view(marker_x=right_x), "west": _view(marker_x=left_x),
    }
    gate = check_direction_set(good, identity=ident)
    assert gate.details["checks"]["tool_side"]["passed"] is True, gate.details["checks"]["tool_side"]

    bad = dict(good)
    bad["north"] = _view(marker_x=right_x)  # wrench swapped hands in the back view
    bad["south_west"] = _view()  # wrench vanished
    gate = check_direction_set(bad, identity=ident)
    detail = gate.details["checks"]["tool_side"]["detail"]
    assert gate.details["checks"]["tool_side"]["passed"] is False
    assert "wrong" not in detail and "north" in detail and "expected left" in detail
    assert "wrench missing in south_west" in detail


def test_palette_check_names_views():
    frames = _set(west=_view(extra=[]))
    frames["west"].putpixel((20, 30), (12, 200, 90, 255))
    gate = check_direction_set(frames, palette="luap-40")
    assert gate.details["checks"]["palette"]["passed"] is False
    assert "west" in gate.details["checks"]["palette"]["detail"]


def test_load_frames_from_folder_and_strip(tmp_path: Path):
    folder = tmp_path / "views"
    folder.mkdir()
    for name in ("south", "down_right", "right", "up_right", "up", "up_left", "left", "down_left"):
        _view().save(folder / f"bot_{name}.png")
    frames = load_direction_frames(folder)
    assert set(frames) == set(GAME_ORDER)

    strip = Image.new("RGBA", (48 * 8, 48), (0, 0, 0, 0))
    for i in range(8):
        strip.paste(_view(), (i * 48, 0))
    strip_path = tmp_path / "strip.png"
    strip.save(strip_path)
    frames = load_direction_frames(strip_path)  # cell inferred from height
    assert list(frames) == GAME_ORDER
    four = Image.new("RGBA", (48 * 4, 48), (0, 0, 0, 0))
    for i in range(4):
        four.paste(_view(), (i * 48, 0))
    four_path = tmp_path / "four.png"
    four.save(four_path)
    assert list(load_direction_frames(four_path, cell=48)) == ["south", "east", "north", "west"]
