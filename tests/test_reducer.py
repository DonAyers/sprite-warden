"""Tests for the spritewarden.reducer suite (offline, deterministic)."""

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from spritewarden.judge.palette import check_palette
from spritewarden.reducer.core import (
    ReduceParams,
    bayer_matrix,
    oklab_from_rgb,
    pixelize,
    reduce_image,
    rgb_from_oklab,
    snap_palette,
)
from spritewarden.reducer.metrics import delta_stats, evaluate_candidate, palette_usage_stats
from spritewarden.reducer.iterate import (
    apply_suggestions,
    build_contact_sheet,
    extract_suggestions,
    initial_candidates,
    neighbor_candidates,
    refine,
)


def _gradient_image(w: int = 64, h: int = 64) -> Image.Image:
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    xx, yy = np.meshgrid(np.arange(w), np.arange(h))
    arr[:, :, 0] = (xx / max(1, w - 1) * 255).astype(np.uint8)
    arr[:, :, 1] = (yy / max(1, h - 1) * 255).astype(np.uint8)
    arr[:, :, 2] = ((xx + yy) / max(1, w + h - 2) * 255).astype(np.uint8)
    return Image.fromarray(arr, mode="RGB")


def test_oklab_roundtrip_matches_pure_python():
    from spritewarden.judge.palette import to_oklab

    rgb = (123, 45, 200)
    single = to_oklab(rgb)
    arr = oklab_from_rgb(np.array([rgb], dtype=np.float64))
    assert np.allclose(arr[0], single, atol=1e-6)
    back = rgb_from_oklab(arr)[0]
    # Oklab roundtrip should land within 1 unit per channel
    assert all(abs(int(back[i]) - rgb[i]) <= 1 for i in range(3))


def test_snap_palette_stays_on_palette_and_matches_guards():
    from spritewarden.judge.palette import load_palette

    palette = load_palette("luap-40")
    pal = np.array(palette, dtype=np.uint8)
    rgb = np.array([(0, 0, 0), (255, 255, 255), (100, 100, 100), (200, 30, 30)], dtype=np.uint8)
    snapped = snap_palette(rgb, pal, guards=True)
    assert set(tuple(int(v) for v in px) for px in snapped) <= set(palette)

    # Mid-grey must land on a low-chroma palette entry (chroma guard); sweetie-16 has no greys,
    # so it would legitimately fall back to all candidates there.
    grey = snap_palette(np.array([(100, 100, 100)], dtype=np.uint8), pal)[0]
    chroma = float(np.hypot(*(oklab_from_rgb(np.array([grey], dtype=np.float64))[0][1:])))
    assert chroma < 0.03


def test_bayer_matrix_normalized():
    for size in (2, 4, 8):
        m = bayer_matrix(size)
        assert m.shape == (size, size)
        assert m.min() >= -0.5 and m.max() < 0.5
        assert abs(m.mean()) < 0.01


def test_pixelize_honors_width_and_stays_on_palette():
    src = _gradient_image()
    params = ReduceParams(width=32)
    result = pixelize(src, "sweetie-16", params, auto_width=False)
    assert (result.image.width, result.image.height) == (32, 32)
    res, _ = check_palette(result.image, "sweetie-16")
    assert res.passed is True


def test_pixelize_auto_width_picks_ladder_value():
    src = _gradient_image(200, 200)
    result = pixelize(src, "sweetie-16", ReduceParams())
    from spritewarden.reducer.core import AUTO_WIDTH_LADDER

    assert result.image.width in AUTO_WIDTH_LADDER


def test_dither_changes_pixels_but_stays_on_palette():
    src = _gradient_image(32, 32)
    base = reduce_image(src, ReduceParams(width=32), np.array([(89, 112, 128), (198, 109, 45), (26, 28, 43)], dtype=np.uint8))
    dith = reduce_image(src, ReduceParams(width=32, dither="bayer4", dither_strength=1.0),
                        np.array([(89, 112, 128), (198, 109, 45), (26, 28, 43)], dtype=np.uint8))
    assert np.asarray(base).tobytes() != np.asarray(dith).tobytes()
    res, _ = check_palette(dith, [(89, 112, 128), (198, 109, 45), (26, 28, 43)])
    assert res.passed is True


def test_dominant_mode_preserves_flat_blocks():
    img = Image.new("RGBA", (64, 64), (10, 200, 10, 255))
    img.putpixel((63, 63), (10, 201, 10, 255))  # one near-identical pixel
    palette = np.array([(10, 200, 10)], dtype=np.uint8)
    out = reduce_image(img, ReduceParams(width=8, downscale="dominant"), palette)
    colors = {tuple(px) for px in np.asarray(out.convert("RGB")).reshape(-1, 3)}
    assert colors == {(10, 200, 10)}


