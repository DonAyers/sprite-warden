"""Boundary and Symmetry Judges: detect clipped edges and measure silhouette/feature symmetry."""

from pathlib import Path
from PIL import Image

from .models import GateResult

OUTLINE_SLACK = 12


def _lum(p: tuple[int, int, int, int]) -> int:
    return (p[0] * 299 + p[1] * 587 + p[2] * 114) // 1000


def detect_cut_edges(image: Image.Image, margin: int = 0) -> dict[str, int]:
    """Find edges where non-outline pixels touch or penetrate canvas margins."""
    img = image.convert("RGBA")
    px = img.load()
    w, h = img.size
    opaque = [p for p in img.getdata() if p[3] > 0]
    if not opaque:
        return {}
    darkest = min(_lum(p) for p in opaque)

    edges = {
        "left": [(x, y) for x in range(margin + 1) for y in range(h)],
        "right": [(w - 1 - x, y) for x in range(margin + 1) for y in range(h)],
        "top": [(x, y) for y in range(margin + 1) for x in range(w)],
        "bottom": [(x, h - 1 - y) for y in range(margin + 1) for x in range(w)],
    }
    out = {}
    for name, points in edges.items():
        drawn = [px[p] for p in points if px[p][3] > 0]
        # Flag if any pixel on the boundary is lighter than outline (internal fill / highlight sliced)
        if drawn and any(_lum(p) > darkest + OUTLINE_SLACK for p in drawn):
            out[name] = len(drawn)
    return out


def check_clipping(
    image_or_path: str | Path | Image.Image,
    margin: int = 0,
    cell: int | None = None,
) -> GateResult:
    """Verify that a sprite or sheet cell is not cut off at the boundaries."""
    if isinstance(image_or_path, (str, Path)):
        img = Image.open(image_or_path).convert("RGBA")
    else:
        img = image_or_path.convert("RGBA")

    bad_cuts: list[dict[str, Any]] = []
    if cell:
        rows = img.height // cell
        cols = img.width // cell
        for r in range(rows):
            for c in range(cols):
                piece = img.crop((c * cell, r * cell, (c + 1) * cell, (r + 1) * cell))
                if piece.getbbox():
                    cuts = detect_cut_edges(piece, margin)
                    if cuts:
                        bad_cuts.append({"cell": [r, c], "edges": cuts})
    else:
        cuts = detect_cut_edges(img, margin)
        if cuts:
            bad_cuts.append({"edges": cuts})

    passed = (len(bad_cuts) == 0)
    score = 1.0 if passed else max(0.0, 1.0 - (len(bad_cuts) * 0.25))

    errors = []
    for cut in bad_cuts:
        loc = f"cell {cut['cell']}" if "cell" in cut else "sprite"
        edge_str = ", ".join(f"{e} ({n}px)" for e, n in cut["edges"].items())
        errors.append(f"{loc} sliced on edge(s): {edge_str}")

    summary = (
        "Clean padding; no edges sliced or truncated"
        if passed
        else f"Clipped art detected across {len(bad_cuts)} section(s)"
    )

    return GateResult(
        name="clipping",
        passed=passed,
        score=score,
        summary=summary,
        errors=errors,
        metrics={"cut_sections": len(bad_cuts), "margin": margin},
        details={"violations": bad_cuts},
    )


def check_symmetry(
    image_or_path: str | Path | Image.Image,
    marker_colors: list[str] | None = None,
    min_silhouette_match: float = 0.85,
) -> GateResult:
    """Evaluate horizontal silhouette and feature symmetry around the vertical midline."""
    if isinstance(image_or_path, (str, Path)):
        img = Image.open(image_or_path).convert("RGBA")
    else:
        img = image_or_path.convert("RGBA")

    box = img.getbbox()
    if not box:
        return GateResult(
            name="symmetry",
            passed=False,
            score=0.0,
            summary="Empty image; no drawn pixels to analyze",
            errors=["Image has no opaque pixels"],
        )

    left, top, right, bottom = box
    drawn = {
        (x, y): img.getpixel((x, y))[:3]
        for y in range(top, bottom)
        for x in range(left, right)
        if img.getpixel((x, y))[3] > 0
    }
    axis2 = left + right - 1  # 2 * midpoint
    shape_matches = sum(1 for (x, y) in drawn if (axis2 - x, y) in drawn)
    color_matches = sum(1 for (x, y), c in drawn.items() if drawn.get((axis2 - x, y)) == c)

    silhouette_pct = shape_matches / len(drawn) if drawn else 0.0
    color_pct = color_matches / len(drawn) if drawn else 0.0

    marker_offset_px = None
    if marker_colors:
        from .palette import hex_to_rgb
        wanted = {hex_to_rgb(h) for h in marker_colors}
        marks = [x for (x, y), c in drawn.items() if c in wanted]
        if marks:
            marker_offset_px = (sum(marks) / len(marks)) - (axis2 / 2.0)

    passed = silhouette_pct >= min_silhouette_match
    score = round(silhouette_pct, 3)

    errors = []
    warnings = []
    if not passed:
        errors.append(
            f"Silhouette symmetry {silhouette_pct * 100:.1f}% is below required threshold ({min_silhouette_match * 100:.1f}%)"
        )
    if marker_offset_px is not None and abs(marker_offset_px) > 1.5:
        warnings.append(f"Visual marker is {marker_offset_px:+.1f}px off center (potential twisted/angled pose)")

    summary = (
        f"Symmetric pose: {silhouette_pct * 100:.1f}% silhouette / {color_pct * 100:.1f}% color match"
        if passed
        else f"Asymmetric silhouette ({silhouette_pct * 100:.1f}% match) indicates angled perspective"
    )

    return GateResult(
        name="symmetry",
        passed=passed,
        score=score,
        summary=summary,
        errors=errors,
        warnings=warnings,
        metrics={
            "silhouette_match": round(silhouette_pct, 4),
            "color_match": round(color_pct, 4),
            "marker_offset_px": round(marker_offset_px, 2) if marker_offset_px is not None else None,
            "bounds": [left, top, right, bottom],
        },
    )
