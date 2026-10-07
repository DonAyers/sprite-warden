"""Animation Anomaly Judge: temporal specks, edge jitter, and bottom spikes."""

import statistics
from pathlib import Path
from PIL import Image

from .models import GateResult


def _connected_pieces(coords: set[tuple[int, int]], diagonal: bool = True) -> list[set[tuple[int, int]]]:
    """Partition a set of pixel coordinates into 8-way (or 4-way) connected components."""
    remaining = set(coords)
    pieces = []
    offsets = [
        (-1, 0), (1, 0), (0, -1), (0, 1),
        *([(-1, -1), (-1, 1), (1, -1), (1, 1)] if diagonal else [])
    ]
    while remaining:
        root = remaining.pop()
        piece = {root}
        queue = [root]
        while queue:
            cx, cy = queue.pop()
            for dx, dy in offsets:
                neighbor = (cx + dx, cy + dy)
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    piece.add(neighbor)
                    queue.append(neighbor)
        pieces.append(piece)
    return pieces


def _odd_pixels(frame: Image.Image, prev: Image.Image, next_: Image.Image, delta: int = 48) -> set[tuple[int, int]]:
    """Pixels this frame draws unlike BOTH neighbors, where the neighbors agree with each other."""
    f_px, p_px, n_px = frame.load(), prev.load(), next_.load()
    w, h = frame.size
    odd = set()
    for y in range(h):
        for x in range(w):
            a, b, c = f_px[x, y], p_px[x, y], n_px[x, y]
            if a == b or b != c:
                continue
            if max(abs(a[i] - b[i]) for i in range(4)) >= delta:
                odd.add((x, y))
    return odd


def _bottom_foot_width(frame: Image.Image) -> int:
    """Number of opaque pixels on the lowest drawn row (ground contact surface)."""
    box = frame.getbbox()
    if not box:
        return 0
    px = frame.load()
    lowest_y = box[3] - 1
    return sum(1 for x in range(frame.width) if px[x, lowest_y][3] > 0)


