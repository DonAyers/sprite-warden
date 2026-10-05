"""Image Reducer: downscale, quantize, and snap any image onto a pixel palette."""

from .core import ReduceParams, PixelizeResult, pixelize, snap_palette
from .metrics import delta_stats, palette_usage_stats, deterministic_score
from .iterate import refine, TuningReport, CandidateScore

__all__ = [
    "ReduceParams",
    "PixelizeResult",
    "pixelize",
    "snap_palette",
    "delta_stats",
    "palette_usage_stats",
    "deterministic_score",
    "refine",
    "TuningReport",
    "CandidateScore",
]
