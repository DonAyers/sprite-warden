"""Codified Generator Playbook: provider rules, prompt sanitization, and constraints."""

import re
from typing import Any
from pydantic import BaseModel, Field


class GenerationRecipe(BaseModel):
    """Execution recipe for a specific pixel art asset generation task."""
    provider: str  # "pixellab" | "sprite_fusion" | "retro_diffusion" | "openrouter_image" | "spritecook"
    operation: str  # e.g. "direction-set", "create_character_v3", "animate", "inpaint_pro_flash"
    prompt: str
    size: int
    canvas_size: int
    perspective: str
    palette_id: str = "luap-40"
    params: dict[str, Any] = Field(default_factory=dict)
    rules: list[str] = Field(default_factory=list)
    suggested_gates: list[str] = Field(default_factory=list)


# Forbidden noise phrases that degrade provider outputs (especially Sprite Fusion and PixelLab)
FORBIDDEN_NOISE_WORDS = [
    r"\btransparent\s+background\b",
    r"\bpixel[\s-]?perfect\b",
    r"\bcrisp\s+pixel\s+art\b",
    r"\bhigh\s+quality\b",
    r"\b8[\s-]?bit\b",
    r"\b16[\s-]?bit\b",
]


def sanitize_prompt_for_provider(prompt: str, provider: str) -> str:
    """Strip noise keywords and formatting that providers explicitly warn against in their docs."""
    cleaned = prompt
    p_lower = provider.lower()

    if p_lower in ("sprite_fusion", "spritefusion"):
        for pattern in FORBIDDEN_NOISE_WORDS:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        # Remove raw dimension numbers like "32x32" or "64x64" from prompt text (SF API takes size separately)
        cleaned = re.sub(r"\b\d+x\d+\b", "", cleaned, flags=re.IGNORECASE)

    elif p_lower == "pixellab":
        # PixelLab docs: descriptions should be short (3-10 words) and avoid technical jargon
        for pattern in FORBIDDEN_NOISE_WORDS:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)

    elif p_lower == "spritecook":
        # SpriteCook takes bg_mode/colors as separate API params, so strip them from prose.
        for pattern in FORBIDDEN_NOISE_WORDS:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b\d+x\d+\b", "", cleaned, flags=re.IGNORECASE)

    # Clean double spaces and commas
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r",\s*,", ",", cleaned).strip(" ,")
    return cleaned


def determine_fallback_recipe(
    subject: str,
    asset_type: str,
    perspective: str = "low top-down",
    size: int = 64,
    has_character_identity: bool = False,
    is_animation: bool = False,
    is_vehicle_or_rigid: bool = False,
) -> GenerationRecipe:
    """Deterministic playbook routing based on empirical lessons from generator-playbook.md."""
    if is_animation:
        # Loop / walk animations
        return GenerationRecipe(
            provider="pixellab",
            operation="animate_character_v3",
            prompt=f"{subject} walk cycle, alternating strides",
            size=size,
            canvas_size=int(size * 1.4),  # PixelLab animations add ~40% headroom
            perspective=perspective,
            params={
                "mid_stride_start": True,
                "mid_stride_end": True,
                "frames": 8,
            },
            rules=[
                "Start pose MUST be mid-stride, never standing idle.",
                "Set end frame equal to start frame for seamless loop.",
                "Gate output with walk_check and loop_check before accepting.",
            ],
            suggested_gates=["walk_mechanics", "loop_smoothness", "animation_anomalies"],
        )

    if is_vehicle_or_rigid:
        # 8-direction rigid vehicle, hull, or object
        return GenerationRecipe(
            provider="sprite_fusion",
            operation="direction-set",
            prompt="",  # Sprite Fusion direction-set takes NO prompt; visual cues in image only
            size=size,
            canvas_size=size,
            perspective=perspective,
            params={
                "start_direction": "east",
                "cues": "headlights on front, taillights on rear",
            },
            rules=[
                "Start image must face East (Right), square canvas.",
                "Include front cues (headlights) and back cues (taillights).",
                "Direction-set takes no text prompt.",
            ],
            suggested_gates=["clipping", "pixel_grid", "palette_adherence"],
        )

    if asset_type in ("tileset", "dungeon_walls"):
        # Wang corner / cliff tileset
        return GenerationRecipe(
            provider="pixellab",
            operation="create_topdown_tileset",
            prompt=sanitize_prompt_for_provider(subject, "pixellab"),
            size=32,
            canvas_size=32,
            perspective="top-down",
            params={
                "shape_style": "square",
                "transition_size": 1.0,
                "tileset_adherence": 250,
            },
            rules=[
                "Use Standard mode with shape_style='square' (not round).",
                "transition_size=1.0 generates 25-tile cliff sets with proper wall faces.",
                "tileset_adherence should be ~250 for 32px tiles.",
            ],
            suggested_gates=["pixel_grid", "palette_adherence"],
        )

    if asset_type in ("icon", "ui_icon"):
        # Icons / isolated UI glyphs: SpriteCook enforces the palette at generation time.
        return GenerationRecipe(
            provider="spritecook",
            operation="generate",
            prompt=sanitize_prompt_for_provider(subject, "spritecook"),
            size=size or 64,
            canvas_size=size or 64,
            perspective=perspective,
            params={"bg_mode": "transparent", "smart_crop_mode": "tightest", "mode": "assets"},
            rules=[
                "Request a single centered glyph with transparent background.",
                "Palette is enforced via the colors hex list; re-snap with Oklab afterwards.",
                "Stills return a canonical asset_id + sprite_url; keep the id for later edits.",
            ],
            suggested_gates=["pixel_grid", "palette_adherence", "clipping"],
        )

    if asset_type == "props":
        # Props, crates, pillars, decals
        return GenerationRecipe(
            provider="pixellab",
            operation="create_1_direction_object",
            prompt=sanitize_prompt_for_provider(subject, "pixellab"),
            size=32,
            canvas_size=32,
            perspective=perspective,
            params={"batch_count": 16},
            rules=[
                "Use square canvas <=42px for 64 candidates.",
                "Keep descriptions short (3-8 words).",
                "Pad reference images square first.",
            ],
            suggested_gates=["clipping", "palette_adherence"],
        )

    # Default character still
    return GenerationRecipe(
        provider="pixellab" if not is_vehicle_or_rigid else "sprite_fusion",
        operation="create_character_v3",
        prompt=sanitize_prompt_for_provider(subject, "pixellab"),
        size=size,
        canvas_size=size,
        perspective=perspective,
        params={"directions": 8, "style": "square-on south reference"},
        rules=[
            "Reference image must face true South (viewer).",
            "Symmetry must be checked before generating rotations.",
            "Canvas must be square.",
        ],
        suggested_gates=["symmetry", "clipping", "palette_adherence"],
    )
