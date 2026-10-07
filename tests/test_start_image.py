"""Tests for the start-image readiness gate."""

from PIL import Image

from spritewarden.judge import evaluate_still
from spritewarden.judge.start_image import check_start_image, find_components
from spritewarden.repo.identity import CharacterIdentity, FeatureLandmark

BODY = (89, 112, 128, 255)  # #597080, Luap-40


def _anchor(canvas: int = 64, body_h: int = 40, body_w: int = 20, color=BODY) -> Image.Image:
    """A symmetric rectangular body centred on a square canvas with a clear margin."""
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    x0 = (canvas - body_w) // 2
    y0 = canvas - 4 - body_h
    for y in range(y0, y0 + body_h):
        for x in range(x0, x0 + body_w):
            img.putpixel((x, y), color)
    return img


def test_clean_anchor_passes():
    res = check_start_image(_anchor(), target_height=40, palette="luap-40")
    assert res.passed, res.errors
    assert res.metrics["components"] == 1
    assert res.metrics["pixel_scale"] == 1
    checks = res.details["checks"]
    assert checks["symmetry"]["passed"] and checks["margin"]["passed"] and checks["content_height"]["passed"]


def test_stray_orbs_fail_and_are_located():
    img = _anchor()
    # two tiny "orbs" beside the head, not touching the body
    for x, y in ((8, 24), (9, 24), (8, 25), (55, 24), (55, 25)):
        img.putpixel((x, y), BODY)
    res = check_start_image(img, target_height=40, palette="luap-40")
    assert res.passed is False
    assert res.details["checks"]["strays"]["passed"] is False
    assert len(res.metrics["strays"]) == 2
    assert any("stray" in e for e in res.errors)
    comp = find_components(img)
    assert comp["count"] == 3 and comp["main"]["pixels"] == 40 * 20


def test_margin_square_and_symmetry_checks():
    img = _anchor()
    # art touching the margin
    for y in range(20, 60):
        img.putpixel((1, y), BODY)
    res = check_start_image(img, target_height=40)
    assert res.details["checks"]["margin"]["passed"] is False
    assert res.details["checks"]["symmetry"]["passed"] is False  # one-sided slab breaks symmetry

    wide = Image.new("RGBA", (96, 64), (0, 0, 0, 0))
    wide.paste(_anchor(), (16, 0))
    res = check_start_image(wide, target_height=40)
    assert res.details["checks"]["square_canvas"]["passed"] is False

    # east facings skip symmetry
    asym = _anchor()
    for y in range(30, 50):
        asym.putpixel((44, y), BODY)
    assert check_start_image(asym, facing="east", target_height=40).details["checks"]["symmetry"]["passed"] is None
    assert check_start_image(asym, facing="south", target_height=40).details["checks"]["symmetry"]["passed"] is False


def test_height_tier_and_target():
    img = _anchor(canvas=96, body_h=60)
    assert check_start_image(img, size_tier="48-64").details["checks"]["content_height"]["passed"] is True
    assert check_start_image(img, size_tier="128").details["checks"]["content_height"]["passed"] is False
    assert check_start_image(img, target_height=40).details["checks"]["content_height"]["passed"] is False
    assert check_start_image(img, target_height=58).details["checks"]["content_height"]["passed"] is True
    assert check_start_image(img, size_tier="bogus").passed is False


def test_upscaled_art_is_not_native():
    import random

    rnd = random.Random(3)
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    for y in range(6, 26):
        for x in range(10, 22):
            img.putpixel((x, y), rnd.choice([(50, 60, 70, 255), (200, 100, 30, 255)]))
    up = img.resize((64, 64), Image.NEAREST)
    res = check_start_image(up, facing="east")
    assert res.details["checks"]["native_pixels"]["passed"] is False
    assert res.metrics["pixel_scale"] == 2


def test_identity_supplies_palette_cell_and_markers():
    img = _anchor()
    img.putpixel((26, 40), (255, 0, 255, 255))  # left hand marker
    img.putpixel((37, 40), (255, 0, 255, 255))
    ident = CharacterIdentity(
        id="t", name="T", palette_id="luap-40", dimensions=[64, 64], prompt_description="bot",
        features={"hands": FeatureLandmark(name="hands", description="hand markers", marker_hex="#ff00ff"),
                  "visor": FeatureLandmark(name="visor", description="visor", marker_hex="#00ff00")},
    )
    res = check_start_image(img, identity=ident)
    assert res.details["checks"]["content_height"]["passed"] is True  # 40px inside the 64 cell
    assert res.details["checks"]["palette"]["passed"] is False  # magenta marker is off-palette
    assert res.metrics["landmarks"]["hands"]["pixels"] == 2
    assert res.metrics["landmarks"]["visor"] is None
    assert any("visor" in w for w in res.warnings)


def test_evaluate_still_opt_in():
    card = evaluate_still(_anchor(), "luap-40")
    assert card.gate("start_image") is None
    card = evaluate_still(_anchor(), "luap-40", start_image=True, target_height=40)
    assert card.gate("start_image") is not None and card.passed
