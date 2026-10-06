"""Sprite padding: normalize provider output onto a uniform, aligned canvas.

Every generation provider returns varying canvas sizes with the art snapped
close to the edges (SpriteCook especially, since its size is only a hint).
Padding re-places the art on a fixed cell -- bottom-center by default, so the
ground row stays consistent -- which keeps catalog entries, sheet cells, and
re-animation imports (e.g. SpriteCook ``animate`` framing) uniform.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image
from pydantic import BaseModel, Field

from .builder import discover_inputs
from ..judge.palette import remap_image


class PadReport(BaseModel):
    """The result of a padding run."""

    outputs: list[str] = Field(default_factory=list)
    canvas: list[int] = Field(description="[width, height]")
    align: str
    warnings: list[str] = Field(default_factory=list)


def _place_in_canvas(
    canvas_w: int,
    canvas_h: int,
    w: int,
    h: int,
    align: str,
    margin: int,
) -> tuple[int, int]:
    vertical, horizontal = _parse_align(align)
    usable_w = max(0, canvas_w - 2 * margin)
    usable_h = max(0, canvas_h - 2 * margin)
    if vertical == "top":
        y = margin
    elif vertical == "center":
        y = margin + (usable_h - h) // 2
    else:
        y = canvas_h - margin - h
    if horizontal == "left":
        x = margin
    elif horizontal == "right":
        x = canvas_w - margin - w
    else:
        x = margin + (usable_w - w) // 2
    return max(0, x), max(0, y)


def _parse_align(align: str) -> tuple[str, str]:
    parts = (align or "bottom-center").lower().replace("_", "-").split("-")
    vertical = parts[0] if parts and parts[0] in ("top", "center", "bottom") else "bottom"
    horizontal = parts[1] if len(parts) > 1 and parts[1] in ("left", "center", "right") else "center"
    return vertical, horizontal


def _shrink_to_fit(img: Image.Image, max_w: int, max_h: int) -> tuple[Image.Image, bool]:
    """Integer-downscale first (keeps the pixel grid), then exact-ratio as a last resort."""
    w, h = img.size
    if w <= max_w and h <= max_h:
        return img, False
    factor = min(max_w / w, max_h / h)
    step = math.floor(factor)
    if step >= 2:
        img = img.resize((max(1, w // step), max(1, h // step)), Image.NEAREST)
        w, h = img.size
        if w <= max_w and h <= max_h:
            return img, True
    factor = min(max_w / w, max_h / h)
    img = img.resize((max(1, round(w * factor)), max(1, round(h * factor))), Image.NEAREST)
    return img, True


def _next_power_of_two(value: int) -> int:
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()


def resolve_canvas(
    size: int | None = None,
    canvas: str | None = None,
    grow: int | None = None,
    content: tuple[int, int] = (0, 0),
    power_of_two: bool = False,
) -> tuple[int, int]:
    """Resolve the target canvas from --size (square), --canvas WxH, or --grow N."""
    if canvas:
        text = canvas.lower().replace("x", " ").split()
        if len(text) != 2:
            raise ValueError(f"--canvas expects WxH, got {canvas!r}")
        w, h = int(text[0]), int(text[1])
    elif grow is not None:
        w, h = content[0] + 2 * grow, content[1] + 2 * grow
    elif size is not None:
        w = h = int(size)
    else:
        raise ValueError("specify --size, --canvas WxH, or --grow")

    if power_of_two:
        w = _next_power_of_two(w)
        h = _next_power_of_two(h)
    if w < 1 or h < 1:
        raise ValueError(f"canvas too small: {w}x{h}")
    return w, h


def pad_image(
    img: Image.Image,
    canvas: tuple[int, int],
    align: str = "bottom-center",
    margin: int = 0,
    crop_content: bool = True,
    fit: str = "clip",
) -> tuple[Image.Image, list[str]]:
    """Place art on a transparent canvas of the given size; returns (image, warnings)."""
    warnings: list[str] = []
    if fit not in ("clip", "shrink"):
        raise ValueError(f"unknown fit mode: {fit!r}")

    if crop_content:
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        else:
            warnings.append("frame is fully transparent")

    margin = max(0, int(margin))
    max_w = max(1, canvas[0] - 2 * margin)
    max_h = max(1, canvas[1] - 2 * margin)
    if img.width > max_w or img.height > max_h:
        if fit == "shrink":
            img, _ = _shrink_to_fit(img, max_w, max_h)
        else:
            warnings.append(
                f"content {img.width}x{img.height} exceeds usable {max_w}x{max_h}; clipped"
            )

    out = Image.new("RGBA", (canvas[0], canvas[1]), (0, 0, 0, 0))
    x, y = _place_in_canvas(canvas[0], canvas[1], img.width, img.height, align, margin)
    out.paste(img, (x, y))
    return out, warnings


def pad_files(
    paths: list[str | Path],
    out: str | Path | None = None,
    size: int | None = None,
    canvas: str | None = None,
    grow: int | None = None,
    align: str = "bottom-center",
    margin: int = 0,
    power_of_two: bool = False,
    fit: str = "clip",
    crop_content: bool = True,
    palette: str | None = None,
    in_place: bool = False,
    recursive: bool = False,
) -> PadReport:
    """Pad one or many images (files/folders/globs) onto a uniform canvas.

    Canvas precedence: ``canvas`` > ``size`` > ``grow`` (grow is per-frame, sized
    from each frame's content + 2*N).
    """
    inputs = discover_inputs(paths, recursive=recursive)
    if not inputs:
        raise ValueError("no input images found")
    if canvas is None and size is None and grow is None:
        raise ValueError("specify --size, --canvas WxH, or --grow")

    warnings: list[str] = []
    outputs: list[str] = []

    if in_place:
        out_paths = list(inputs)
    elif out is not None and len(inputs) == 1 and Path(out).suffix.lower() == ".png":
        out_paths = [Path(out)]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
    else:
        out_dir: Path | None
        if out is not None:
            out_dir = Path(out)
        elif len(inputs) == 1:
            out_paths = [inputs[0].with_name(f"{inputs[0].stem}.padded.png")]
            out_dir = None
        else:
            parents = {p.parent for p in inputs}
            out_dir = (
                next(iter(parents)).parent / f"{next(iter(parents)).name}_padded"
                if len(parents) == 1 and next(iter(parents)).name
                else Path("padded")
            )
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_paths = [out_dir / p.name for p in inputs]

    last_canvas = (0, 0)
    for i, src in enumerate(inputs):
        img = Image.open(src).convert("RGBA")
        if canvas is not None or size is not None:
            per_frame = resolve_canvas(size, canvas, None, (0, 0), power_of_two)
            use_align, use_margin = align, margin
        else:
            # grow: the canvas is exactly content + 2N, so pin the art at (N, N)
            # regardless of align (bottom-centering would re-shift it).
            bbox = img.getbbox() or (0, 0, img.width, img.height)
            per_frame = resolve_canvas(None, None, grow, (bbox[2] - bbox[0], bbox[3] - bbox[1]), power_of_two)
            use_align, use_margin = "top-left", max(0, grow or 0)
        last_canvas = per_frame

        padded, frame_warnings = pad_image(
            img, per_frame, align=use_align, margin=use_margin, crop_content=crop_content, fit=fit
        )
        if palette:
            padded, _, _ = remap_image(padded, palette)
        dst = out_paths[i]
        padded.save(dst)
        outputs.append(str(dst))
        warnings.extend(f"{src.name}: {w}" for w in frame_warnings)

    return PadReport(outputs=outputs, canvas=list(last_canvas), align=align, warnings=warnings)
