"""Core deterministic reducer: any image -> downscaled, quantized, palette-snapped pixel art."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
from pydantic import BaseModel

from ..judge.palette import (
    NEUTRAL_CHROMA,
    GREY_CHROMA,
    DARK_LIGHTNESS,
    HUE_WINDOW,
    load_palette,
    rgb_to_hex,
)

DownscaleMode = Literal["lanczos", "box", "nearest", "dominant"]
DitherMode = Literal["none", "bayer2", "bayer4", "bayer8", "floyd", "sierra"]
AlphaMode = Literal["threshold", "keep", "opaque"]

AUTO_WIDTH_LADDER = [16, 24, 32, 48, 64, 96, 128, 192, 256]
DITHER_AMPLITUDE = 0.075  # Oklab units for ordered dithering at strength 1.0

_BAYER2 = np.array([[0, 2], [3, 1]], dtype=np.float64)

_RESAMPLE = {
    "lanczos": Image.Resampling.LANCZOS,
    "box": Image.Resampling.BOX,
    "nearest": Image.Resampling.NEAREST,
}


class ReduceParams(BaseModel):
    """Knobs for the deterministic reduction pipeline (the axes the AI tuner explores)."""

    width: int | None = None
    height: int | None = None
    downscale: DownscaleMode = "lanczos"
    pre_blur: float = 0.0
    saturation: float = 1.0
    brightness: float = 1.0
    contrast: float = 1.0
    dither: DitherMode = "none"
    dither_strength: float = 1.0
    max_colors: int | None = None
    snap_guards: bool = True
    alpha: AlphaMode = "threshold"


@dataclass
class PixelizeResult:
    """A rendered candidate plus diagnostics."""

    image: Image.Image
    params: ReduceParams
    palette_hex: list[str]
    source_size: tuple[int, int]
    usage: list[tuple[str, int]] = field(default_factory=list)
    unique_colors_in: int = 0

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height


# ---------------------------------------------------------------------------
# Oklab helpers (vectorized mirrors of judge.palette.to_oklab)
# ---------------------------------------------------------------------------


def oklab_from_rgb(rgb: np.ndarray) -> np.ndarray:
    """(N,3) sRGB 0..255 -> (N,3) Oklab floats."""
    c = rgb.astype(np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = lin[:, 0], lin[:, 1], lin[:, 2]
    l = np.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m = np.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s = np.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return np.column_stack([
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    ])


def rgb_from_oklab(lab: np.ndarray) -> np.ndarray:
    """(N,3) Oklab -> (N,3) sRGB uint8 (clamped)."""
    lab = np.clip(lab, -0.6, 1.8)
    l, a, bb = lab[:, 0], lab[:, 1], lab[:, 2]
    l_ = l + 0.3963377774 * a + 0.2158037573 * bb
    m_ = l - 0.1055613458 * a - 0.0638541728 * bb
    s_ = l - 0.0894841775 * a - 1.2914855480 * bb
    l = l_ ** 3
    m = m_ ** 3
    s = s_ ** 3
    r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    lin = np.clip(np.column_stack([r, g, b]), 0.0, 1.0)
    srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.clip(np.round(srgb * 255.0), 0, 255).astype(np.uint8)


def _chroma_hue(lab: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    chroma = np.hypot(lab[:, 1], lab[:, 2])
    hue = np.degrees(np.arctan2(lab[:, 2], lab[:, 1]))
    return chroma, hue


def snap_palette(rgb: np.ndarray, palette_rgb: np.ndarray, guards: bool = True) -> np.ndarray:
    """Vectorized Oklab nearest-neighbor snap with the same grey/hue guards as judge.palette."""
    palette_lab = oklab_from_rgb(palette_rgb.astype(np.float64))
    p_chroma, p_hue = _chroma_hue(palette_lab)
    grey_mask = p_chroma < GREY_CHROMA if guards else np.zeros(len(palette_lab), dtype=bool)
    grey_idx = np.flatnonzero(grey_mask)
    has_greys = len(grey_idx) > 0

    out = np.empty((len(rgb), 3), dtype=np.uint8)
    CHUNK = 200_000
    for start in range(0, len(rgb), CHUNK):
        chunk = rgb[start:start + CHUNK].astype(np.float64)
        lab = oklab_from_rgb(chunk)
        dist = ((lab[:, None, :] - palette_lab[None, :, :]) ** 2).sum(axis=2)
        if guards:
            chroma, hue = _chroma_hue(lab)
            allowed = np.ones_like(dist, dtype=bool)
            neutral = chroma < NEUTRAL_CHROMA
            if has_greys:
                allowed[np.ix_(np.flatnonzero(neutral), np.flatnonzero(~grey_mask))] = False
            dark = lab[:, 0] < DARK_LIGHTNESS
            restrict = (~neutral) & (~dark)
            rows = np.flatnonzero(restrict)
            if len(rows):
                allowed[np.ix_(rows, np.flatnonzero(p_chroma < GREY_CHROMA))] = False
                hue_diff = np.abs((p_hue[None, :] - hue[:, None] + 180.0) % 360.0 - 180.0)
                allowed[rows] &= ~(hue_diff[rows] > HUE_WINDOW)
            empty_rows = ~allowed.any(axis=1)
            if empty_rows.any():
                allowed[empty_rows, :] = True
            dist = np.where(allowed, dist, np.inf)
        idx = np.argmin(dist, axis=1)
        out[start:start + CHUNK] = palette_rgb[idx]
    return out


def bayer_matrix(size: int) -> np.ndarray:
    """Recursive Bayer threshold matrix centered at 0 (values in [-0.5, 0.5))."""
    m = _BAYER2
    n = 2
    while n < size:
        m = np.block([[4 * m + 0, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
        n *= 2
    return (m + 0.5) / (n * n) - 0.5


def quantize_kmeans(lab: np.ndarray, k: int, iters: int = 12, seed: int = 0) -> np.ndarray:
    """Tiny Lloyd k-means over unique Oklab colors; returns per-pixel centroid labs."""
    k = max(2, min(k, len(lab)))
    uniq, inverse = np.unique(lab, axis=0, return_inverse=True)
    if len(uniq) <= k:
        return uniq[inverse]
    rng = np.random.default_rng(seed)
    centroids = [uniq[int(rng.integers(len(uniq)))]]
    for _ in range(k - 1):
        d = np.min(((uniq[:, None, :] - np.array(centroids)[None, :, :]) ** 2).sum(axis=2), axis=1)
        centroids.append(uniq[int(np.argmax(d))])
    cent = np.array(centroids, dtype=np.float64)
    dist = ((uniq[:, None, :] - cent[None, :, :]) ** 2).sum(axis=2)
    assign = np.argmin(dist, axis=1)
    for _ in range(iters):
        new = cent.copy()
        for c in range(k):
            members = uniq[assign == c]
            if len(members):
                new[c] = members.mean(axis=0)
        if np.allclose(new, cent, atol=1e-9):
            cent = new
            break
        cent = new
        dist = ((uniq[:, None, :] - cent[None, :, :]) ** 2).sum(axis=2)
        assign = np.argmin(dist, axis=1)
    return cent[assign]


# ---------------------------------------------------------------------------
# Dithering (applied on the small downscaled canvas, in Oklab)
# ---------------------------------------------------------------------------


def apply_ordered_dither(
    lab: np.ndarray,
    shape: tuple[int, int],
    mode: str,
    strength: float,
) -> np.ndarray:
    """lab is (N,3) in row-major order for an h x w canvas."""
    h, w = shape
    n = {"bayer2": 2, "bayer4": 4, "bayer8": 8}[mode]
    mat = bayer_matrix(n)
    tile = np.tile(mat, (math.ceil(h / n), math.ceil(w / n)))[:h, :w]
    offset = tile * strength * DITHER_AMPLITUDE
    return (lab.reshape(h, w, 3) + offset[:, :, None]).reshape(-1, 3)


_FLOYD_KERNEL = [(1, 0, 7 / 16), (-1, 1, 3 / 16), (0, 1, 5 / 16), (1, 1, 1 / 16)]
_SIERRA_KERNEL = [(1, 0, 2 / 4), (-1, 1, 1 / 4), (0, 1, 1 / 4)]


def apply_diffusion_dither(
    lab: np.ndarray,
    shape: tuple[int, int],
    palette_rgb: np.ndarray,
    mode: str,
    strength: float,
    guards: bool,
) -> np.ndarray:
    """Floyd-Steinberg / Sierra-lite error diffusion in Oklab with serpentine traversal."""
    h, w = shape
    kernel = _FLOYD_KERNEL if mode == "floyd" else _SIERRA_KERNEL
    work = lab.copy()
    out = np.zeros_like(lab)
    for y in range(h):
        rtl = y % 2 == 1
        for x in (range(w - 1, -1, -1) if rtl else range(w)):
            i = y * w + x
            target = work[i]
            snapped = snap_palette(rgb_from_oklab(target[None, :]), palette_rgb, guards)[0]
            snapped_lab = oklab_from_rgb(snapped[None, :].astype(np.float64))[0]
            out[i] = snapped_lab
            err = (target - snapped_lab) * strength
            for dx, dy, frac in kernel:
                kx = x - dx if rtl else x + dx
                ky = y + dy
                if 0 <= kx < w and ky < h:
                    work[ky * w + kx] += err * frac
    return out


# ---------------------------------------------------------------------------
# Downscale modes
# ---------------------------------------------------------------------------


def _dominant_downscale(
    img: Image.Image,
    tw: int,
    th: int,
    palette_rgb: np.ndarray,
    guards: bool,
) -> Image.Image:
    """Snap at full resolution then take the majority palette color per cell.

    Voting pools opaque pixels when present, else all pixels, so RGB is always
    meaningful; the alpha mode is applied separately in reduce_image.
    """
    rgba = np.asarray(img, dtype=np.uint8)
    h, w = rgba.shape[:2]
    sx, sy = w / tw, h / th
    snapped = snap_palette(rgba[:, :, :3].reshape(-1, 3), palette_rgb, guards).reshape(h, w, 3)
    alpha = rgba[:, :, 3]
    cells = np.zeros((th, tw, 3), dtype=np.uint8)
    for cy in range(th):
        y0 = int(round(cy * sy))
        y1 = max(int(round((cy + 1) * sy)), y0 + 1)
        for cx in range(tw):
            x0 = int(round(cx * sx))
            x1 = max(int(round((cx + 1) * sx)), x0 + 1)
            block = snapped[y0:y1, x0:x1].reshape(-1, 3)
            block_a = alpha[y0:y1, x0:x1].reshape(-1)
            pool = block[block_a >= 128]
            if not len(pool):
                pool = block
            uniq, counts = np.unique(pool, axis=0, return_counts=True)
            cells[cy, cx] = uniq[np.argmax(counts)]
    return Image.fromarray(cells, mode="RGB").convert("RGBA")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def _target_size(img: Image.Image, params: ReduceParams) -> tuple[int, int]:
    w, h = img.size
    if params.width and params.height:
        return params.width, params.height
    if params.width:
        return params.width, max(1, round(params.width * h / w))
    if params.height:
        return max(1, round(params.height * w / h)), params.height
    return max(1, round(w / 2)), max(1, round(h / 2))


def _adjust(img: Image.Image, params: ReduceParams) -> Image.Image:
    work = img
    if params.saturation != 1.0:
        work = ImageEnhance.Color(work).enhance(params.saturation)
    if params.brightness != 1.0:
        work = ImageEnhance.Brightness(work).enhance(params.brightness)
    if params.contrast != 1.0:
        work = ImageEnhance.Contrast(work).enhance(params.contrast)
    if params.pre_blur > 0:
        work = work.filter(ImageFilter.GaussianBlur(params.pre_blur))
    return work


def reduce_image(img: Image.Image, params: ReduceParams, palette_rgb: np.ndarray) -> Image.Image:
    """Downscale + quantize + dither + snap. Shared by pixelize and the width search."""
    tw, th = _target_size(img, params)
    work = _adjust(img, params)

    if params.downscale == "dominant":
        small = _dominant_downscale(work, tw, th, palette_rgb, params.snap_guards)
        flat_alpha = np.asarray(work.resize((tw, th), Image.Resampling.BOX))[:, :, 3].reshape(-1)
    else:
        small = work.resize((tw, th), _RESAMPLE[params.downscale]).convert("RGBA")
        flat_alpha = np.asarray(small)[:, :, 3].reshape(-1)

    arr = np.asarray(small, dtype=np.uint8)
    flat = arr[:, :, :3].reshape(-1, 3).astype(np.float64)

    if params.max_colors:
        lab = quantize_kmeans(oklab_from_rgb(flat), params.max_colors)
        flat = rgb_from_oklab(lab).astype(np.float64)

    if params.dither.startswith("bayer"):
        lab = apply_ordered_dither(oklab_from_rgb(flat), (th, tw), params.dither, params.dither_strength)
        snapped = snap_palette(rgb_from_oklab(lab).astype(np.float64), palette_rgb, params.snap_guards)
    elif params.dither in ("floyd", "sierra"):
        lab = apply_diffusion_dither(
            oklab_from_rgb(flat), (th, tw), palette_rgb, params.dither, params.dither_strength, params.snap_guards
        )
        snapped = snap_palette(rgb_from_oklab(lab).astype(np.float64), palette_rgb, params.snap_guards)
    else:
        snapped = snap_palette(flat, palette_rgb, params.snap_guards)

    if params.alpha == "opaque":
        alpha_out = np.full(len(snapped), 255, dtype=np.uint8)
    elif params.alpha == "keep":
        alpha_out = flat_alpha.astype(np.uint8)
    else:
        alpha_out = np.where(flat_alpha >= 128, 255, 0).astype(np.uint8)

    out = np.zeros((th * tw, 4), dtype=np.uint8)
    out[:, :3] = snapped
    out[:, 3] = alpha_out
    return Image.fromarray(out.reshape(th, tw, 4), mode="RGBA")


def _usage_of(small: Image.Image, palette_rgb: np.ndarray) -> list[tuple[str, int]]:
    rgb = np.asarray(small.convert("RGB"), dtype=np.uint8).reshape(-1, 3)
    allowed = {tuple(int(v) for v in c) for c in palette_rgb}
    uniq, counts = np.unique(rgb, axis=0, return_counts=True)
    return sorted(
        [(rgb_to_hex(tuple(int(v) for v in u)), int(c)) for u, c in zip(uniq, counts) if tuple(int(v) for v in u) in allowed],
        key=lambda kv: -kv[1],
    )


def _auto_width(img: Image.Image, params: ReduceParams, palette_rgb: np.ndarray) -> tuple[int, Image.Image]:
    """Search the width ladder for the best deterministic fidelity score."""
    from .metrics import evaluate_candidate

    max_dim = max(img.size)
    widths = [w for w in AUTO_WIDTH_LADDER if w <= max_dim * 0.5] or [32]
    best: tuple[float, int, Image.Image] | None = None
    for w in widths:
        cand = params.model_copy(update={"width": w, "height": None})
        small = reduce_image(img, cand, palette_rgb)
        score, _metrics = evaluate_candidate(small, img, [rgb_to_hex(tuple(c)) for c in palette_rgb])
        if best is None or score > best[0]:
            best = (score, w, small)
    assert best is not None
    return best[1], best[2]


def pixelize(
    image_or_path: str | Path | Image.Image,
    palette_source: str | Path | list[str] | list[tuple[int, int, int]] | None = None,
    params: ReduceParams | None = None,
    auto_width: bool | None = None,
) -> PixelizeResult:
    """Reduce any image to palette-snapped pixel art deterministically.

    When no width/height is given, the width ladder is searched for the best
    fidelity score (disable with auto_width=False to halve the source instead).
    """
    params = params or ReduceParams()
    palette = load_palette(palette_source)
    palette_rgb = np.array(palette, dtype=np.uint8)

    if isinstance(image_or_path, (str, Path)):
        img = Image.open(image_or_path).convert("RGBA")
    else:
        img = image_or_path.convert("RGBA")

    unique_in = int(np.unique(np.asarray(img).reshape(-1, 4), axis=0).shape[0])

    bounded = params.width is not None or params.height is not None
    if auto_width is None:
        auto_width = not bounded
    if auto_width:
        chosen_w, small = _auto_width(img, params, palette_rgb)
        params = params.model_copy(update={"width": chosen_w, "height": None})
    else:
        small = reduce_image(img, params, palette_rgb)

    return PixelizeResult(
        image=small,
        params=params,
        palette_hex=[rgb_to_hex(tuple(c)) for c in palette],
        source_size=img.size,
        usage=_usage_of(small, palette_rgb),
        unique_colors_in=unique_in,
    )
