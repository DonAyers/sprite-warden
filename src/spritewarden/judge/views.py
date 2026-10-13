"""Cross-view gate: do the views of a direction set belong to one character?

Each rotation tool paints its eight views at one camera; edits and mixed
providers do not. This gate compares every facing against the anchor (the
south view unless given): same ground row, same drawn height, similar opaque
area, the same number of connected components (a part present in some views
and absent in others is named), tool side per facing from identity marker
colours, and palette.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from .models import GateResult
from .palette import check_palette
from .start_image import find_components, locate_markers

# Canonical compass facings in game strip order (down, down_right, right, ...).
GAME_ORDER = ["south", "south_east", "east", "north_east", "north", "north_west", "west", "south_west"]
GAME_ORDER_4 = ["south", "east", "north", "west"]
FACING_ALIASES: dict[str, str] = {
    "s": "south", "down": "south", "front": "south", "south": "south",
    "se": "south_east", "down_right": "south_east", "southeast": "south_east", "south_east": "south_east",
    "e": "east", "right": "east", "east": "east",
    "ne": "north_east", "up_right": "north_east", "northeast": "north_east", "north_east": "north_east",
    "n": "north", "up": "north", "back": "north", "north": "north",
    "nw": "north_west", "up_left": "north_west", "northwest": "north_west", "north_west": "north_west",
    "w": "west", "left": "west", "west": "west",
    "sw": "south_west", "down_left": "south_west", "southwest": "south_west", "south_west": "south_west",
}
# Screen side (sign of x offset from the body centre) of the character's own LEFT
# side per facing; 0 = the sides overlap in x (profile views), not checkable.
LEFT_SIDE_SCREEN_SIGN: dict[str, int] = {
    "south": +1, "south_east": +1, "east": 0, "north_east": -1,
    "north": -1, "north_west": -1, "west": 0, "south_west": +1,
}


def canonical_facing(name: str) -> str | None:
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    for suffix in ("_view", "_frame"):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    return FACING_ALIASES.get(key)


def _feature_side(feature: Any) -> str | None:
    explicit = str(getattr(feature, "side", None) or "").strip().lower()
    if explicit in ("left", "right"):
        return explicit
    text = f"{getattr(feature, 'name', '')} {getattr(feature, 'description', '')}".lower()
    has_left = "left" in text
    has_right = "right" in text
    if has_left and not has_right:
        return "left"
    if has_right and not has_left:
        return "right"
    return None


def _view_stats(img: Image.Image) -> dict[str, Any] | None:
    box = img.getbbox()
    if not box:
        return None
    opaque = sum(1 for p in img.getdata() if p[3] > 0)
    comp = find_components(img)
    return {
        "size": list(img.size),
        "bbox": list(box),
        "ground_row": box[3] - 1,
        "height": box[3] - box[1],
        "width": box[2] - box[0],
        "center_x": (box[0] + box[2]) / 2.0,
        "opaque": opaque,
        "components": comp["count"],
        "extras": comp["strays"] + comp["detached"],
    }


def check_direction_set(
    frames: dict[str, Image.Image],
    *,
    anchor: Image.Image | None = None,
    anchor_facing: str = "south",
    identity: Any | None = None,
    palette: str | Path | list[str] | None = None,
    ground_tolerance: int = 1,
    height_tolerance: int = 2,
    area_tolerance: float = 0.20,
) -> GateResult:
    """Compare 4-8 facings against an anchor. Returns one GateResult named ``direction_set``."""
    import math
    from ..repo.identity import validate_side_front_area_ratio
    if not math.isfinite(area_tolerance) or not 0 <= area_tolerance < 1:
        raise ValueError("area_tolerance must be finite and in [0, 1)")
    validate_side_front_area_ratio(getattr(identity, "side_front_area_ratio", None))
    anchor_canon = canonical_facing(anchor_facing)
    if anchor_canon is None:
        raise ValueError("anchor_facing must be a recognised facing")
    views: dict[str, Image.Image] = {}
    unknown: list[str] = []
    for name, img in frames.items():
        canon = canonical_facing(name)
        if canon is None:
            unknown.append(name)
            continue
        views[canon] = img.convert("RGBA")

    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {"facings": list(views), "unknown_names": unknown}
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, passed: bool | None, detail: str, *, error: bool = True) -> None:
        checks[name] = {"passed": passed, "detail": detail}
        if passed is False:
            (errors if error else warnings).append(f"{name}: {detail}")

    if unknown:
        warnings.append(f"ignored views with unknown facing names: {', '.join(unknown)}")
    if len(views) < 4:
        return GateResult(
            name="direction_set", passed=False, score=0.0,
            summary=f"Need 4-8 facings, got {len(views)}",
            errors=[f"count: {len(views)} recognised facing(s) ({', '.join(views) or 'none'})"],
            warnings=warnings, metrics=metrics, details={"checks": checks},
        )

    anchor_img = anchor.convert("RGBA") if anchor is not None else views.get("south")
    anchor_name = "given anchor" if anchor is not None else "south view"
    if anchor_img is None:
        anchor_img = next(iter(views.values()))
        anchor_name = f"{next(iter(views))} view"
    a_stats = _view_stats(anchor_img)
    if a_stats is None:
        return GateResult(
            name="direction_set", passed=False, score=0.0, summary="Anchor image is empty",
            errors=["anchor: no opaque pixels"], metrics=metrics, details={"checks": checks},
        )
    metrics["anchor"] = {"source": anchor_name, **{k: v for k, v in a_stats.items() if k != "extras"}}

    stats: dict[str, dict[str, Any]] = {}
    empty = []
    for facing in GAME_ORDER:
        if facing not in views:
            continue
        st = _view_stats(views[facing])
        if st is None:
            empty.append(facing)
            continue
        stats[facing] = st
    if empty:
        record("empty_views", False, f"no pixels drawn in {', '.join(empty)}")
    metrics["views"] = {f: {k: v for k, v in st.items() if k != "extras"} for f, st in stats.items()}

    # 0. Same canvas size (cells)
    sizes = sorted({tuple(st["size"]) for st in stats.values()})
    record(
        "canvas",
        len(sizes) == 1,
        f"all views {sizes[0][0]}x{sizes[0][1]}" if len(sizes) == 1 else f"mixed canvas sizes: {sizes}",
    )

    # 1. Ground row within +-1 of the anchor. An anchor on another canvas (a 96x96 still vs 60x60
    #    views) has no comparable absolute row: the views are then checked against each other and
    #    the anchor contributes its content height and area only.
    anchor_size = tuple(a_stats["size"])
    anchor_differs = anchor is not None and bool(stats) and any(tuple(st["size"]) != anchor_size for st in stats.values())
    metrics["anchor_canvas_differs"] = anchor_differs
    if anchor_differs:
        rows = [st["ground_row"] for st in stats.values()]
        ref_ground = stats["south"]["ground_row"] if "south" in stats else max(set(rows), key=rows.count)
        ref_label = f"views' ground row {ref_ground} (from {'south' if 'south' in stats else 'the most common row'})"
        note = (f"anchor canvas differs ({anchor_size[0]}x{anchor_size[1]} vs views {sizes[0][0]}x{sizes[0][1]}): "
                f"its ground row {a_stats['ground_row']} is not compared; ground row is checked among the views, "
                f"the anchor supplies content height and area only")
        checks["anchor_canvas"] = {"passed": None, "detail": note}
        warnings.append(f"note: {note}")
    else:
        ref_ground = a_stats["ground_row"]
        ref_label = f"anchor ground row {ref_ground}"
    metrics["ground_reference"] = ref_ground
    off = {f: st["ground_row"] - ref_ground for f, st in stats.items() if abs(st["ground_row"] - ref_ground) > ground_tolerance}
    record("ground_row", not off, ref_label + (f"; off by {off}" if off else ", all views within +-%d" % ground_tolerance))

    # 2. Content height within +-2px of the anchor
    off = {f: st["height"] - a_stats["height"] for f, st in stats.items() if abs(st["height"] - a_stats["height"]) > height_tolerance}
    record("height", not off, f"anchor {a_stats['height']}px" + (f"; views off by {off}px" if off else ", all views within +-%dpx" % height_tolerance))

    # An explicit profile policy changes area only; south remains the FRONT
    # reference even when the supplied anchor is a side view.
    policy = getattr(identity, "side_front_area_ratio", None)
    metrics["side_front_area_ratio"] = policy
    area_reference = a_stats
    if policy is not None:
        front = stats.get("south")
        record("profile_reference", front is not None and "east" in stats and "west" in stats,
               "profile policy requires nonempty south, east and west views")
        if front is not None:
            area_reference = front
            if anchor is not None:
                ratio = a_stats["opaque"] / front["opaque"]
                is_profile = anchor_canon in ("east", "west")
                valid = (policy[0] <= ratio <= policy[1] if is_profile
                         else abs(ratio - 1) <= area_tolerance)
                record("anchor_front_area", valid,
                       f"external {anchor_canon}/south area {ratio:.4f}; expected "
                       f"{policy if is_profile else '1 +/- ' + str(area_tolerance)}")
                metrics["anchor_facing"] = anchor_canon
            for side in ("east", "west"):
                if side in stats:
                    ratio = stats[side]["opaque"] / front["opaque"]
                    record(f"{side}_front_area", policy[0] <= ratio <= policy[1],
                           f"{side}/south area {ratio:.4f}; expected {policy}")
        if "east" in stats and "west" in stats:
            areas = [stats[f]["opaque"] for f in ("east", "west")]
            difference = max(areas) / min(areas) - 1
            record("profile_pair_area", difference <= area_tolerance,
                   f"symmetric max/min area difference {difference:.4f}; tolerance {area_tolerance}")
    # North and diagonals retain the ordinary area bound against FRONT.
    off = {
        f: round(st["opaque"] / area_reference["opaque"], 2)
        for f, st in stats.items()
        if not (policy is not None and f in ("east", "west"))
        and abs(st["opaque"] - area_reference["opaque"]) > area_tolerance * area_reference["opaque"]
    }
    record("area", not off, f"{'front south' if policy is not None else 'anchor'} {area_reference['opaque']}px" + (f"; views outside +-{int(area_tolerance * 100)}%: {off}" if off else f", all views within +-{int(area_tolerance * 100)}%"))

    # 4. Component count equal across views; name the extra parts
    counts = {f: st["components"] for f, st in stats.items()}
    if len(set(counts.values())) > 1:
        baseline = min(counts.values())
        named = []
        for f, st in stats.items():
            if st["components"] > baseline:
                parts = "; ".join(f"{e['pixels']}px at {e['bbox']}" for e in st["extras"][: st["components"] - baseline])
                named.append(f"{f} has {st['components'] - baseline} extra part(s): {parts}")
        record("components", False, f"component count differs {counts}: " + " | ".join(named))
    else:
        record("components", True, f"{counts[next(iter(counts))]} component(s) in every view")

    # 5. Tool side per facing from identity marker colours
    features = getattr(identity, "features", None) if identity is not None else None
    sided = {}
    if features:
        for fname, feat in features.items():
            side = _feature_side(feat)
            hexv = getattr(feat, "marker_hex", None)
            if side and hexv:
                sided[fname] = (side, hexv)
    if sided:
        wrong: list[str] = []
        missing: list[str] = []
        side_metrics: dict[str, Any] = {}
        anchor_marks = locate_markers(anchor_img, {n: hx for n, (_, hx) in sided.items()})
        for f, img in views.items():
            if f not in stats:
                continue
            marks = locate_markers(img, {n: hx for n, (_, hx) in sided.items()})
            for fname, (side, _) in sided.items():
                mark = marks.get(fname)
                if mark is None:
                    if anchor_marks.get(fname) is not None:
                        missing.append(f"{fname} missing in {f}")
                    continue
                expected = LEFT_SIDE_SCREEN_SIGN[f] * (1 if side == "left" else -1)
                dx = mark["centroid"][0] - stats[f]["center_x"]
                side_metrics.setdefault(f, {})[fname] = round(dx, 1)
                if expected == 0 or abs(dx) <= 1.0:
                    continue
                if (dx > 0) != (expected > 0):
                    where = "right" if dx > 0 else "left"
                    want = "right" if expected > 0 else "left"
                    wrong.append(f"{fname} ({side} side) is on the screen {where} in {f}, expected {want}")
        metrics["tool_sides"] = side_metrics
        problems = wrong + missing
        record("tool_side", not problems, "; ".join(problems) if problems else f"{len(sided)} sided feature(s) on the expected side in every view")
    else:
        record("tool_side", None, "no sided marker features in the identity")

    # 6. Palette
    pal_source = palette
    if pal_source is None and identity is not None and getattr(identity, "palette_id", None):
        pal_source = identity.palette_id
    if pal_source is not None:
        bad = {}
        for f, img in views.items():
            res, _ = check_palette(img, palette_source=pal_source)
            if not res.passed:
                bad[f] = res.metrics.get("bad_pixels")
        record("palette", not bad, f"off-palette pixels in {bad}" if bad else "every view 100% on palette")
    else:
        record("palette", None, "no palette given")

    judged = [c for c in checks.values() if c["passed"] is not None]
    passed = all(c["passed"] for c in judged) and not errors
    score = round(sum(1 for c in judged if c["passed"]) / len(judged), 3) if judged else 0.0
    failed = [n for n, c in checks.items() if c["passed"] is False]
    summary = (
        f"Direction set consistent: {len(stats)} views match the {anchor_name} ({len(judged)} checks)"
        if passed
        else f"Direction set inconsistent: {', '.join(failed)}"
    )
    return GateResult(
        name="direction_set", passed=passed, score=score, summary=summary,
        errors=errors, warnings=warnings, metrics=metrics, details={"checks": checks},
    )


def compare_views_to_anchor(
    frames: dict[str, Image.Image],
    anchor: Image.Image,
    judge: Any,
    *,
    anchor_facing: str = "south",
    skip_anchor_view: bool = False,
    description: str = "",
    expected_perspective: str = "low top-down (approx 20-30 degrees)",
    faces: dict[str, str] | None = None,
    refresh: bool = False,
) -> dict[str, GateResult]:
    """Reference-aware vision for every view of a set: facing -> ``vision_reference`` gate.

    ``faces`` (the pipeline's description of the back / left / right of the design) goes into
    every comparison so "invented" has a definition.

    Each view is compared with the anchor by ``judge.compare_to_reference`` with the view's
    facing and the anchor's facing in the prompt (parts hidden by the turn are expected;
    invented parts fail). One paid model call per view: opt-in (``judge-views --vision``).
    ``skip_anchor_view`` leaves out the view that IS the anchor.
    """
    from .vision import reference_gate

    anchor_canon = canonical_facing(anchor_facing) or anchor_facing
    out: dict[str, GateResult] = {}
    for name, img in frames.items():
        facing = canonical_facing(name) or name
        if skip_anchor_view and facing == anchor_canon:
            continue
        extra = {"faces": faces} if faces else {}
        if refresh:
            extra['refresh'] = True
        comparison = judge.compare_to_reference(
            img, anchor, facing=facing, anchor_facing=anchor_canon,
            description=description, expected_perspective=expected_perspective, **extra,
        )
        out[facing] = reference_gate(comparison, facing=facing)
    return out


def vision_summary_gate(per_view: dict[str, GateResult]) -> GateResult:
    """One ``vision_reference`` gate for a whole set (a strip has a single record)."""
    failed = {f: g for f, g in per_view.items() if not g.passed}
    unassessed = {f: g for f, g in per_view.items() if g.metrics.get('status') in {'error','unavailable'}}
    mismatches = [f for f in failed if f not in unassessed]
    status = ('error' if any(g.metrics.get('status')=='error' for g in unassessed.values())
              else 'unavailable' if unassessed or not per_view else 'mismatch' if mismatches else 'match')
    errors = [f"{f}: {e}" for f, g in failed.items() for e in g.errors[:3]]
    return GateResult(
        name="vision_reference",
        passed=not failed and bool(per_view),
        score=round(sum(g.score for g in per_view.values()) / len(per_view), 3) if per_view else 0.0,
        summary=(f"All {len(per_view)} view(s) match the anchor" if per_view and not failed
                 else (f"{len(mismatches)} mismatch view(s): {', '.join(mismatches) or 'none'}; "
                       f"{len(unassessed)} unassessed view(s): {', '.join(unassessed) or 'none'}"
                       if per_view else "No views were compared; unassessed")),
        errors=errors or ([] if per_view else ["no views compared"]),
        warnings=[f"{f}: {w}" for f, g in per_view.items() for w in g.warnings[:3]],
        metrics={"views": {f: g.passed for f, g in per_view.items()},'status':status,
                 'mismatch_views':mismatches,'unassessed_views':list(unassessed)},
    )


def direction_frame_files(folder: str | Path) -> dict[str, Path]:
    """Map facing -> PNG path for a folder of <facing>.png (or <prefix>_<facing>.png) files."""
    out: dict[str, Path] = {}
    for f in sorted(Path(folder).iterdir()):
        if f.suffix.lower() != ".png" or f.name.endswith(".gate.json"):
            continue
        facing = canonical_facing(f.stem)
        if facing is None:
            # allow prefixes like "bot_down_right.png": longest facing suffix wins
            tokens = f.stem.replace("-", "_").split("_")
            for n in (3, 2, 1):
                if len(tokens) >= n:
                    facing = canonical_facing("_".join(tokens[-n:]))
                    if facing:
                        break
        if facing:
            out[facing] = f
    return out


def load_direction_frames(
    source: str | Path,
    *,
    cell: int | None = None,
) -> dict[str, Image.Image]:
    """Load a direction set from a folder of <facing>.png files or a strip in game order."""
    p = Path(source)
    frames: dict[str, Image.Image] = {}
    if p.is_dir():
        for facing, f in direction_frame_files(p).items():
            frames[facing] = Image.open(f).convert("RGBA")
        return frames
    sheet = Image.open(p).convert("RGBA")
    if not cell:
        if sheet.width % sheet.height == 0 and sheet.width // sheet.height in (4, 8):
            cell = sheet.height
        else:
            raise ValueError("cell size required for a strip that is not N square cells wide")
    count = sheet.width // cell
    order = GAME_ORDER if count >= 8 else GAME_ORDER_4
    for i, facing in enumerate(order[:count]):
        frames[facing] = sheet.crop((i * cell, 0, (i + 1) * cell, cell))
    return frames
