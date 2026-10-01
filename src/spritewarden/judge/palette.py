"""Palette Judge & Remapper: Oklab perceptual color space validation and snapping."""

import json
import math
import re
from collections import Counter
from pathlib import Path
from PIL import Image

from .models import GateResult

# Luap-40 Plus palette (canonical 47-color palette from theme/luap-40.tres)
LUAP_40_PLUS_HEX = [
    "#597080", "#6da18a", "#86bf8f", "#b5d9a3", "#e0e6c3", "#f0ebc0", "#e6cca1",
    "#d99f82", "#c4736c", "#bd555f", "#8c3f52", "#733453", "#66294c", "#331a33",
    "#433057", "#464a73", "#546799", "#6699cc", "#82b5d9", "#a1d5e6", "#c2f2f2",
    "#f5f5f5", "#dadada", "#bfbfbf", "#a3a3a3", "#888888", "#6d6d6d", "#515151",
    "#363636", "#1a1a1a", "#33291f", "#403326", "#574639", "#735f4b", "#b39374",
    "#d9b898", "#e6d4c3", "#d9b8ad", "#c98f8f", "#bf6979", "#25562e", "#468232",
    "#884b2b", "#da863e", "#de9e41", "#172038", "#253a5e"
]

SWEETIE_16_HEX = [
    "#1a1c2b", "#57294b", "#9b2226", "#d84f2a", "#e98537", "#fbb954", "#ffff73", "#1a4637",
    "#2f7c46", "#56ab57", "#98dc76", "#1e3752", "#2e688e", "#4ea9cb", "#99dbdf", "#f6e4d8"
]

BUILTIN_PALETTES = {
    "luap-40": LUAP_40_PLUS_HEX,
    "luap-40-plus": LUAP_40_PLUS_HEX,
    "sweetie-16": SWEETIE_16_HEX,
}

NEUTRAL_CHROMA = 0.03
GREY_CHROMA = 0.01
DARK_LIGHTNESS = 0.3
HUE_WINDOW = 45


def hex_to_rgb(hex_code: str) -> tuple[int, int, int]:
    h = hex_code.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def load_palette(source: str | Path | list[str] | list[tuple[int, int, int]] | None = None) -> list[tuple[int, int, int]]:
    """Load a list of RGB tuples from a named preset, json file, godot .tres, or hex list."""
    if source is None:
        source = "luap-40"
    if isinstance(source, str) and source.lower() in BUILTIN_PALETTES:
        return [hex_to_rgb(h) for h in BUILTIN_PALETTES[source.lower()]]

    if isinstance(source, list):
        if not source:
            return [hex_to_rgb(h) for h in LUAP_40_PLUS_HEX]
        if isinstance(source[0], str):
            return [hex_to_rgb(h) for h in source]
        return source  # type: ignore

    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"Palette source not found: {source}")

    text = path.read_text(encoding="utf-8")
    if path.suffix == ".tres":
        match = re.search(r"PackedColorArray\(([^)]*)\)", text)
        if match:
            vals = [float(v.strip()) for v in match.group(1).split(",") if v.strip()]
            return [tuple(round(c * 255) for c in vals[i:i + 3]) for i in range(0, len(vals), 4)]

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [hex_to_rgb(c) if isinstance(c, str) else tuple(c[:3]) for c in data]
        if isinstance(data, dict) and "colors" in data:
            return [hex_to_rgb(c) if isinstance(c, str) else tuple(c[:3]) for c in data["colors"]]
    except Exception:
        pass

    # Fallback: find all hex colors in text
    hexes = re.findall(r"#(?:[0-9a-fA-F]{3}){1,2}\b", text)
    if hexes:
        return [hex_to_rgb(h) for h in hexes]

    raise ValueError(f"Could not parse palette from {source}")


def to_oklab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    def linear(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in rgb)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    )


def chroma_hue(lab: tuple[float, float, float]) -> tuple[float, float]:
    return math.hypot(lab[1], lab[2]), math.degrees(math.atan2(lab[2], lab[1]))


