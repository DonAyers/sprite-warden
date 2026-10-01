"""Tests for spritewarden.tools.aseprite (executable discovery and palette generation)."""

import pytest
from pathlib import Path
from spritewarden.tools.aseprite import find_aseprite, write_gpl_palette


def test_find_aseprite():
    exe = find_aseprite()
    assert exe is not None
    assert exe.is_file()
    assert "Aseprite.exe" in exe.name


def test_write_gpl_palette(tmp_path: Path):
    gpl = tmp_path / "test.gpl"
    res = write_gpl_palette("luap-40", gpl)
    assert res.is_file()
    text = res.read_text(encoding="utf-8")
    assert "GIMP Palette" in text
    assert "Luap-40" in text
