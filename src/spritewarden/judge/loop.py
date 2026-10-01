"""Loop Smoothness Judge: detects wrap jumps, hesitation stalls, and retimes loops."""

import statistics
from pathlib import Path
from PIL import Image, ImageChops, ImageSequence

from .models import GateResult


def _find_foot_anchor(img: Image.Image) -> tuple[int, int]:
    box = img.getbbox()
    if not box:
        return img.width // 2, img.height
    band = img.crop((0, max(0, box[3] - 8), img.width, box[3])).getbbox()
    if not band:
        return (box[0] + box[2]) // 2, box[3]
    return (band[0] + band[2]) // 2, box[3]


def _pixel_difference_count(a: Image.Image, b: Image.Image) -> int:
    diff = ImageChops.difference(a, b).convert("L")
    # Count pixels with delta > 24 of 255
    return sum(diff.point(lambda v: 255 if v > 24 else 0).histogram()[255:])


def check_loop_smoothness(
    frames: list[Image.Image],
    ratio: float = 1.8,
    seat_by_feet: bool = True,
) -> tuple[GateResult, list[int]]:
    """Verify that an animation loop wraps smoothly without wrap jumps or hesitation stalls.

    Returns (GateResult, list_of_recommended_frame_indices_if_stalled).
    """
    if len(frames) < 3:
        return GateResult(
            name="loop_smoothness",
            passed=False,
            score=0.0,
            summary="Loop requires at least 3 frames",
            errors=["Insufficient frames for loop evaluation"],
        ), list(range(len(frames)))

    size = 2 * max(max(img.size) for img in frames)
    if seat_by_feet:
        fx, fy = _find_foot_anchor(frames[0])
        aligned = []
        for img in frames:
            cell = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            cell.paste(img, (size // 2 - fx, size * 3 // 4 - fy), img)
            aligned.append(cell)
    else:
        aligned = frames

    steps = [_pixel_difference_count(aligned[i], aligned[(i + 1) % len(aligned)]) for i in range(len(aligned))]
    typical = statistics.median(steps[:-1]) if len(steps) > 1 else steps[0]
    wrap = steps[-1]

    errors = []
    warnings = []
    held_steps = [i for i, n in enumerate(steps) if n == 0]
    stalls = [i for i, n in enumerate(steps) if n and typical and n * ratio < typical]
    jumps = [i for i, n in enumerate(steps[:-1]) if typical and n > ratio * typical]

    if typical and wrap > ratio * typical:
        errors.append(f"Wrap step jumps {wrap / typical:.1f}x typical step ({wrap} vs {typical:.0f}px)")

    if held_steps:
        errors.append(f"Held (duplicate) frames at step(s): {held_steps}")

    if stalls:
        errors.append(f"Stall/hesitation at step(s) {stalls}: motion nearly halts ({[steps[s] for s in stalls]} vs {typical:.0f}px)")

    if jumps:
        warnings.append(f"Internal step jump at {jumps}")

    passed = (len(errors) == 0)
    score = 1.0 if passed else max(0.0, 1.0 - (len(errors) * 0.4))

    # Calculate recommended kept frames if stalls exist
    recommended_frames = drop_loop_stalls(aligned, ratio=ratio)

    summary = (
        f"Seamless loop: wrap step is {wrap / typical:.1f}x typical ({wrap} vs {typical:.0f}px)"
        if passed
        else f"Loop cadence flaw: {errors[0]}"
    )

    result = GateResult(
        name="loop_smoothness",
        passed=passed,
        score=score,
        summary=summary,
        errors=errors,
        warnings=warnings,
        metrics={
            "step_deltas": steps,
            "typical_delta": round(typical, 1),
            "wrap_delta": wrap,
            "wrap_ratio": round(wrap / typical, 2) if typical else 1.0,
            "stall_steps": stalls,
            "held_steps": held_steps,
        },
        details={"recommended_retime_frames": recommended_frames},
    )
    return result, recommended_frames


def drop_loop_stalls(aligned_frames: list[Image.Image], ratio: float = 1.8) -> list[int]:
    """Calculate which frames to retain to eliminate stalls while preserving keyframe 0."""
    keep = list(range(len(aligned_frames)))
    while len(keep) > 4:
        steps = [
            _pixel_difference_count(aligned_frames[keep[i]], aligned_frames[keep[(i + 1) % len(keep)]])
            for i in range(len(keep))
        ]
        typical = statistics.median(steps)
        if not typical:
            break
        worst = min(range(len(steps)), key=lambda i: steps[i])
        if steps[worst] * ratio >= typical:
            break  # No stall left
        # Drop frame after stall unless it's frame 0
        drop = (worst + 1) % len(keep)
        keep.pop(worst if keep[drop] == 0 else drop)
    return keep


def check_gif_timing(gif_path: str | Path) -> GateResult:
    """Verify that frame durations in an exported GIF are uniform."""
    img = Image.open(gif_path)
    durations = [f.info.get("duration", 0) for f in ImageSequence.Iterator(img)]
    uneven = sorted(set(durations))
    passed = (len(uneven) <= 1)

    errors = []
    if not passed:
        errors.append(f"Uneven frame durations found ({uneven} ms): frames were merged or delayed unevenly")

    return GateResult(
        name="gif_frame_timing",
        passed=passed,
        score=1.0 if passed else 0.5,
        summary=f"Uniform {durations[0]}ms per frame" if passed else f"Uneven durations: {uneven}ms",
        errors=errors,
        metrics={"frame_count": len(durations), "durations_ms": durations},
    )
