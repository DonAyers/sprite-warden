"""Sprite-sheet builder: discover frames, snap them, and compose cohesive sheets.

Turns a loose set of PNGs or a folder into an evenly-celled, palette-snapped,
ground-aligned spritesheet with a JSON manifest -- the standardized input the
``judge-anim`` gates expect.
"""

from __future__ import annotations

import glob
import json
import math
import re
from pathlib import Path

from PIL import Image
from pydantic import BaseModel, Field

from .snap import SnapResult, find_pixel_snapper, snap_image

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


class SheetFrame(BaseModel):
    """One placed frame in a composed sheet."""

    index: int
    name: str
    source: str
    normalized: str
    rect: list[int] = Field(description="[x, y, w, h] on the sheet")
    content_size: list[int]
    pixel_size: float | None = None


class SheetReport(BaseModel):
    """The result of building one spritesheet."""

    sheet_path: str
    manifest_path: str
    palette_id: str | None
    engine: str
    cell_size: int
    columns: int
    rows: int
    frame_count: int
    frames: list[SheetFrame] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _natural_key(path: Path) -> list[object]:
    parts = re.split(r"(\d+)", path.stem.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def discover_inputs(paths: list[str | Path], recursive: bool = False) -> list[Path]:
    """Expand files, directories, and glob patterns into an ordered, de-duplicated list."""
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
            key = str(path.resolve())
            if key not in seen:
                seen.add(key)
                found.append(path)

    for raw in paths:
        text = str(raw)
        if any(ch in text for ch in "*?["):
            for match in glob.glob(text, recursive=recursive):
                add(Path(match))
            continue
        p = Path(raw)
        if p.is_dir():
            iterator = p.rglob("*") if recursive else p.iterdir()
            for child in iterator:
                add(child)
        else:
            add(p)

    found.sort(key=_natural_key)
    return found


def _parse_align(align: str) -> tuple[str, str]:
    parts = (align or "bottom-center").lower().replace("_", "-").split("-")
    vertical = parts[0] if parts[0] in ("top", "center", "bottom") else "bottom"
    horizontal = parts[1] if len(parts) > 1 and parts[1] in ("left", "center", "right") else "center"
    return vertical, horizontal


def _next_power_of_two(value: int) -> int:
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()


def _content_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    bbox = img.getbbox()
    if not bbox:
        return None
    if bbox == (0, 0, img.width, img.height):
        return None
    return bbox


def _place(cell: int, cw: int, ch: int, align: str, margin: int) -> tuple[int, int]:
    vertical, horizontal = _parse_align(align)
    usable = max(0, cell - 2 * margin)
    if vertical == "top":
        y = margin
    elif vertical == "center":
        y = margin + (usable - ch) // 2
    else:
        y = cell - margin - ch
    if horizontal == "left":
        x = margin
    elif horizontal == "right":
        x = cell - margin - cw
    else:
        x = margin + (usable - cw) // 2
    return max(0, x), max(0, y)


def build_sheet(
    sources: list[str | Path],
    out_path: str | Path,
    palette: str | None = "luap-40",
    cell: int | None = None,
    columns: int | None = None,
    snap: bool = True,
    engine: str = "auto",
    align: str = "bottom-center",
    margin: int = 0,
    power_of_two: bool = False,
    crop: bool = True,
    pixel_size: int | None = None,
    recursive: bool = False,
) -> SheetReport:
    """Build one cohesive spritesheet from files/folders/globs."""
    inputs = discover_inputs(sources, recursive=recursive)
    if not inputs:
        raise ValueError("no input images found")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames_dir = out_path.parent / f"{out_path.stem}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    snap_engine = engine
    if not snap and snap_engine == "auto":
        snap_engine = "none"

    prepared: list[tuple[Path, SnapResult, Image.Image]] = []
    warnings: list[str] = []
    engine_used = "none"
    for src in inputs:
        result = snap_image(
            src,
            frames_dir / f"{src.stem}.png",
            palette_source=palette,
            engine=snap_engine,
            pixel_size=pixel_size,
        )
        warnings.extend(result.warnings)
        engine_used = result.engine if engine_used == "none" else engine_used
        img = Image.open(result.path).convert("RGBA")
        if crop:
            bbox = _content_bbox(img)
            if bbox:
                img = img.crop(bbox)
        prepared.append((src, result, img))

    if cell is not None:
        cell_size = max(1, int(cell))
    else:
        max_w = max(img.width for _, _, img in prepared)
        max_h = max(img.height for _, _, img in prepared)
        cell_size = max(max_w, max_h) + 2 * max(0, margin)
        if power_of_two:
            cell_size = _next_power_of_two(cell_size)

    n = len(prepared)
    cols = int(columns) if columns else max(1, math.ceil(math.sqrt(n)))
    cols = max(1, cols)
    rows = math.ceil(n / cols)

    sheet = Image.new("RGBA", (cols * cell_size, rows * cell_size), (0, 0, 0, 0))
    frames: list[SheetFrame] = []
    for i, (src, result, img) in enumerate(prepared):
        col = i % cols
        row = i // cols
        dx, dy = _place(cell_size, img.width, img.height, align, margin)
        x = col * cell_size + dx
        y = row * cell_size + dy
        if img.width > cell_size or img.height > cell_size:
            warnings.append(f"frame '{src.stem}' larger than cell {cell_size}px; clipped")
        sheet.paste(img, (x, y))
        frames.append(
            SheetFrame(
                index=i,
                name=src.stem,
                source=str(src),
                normalized=str(result.path),
                rect=[x, y, img.width, img.height],
                content_size=[img.width, img.height],
                pixel_size=result.pixel_size,
            )
        )

    sheet.save(out_path)
    manifest_path = out_path.with_suffix(".json")
    manifest_path.write_text(
        json.dumps(
            {
                "sheet": str(out_path),
                "cell_size": cell_size,
                "columns": cols,
                "rows": rows,
                "palette": palette,
                "engine": engine_used,
                "frames": [f.model_dump() for f in frames],
            },
            indent=2,
        )
    )

    return SheetReport(
        sheet_path=str(out_path),
        manifest_path=str(manifest_path),
        palette_id=palette,
        engine=engine_used,
        cell_size=cell_size,
        columns=cols,
        rows=rows,
        frame_count=n,
        frames=frames,
        warnings=warnings,
    )


def _prefix_of(stem: str) -> str:
    match = re.match(r"[A-Za-z]+", stem)
    return match.group(0).lower() if match else "sheet"


def build_sheets(
    sources: list[str | Path],
    out: str | Path,
    group_by: str | None = None,
    **kwargs,
) -> list[SheetReport]:
    """Build one sheet per prefix group (``group_by='prefix'``) or a single sheet."""
    if group_by != "prefix":
        out_path = Path(out)
        if out_path.is_dir() or out_path.suffix.lower() != ".png":
            out_path = out_path / "spritesheet.png" if out_path.is_dir() else out_path.with_suffix(".png")
        return [build_sheet(sources, out_path, **kwargs)]

    groups: dict[str, list[str | Path]] = {}
    for path in discover_inputs(sources, recursive=kwargs.get("recursive", False)):
        groups.setdefault(_prefix_of(path.stem), []).append(path)

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    reports: list[SheetReport] = []
    for prefix in sorted(groups):
        reports.append(build_sheet(groups[prefix], out_dir / f"{prefix}.png", **kwargs))
    return reports


def engine_report() -> dict:
    """Describe which snapping engine will be used (for CLI/MCP diagnostics)."""
    path = find_pixel_snapper()
    return {
        "rust_snapper": path,
        "engine": "rust" if path else "python",
        "env_override": "SPRITEWARDEN_PIXEL_SNAPPER",
    }
