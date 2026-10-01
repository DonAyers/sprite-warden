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
    for i, frame in enumerate(frames):
        prev_f = frames[i - 1]
        next_f = frames[(i + 1) % len(frames)]
        odd = _odd_pixels(frame, prev_f, next_f, delta=delta)
        speck_islands = [p for p in _connected_pieces(odd) if min_speck_size <= len(p) <= max_speck_size]
        odd_scores.append(sum(len(p) for p in speck_islands))

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

    passed = (len(spike_frames) == 0)
    score = 1.0 if passed else max(0.0, 1.0 - (len(spike_frames) * 0.3))

    summary = (
        "Clean animation; no ground spikes or jarring jitter"
        if passed
        else f"{len(spike_frames)} frame(s) exhibit ground contact spikes"
    )

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
        },
    )
