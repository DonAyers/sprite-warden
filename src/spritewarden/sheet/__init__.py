"""Sprite-sheet building: snap loose frames into cohesive, evenly-celled sheets."""

from .snap import (
    SNAPPER_ENV,
    SnapResult,
    detect_pixel_size,
    find_pixel_snapper,
    palette_hexes,
    resample_majority,
    snap_image,
)
from .builder import (
    SheetFrame,
    SheetReport,
    build_sheet,
    build_sheets,
    discover_inputs,
    engine_report,
)

__all__ = [
    "SNAPPER_ENV",
    "SnapResult",
    "SheetFrame",
    "SheetReport",
    "detect_pixel_size",
    "find_pixel_snapper",
    "palette_hexes",
    "resample_majority",
    "snap_image",
    "build_sheet",
    "build_sheets",
    "discover_inputs",
    "engine_report",
]
