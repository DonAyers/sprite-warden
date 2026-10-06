"""Tests for sprite padding (uniform canvas normalization)."""

from __future__ import annotations

import pytest
from PIL import Image

from pathlib import Path

from spritewarden.judge.palette import check_palette
from spritewarden.sheet import pad_files, pad_image, resolve_canvas
from spritewarden.sheet.pad import _shrink_to_fit


def _sprite(w: int = 20, h: int = 30, color=(200, 40, 40, 255)) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for y in range(h):
        for x in range(w):
            img.putpixel((x, y), color)
    return img


def test_pad_image_bottom_center():
    padded, warnings = pad_image(_sprite(), (64, 64))
    assert padded.size == (64, 64)
    assert warnings == []
    assert padded.getbbox() == (22, 34, 42, 64)  # x=(64-20)//2, bottom-aligned


def test_pad_image_align_and_margin():
    padded, _ = pad_image(_sprite(), (64, 64), align="top-left", margin=3)
    assert padded.getbbox() == (3, 3, 23, 33)

    padded, _ = pad_image(_sprite(), (64, 64), align="center", margin=2)
    bbox = padded.getbbox()
    assert bbox[1] == 2 + (60 - 30) // 2


def test_pad_image_preserves_pixels():
    padded, _ = pad_image(_sprite(color=(10, 200, 30, 255)), (32, 32))
    assert padded.getpixel((11, 22)) == (10, 200, 30, 255)  # bottom-center placement
    assert padded.getpixel((0, 0)) == (0, 0, 0, 0)


def test_pad_image_clips_oversized_with_warning():
    padded, warnings = pad_image(_sprite(80, 90), (64, 64))
    assert any("exceeds" in w for w in warnings)
    assert padded.size == (64, 64)


def test_shrink_to_fit_integer_and_exact():
    small, changed = _shrink_to_fit(_sprite(40, 40), 20, 20)
    assert changed and small.size == (20, 20)

    odd, changed = _shrink_to_fit(_sprite(100, 80), 64, 64)
    assert changed
    assert odd.width <= 64 and odd.height <= 64

    same, changed = _shrink_to_fit(_sprite(10, 10), 64, 64)
    assert not changed and same.size == (10, 10)


def test_resolve_canvas_modes():
    assert resolve_canvas(size=64) == (64, 64)
    assert resolve_canvas(canvas="96x64") == (96, 64)
    assert resolve_canvas(grow=5, content=(20, 30)) == (30, 40)
    assert resolve_canvas(size=50, power_of_two=True) == (64, 64)
    with pytest.raises(ValueError):
        resolve_canvas()


def test_pad_files_batch_folder(tmp_path):
    src = tmp_path / "raw"
    src.mkdir()
    for i, (w, h) in enumerate([(20, 30), (64, 64), (12, 48)]):
        _sprite(w, h).save(src / f"f{i}.png")

    report = pad_files([src], size=64, out=tmp_path / "out")
    assert report.canvas == [64, 64]
    assert len(report.outputs) == 3
    out = tmp_path / "out"
    assert {p.name for p in out.iterdir()} == {"f0.png", "f1.png", "f2.png"}
    for p in out.iterdir():
        assert Image.open(p).size == (64, 64)
        # bottom-centered: art must touch the bottom edge of the canvas
        assert Image.open(p).getbbox()[3] == 64


def test_pad_files_single_default_naming(tmp_path):
    src = tmp_path / "hero.png"
    _sprite().save(src)
    report = pad_files([str(src)], size=64)
    out = tmp_path / "hero.padded.png"
    assert report.outputs == [str(out)]
    assert Image.open(out).size == (64, 64)


def test_pad_files_grow_per_frame(tmp_path):
    src = tmp_path / "g"
    src.mkdir()
    _sprite(20, 30).save(src / "a.png")
    report = pad_files([src], grow=4, out=tmp_path / "o")
    assert report.canvas == [28, 38]
    assert Image.open(tmp_path / "o" / "a.png").getbbox() == (4, 4, 24, 34)


def test_pad_files_in_place(tmp_path):
    src = tmp_path / "s.png"
    _sprite(10, 10).save(src)
    pad_files([str(src)], size=32, in_place=True)
    assert Image.open(src).size == (32, 32)
    assert src == Path(src)  # same path, no siblings created
    assert not (tmp_path / "s.padded.png").exists()


def test_pad_files_with_palette(tmp_path):
    src = tmp_path / "p.png"
    _sprite(10, 10, color=(123, 45, 67, 255)).save(src)
    report = pad_files([str(src)], size=32, palette="sweetie-16", out=tmp_path / "o.png")
    gate, _ = check_palette(report.outputs[0], "sweetie-16")
    assert gate.passed
