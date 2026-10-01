"""Character & Scene Identity models for visual consistency across pixel art games."""

from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field


class FeatureLandmark(BaseModel):
    """A distinct landmark feature on a character (visor, antenna, tread mount, etc.)."""
    name: str
    description: str
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

    def format_generation_prompt(self, action_or_pose: str = "") -> str:
        """Compose a prompt with positive description, action, and negative constraints."""
        parts = [self.prompt_description]
        if action_or_pose:
            parts.append(action_or_pose)
        if self.prohibitions:
            negatives = " ".join(f"[{p}]" if not p.startswith("[") else p for p in self.prohibitions)
            return f"{negatives} {' '.join(parts)}"
        return " ".join(parts)


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
