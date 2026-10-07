"""Walk Cycle Judge: validates alternating foot cadence (legs) or a steady, turning
wheel / tread base (wheels, treads); catches hops, skips, jumping jacks and a
wandering ground row.

``locomotion``:
- ``legs``   -- two side-by-side feet must alternate lifting (front/back/diagonal walks).
- ``wheels`` -- no cadence; the ground row must hold (+-1), the wheel/hub bounding
  box in the bottom band must keep its width and x-centre, and the wheel must
  actually change between frames (a frozen wheel is not a roll).
- ``treads`` -- same as wheels (the tread band must turn, the base must not bob).
"""

import statistics
from pathlib import Path
from PIL import Image

from .models import GateResult

LOCOMOTION_MODES = ("legs", "wheels", "treads")


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


def _base_band(
    image: Image.Image, band: int
) -> tuple[tuple[int, int, int, int] | None, Image.Image | None, dict | None]:
    """The wheel / tread mass in the bottom ``band`` rows of the drawn sprite.

    The band can hold more than the wheel: a held tool hanging beside it, a dangling
    cable. Only the connected mass that carries the ground-contact pixels (the lowest
    drawn row) is the base; other masses in the band are ignored and reported.
    Returns (bbox of that mass, crop with every other pixel cleared, info).
    """
    from .animation import _connected_pieces

    alpha = image.getchannel("A").point(lambda v: 255 if v > 127 else 0)
    box = alpha.getbbox()
    if not box:
        return None, None, None
    y0 = max(box[1], box[3] - band)
    ground = box[3] - 1
    px = alpha.load()
    coords = {(x, y) for y in range(y0, box[3]) for x in range(box[0], box[2]) if px[x, y]}
    if not coords:
        return None, None, None
    pieces = _connected_pieces(coords)
    contact = [sum(1 for (_, y) in p if y == ground) for p in pieces]
    best = max(range(len(pieces)), key=lambda i: (contact[i], len(pieces[i])))
    mass = pieces[best]
    xs = [x for x, _ in mass]
    ys = [y for _, y in mass]
    bbox = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
    crop = Image.new("RGBA", (bbox[2] - bbox[0], bbox[3] - bbox[1]), (0, 0, 0, 0))
    src = image.load()
    dst = crop.load()
    for x, y in mass:
        dst[x - bbox[0], y - bbox[1]] = src[x, y]
    ignored = []
    for i, p in enumerate(pieces):
        if i == best:
            continue
        pxs = [x for x, _ in p]
        pys = [y for _, y in p]
        ignored.append({"pixels": len(p), "bbox": [min(pxs), min(pys), max(pxs) + 1, max(pys) + 1],
                        "ground_contact_px": contact[i]})
    info = {
        "bbox": list(bbox),
        "pixels": len(mass),
        "ground_contact_px": contact[best],
        "masses_in_band": len(pieces),
        "ignored": sorted(ignored, key=lambda m: -m["pixels"]),
    }
    return bbox, crop, info


