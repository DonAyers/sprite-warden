"""Grid/palette snapping for the sprite-sheet builder.

Primary engine: the Rust ``spritefusion-pixel-snapper`` binary (detects the
implicit pixel grid, snaps to it, and optionally recolors to a fixed palette).
Graceful fallback: a pure-Python grid detector + majority-vote resampler and the
Oklab ``remap_image`` palette snapper, so the pipeline never hard-fails when the
binary is absent or errors out.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from ..judge.palette import load_palette, remap_image, rgb_to_hex

SNAPPER_ENV = "SPRITEWARDEN_PIXEL_SNAPPER"
_SNAPPER_NAMES = ("spritefusion-pixel-snapper", "spritefusion-pixel-snapper.exe")
_PIXEL_SIZE_RE = re.compile(r"Pixel size:\s*([0-9.]+)px")


def find_pixel_snapper() -> str | None:
    """Locate the Rust snapper: explicit env override, PATH, then ~/.cargo/bin."""
    override = os.environ.get(SNAPPER_ENV)
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return str(candidate)
    for name in _SNAPPER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    cargo_bin = Path.home() / ".cargo" / "bin"
    for name in _SNAPPER_NAMES:
        candidate = cargo_bin / name
        if candidate.is_file():
            return str(candidate)
    return None


def palette_hexes(palette_source) -> list[str]:
    """Resolve any palette source to a list of 6-digit hex codes (no leading #)."""
    return [rgb_to_hex(c).lstrip("#") for c in load_palette(palette_source)]


@dataclass
class SnapResult:
    path: Path
    pixel_size: float | None
    engine: str
    warnings: list[str] = field(default_factory=list)


def _run_rust(
    bin_path: str,
    src: Path,
    dst: Path,
    hexes: list[str],
    pixel_size: int | None,
) -> float | None:
    k_colors = max(2, len(hexes)) if hexes else 16
    cmd = [bin_path, str(src), str(dst), str(k_colors)]
    if hexes:
        cmd += ["--palette", ",".join(hexes)]
    if pixel_size:
        cmd += ["--pixel-size", str(pixel_size)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError((proc.stderr or proc.stdout or "snapper failed").strip()[:300])
    match = _PIXEL_SIZE_RE.search(proc.stdout)
    return float(match.group(1)) if match else None


def _luminance_profile(img: Image.Image) -> tuple[list[float], list[float]]:
    width, height = img.size
    px = img.load()

    def lum(x: int, y: int) -> float:
        r, g, b, a = px[x, y]
        return 0.0 if a == 0 else 0.299 * r + 0.587 * g + 0.114 * b

    col = [0.0] * width
    row = [0.0] * height
    for y in range(height):
        for x in range(1, width - 1):
            col[x] += abs(lum(x + 1, y) - lum(x - 1, y))
    for x in range(width):
        for y in range(1, height - 1):
            row[y] += abs(lum(x, y + 1) - lum(x, y - 1))
    return col, row


def _estimate_step(profile: list[float], peak_threshold: float = 0.2, min_peak_dist: int = 3) -> int | None:
    if not profile:
        return None
    peak = max(profile)
    if peak <= 0:
        return None
    threshold = peak * peak_threshold
    # Collect contiguous runs above threshold and use each run's midpoint as a peak.
    # This handles upscaled/blocky art where a block boundary spans several plateaus.
    peaks: list[int] = []
    i = 0
    limit = len(profile)
    while i < limit:
        if profile[i] > threshold:
            j = i
            while j + 1 < limit and profile[j + 1] > threshold:
                j += 1
            peaks.append((i + j) // 2)
            i = j + 1
        else:
            i += 1
    if len(peaks) < 2:
        return None
    clean = [peaks[0]]
    for p in peaks[1:]:
        if p - clean[-1] > min_peak_dist - 1:
            clean.append(p)
    if len(clean) < 2:
        return None
    diffs = sorted(clean[i + 1] - clean[i] for i in range(len(clean) - 1))
    step = diffs[len(diffs) // 2]
    return step if step >= 2 else None


def detect_pixel_size(img: Image.Image) -> int:
    """Estimate the dominant pixel block size (1 == already native / undetected)."""
    if img.width < 3 or img.height < 3:
        return 1
    col, row = _luminance_profile(img)
    sx = _estimate_step(col)
    sy = _estimate_step(row)
    if sx and sy:
        ratio = max(sx, sy) / max(1, min(sx, sy))
        return int(max(1, round(min(sx, sy) if ratio > 1.8 else (sx + sy) / 2)))
    if sx:
        return int(max(1, sx))
    if sy:
        return int(max(1, sy))
    return 1


def resample_majority(img: Image.Image, step: int) -> Image.Image:
    """Collapse each step x step block to its most common pixel (ties -> lowest RGBA)."""
    step = max(1, int(step))
    if step == 1:
        return img
    src = img.load()
    out_w = max(1, img.width // step)
    out_h = max(1, img.height // step)
    out = Image.new("RGBA", (out_w, out_h))
    dst = out.load()
    for oy in range(out_h):
        for ox in range(out_w):
            counts: dict[tuple[int, int, int, int], int] = {}
            for y in range(oy * step, min((oy + 1) * step, img.height)):
                for x in range(ox * step, min((ox + 1) * step, img.width)):
                    pixel = src[x, y]
                    counts[pixel] = counts.get(pixel, 0) + 1
            dst[ox, oy] = max(counts.items(), key=lambda kv: (kv[1], tuple(-c for c in kv[0])))[0]
    return out


def snap_image(
    src: str | Path,
    dst: str | Path,
    palette_source=None,
    engine: str = "auto",
    pixel_size: int | None = None,
) -> SnapResult:
    """Snap one image to its pixel grid and palette, writing the result to ``dst``."""
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    if engine not in ("auto", "rust", "python", "none"):
        raise ValueError(f"unknown snap engine: {engine!r}")
    if engine == "none":
        img = Image.open(src).convert("RGBA")
        if palette_source is not None:
            img, _, _ = remap_image(img, palette_source)
        img.save(dst)
        return SnapResult(dst, None, "none", warnings)

    bin_path = find_pixel_snapper() if engine in ("auto", "rust") else None
    if engine == "rust" and not bin_path:
        warnings.append("spritefusion-pixel-snapper not found; using python fallback")

    if bin_path:
        try:
            hexes = palette_hexes(palette_source) if palette_source is not None else []
            detected = _run_rust(bin_path, src, dst, hexes, pixel_size)
            if palette_source is not None:
                # Guarantee exact Oklab adherence even though the Rust palette match is RGB.
                img, _, _ = remap_image(Image.open(dst).convert("RGBA"), palette_source)
                img.save(dst)
            return SnapResult(dst, detected, "rust", warnings)
        except Exception as exc:  # noqa: BLE001 - fall back to the Python engine
            warnings.append(f"rust snapper failed ({exc}); using python fallback")

    img = Image.open(src).convert("RGBA")
    step = int(pixel_size) if pixel_size else detect_pixel_size(img)
    if step > 1:
        img = resample_majority(img, step)
    if palette_source is not None:
        img, _, _ = remap_image(img, palette_source)
    img.save(dst)
    return SnapResult(dst, float(step) if step > 1 else None, "python", warnings)