def test_alpha_threshold_and_opaque():
    img = Image.new("RGBA", (16, 16), (200, 200, 200, 40))
    palette = np.array([(89, 112, 128)], dtype=np.uint8)
    thr = reduce_image(img, ReduceParams(width=8, alpha="threshold"), palette)
    assert set(int(a) for a in np.asarray(thr)[:, :, 3].reshape(-1)) == {0}
    opq = reduce_image(img, ReduceParams(width=8, alpha="opaque"), palette)
    assert set(int(a) for a in np.asarray(opq)[:, :, 3].reshape(-1)) == {255}


def test_delta_stats_zero_for_identical():
    src = _gradient_image(24, 24)
    stats = delta_stats(src, src)
    assert stats["delta_mean"] < 1e-9


def test_usage_and_deterministic_score_ranges():
    src = _gradient_image(32, 32)
    cand = reduce_image(src, ReduceParams(width=32), np.array([(26, 28, 43), (246, 228, 216)], dtype=np.uint8))
    usage = palette_usage_stats(cand, ["#1a1c2b", "#f6e4d8"])
    assert 0.0 <= usage["coverage"] <= 1.0
    assert 0.0 <= usage["dominant_share"] <= 1.0
    score, metrics = evaluate_candidate(cand, src, ["#1a1c2b", "#f6e4d8"])
    assert 0.0 <= score <= 1.0
    assert metrics["off_palette_pixels"] == 0


def test_apply_suggestions_maps_phrases():
    base = ReduceParams(width=64, brightness=1.0, dither="bayer4", dither_strength=1.0)
    adjusted, applied = apply_suggestions(base.model_copy(), ["the result is too dark and noisy"])
    assert applied
    assert adjusted.brightness > 1.0
    assert adjusted.dither_strength < 1.0
    # width step clamping
    adjusted2, _ = apply_suggestions(ReduceParams(width=64), ["too blocky"])
    assert adjusted2.width == 96


def test_apply_suggestions_tolerates_unset_width():
    adjusted, applied = apply_suggestions(ReduceParams(height=64), ["too dark"])
    assert applied == ["brighten"]
    assert adjusted.brightness > 1.0
    assert adjusted.width is None


def test_extract_suggestions_prefers_best_and_set_level():
    from spritewarden.reducer.iterate import VisionCandidateVerdict, VisionCritique

    critique = VisionCritique(
        candidates={
            "A": VisionCandidateVerdict(id="A", artifacts=["A artifact"], notes="A note"),
            "B": VisionCandidateVerdict(id="B", artifacts=["B artifact"], notes="B note"),
        },
        best="B",
        suggestions=["too dark"],
    )
    phrases = extract_suggestions(critique)
    assert phrases[0] == "too dark"
    assert "B artifact" in phrases and "B note" in phrases
    assert "A artifact" not in phrases and "A note" not in phrases


def test_initial_and_neighbor_candidates_deduped():
    base = ReduceParams()
    cands = initial_candidates((256, 256), base)
    assert len(cands) >= 3
    anchor = ReduceParams(width=64)
    around = neighbor_candidates(anchor, 0)
    keys = [c.model_dump_json() for c in around]
    assert len(keys) == len(set(keys))
    assert all(c.model_dump_json() != anchor.model_dump_json() for c in around)


def test_refine_deterministic_fallback(tmp_path):
    src = _gradient_image(48, 48)
    src.save(tmp_path / "src.png")
    report = refine(
        str(tmp_path / "src.png"),
        "sweetie-16",
        out_dir=tmp_path / "session",
        max_rounds=2,
        use_vision=True,
        api_key="",  # force deterministic fallback regardless of host key
    )
    assert report.used_vision is False
    assert report.best_image
    final = Image.open(report.best_image)
    res, _ = check_palette(final, "sweetie-16")
    assert res.passed is True
    assert (tmp_path / "session" / "report.json").exists()
    report_data = json.loads((tmp_path / "session" / "report.json").read_text(encoding="utf-8"))
    assert report_data["best"]["params"]["width"] is not None


def test_refine_height_only_params(tmp_path):
    src = _gradient_image(40, 40)
    report = refine(
        src,
        "sweetie-16",
        params=ReduceParams(height=24),
        out_dir=tmp_path / "session",
        max_rounds=1,
        use_vision=True,
        api_key="",  # regression: _clamp_param used to crash when width is None
    )
    assert report.best.params.height == 24
    assert report.best.params.width is None


def test_contact_sheet_compose():
    src = _gradient_image(32, 32)
    cand = src.resize((16, 16), Image.Resampling.BOX)
    sheet = build_contact_sheet(src, [("A", cand)])
    assert sheet.width > cand.width * 4
    assert sheet.height >= 360
