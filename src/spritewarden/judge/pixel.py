"""Grid & Mixel Judge: verifies true integer pixel scaling and detects subpixel blur.

Scale inference: images at least 640x360 are treated as screenshots of the
640x360 game canvas (scale = integer multiple). Anything smaller is a sprite,
and the block size is measured with the grid detector from ``sheet/snap.py`` so
a 2px (or 3px, 6px...) fake grid is seen and reported instead of silently
passing at scale 1.
"""

from pathlib import Path
from PIL import Image

from .models import GateResult

DEFAULT_BASE_W = 640
DEFAULT_BASE_H = 360


def _block_ranges(origin: int, length: int, scale: int) -> list[tuple[int, int]]:
    """Block spans [start, end) covering the whole axis, including partial edge blocks."""
    starts = list(range(origin, length, scale))
    if origin > 0:
        starts = [origin - scale] + starts
    return [(max(0, s), min(length, s + scale)) for s in starts if min(length, s + scale) > max(0, s)]


def _count_bad_blocks(pixels, width: int, height: int, scale: int, ox: int, oy: int,
                      stride: int = 1, collect: list | None = None, mask_pixels=None) -> tuple[int, int]:
    """Count non-uniform blocks for a grid origin. Partial border blocks count too, so a
    shifted origin cannot hide a mixel by pushing it into an unchecked margin."""
    bad = 0
    total = 0
    ys = _block_ranges(oy, height, scale)[::stride]
    xs = _block_ranges(ox, width, scale)[::stride]
    for y0, y1 in ys:
        for x0, x1 in xs:
            total += 1
            first = pixels[x0, y0]
            uniform = True
            for y in range(y0, y1):
                for x in range(x0, x1):
                    if pixels[x, y] != first:
                        uniform = False
                        break
                if not uniform:
                    break
            if uniform:
                continue
            bad += 1
            if collect is not None and len(collect) < 6:
                collect.append((x0, y0))
            if mask_pixels is not None:
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        mask_pixels[x, y] = (255, 0, 255, 255)
    return bad, total