def check_animation_anomalies(
    frames: list[Image.Image],
    delta: int = 48,
    spike_thresh: int = 2,
    jitter_limit: int = 2,
    max_speck_size: int = 24,
    min_speck_size: int = 5,
) -> GateResult:
    """Evaluate animation frames for single-frame specks, spikes, and edge wandering."""
    if len(frames) < 3:
        return GateResult(
            name="animation_anomalies",
            passed=True,
            score=1.0,
            summary="Too few frames (<3) to compare temporal neighbors",
        )

    errors = []
    warnings = []
    odd_scores = []

    # 1. Temporal Oddness (specks unlike both neighbors)
    #    An odd island that is DETACHED from the frame's body mass is a speck in the strict sense
    #    (a stray blob, not a limb or an outline that moved). Such a speck is an error when it
    #    exceeds max_speck_size, or when the same speck shows up in more than one frame.
    detached_specks: list[dict] = []  # {"frame", "pixels", "bbox", "coords"}
    for i, frame in enumerate(frames):
        prev_f = frames[i - 1]
        next_f = frames[(i + 1) % len(frames)]
        odd = _odd_pixels(frame, prev_f, next_f, delta=delta)
        islands = [p for p in _connected_pieces(odd) if len(p) >= min_speck_size]
        speck_islands = [p for p in islands if len(p) <= max_speck_size]
        odd_scores.append(sum(len(p) for p in speck_islands))
        if islands:
            f_px = frame.load()
            w, h = frame.size
            opaque = {(x, y) for y in range(h) for x in range(w) if f_px[x, y][3] > 0}
            main = max(_connected_pieces(opaque), key=len) if opaque else set()
            for island in islands:
                drawn = island & opaque
                if not drawn or drawn & main:
                    continue  # part of the body (a moving limb / edge) or pixels that vanished
                xs = [x for x, _ in island]
                ys = [y for _, y in island]
                detached_specks.append({"frame": i, "pixels": len(island), "coords": island,
                                        "bbox": [min(xs), min(ys), max(xs) + 1, max(ys) + 1]})

    speck_error_frames: set[int] = set()
    for s in detached_specks:
        if s["pixels"] > max_speck_size:
            speck_error_frames.add(s["frame"])
            errors.append(f"Frame {s['frame']}: a detached island of {s['pixels']}px at {s['bbox']} appears for one frame "
                          f"(over the {max_speck_size}px speck limit)")
    # the same small speck in more than one frame: group by overlap (>= half of the smaller island)
    groups: list[list[dict]] = []
    for s in (s for s in detached_specks if s["pixels"] <= max_speck_size):
        for group in groups:
            ref = group[0]
            if len(s["coords"] & ref["coords"]) * 2 >= min(len(s["coords"]), len(ref["coords"])):
                group.append(s)
                break
        else:
            groups.append([s])
    repeated_specks = []
    for group in groups:
        in_frames = sorted({s["frame"] for s in group})
        if len(in_frames) > 1:
            speck_error_frames.update(in_frames)
            repeated_specks.append({"frames": in_frames, "pixels": group[0]["pixels"], "bbox": group[0]["bbox"]})
            errors.append(f"A detached speck of {group[0]['pixels']}px at {group[0]['bbox']} repeats in frames {in_frames} "
                          f"(flicker: a one-frame speck is a warning, a repeating one is a defect)")

    med_odd = statistics.median(odd_scores) if odd_scores else 0
    anomalous_frames = [
        i for i, s in enumerate(odd_scores)
        if s > max(med_odd * 2, med_odd + 12)
    ]
    if anomalous_frames:
        warnings.append(f"Frames with transient specks/artifacts: {anomalous_frames} (median oddness {med_odd:.1f})")

    # 2. Bottom Spike Check (flickering pointed ground contact)
    foot_widths = [_bottom_foot_width(f) for f in frames]
    valid_widths = [w for w in foot_widths if w > 0]
    standing_med = statistics.median(valid_widths) if valid_widths else 0

    spike_frames = []
    for i, w in enumerate(foot_widths):
        if 0 < w <= spike_thresh and standing_med >= spike_thresh * 3:
            spike_frames.append(i)
            errors.append(f"Frame {i} stands on pointy spike ({w}px) vs cycle median ({standing_med:.0f}px)")

    # 3. Jitter / wandering bounding box
    boxes = [f.getbbox() for f in frames if f.getbbox()]
    jitter_edges = []
    for edge_name, idx in (("left", 0), ("top", 1), ("right", 2), ("bottom", 3)):
        vals = [b[idx] for b in boxes]
        if vals:
            med_val = statistics.median(vals)
            drift = [i for i, v in enumerate(vals) if abs(v - med_val) > jitter_limit]
            if drift:
                jitter_edges.append(f"{edge_name} (frames {drift})")
    if jitter_edges:
        warnings.append(f"Edge bounds jitter > {jitter_limit}px on: {', '.join(jitter_edges)}")

    bad_frames = set(spike_frames) | speck_error_frames
    passed = not bad_frames
    score = 1.0 if passed else max(0.0, 1.0 - (len(bad_frames) * 0.3))

    problems = []
    if spike_frames:
        problems.append(f"{len(spike_frames)} frame(s) exhibit ground contact spikes")
    if speck_error_frames:
        problems.append(f"detached speck(s) in frame(s) {sorted(speck_error_frames)}")
    summary = "Clean animation; no ground spikes, repeating specks or jarring jitter" if passed else "; ".join(problems)

    return GateResult(
        name="animation_anomalies",
        passed=passed,
        score=score,
        summary=summary,
        errors=errors,
        warnings=warnings,
        metrics={
            "foot_widths": foot_widths,
            "standing_median": standing_med,
            "spike_frames": spike_frames,
            "odd_scores": odd_scores,
            "anomalous_odd_frames": anomalous_frames,
            "detached_specks": [{k: v for k, v in s.items() if k != "coords"} for s in detached_specks],
            "repeated_specks": repeated_specks,
            "speck_error_frames": sorted(speck_error_frames),
        },
    )
