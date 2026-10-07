"""Animation integrity gate: frames must not lose parts of the sprite the still has.

A background-removal matte that matches a colour of the sprite keys that colour
out of every frame: the outline survives, the fill becomes transparent (a grey
robot's head on the default grey matte). No per-frame gate saw it, because
every frame was wrong the same way. This gate compares each frame with the
source still (or, without one, with the other frames):

- enclosed transparent holes: transparent pixels not connected to the canvas
  border. A frame fails when its holes total more than ``max(12 px, 1.5% of the
  frame's opaque area)`` above what the reference has;
- opaque area: a frame fails when it draws more than 15% fewer opaque pixels
  than the source still.
"""

from __future__ import annotations

import statistics
from typing import Any

from PIL import Image

from .models import GateResult

ALPHA_THRESHOLD = 128  # below this a pixel counts as transparent


def _alpha_rows(image: Image.Image) -> tuple[int, int, bytes]:
    alpha = image.convert("RGBA").getchannel("A")
    return alpha.width, alpha.height, alpha.tobytes()


def enclosed_holes(image: Image.Image) -> list[int]:
    """Sizes (px, largest first) of the transparent regions that do not reach the canvas border."""
    w, h, alpha = _alpha_rows(image)
    if not w or not h:
        return []
    # 0 = opaque, 1 = transparent and unvisited, 2 = visited
    state = bytearray(1 if a < ALPHA_THRESHOLD else 0 for a in alpha)

    def flood(start: int) -> int:
        stack = [start]
        state[start] = 2
        size = 0
        while stack:
            i = stack.pop()
            size += 1
            x = i % w
            if x > 0 and state[i - 1] == 1:
                state[i - 1] = 2
                stack.append(i - 1)
            if x < w - 1 and state[i + 1] == 1:
                state[i + 1] = 2
                stack.append(i + 1)
            if i >= w and state[i - w] == 1:
                state[i - w] = 2
                stack.append(i - w)
            if i < w * (h - 1) and state[i + w] == 1:
                state[i + w] = 2
                stack.append(i + w)
        return size

    border = [*range(w), *range(w * (h - 1), w * h), *range(0, w * h, w), *range(w - 1, w * h, w)]
    for i in border:
        if state[i] == 1:
            flood(i)
    holes = []
    for i in range(w * h):
        if state[i] == 1:
            holes.append(flood(i))
    return sorted(holes, reverse=True)


def opaque_area(image: Image.Image) -> int:
    _, _, alpha = _alpha_rows(image)
    return sum(1 for a in alpha if a >= ALPHA_THRESHOLD)


def check_frame_integrity(
    frames: list[Image.Image],
    source: Image.Image | None = None,
    *,
    min_hole_px: int = 12,
    hole_fraction: float = 0.015,
    area_drop: float = 0.15,
) -> GateResult:
    """Fail frames with enclosed holes the reference lacks, or with far less drawn area than the source."""
    if not frames:
        return GateResult(name="frame_integrity", passed=False, score=0.0, summary="No frames to check",
                          errors=["no frames"])
    holes = [enclosed_holes(f) for f in frames]
    totals = [sum(h) for h in holes]
    areas = [opaque_area(f) for f in frames]
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {"hole_totals": totals, "opaque_areas": areas, "holes": [h[:6] for h in holes]}

    if source is not None:
        source_holes = enclosed_holes(source)
        source_total = sum(source_holes)
        source_area = opaque_area(source)
        reference_name = "source still"
        metrics.update(reference="source", source_hole_total=source_total, source_holes=source_holes[:6],
                       source_opaque_area=source_area)
    else:
        source_total = source_area = None
        reference_name = "median of the other frames"
        metrics["reference"] = "median_of_other_frames"

    holed: list[int] = []
    shrunk: list[int] = []
    limits: list[int] = []
    for i, total in enumerate(totals):
        limit = max(min_hole_px, int(round(hole_fraction * areas[i])))
        limits.append(limit)
        if source_total is not None:
            baseline = source_total
        else:
            others = [t for j, t in enumerate(totals) if j != i]
            baseline = statistics.median(others) if others else 0
        excess = total - baseline
        if excess > limit:
            holed.append(i)
            sizes = ", ".join(str(s) for s in holes[i][:6]) + (", ..." if len(holes[i]) > 6 else "")
            errors.append(
                f"frame {i}: enclosed transparent hole(s) of {sizes} px ({total} px in all; the {reference_name} "
                f"has {baseline:g} px; allowed excess {limit} px) -- part of the sprite was removed"
            )
        if source_area:
            if areas[i] < (1.0 - area_drop) * source_area:
                shrunk.append(i)
                errors.append(
                    f"frame {i}: opaque area {areas[i]} px is {100 * (1 - areas[i] / source_area):.0f}% below the "
                    f"source still's {source_area} px (limit {int(area_drop * 100)}%)"
                )
    if source is None and len(frames) >= 3:
        med_area = statistics.median(areas)
        low = [i for i, a in enumerate(areas) if med_area and a < (1.0 - area_drop) * med_area]
        if low:
            warnings.append(f"frame(s) {low} draw more than {int(area_drop * 100)}% fewer opaque pixels than the cycle "
                            f"median ({med_area:g} px); pass the source still to make this a verdict")
    if source is None:
        warnings.append("no source still given: holes present in EVERY frame cannot be seen (pass --source / source_path)")
    metrics.update(hole_limits=limits, holed_frames=holed, shrunk_frames=shrunk)

    passed = not errors
    bad = sorted(set(holed + shrunk))
    if passed:
        summary = f"All {len(frames)} frames intact against the {reference_name} (no new enclosed holes, area kept)"
    else:
        bits = []
        if holed:
            bits.append(f"new enclosed holes in frame(s) {holed} (largest {max(holes[i][0] for i in holed)} px)")
        if shrunk:
            bits.append(f"opaque area dropped >{int(area_drop * 100)}% in frame(s) {shrunk}")
        summary = "Frames lost part of the sprite: " + "; ".join(bits)
    return GateResult(
        name="frame_integrity",
        passed=passed,
        score=1.0 if passed else max(0.0, round(1.0 - len(bad) / len(frames), 3)),
        summary=summary,
        errors=errors,
        warnings=warnings,
        metrics=metrics,
    )
