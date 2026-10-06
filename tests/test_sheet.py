"""Tests for the sprite-sheet builder and snapping engines (offline)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from spritewarden.judge.palette import check_palette, load_palette
from spritewarden.sheet import builder as builder_mod
from spritewarden.sheet import snap as snap_mod
from spritewarden.sheet import (
    build_sheet,
    build_sheets,
    detect_pixel_size,
    discover_inputs,
    find_pixel_snapper,
    resample_majority,
    snap_image,
)


@pytest.fixture
def no_rust(monkeypatch):
    """Force the Python fallback so tests never depend on the host binary."""
    monkeypatch.setattr(snap_mod, "find_pixel_snapper", lambda: None)


def _solid(path: Path, size: tuple[int, int], color: tuple[int, int, int, int]) -> Path:
    Image.new("RGBA", size, color).save(path)
    return path


def _vertical_stripes(blocks: int = 16, scale: int = 4) -> tuple[Image.Image, Image.Image]:
    base = Image.new("RGBA", (blocks, blocks))
    px = base.load()
    for y in range(blocks):
        for x in range(blocks):
            px[x, y] = ((x * 37 + 20) % 256, (x * 53 + 80) % 256, (x * 97 + 10) % 256, 255)
    return base, base.resize((base.width * scale, base.height * scale), Image.NEAREST)


def test_discover_inputs_natural_order_and_filter(tmp_path):
    for name in ("frame1.png", "frame2.png", "frame10.png"):
        _solid(tmp_path / name, (4, 4), (10, 20, 30, 255))
    (tmp_path / "notes.txt").write_text("ignore me")
    nested = tmp_path / "sub"
    nested.mkdir()
    _solid(nested / "extra.png", (4, 4), (1, 2, 3, 255))

    found = discover_inputs([tmp_path])
    assert [p.name for p in found] == ["frame1.png", "frame2.png", "frame10.png"]

    recursive = discover_inputs([tmp_path], recursive=True)
    assert [p.name for p in recursive] == ["extra.png", "frame1.png", "frame2.png", "frame10.png"]

    globbed = discover_inputs([str(tmp_path / "frame*.png")])
    assert len(globbed) == 3


def test_find_pixel_snapper_type():
    assert find_pixel_snapper() is None or isinstance(find_pixel_snapper(), str)


def test_detect_and_resample_grid():
    base, upscaled = _vertical_stripes(blocks=16, scale=4)  # 64x64, grid step 4
    step = detect_pixel_size(upscaled)
    assert step == 4
    restored = resample_majority(upscaled, step)
    assert restored.size == (16, 16)
    assert list(restored.getdata()) == list(base.getdata())


def test_snap_image_python_engine(tmp_path, no_rust):
    src = _solid(tmp_path / "red.png", (8, 8), (200, 40, 40, 255))
    out = tmp_path / "out.png"
    result = snap_image(src, out, palette_source="sweetie-16", engine="python")
    assert result.engine == "python"
    assert out.exists()
    gate, _ = check_palette(out, "sweetie-16")
    assert gate.passed


def test_snap_image_rust_engine_uses_binary(tmp_path, monkeypatch):
    src = _solid(tmp_path / "src.png", (8, 8), (200, 40, 40, 255))
    out = tmp_path / "out.png"

    class _Proc:
        returncode = 0
        stdout = "Pixel size: 3.0px (auto-detected)\nOutput size: 3x3\n"
        stderr = ""

    def fake_run(cmd, capture_output, text):
        # Emulate the snapper writing a PNG and reporting the detected grid size.
        Image.new("RGBA", (3, 3), (255, 236, 214, 255)).save(cmd[2])
        return _Proc()

    monkeypatch.setattr(snap_mod, "find_pixel_snapper", lambda: "fake-snapper")
    monkeypatch.setattr(snap_mod.subprocess, "run", fake_run)

    result = snap_image(src, out, palette_source="sweetie-16", engine="auto")
    assert result.engine == "rust"
    assert result.pixel_size == 3.0


def test_build_sheet_layout_and_manifest(tmp_path, no_rust):
    sources = [
        _solid(tmp_path / "a.png", (10, 20), (10, 10, 10, 255)),
        _solid(tmp_path / "b.png", (16, 16), (20, 20, 20, 255)),
        _solid(tmp_path / "c.png", (12, 8), (30, 30, 30, 255)),
        _solid(tmp_path / "d.png", (8, 8), (40, 40, 40, 255)),
        _solid(tmp_path / "e.png", (8, 12), (50, 50, 50, 255)),
    ]
    out = tmp_path / "sheet.png"
    report = build_sheet(sources, out, palette=None, snap=False, columns=3, cell=24, margin=2)

    assert report.frame_count == 5
    assert report.columns == 3
    assert report.rows == 2
    assert report.cell_size == 24
    assert report.engine == "none"
    sheet = Image.open(out)
    assert sheet.size == (72, 48)
    assert out.exists() and Path(report.manifest_path).exists()

    manifest = json.loads(Path(report.manifest_path).read_text())
    assert len(manifest["frames"]) == 5
    # bottom-center, margin 2 -> frame 'a' (10x20) at y = 24 - 2 - 20 = 2
    assert report.frames[0].rect[1] == 2


def test_build_sheet_auto_cell_and_palette(tmp_path, no_rust):
    src = tmp_path / "src"
    src.mkdir()
    for i, color in enumerate([(200, 40, 40, 255), (40, 200, 40, 255), (40, 40, 200, 255)]):
        _solid(src / f"icon{i}.png", (20 + i, 20), color)

    report = build_sheet([src], tmp_path / "icons.png", palette="sweetie-16", engine="python", align="center")
    assert report.frame_count == 3
    assert report.engine == "python"
    assert report.cell_size == 22  # max content 22px
    gate, _ = check_palette(report.sheet_path, "sweetie-16")
    assert gate.passed


def test_build_sheets_group_by_prefix(tmp_path, no_rust):
    src = tmp_path / "frames"
    src.mkdir()
    for name in ("walk_1.png", "walk_2.png", "idle_1.png", "idle_2.png"):
        _solid(src / name, (8, 8), (60, 60, 60, 255))

    reports = build_sheets([src], tmp_path / "sheets", group_by="prefix", palette=None, snap=False)
    names = sorted(Path(r.sheet_path).stem for r in reports)
    assert names == ["idle", "walk"]
    assert all(r.frame_count == 2 for r in reports)


def test_power_of_two_cell(tmp_path, no_rust):
    sources = [_solid(tmp_path / f"p{i}.png", (18, 18), (i, i, i, 255)) for i in range(2)]
    report = build_sheet(sources, tmp_path / "p.png", palette=None, snap=False, power_of_two=True)
    assert report.cell_size == 32
