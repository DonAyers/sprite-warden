"""Image inspection for the enforcement rules. Imported lazily: only when a rule has to look at pixels."""

from __future__ import annotations

from pathlib import Path


def parse_hex(color: str) -> tuple[int, int, int]:
    text = str(color).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) not in (6, 8):
        raise ValueError(f"not a hex colour: {color!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def matte_fraction(path: str | Path, matte: str, distance: float = 24.0) -> tuple[float, int]:
    """(fraction of the opaque pixels within RGB ``distance`` of ``matte``, opaque pixel count)."""
    from PIL import Image

    mr, mg, mb = parse_hex(matte)
    limit = distance * distance
    with Image.open(path) as im:
        rgba = im.convert("RGBA")
    data = rgba.tobytes()
    opaque = close = 0
    for i in range(0, len(data), 4):
        if data[i + 3] == 0:
            continue
        opaque += 1
        dr, dg, db = data[i] - mr, data[i + 1] - mg, data[i + 2] - mb
        if dr * dr + dg * dg + db * db <= limit:
            close += 1
    return (close / opaque if opaque else 0.0), opaque