def _best_origin(pixels, width: int, height: int, scale: int) -> tuple[int, int]:
    """Find the grid offset with the fewest broken blocks (handles letterboxing/margins).

    Ties go to the smallest offset, (0, 0) first.
    """
    if scale < 2:
        return 0, 0
    stride = max(1, (min(width, height) // 40))
    best = (0, 0)
    fewest = None
    for oy in range(scale):
        for ox in range(scale):
            bad, _ = _count_bad_blocks(pixels, width, height, scale, ox, oy, stride=stride)
            if fewest is None or bad < fewest:
                fewest = bad
                best = (ox, oy)
    return best


def _uniform_fraction(img: Image.Image, scale: int) -> tuple[float, int]:
    """Fraction of blocks touching opaque pixels that are flat, at the best origin for ``scale``."""
    pixels = img.load()
    width, height = img.size
    ox, oy = _best_origin(pixels, width, height, scale)
    opaque_blocks = 0
    uniform_blocks = 0
    for y0, y1 in _block_ranges(oy, height, scale):
        for x0, x1 in _block_ranges(ox, width, scale):
            first = pixels[x0, y0]
            any_opaque = False
            uniform = True
            for y in range(y0, y1):
                for x in range(x0, x1):
                    p = pixels[x, y]
                    if p[3] > 0:
                        any_opaque = True
                    if p != first:
                        uniform = False
            if not any_opaque:
                continue
            opaque_blocks += 1
            if uniform:
                uniform_blocks += 1
    return (uniform_blocks / opaque_blocks if opaque_blocks else 0.0), opaque_blocks


def detect_block_size(img: Image.Image, max_scale: int = 16, min_fraction: float = 0.97) -> int:
    """Largest block size (2..max_scale) that tiles the drawn pixels almost perfectly, else 1.

    Fully transparent blocks are ignored so padding cannot fake a grid. A native
    sprite has shading detail inside every 2x2, so it stays at 1; a nearest-
    neighbour upscale (or an image model's fake pixels) reports its block size.
    """
    width, height = img.size
    best = 1
    for scale in range(2, min(max_scale, width // 2, height // 2) + 1):
        fraction, opaque = _uniform_fraction(img, scale)
        if opaque >= 8 and fraction >= min_fraction:
            best = scale
    return best


def infer_scale(img: Image.Image, base_res: tuple[int, int] = (DEFAULT_BASE_W, DEFAULT_BASE_H)) -> tuple[int, str]:
    """Return (scale, how): the screenshot multiple for large images, else the detected block size.

    ``how`` is "screenshot", "detected" (exact block grid), "estimated" (the
    edge-profile detector saw a grid that is not flat: a blurred upscale, reported
    as mixels at that scale) or "native".
    """
    width, height = img.size
    if width >= base_res[0] and height >= base_res[1]:
        return max(1, min(width // base_res[0], height // base_res[1])), "screenshot"

    exact = detect_block_size(img)
    if exact > 1:
        return exact, "detected"

    from ..sheet.snap import detect_pixel_size

    estimated = int(detect_pixel_size(img))
    if estimated > 1 and estimated <= min(width, height) // 2:
        fraction, opaque = _uniform_fraction(img, estimated)
        if opaque >= 8 and fraction >= 0.6:
            return estimated, "estimated"
    return 1, "native"


def check_pixel_grid(
    image_or_path: str | Path | Image.Image,
    scale: int | None = None,
    save_mask_path: str | Path | None = None,
    base_res: tuple[int, int] = (DEFAULT_BASE_W, DEFAULT_BASE_H),
) -> tuple[GateResult, Image.Image | None]:
    """Check that an image consists strictly of flat scale x scale pixel blocks."""
    if isinstance(image_or_path, (str, Path)):
        img = Image.open(image_or_path).convert("RGBA")
    else:
        img = image_or_path.convert("RGBA")

    width, height = img.size
    how = "given"
    if scale is None:
        scale, how = infer_scale(img, base_res)
    if scale < 1:
        scale = 1

    pixels = img.load()
    ox, oy = _best_origin(pixels, width, height, scale)

    mask = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    mask_pixels = mask.load()
    examples: list[tuple[int, int]] = []
    bad_blocks, total_blocks = _count_bad_blocks(
        pixels, width, height, scale, ox, oy, collect=examples, mask_pixels=mask_pixels
    )

    bad_pct = (bad_blocks / total_blocks * 100.0) if total_blocks else 0.0
    passed = (bad_blocks == 0)
    score = max(0.0, 1.0 - (bad_pct / 10.0))  # 10% mixels -> score 0.0

    errors = []
    warnings = []
    if not passed:
        spots = ", ".join(f"({x},{y})" for x, y in examples)
        errors.append(f"{bad_blocks}/{total_blocks} blocks ({bad_pct:.2f}%) are off-grid (mixels/blur); spots: {spots}")
    if how in ("detected", "estimated") and scale > 1:
        warnings.append(f"Art is drawn on a {scale}px block grid (upscaled / fake pixels), not native 1:1")

    summary = (
        f"Perfect {scale}x{scale} pixel grid ({total_blocks} blocks)"
        if passed
        else f"{bad_blocks} off-grid blocks detected ({bad_pct:.2f}%) at scale {scale}x{scale}"
    )

    if save_mask_path:
        mask.save(save_mask_path)

    result = GateResult(
        name="pixel_grid",
        passed=passed,
        score=score,
        summary=summary,
        errors=errors,
        warnings=warnings,
        metrics={
            "scale": scale,
            "scale_source": how,
            "origin": [ox, oy],
            "total_blocks": total_blocks,
            "bad_blocks": bad_blocks,
            "bad_percentage": round(bad_pct, 3),
        },
        details={"sample_locations": examples},
    )
    return result, mask
