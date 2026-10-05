"""Deterministic fidelity metrics for palette reduction candidates (the offline objective)."""

from __future__ import annotations

import numpy as np
from PIL import Image

from ..judge.palette import hex_to_rgb
from .core import oklab_from_rgb

FIDELITY_SCALE = 0.25  # mean Oklab distance that maps to zero fidelity points


def _flatten_white(img: Image.Image) -> Image.Image:
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    return Image.alpha_composite(bg, img.convert("RGBA"))


def reference_image(source: Image.Image, w: int, h: int) -> np.ndarray:
    """Area-average downsample of the source at the candidate's resolution (the continuous-tone ideal)."""
    ref = _flatten_white(source).resize((w, h), Image.Resampling.BOX).convert("RGB")
    return np.asarray(ref, dtype=np.float64)


def delta_stats(candidate: Image.Image, source: Image.Image) -> dict[str, float]:
    """Oklab distance between candidate and the BOX-downsampled source (mean, p95, max)."""
    cand = _flatten_white(candidate)
    cand_lab = oklab_from_rgb(np.asarray(cand.convert("RGB"), dtype=np.float64).reshape(-1, 3))
    ref = reference_image(source, candidate.width, candidate.height)
    ref_lab = oklab_from_rgb(ref.reshape(-1, 3))
    d = np.linalg.norm(cand_lab - ref_lab, axis=1)
    return {
        "delta_mean": float(d.mean()),
        "delta_p95": float(np.percentile(d, 95)),
        "delta_max": float(d.max()),
    }


def palette_usage_stats(candidate: Image.Image, palette_hex: list[str]) -> dict[str, float]:
    """How much of the palette the candidate actually uses and how dominant the top color is."""
    rgb = np.asarray(candidate.convert("RGB"), dtype=np.uint8).reshape(-1, 3)
    allowed = {hex_to_rgb(h) for h in palette_hex}
    off = int(sum(1 for px in rgb if tuple(int(v) for v in px) not in allowed))
    uniq, counts = np.unique(rgb, axis=0, return_counts=True)
    total = int(counts.sum())
    return {
        "coverage": round(len(uniq) / max(1, len(palette_hex)), 3),
        "used_count": int(len(uniq)),
        "palette_size": len(palette_hex),
        "dominant_share": round(float(counts.max() / total), 3) if total else 0.0,
        "off_palette_pixels": off,
    }


def deterministic_score(delta: dict[str, float], usage: dict[str, float]) -> float:
    """Offline objective in 0..1: fidelity 60%, palette coverage 25%, non-degeneracy 15%."""
    fidelity = max(0.0, 1.0 - delta["delta_mean"] / FIDELITY_SCALE)
    coverage_component = min(1.0, usage["coverage"] / 0.6)
    degenerate = max(0.0, (usage["dominant_share"] - 0.8) / 0.2)
    return round(0.6 * fidelity + 0.25 * coverage_component + 0.15 * (1.0 - degenerate), 4)


def evaluate_candidate(
    candidate: Image.Image,
    source: Image.Image,
    palette_hex: list[str],
) -> tuple[float, dict[str, float]]:
    """Bundle all deterministic metrics + the composite offline score."""
    delta = delta_stats(candidate, source)
    usage = palette_usage_stats(candidate, palette_hex)
    metrics = {**delta, **usage, "score": deterministic_score(delta, usage)}
    return metrics["score"], metrics
