"""Start-image readiness gate: is this still fit to be the anchor everything derives from?

Every provider derives its views and motions from one input image, so a flaw in
it is a flaw times forty. This gate checks what the providers' own docs ask of
that image (pipeline-audit.md section 2/3): square canvas, a clear transparent
margin, symmetry for south/north facings, drawn height inside the size tier,
no stray components outside the body mass (the "orbs beside the head"), native
1:1 pixels, and the palette.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from .animation import _connected_pieces
from .bounds import check_symmetry
from .models import GateResult
from .palette import check_palette, hex_to_rgb
from .pixel import infer_scale

# Resolution tiers (pipeline-audit 2c): keep one tier for a whole set. Values are
# the drawn (content) height range that counts as "inside" the tier.
SIZE_TIERS: dict[str, tuple[int, int]] = {
    "16-32": (12, 32),
    "48-64": (40, 64),
    "96": (72, 96),
    "128": (100, 128),
}
SYMMETRIC_FACINGS = {"south", "north", "s", "n", "down", "up", "front", "back"}
DEFAULT_SYMMETRY_THRESHOLD = 0.95
DEFAULT_STRAY_FRACTION = 0.06


def _alpha_coords(img: Image.Image) -> set[tuple[int, int]]:
    px = img.load()
    w, h = img.size
    return {(x, y) for y in range(h) for x in range(w) if px[x, y][3] > 0}


def _bbox(coords: set[tuple[int, int]]) -> list[int]:
    xs = [x for x, _ in coords]
    ys = [y for _, y in coords]
    return [min(xs), min(ys), max(xs) + 1, max(ys) + 1]


def find_components(img: Image.Image, stray_fraction: float = DEFAULT_STRAY_FRACTION) -> dict[str, Any]:
    """Split the drawn pixels into the main body mass, strays, and detached large parts."""
    coords = _alpha_coords(img)
    if not coords:
        return {"main": None, "strays": [], "detached": [], "count": 0}
    pieces = sorted(_connected_pieces(coords, diagonal=True), key=len, reverse=True)
    main = pieces[0]
    strays = []
    detached = []
    for piece in pieces[1:]:
        entry = {"pixels": len(piece), "bbox": _bbox(piece), "fraction": round(len(piece) / len(main), 4)}
        if len(piece) < stray_fraction * len(main):
            strays.append(entry)
        else:
            detached.append(entry)
    return {
        "main": {"pixels": len(main), "bbox": _bbox(main)},
        "strays": strays,
        "detached": detached,
        "count": len(pieces),
    }


def margin_violations(img: Image.Image, margin_px: int) -> dict[str, int]:
    """Opaque pixel counts inside the required transparent margin, per edge."""
    if margin_px <= 0:
        return {}
    px = img.load()
    w, h = img.size
    out: dict[str, int] = {}
    counts = {"left": 0, "right": 0, "top": 0, "bottom": 0}
    for y in range(h):
        for x in range(w):
            if px[x, y][3] == 0:
                continue
            if x < margin_px:
                counts["left"] += 1
            if x >= w - margin_px:
                counts["right"] += 1
            if y < margin_px:
                counts["top"] += 1
            if y >= h - margin_px:
                counts["bottom"] += 1
    for k, v in counts.items():
        if v:
            out[k] = v
    return out


def locate_markers(img: Image.Image, markers: dict[str, str]) -> dict[str, dict[str, Any] | None]:
    """Centroid of each marker colour (feature name -> hex), or None when absent."""
    px = img.load()
    w, h = img.size
    wanted = {name: hex_to_rgb(hx) for name, hx in markers.items() if hx}
    found: dict[str, list[tuple[int, int]]] = {name: [] for name in wanted}
    for y in range(h):
        for x in range(w):
            p = px[x, y]
            if p[3] == 0:
                continue
            for name, rgb in wanted.items():
                if p[:3] == rgb:
                    found[name].append((x, y))
    out: dict[str, dict[str, Any] | None] = {}
    for name, pts in found.items():
        if not pts:
            out[name] = None
            continue
        cx = sum(x for x, _ in pts) / len(pts)
        cy = sum(y for _, y in pts) / len(pts)
        out[name] = {"centroid": [round(cx, 1), round(cy, 1)], "pixels": len(pts), "bbox": _bbox(set(pts))}
    return out


def check_start_image(
    image_or_path: str | Path | Image.Image,
    *,
    target_height: int | None = None,
    size_tier: str | None = None,
    margin_px: int = 3,
    facing: str = "south",
    palette: str | Path | list[str] | None = None,
    identity: Any | None = None,
    symmetry_threshold: float = DEFAULT_SYMMETRY_THRESHOLD,
    stray_fraction: float = DEFAULT_STRAY_FRACTION,
    height_tolerance: float = 0.10,
) -> GateResult:
    """Readiness gate for an anchor / start image. Returns one GateResult named ``start_image``."""
    if isinstance(image_or_path, (str, Path)):
        img = Image.open(image_or_path).convert("RGBA")
    else:
        img = image_or_path.convert("RGBA")

    w, h = img.size
    checks: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {"canvas": [w, h], "facing": facing, "margin_px": margin_px}

    def record(name: str, passed: bool | None, detail: str, *, error: bool = True) -> None:
        checks[name] = {"passed": passed, "detail": detail}
        if passed is False:
            (errors if error else warnings).append(f"{name}: {detail}")

    box = img.getbbox()
    if not box:
        return GateResult(
            name="start_image", passed=False, score=0.0, summary="Empty image; nothing drawn",
            errors=["empty: image has no opaque pixels"], metrics=metrics, details={"checks": checks},
        )
    content_w = box[2] - box[0]
    content_h = box[3] - box[1]
    metrics.update({"bbox": list(box), "content_width": content_w, "content_height": content_h})

    # 1. Square canvas
    record("square_canvas", w == h, f"{w}x{h}" + ("" if w == h else " is not square; providers pad/crop to a square and lose the margin"))

    # 2. Transparent margin all round
    viol = margin_violations(img, margin_px)
    actual_margin = min(box[0], box[1], w - box[2], h - box[3])
    metrics["actual_margin_px"] = actual_margin
    record(
        "margin",
        not viol,
        f"{actual_margin}px clear on the tightest side (need >={margin_px}px)"
        + (f"; opaque pixels inside the margin: {viol}" if viol else ""),
    )

    # 3. Symmetry for south/north facings
    if facing.lower() in SYMMETRIC_FACINGS:
        markers = None
        if identity is not None and getattr(identity, "features", None):
            markers = [f.marker_hex for f in identity.features.values() if getattr(f, "marker_hex", None)] or None
        sym = check_symmetry(img, marker_colors=markers, min_silhouette_match=symmetry_threshold)
        metrics["symmetry"] = sym.metrics.get("silhouette_match")
        record(
            "symmetry",
            sym.passed,
            f"silhouette {sym.metrics.get('silhouette_match', 0) * 100:.1f}% mirrored (need >={symmetry_threshold * 100:.0f}% for a {facing} start)",
        )
        for wmsg in sym.warnings:
            warnings.append(f"symmetry: {wmsg}")
    else:
        record("symmetry", None, f"skipped for facing '{facing}'")

    # 4. Content height vs target / tier / identity tier / identity cell
    lo = hi = None
    source = None
    if not target_height and not size_tier and identity is not None and getattr(identity, "size_tier", None):
        size_tier = identity.size_tier
    if target_height:
        tol = max(2, int(round(target_height * height_tolerance)))
        lo, hi, source = target_height - tol, target_height + tol, f"target {target_height}px (+-{tol})"
    elif size_tier:
        tier = SIZE_TIERS.get(str(size_tier))
        if tier is None:
            record("content_height", False, f"unknown size tier '{size_tier}' (known: {', '.join(SIZE_TIERS)})")
        else:
            lo, hi, source = tier[0], tier[1], f"tier {size_tier} ({tier[0]}-{tier[1]}px)"
    elif identity is not None and getattr(identity, "dimensions", None):
        cell_h = int(identity.dimensions[1])
        lo, hi, source = cell_h // 2, cell_h - 2 * margin_px, f"identity cell {cell_h}px"
    if lo is not None and hi is not None:
        ok = lo <= content_h <= hi
        record("content_height", ok, f"drawn height {content_h}px vs {source}")
    elif "content_height" not in checks:
        record("content_height", None, f"drawn height {content_h}px (no target/tier given)")

    # 5. Stray appendages: components outside the main body mass
    comp = find_components(img, stray_fraction=stray_fraction)
    metrics["components"] = comp["count"]
    metrics["strays"] = comp["strays"]
    metrics["detached"] = comp["detached"]
    if comp["strays"]:
        spots = "; ".join(f"{s['pixels']}px at {s['bbox']}" for s in comp["strays"][:6])
        record("strays", False, f"{len(comp['strays'])} stray component(s) outside the body mass: {spots}")
    else:
        record("strays", True, "one connected body" if comp["count"] == 1 else f"{comp['count']} components, none stray")
    if comp["detached"]:
        warnings.append(
            "detached: large component(s) not touching the body: "
            + "; ".join(f"{d['pixels']}px at {d['bbox']}" for d in comp["detached"][:4])
        )

    # 6. Native 1:1 pixels
    scale, how = infer_scale(img)
    metrics["pixel_scale"] = scale
    metrics["pixel_scale_source"] = how
    record(
        "native_pixels",
        scale == 1 or how == "screenshot",
        "native 1:1 pixel grid" if scale == 1 else f"drawn on a {scale}px block grid ({how}); unzoom / snap before using it",
    )

    # 7. Palette
    pal_source = palette
    if pal_source is None and identity is not None and getattr(identity, "palette_id", None):
        pal_source = identity.palette_id
    if pal_source is not None:
        pal, _ = check_palette(img, palette_source=pal_source)
        metrics["palette_adherence"] = pal.score
        record("palette", pal.passed, pal.summary)
    else:
        record("palette", None, "no palette given")

    # 8. Feature markers (hands, visor...) recorded for downstream tool-side checks
    if identity is not None and getattr(identity, "features", None):
        markers = {name: f.marker_hex for name, f in identity.features.items() if getattr(f, "marker_hex", None)}
        if markers:
            located = locate_markers(img, markers)
            metrics["landmarks"] = located
            missing = [n for n, v in located.items() if v is None]
            if missing:
                warnings.append(f"landmarks: marker colour not found for {', '.join(missing)}")

    judged = [c for c in checks.values() if c["passed"] is not None]
    passed = all(c["passed"] for c in judged) and not errors
    score = round(sum(1 for c in judged if c["passed"]) / len(judged), 3) if judged else 0.0
    failed = [n for n, c in checks.items() if c["passed"] is False]
    summary = (
        f"Start image ready: {content_w}x{content_h}px drawn on a {w}x{h} canvas, {len(judged)} checks passed"
        if passed
        else f"Not a usable start image: {', '.join(failed)}"
    )
    return GateResult(
        name="start_image",
        passed=passed,
        score=score,
        summary=summary,
        errors=errors,
        warnings=warnings,
        metrics=metrics,
        details={"checks": checks},
    )
