"""Grid & Mixel Judge: verifies true integer pixel scaling and detects subpixel blur."""

from pathlib import Path
from PIL import Image

from .models import GateResult

DEFAULT_BASE_W = 640
DEFAULT_BASE_H = 360


def _best_origin(pixels, width: int, height: int, scale: int) -> tuple[int, int]:
    """Find the grid offset with the fewest broken blocks (handles letterboxing/margins)."""
    if scale < 2:
        return 0, 0
    best = (0, 0)
    fewest = None
    step = max(scale, (min(width, height) // 40) * scale)
    for oy in range(scale):
        for ox in range(scale):
            bad = 0
            for by in range(oy, height - scale + 1, step):
                for bx in range(ox, width - scale + 1, step):
                    first = pixels[bx, by]
                    if any(pixels[bx + x, by + y] != first for y in range(scale) for x in range(scale)):
                        bad += 1
            if fewest is None or bad < fewest:
                fewest = bad
                best = (ox, oy)
    return best


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
    if scale is None:
        scale = max(1, min(width // base_res[0], height // base_res[1]))
    if scale < 1:
        scale = 1

    pixels = img.load()
    ox, oy = _best_origin(pixels, width, height, scale)

    mask = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    mask_pixels = mask.load()

    bad_blocks = 0
    total_blocks = 0
    examples: list[tuple[int, int]] = []

    for by in range(oy, height - scale + 1, scale):
        for bx in range(ox, width - scale + 1, scale):
            total_blocks += 1
            first = pixels[bx, by]
            uniform = all(
                pixels[bx + x, by + y] == first
                for y in range(scale)
                for x in range(scale)
            )
            if uniform:
                continue
            bad_blocks += 1
            if len(examples) < 6:
                examples.append((bx, by))
            for y in range(scale):
                for x in range(scale):
                    mask_pixels[bx + x, by + y] = (255, 0, 255, 255)

    bad_pct = (bad_blocks / total_blocks * 100.0) if total_blocks else 0.0
    passed = (bad_blocks == 0)
    score = max(0.0, 1.0 - (bad_pct / 10.0))  # 10% mixels -> score 0.0

    errors = []
    if not passed:
        spots = ", ".join(f"({x},{y})" for x, y in examples)
        errors.append(f"{bad_blocks}/{total_blocks} blocks ({bad_pct:.2f}%) are off-grid (mixels/blur); spots: {spots}")

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
        metrics={
            "scale": scale,
            "origin": [ox, oy],
            "total_blocks": total_blocks,
            "bad_blocks": bad_blocks,
            "bad_percentage": round(bad_pct, 3),
        },
        details={"sample_locations": examples},
    )
    return result, mask
