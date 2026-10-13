"""Tests for the spritewarden.judge suite."""

import pytest
from PIL import Image

from spritewarden.judge.pixel import check_pixel_grid
from spritewarden.judge.palette import check_palette, remap_image, LUAP_40_PLUS_HEX
from spritewarden.judge.bounds import check_clipping, check_symmetry
from spritewarden.judge.animation import check_animation_anomalies
from spritewarden.judge.walk import check_walk_cycle
from spritewarden.judge.loop import check_loop_smoothness


def test_pixel_grid_flat_vs_mixel():
    # 2x2 scaled image: 4x4 image with 2x2 flat blocks
    clean = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
    res, _ = check_pixel_grid(clean, scale=2)
    assert res.passed is True
    assert res.score == 1.0

    # Off-grid / mixel: single pixel changed
    dirty = clean.copy()
    dirty.putpixel((0, 1), (0, 255, 0, 255))
    res_dirty, mask = check_pixel_grid(dirty, scale=2)
    assert res_dirty.passed is False
    assert res_dirty.metrics["bad_blocks"] > 0
    assert mask is not None


def test_palette_check_and_remap():
    # An image with only Luap-40 color
    clean = Image.new("RGBA", (8, 8), (89, 112, 128, 255))  # #597080
    res, _ = check_palette(clean, "luap-40")
    assert res.passed is True

    # An image with completely unaligned color
    dirty = Image.new("RGBA", (8, 8), (123, 234, 45, 255))
    res_dirty, _ = check_palette(dirty, "luap-40")
    assert res_dirty.passed is False
    assert res_dirty.metrics["bad_pixels"] == 64

    # Remapping brings it onto palette
    remapped, before, after = remap_image(dirty, "luap-40")
    res_remapped, _ = check_palette(remapped, "luap-40")
    assert res_remapped.passed is True


def test_clipping_gate():
    # Clean padded sprite
    img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    # Draw a 4x4 square in middle
    for y in range(6, 10):
        for x in range(6, 10):
            img.putpixel((x, y), (200, 200, 200, 255))
    res = check_clipping(img)
    assert res.passed is True

    # Cut sprite: bright pixel on border (x=0)
    img.putpixel((0, 8), (255, 255, 255, 255))
    res_cut = check_clipping(img)
    assert res_cut.passed is False
    assert "left" in res_cut.details["violations"][0]["edges"]


def test_symmetry_gate():
    # Symmetric 8x8 sprite
    img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    # Draw symmetric cross
    for y in range(2, 6):
        img.putpixel((3, y), (100, 100, 100, 255))
        img.putpixel((4, y), (100, 100, 100, 255))
    res = check_symmetry(img)
    assert res.passed is True
    assert res.metrics["silhouette_match"] == 1.0


def test_walk_cycle_gate():
    # Create 4 frames simulating walking
    # Alternating lift: frame 0 has right foot planted and left up; frame 2 vice versa
    frames = []
    for step in range(4):
        f = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        # Left foot at x=4, right foot at x=11
        # Ground is row 15
        left_y = 13 if step == 0 else 15
        right_y = 13 if step == 2 else 15
        f.putpixel((4, left_y), (100, 100, 100, 255))
        f.putpixel((11, right_y), (100, 100, 100, 255))
        frames.append(f)

    res = check_walk_cycle(frames, lift_thresh=2)
    assert res.passed is True
    assert res.metrics["max_lift_left"] >= 2
    assert res.metrics["max_lift_right"] >= 2


def test_vision_judge_orchestration():
    from spritewarden.judge.vision import VisionJudge, prepare_sprite_for_vision
    from spritewarden.judge import evaluate_still

    img = Image.new("RGBA", (16, 16), (89, 112, 128, 255))
    data_url = prepare_sprite_for_vision(img, zoom=2)
    assert data_url.startswith("data:image/png;base64,")

    card = evaluate_still(img, "luap-40")
    judge = VisionJudge()
    verdict = judge.orchestrate_judgment(card, img, prompt_description="steel cube")
    assert verdict.recommended_action == 'review'
    assert verdict.decision_status == 'unknown'
    assert not verdict.passed
    assert 0.0 <= verdict.production_ready_prob <= 1.0


def _noisy_sprite(size: int = 32, pad: int = 4) -> Image.Image:
    import random

    rnd = random.Random(7)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    colors = [(50, 60, 70, 255), (200, 100, 30, 255), (90, 180, 120, 255)]
    for y in range(pad, size - pad):
        for x in range(pad, size - pad):
            img.putpixel((x, y), rnd.choice(colors))
    return img


def test_pixel_grid_infers_block_size_for_sprites():
    from spritewarden.judge.pixel import infer_scale

    native = _noisy_sprite()
    assert infer_scale(native) == (1, "native")
    for k in (2, 3, 4):
        up = native.resize((32 * k, 32 * k), Image.NEAREST)
        assert infer_scale(up) == (k, "detected")
        res, _ = check_pixel_grid(up)
        assert res.passed is True and res.metrics["scale"] == k
        assert any("not native" in w for w in res.warnings)
    # a mixel inside an upscaled image fails at the detected scale
    dirty = native.resize((64, 64), Image.NEAREST)
    dirty.putpixel((20, 20), (255, 255, 255, 255))
    res, _ = check_pixel_grid(dirty)
    assert res.metrics["scale"] == 2 and res.passed is False


def test_pixel_grid_keeps_screenshot_heuristic():
    from spritewarden.judge.pixel import infer_scale

    shot = Image.new("RGBA", (1280, 720), (10, 20, 30, 255))
    assert infer_scale(shot) == (2, "screenshot")
    assert infer_scale(Image.new("RGBA", (640, 360), (1, 2, 3, 255))) == (1, "screenshot")


def test_pixel_grid_shifted_origin_cannot_hide_a_mixel():
    clean = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
    dirty = clean.copy()
    dirty.putpixel((0, 1), (0, 255, 0, 255))  # on the border: an offset origin used to skip it
    res, _ = check_pixel_grid(dirty, scale=2)
    assert res.passed is False and res.metrics["origin"] == [0, 0]