def _check_rolling_base(frames: list[Image.Image], locomotion: str, band: int, lift_thresh: int) -> GateResult:
    """wheels / treads: ground steadiness + hub/wheel bbox stability + the base must turn."""
    ground_rows: list[int] = []
    widths: list[int] = []
    centers: list[float] = []
    crops: list[Image.Image] = []
    frame_stats = []
    ignored_frames: list[int] = []
    for i, frame in enumerate(frames):
        bbox, crop, info = _base_band(frame, band)
        if bbox is None or crop is None or info is None:
            continue
        ground_rows.append(bbox[3] - 1)
        widths.append(bbox[2] - bbox[0])
        centers.append((bbox[0] + bbox[2]) / 2.0)
        crops.append(crop)
        if info["ignored"]:
            ignored_frames.append(i)
        frame_stats.append({"frame": i, "ground_row": bbox[3] - 1, "base_width": bbox[2] - bbox[0],
                            "base_center_x": round((bbox[0] + bbox[2]) / 2.0, 1), "base_mass": info})
    if not ground_rows:
        return GateResult(
            name="walk_mechanics", passed=False, score=0.0,
            summary=f"Failed to locate the {locomotion} base across frames",
            errors=["Could not detect a drawn base band"], metrics={"locomotion": locomotion},
        )

    errors: list[str] = []
    warnings: list[str] = []
    ground_med = max(ground_rows)  # the lowest contact row is the ground
    bob = [i for i, g in enumerate(ground_rows) if abs(g - ground_med) > 1]
    if max(ground_rows) - min(ground_rows) > 1:
        bob = bob or [i for i, g in enumerate(ground_rows) if g != ground_med]
        errors.append(f"Ground row wanders > 1px in frame(s) {bob} (rows {[ground_rows[i] for i in bob]} vs ground {ground_med}): a {locomotion} base must not bob")
    hops = [i for i, g in enumerate(ground_rows) if ground_med - g > lift_thresh]
    if hops and not bob:
        errors.append(f"Base leaves the ground in frame(s) {hops}")

    width_med = statistics.median(widths)
    width_off = [i for i, w in enumerate(widths) if abs(w - width_med) > 2]
    if width_off:
        errors.append(f"Wheel/hub width changes > 2px in frame(s) {width_off} ({[widths[i] for i in width_off]} vs median {width_med:.0f}px)")
    center_med = statistics.median(centers)
    center_off = [i for i, c in enumerate(centers) if abs(c - center_med) > 1.5]
    if center_off:
        errors.append(f"Wheel/hub x-centre drifts > 1.5px in frame(s) {center_off}")

    # The base must turn: at least some pixels in the band change between consecutive frames.
    static_steps = []
    for i in range(len(crops)):
        a, b = crops[i], crops[(i + 1) % len(crops)]
        if a.size == b.size and list(a.getdata()) == list(b.getdata()):
            static_steps.append(i)
    if len(crops) >= 2 and len(static_steps) == len(crops):
        warnings.append(f"The {locomotion} band never changes between frames: the base does not visibly turn")
    elif static_steps:
        warnings.append(f"The {locomotion} band is frozen across step(s) {static_steps}")

    first_mass = frame_stats[0]["base_mass"]
    mass_note = (f"base = the ground-contact mass (frame {frame_stats[0]['frame']}: {first_mass['pixels']}px at "
                 f"{first_mass['bbox']}, {first_mass['ground_contact_px']}px on the ground)")
    if ignored_frames:
        biggest = max((m for s in frame_stats for m in s["base_mass"]["ignored"]), key=lambda m: m["pixels"])
        warnings.append(
            f"{mass_note}; other mass(es) in the bottom {band} rows were ignored in frame(s) {ignored_frames} "
            f"(largest {biggest['pixels']}px at {biggest['bbox']}): a held tool or part beside the base is not the base"
        )

    passed = not errors
    score = 1.0 if passed else max(0.0, 1.0 - 0.3 * len(errors))
    summary = (
        f"Steady {locomotion}: ground row {ground_med} held, base {width_med:.0f}px wide and centred in every frame"
        if passed else f"Unsteady {locomotion}: {errors[0]}"
    )
    return GateResult(
        name="walk_mechanics", passed=passed, score=score, summary=summary, errors=errors, warnings=warnings,
        metrics={
            "locomotion": locomotion,
            "ground_row": ground_med,
            "ground_rows": ground_rows,
            "bob_frames": bob,
            "base_widths": widths,
            "base_centers": [round(c, 1) for c in centers],
            "static_steps": static_steps,
            "base_mass": first_mass,
            "base_mass_note": mass_note,
            "frames_with_ignored_masses": ignored_frames,
        },
        details={"frame_base": frame_stats},
    )


def check_walk_cycle(
    frames: list[Image.Image],
    lift_thresh: int = 2,
    band: int = 16,
    locomotion: str = "legs",
) -> GateResult:
    """Validate walk biomechanics: alternating feet (legs) or a steady turning base (wheels/treads)."""
    locomotion = (locomotion or "legs").lower()
    if locomotion not in LOCOMOTION_MODES:
        raise ValueError(f"unknown locomotion {locomotion!r}; expected one of {LOCOMOTION_MODES}")
    if len(frames) < 3:
        return GateResult(
            name="walk_mechanics",
            passed=False,
            score=0.0,
            summary="Walk cycle requires at least 3 frames",
            errors=["Insufficient frames for walk cycle analysis"],
            metrics={"locomotion": locomotion},
        )
    if locomotion in ("wheels", "treads"):
        return _check_rolling_base(frames, locomotion, band, lift_thresh)

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
            "locomotion": locomotion,
            "ground_row": ground_row,
            "max_lift_left": max_diff,
            "max_lift_right": -min_diff,
            "spread_range": round(spread_range, 2),
            "hop_frames": hops,
        },
        details={"frame_cadence": frame_stats},
    )