def nearest_palette_color(
    rgb: tuple[int, int, int],
    palette_lab: list[tuple[tuple[int, int, int], tuple[float, float, float]]],
) -> tuple[int, int, int]:
    """Find closest palette color with grey and hue consistency guards."""
    lab = to_oklab(rgb)
    chroma, hue = chroma_hue(lab)
    if chroma < NEUTRAL_CHROMA:
        candidates = [p for p in palette_lab if chroma_hue(p[1])[0] < GREY_CHROMA] or palette_lab
    elif lab[0] < DARK_LIGHTNESS:
        candidates = palette_lab
    else:
        candidates = [
            p for p in palette_lab
            if chroma_hue(p[1])[0] >= GREY_CHROMA
            and abs((chroma_hue(p[1])[1] - hue + 180) % 360 - 180) <= HUE_WINDOW
        ] or palette_lab
    return min(candidates, key=lambda p: sum((a - b) ** 2 for a, b in zip(lab, p[1])))[0]


def check_palette(
    image_or_path: str | Path | Image.Image,
    palette_source: str | Path | list[str] | list[tuple[int, int, int]] | None = None,
    save_mask_path: str | Path | None = None,
    top_violations: int = 8,
) -> tuple[GateResult, Image.Image | None]:
    """Verify that an image adheres 100% to the designated palette."""
    palette = load_palette(palette_source)
    allowed = set(palette)
    palette_lab = [(color, to_oklab(color)) for color in palette]

    if isinstance(image_or_path, (str, Path)):
        img = Image.open(image_or_path).convert("RGBA")
    else:
        img = image_or_path.convert("RGBA")

    counts: Counter[tuple[int, int, int, int]] = Counter()
    mask = Image.new("RGBA", img.size, (0, 0, 0, 0))
    mask_pixels = []
    opaque = 0

    for r, g, b, a in img.getdata():
        if a == 0:
            mask_pixels.append((0, 0, 0, 0))
            continue
        opaque += 1
        if (r, g, b) in allowed and a == 255:
            mask_pixels.append((0, 0, 0, 0))
        else:
            counts[(r, g, b, a)] += 1
            mask_pixels.append((255, 0, 255, 255))

    mask.putdata(mask_pixels)
    bad_count = sum(counts.values())
    bad_pct = (bad_count / opaque * 100.0) if opaque else 0.0
    passed = (bad_count == 0)
    score = max(0.0, 1.0 - (bad_pct / 100.0))

    errors = []
    top_details = []
    for (r, g, b, a), cnt in counts.most_common(top_violations):
        sugg = nearest_palette_color((r, g, b), palette_lab)
        alpha_note = "" if a == 255 else f" (alpha {a})"
        err_msg = f"{rgb_to_hex((r, g, b))}{alpha_note} x{cnt} -> nearest {rgb_to_hex(sugg)}"
        errors.append(err_msg)
        top_details.append({
            "hex": rgb_to_hex((r, g, b)),
            "alpha": a,
            "count": cnt,
            "nearest": rgb_to_hex(sugg),
        })

    summary = (
        f"100% on palette ({opaque} opaque pixels)"
        if passed
        else f"{bad_count}/{opaque} pixels off palette ({bad_pct:.2f}%) across {len(counts)} colors"
    )

    if save_mask_path:
        mask.save(save_mask_path)

    result = GateResult(
        name="palette_adherence",
        passed=passed,
        score=score,
        summary=summary,
        errors=errors[:top_violations],
        metrics={
            "opaque_pixels": opaque,
            "bad_pixels": bad_count,
            "bad_percentage": round(bad_pct, 3),
            "off_colors_count": len(counts),
            "palette_size": len(palette),
        },
        details={"top_violations": top_details},
    )
    return result, mask


def remap_image(
    image: Image.Image,
    palette_source: str | Path | list[str] | list[tuple[int, int, int]] | None = None,
    solid_alpha: bool = True,
) -> tuple[Image.Image, int, int]:
    """Remap every pixel of image to nearest palette color in Oklab."""
    palette = load_palette(palette_source)
    palette_lab = [(color, to_oklab(color)) for color in palette]
    exact = set(palette)
    rgba = image.convert("RGBA")
    mapping: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    pixels = []

    for r, g, b, a in rgba.getdata():
        if a == 0 or (solid_alpha and a < 128):
            pixels.append((0, 0, 0, 0))
            continue
        rgb = (r, g, b)
        if rgb not in mapping:
            mapping[rgb] = rgb if rgb in exact else nearest_palette_color(rgb, palette_lab)
        pixels.append(mapping[rgb] + (255 if solid_alpha else a,))

    out = Image.new("RGBA", rgba.size)
    out.putdata(pixels)
    return out, len(mapping), len(set(mapping.values()))
