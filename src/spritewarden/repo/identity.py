"""Character & Scene Identity models for visual consistency across pixel art games."""

from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


def validate_side_front_area_ratio(value):
    """Require a finite inclusive thin-profile range: 0 < minimum <= maximum <= 1."""
    import math
    if value is None:
        return None
    if len(value) != 2 or not all(math.isfinite(v) for v in value) or not 0 < value[0] <= value[1] <= 1:
        raise ValueError("side_front_area_ratio must be two finite bounds: 0 < minimum <= maximum <= 1")
    return value


class FeatureLandmark(BaseModel):
    """A distinct landmark feature on a character (visor, antenna, tread mount, etc.)."""
    name: str
    description: str = ""
    side: Literal["left", "right"] | None = None  # the CHARACTER's own side (a held tool); enables the tool-side check
    relative_box: list[int] | None = None  # [x, y, w, h] on the canonical front cell
    marker_hex: str | None = None
    rigid: bool = True  # Whether this part must remain stationary during idle/walk


class CharacterIdentity(BaseModel):
    """The canonical identity definition for a character across all viewpoints and animations."""
    id: str = Field(description="Unique slug, e.g. 'smolbot', 'tredbot', 'builder_treads'")
    name: str
    game: str = Field(default="global", description="Project slug: 'i-require-maintenance', 'smol-experiment', etc.")
    palette_id: str = Field(default="luap-40", description="Assigned palette identifier")
    perspective: str = Field(default="low top-down", description="side | top-down | low top-down | three-quarter | isometric")
    dimensions: list[int] = Field(default=[64, 64], description="Standard cell [width, height] in game pixels")
    ground_row_offset: int = Field(default=7, description="Number of pixels up from the bottom cell edge where feet rest")
    prompt_description: str = Field(description="Concise, concrete visual description")
    prohibitions: list[str] = Field(default_factory=list, description="Explicitly forbidden features (e.g. 'no toes', 'no human limbs')")
    features: dict[str, FeatureLandmark] = Field(default_factory=dict)
    style_anchors: dict[str, Any] = Field(default_factory=dict, description="Provider IDs, e.g. pixellab_character_id, sf_reference_urls")
    turnaround_stills: dict[str, str] = Field(default_factory=dict, description="Mapping from direction ('south', 'east'...) to image path")
    locomotion: Literal["legs", "wheels", "treads"] | None = Field(default=None, description="How it moves: selects the walk gate's mode")
    body_class: Literal["figure", "rigid"] | None = Field(default=None, description="figure (head/torso/limbs) or rigid (vehicle, prop): selects the rotation tool")
    size_tier: Literal["16-32", "48-64", "96", "128"] | None = Field(default=None, description="Resolution tier of the whole set (drawn height range)")

    side_front_area_ratio: list[float] | None = Field(default=None, description="Opt-in inclusive [minimum, maximum] east/west opaque area / south area")

    @field_validator("side_front_area_ratio")
    @classmethod
    def validate_profile(cls, value):
        return validate_side_front_area_ratio(value)

    def format_generation_prompt(self, action_or_pose: str = "") -> str:
        """Compose a prompt with positive description, action, and negative constraints."""
        parts = [self.prompt_description]
        if action_or_pose:
            parts.append(action_or_pose)
        if self.prohibitions:
            negatives = " ".join(f"[{p}]" if not p.startswith("[") else p for p in self.prohibitions)
            return f"{negatives} {' '.join(parts)}"
        return " ".join(parts)


def parse_feature(spec: "str | dict[str, Any] | FeatureLandmark") -> FeatureLandmark:
    """A feature from a dict or from the CLI form ``name:side:marker_hex[:x,y,w,h]`` (``-`` = not set)."""
    if isinstance(spec, FeatureLandmark):
        return spec
    if isinstance(spec, dict):
        data = dict(spec)
        if data.get("side") in ("", "-", "none"):
            data["side"] = None
        return FeatureLandmark(**data)
    parts = [p.strip() for p in str(spec).split(":")]
    if not parts or not parts[0]:
        raise ValueError(f"feature needs a name: {spec!r} (form: name:side:marker_hex[:x,y,w,h])")

    def part(i: int) -> str | None:
        return parts[i] if len(parts) > i and parts[i] not in ("", "-", "none") else None

    side = part(1)
    if side is not None and side.lower() not in ("left", "right"):
        raise ValueError(f"feature side must be left or right, got {side!r}")
    marker = part(2)
    if marker is not None and not marker.startswith("#"):
        marker = "#" + marker
    box = part(3)
    relative_box = [int(v) for v in box.split(",")] if box else None
    if relative_box is not None and len(relative_box) != 4:
        raise ValueError(f"feature relative_box must be x,y,w,h, got {box!r}")
    return FeatureLandmark(
        name=parts[0],
        description=" ".join(b for b in (side.lower() if side else "", parts[0]) if b),
        side=side.lower() if side else None,
        marker_hex=marker.lower() if marker else None,
        relative_box=relative_box,
    )


def build_identity(
    existing: CharacterIdentity | None,
    identity_id: str,
    *,
    name: str | None = None,
    prompt_description: str | None = None,
    game: str | None = None,
    palette_id: str | None = None,
    perspective: str | None = None,
    dimensions: list[int] | None = None,
    ground_row_offset: int | None = None,
    prohibitions: list[str] | None = None,
    features: "list[Any] | dict[str, Any] | None" = None,
    turnaround_stills: dict[str, str] | None = None,
    style_anchors: dict[str, Any] | None = None,
    locomotion: str | None = None,
    body_class: str | None = None,
    size_tier: str | None = None,
    side_front_area_ratio: list[float] | None = None,
) -> CharacterIdentity:
    """Create an identity or update ``existing``: ``None`` keeps a field; features, stills and
    style anchors merge by key (a new entry replaces the one with the same name)."""
    data: dict[str, Any] = existing.model_dump() if existing is not None else {"id": identity_id}
    data["id"] = identity_id
    simple = {
        "name": name, "prompt_description": prompt_description, "game": game, "palette_id": palette_id,
        "perspective": perspective, "dimensions": dimensions, "ground_row_offset": ground_row_offset,
        "side_front_area_ratio": side_front_area_ratio,
        "prohibitions": prohibitions, "locomotion": locomotion, "body_class": body_class, "size_tier": size_tier,
    }
    for key, value in simple.items():
        if value is not None:
            data[key] = value
    if features:
        merged = dict(data.get("features") or {})
        items = features.values() if isinstance(features, dict) else features
        for spec in items:
            feat = parse_feature(spec)
            merged[feat.name] = feat.model_dump()
        data["features"] = merged
    if turnaround_stills:
        data["turnaround_stills"] = {**(data.get("turnaround_stills") or {}), **{str(k): str(v) for k, v in turnaround_stills.items()}}
    if style_anchors:
        data["style_anchors"] = {**(data.get("style_anchors") or {}), **style_anchors}
    data.setdefault("name", identity_id)
    if not data.get("prompt_description"):
        raise ValueError(f"identity '{identity_id}' needs a prompt_description (--desc) when it is first created")
    return CharacterIdentity.model_validate(data)


class SceneIdentity(BaseModel):
    """Environment, biome, or scene definition establishing perspective and palette context."""
    id: str
    name: str
    game: str
    perspective: str
    tile_size: int = 32
    palette_id: str = "luap-40"
    biome: str = "dungeon"
    theme_tags: list[str] = Field(default_factory=list)
    wall_style: str = "cliff"  # cliff | flat | corner
