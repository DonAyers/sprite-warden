"""Walk Cycle Judge: validates alternating foot cadence and catches hops, skips, and jumping jacks."""

from pathlib import Path
from PIL import Image

from .models import GateResult


def _find_feet(image: Image.Image, x_mid: int, band: int) -> tuple[int | None, int | None, float | None, float | None]:
    """Return (left_bottom_row, right_bottom_row, left_x_center, right_x_center)."""
    alpha = image.getchannel("A").point(lambda v: 255 if v > 127 else 0)
    box = alpha.getbbox()
    if not box:
        return None, None, None, None

    out = []
    y_min = max(0, box[3] - band)
    for x0, x1 in ((0, x_mid), (x_mid, image.width)):
        half = alpha.crop((x0, y_min, x1, box[3]))
        hb = half.getbbox()
        if hb is None:
            out.append((None, None))
        else:
            out.append((y_min + hb[3], x0 + (hb[0] + hb[2]) / 2.0))
    return out[0][0], out[1][0], out[0][1], out[1][1]


def check_walk_cycle(
    frames: list[Image.Image],
    lift_thresh: int = 2,
    band: int = 16,
) -> GateResult:
    """Validate alternating foot biomechanics for front/back (and diagonal) walk cycles."""
    if len(frames) < 3:
        return GateResult(
            name="walk_mechanics",
            passed=False,
            score=0.0,
            summary="Walk cycle requires at least 3 frames",
            errors=["Insufficient frames for walk cycle analysis"],
        )

    # Establish x midpoint from the first frame's silhouette
    first_box = frames[0].getchannel("A").getbbox()
    if not first_box:
        return GateResult(
            name="walk_mechanics",
            passed=False,
            score=0.0,
            summary="First frame is completely transparent",
            errors=["Empty first frame"],
        )
    x_mid = (first_box[0] + first_box[2]) // 2

    diffs = []
    spreads = []
    bottoms = []
    frame_stats = []

    for i, frame in enumerate(frames):
        lb, rb, lx, rx = _find_feet(frame, x_mid, band)
        if lb is None or rb is None:
            continue
        diff = lb - rb
        spread = rx - lx if rx is not None and lx is not None else 0.0
        diffs.append(diff)
        spreads.append(spread)
        bottoms.append((lb, rb))
        frame_stats.append({
            "frame": i,
            "left_bottom": lb,
            "right_bottom": rb,
            "lift_diff": diff,
            "spread": round(spread, 1),
        })

    if not diffs:
        return GateResult(
            name="walk_mechanics",
            passed=False,
            score=0.0,
            summary="Failed to locate feet across frames",
            errors=["Could not detect feet in bottom rows"],
        )

    errors = []
    # 1. Ground contact check: Both feet cannot be elevated simultaneously (hop / skip)
    ground_row = max(max(l, r) for l, r in bottoms)
    hops = [i for i, (l, r) in enumerate(bottoms) if max(l, r) < ground_row - lift_thresh]
    if hops:
        errors.append(f"Both feet off ground by > {lift_thresh}px in frame(s) {hops} (hop/skip rather than walk)")

    # 2. Alternation check: left foot must be higher at some point, and right foot higher at some point
    # In screen coordinates, a smaller y-row is higher on screen. So lb - rb > 0 means left foot is raised.
    max_diff = max(diffs)
    min_diff = min(diffs)
    spread_range = max(spreads) - min(spreads) if spreads else 0.0

    passed_alternation = (max_diff >= lift_thresh and min_diff <= -lift_thresh)
    if not passed_alternation:
        if max_diff < lift_thresh and min_diff > -lift_thresh:
            why = f"Neither foot lifts above the other (lift range {min_diff:+d}..{max_diff:+d}px)"
            if spread_range >= 3.0:
                why += f"; feet simply spread and close laterally ({spread_range:.1f}px) - jumping jacks"
            errors.append(why)
        else:
            elevated_side = "left" if max_diff >= lift_thresh else "right"
            errors.append(f"Only the {elevated_side} foot ever lifts ({min_diff:+d}..{max_diff:+d}px) - limp or one-sided skip")

    passed = (len(errors) == 0)
    score = 1.0 if passed else (0.5 if passed_alternation else 0.0)

    summary = (
        f"Valid walk cadence: feet alternate smoothly ({min_diff:+d} to {max_diff:+d}px lift)"
        if passed
        else f"Invalid walk mechanics: {errors[0]}"
    )

    return GateResult(
        name="walk_mechanics",
        passed=passed,
        score=score,
        summary=summary,
        errors=errors,
        metrics={
            "ground_row": ground_row,
            "max_lift_left": max_diff,
            "max_lift_right": -min_diff,
            "spread_range": round(spread_range, 2),
            "hop_frames": hops,
        },
        details={"frame_cadence": frame_stats},
    )
