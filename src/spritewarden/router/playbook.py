"""Codified Generator Playbook: provider recipes, prompt sanitization, and constraints.

The provider-specific facts (forbidden words, start-image rules, argument limits,
required gates) live in the recipe registry (``router/recipes.py`` +
``data/recipes.json``). This module turns a routing decision into a
``GenerationRecipe`` whose ``rules`` and ``suggested_gates`` come from that data.
"""

import re
from typing import Any
from pydantic import BaseModel, Field

from .recipes import Recipe, registry


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
    required_gates_before: list[str] = Field(default_factory=list)


# Generic noise phrases every provider warns against (the registry adds per-provider ones).
FORBIDDEN_NOISE_WORDS = [
    r"\btransparent\s+background\b",
    r"\bpixel[\s-]?perfect\b",
    r"\bcrisp\s+pixel\s+art\b",
    r"\bhigh\s+quality\b",
    r"\b8[\s-]?bit\b",
    r"\b16[\s-]?bit\b",
]

_DIMENSION_RE = re.compile(r"\b\d+\s*[x×]\s*\d+\s*(?:px|pixels?)?\b", re.IGNORECASE)
_COUNT_RE = re.compile(r"\b\d+\s*(?:frames?|directions?|views?|variations?|poses?)\b", re.IGNORECASE)


def sanitize_prompt_for_provider(prompt: str, provider: str, operation: str | None = None) -> str:
    """Strip noise keywords and formatting that the provider's docs warn against.

    Patterns come from the recipe registry (provider defaults + the operation's
    own list). Unknown providers are left untouched apart from whitespace cleanup.
    """
    cleaned = prompt
    reg = registry()
    for pattern in reg.forbidden_patterns(provider, operation):
        cleaned = pattern.sub("", cleaned)
    if reg.strip_dimensions(provider, operation):
        cleaned = _DIMENSION_RE.sub("", cleaned)
    if reg.strip_counts(provider, operation):
        cleaned = _COUNT_RE.sub("", cleaned)

    # Clean double spaces and commas
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r",\s*,", ",", cleaned)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    return cleaned.strip(" ,")


def _registry_recipe(provider: str, operation: str) -> Recipe | None:
    return registry().get(provider, operation)


def _apply_registry(recipe: GenerationRecipe, extra_rules: list[str] | None = None) -> GenerationRecipe:
    """Fill rules / gates from the registry entry for the recipe's provider+operation."""
    entry = _registry_recipe(recipe.provider, recipe.operation)
    rules = list(extra_rules or [])
    if entry:
        rules += [r for r in entry.rules_text() if r not in rules]
        recipe.suggested_gates = list(dict.fromkeys(recipe.suggested_gates + entry.required_gates_after))
        recipe.required_gates_before = list(entry.required_gates_before)
    recipe.rules = rules
    return recipe


def recompile_recipe(recipe: GenerationRecipe, provider: str, operation: str | None = None) -> GenerationRecipe:
    """Select a valid pair, then rebuild provider-owned policy and parameters.

    Callers changing provider must use this rather than mutating ``provider``.
    Source/identity arguments are deliberately not carried across providers.
    """
    reg = registry()
    canonical = reg.canonical_provider(provider)
    if canonical is None:
        raise ValueError(f"Unknown provider: {provider}")
    previous = reg.get(recipe.provider, recipe.operation)
    stage = previous.enforce.get("stage") if previous else None
    defaults = {
        "spritefusion": {"rotation": "direction-set", "animation": "animate", None: "generate"},
        "pixellab": {"rotation": "create_character_v3", "animation": "animate_skeleton", None: "inpaint"},
        "spritecook": {"rotation": "generate_character", "animation": "animate_game_art", None: "generate_game_art"},
        "retrodiffusion": {"rotation": "rotate", "animation": "walking", None: "rd_pro"},
        "openrouter_image": {None: "generate"},
    }
    selected = operation or defaults.get(canonical, {}).get(stage)
    entry = reg.get(canonical, selected or "")
    if entry is None:
        raise ValueError(f"Unsupported workflow: {canonical}/{stage or selected}")
    result = GenerationRecipe(
        provider=canonical, operation=entry.operation,
        prompt="" if entry.operation == "direction-set" else sanitize_prompt_for_provider(recipe.prompt, canonical, entry.operation),
        size=recipe.size, canvas_size=recipe.size, perspective=recipe.perspective,
        palette_id=recipe.palette_id,
    )
    return _apply_registry(result)


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
        # Loop / walk animations: PixelLab skeleton/template from a mid-stride state.
        return _apply_registry(
            GenerationRecipe(
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
                suggested_gates=["walk_mechanics", "loop_smoothness", "animation_anomalies"],
            ),
            extra_rules=[
                "Start pose MUST be mid-stride, never standing idle.",
                "Set end frame equal to start frame for seamless loop.",
                "Gate output with walk_check and loop_check before accepting.",
            ],
        )

    if is_vehicle_or_rigid:
        # 8-direction rigid vehicle, hull, or object
        return _apply_registry(
            GenerationRecipe(
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
                suggested_gates=["clipping", "pixel_grid", "palette_adherence"],
            ),
            extra_rules=[
                "Start image must face East (Right), square canvas.",
                "Include front cues (headlights) and back cues (taillights).",
            ],
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
        return _apply_registry(
            GenerationRecipe(
                provider="spritecook",
                operation="generate",
                prompt=sanitize_prompt_for_provider(subject, "spritecook"),
                size=size or 64,
                canvas_size=size or 64,
                perspective=perspective,
                params={"bg_mode": "transparent", "smart_crop_mode": "tightest", "mode": "assets"},
            ),
            extra_rules=[
                "Request a single centered glyph; the background mode is an API field, not prose.",
                "Palette is enforced via the colors hex list (max 8); re-snap with Oklab afterwards.",
                "Stills return a canonical asset_id + sprite_url; keep the id for later edits.",
            ],
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

    # Default character still -> turnaround via PixelLab v3 from a gated south anchor
    return _apply_registry(
        GenerationRecipe(
            provider="pixellab",
            operation="create_character_v3",
            prompt=sanitize_prompt_for_provider(subject, "pixellab"),
            size=size,
            canvas_size=size,
            perspective=perspective,
            params={"directions": 8, "style": "square-on south reference"},
            suggested_gates=["symmetry", "clipping", "palette_adherence"],
        ),
        extra_rules=["Reference image must face true South (viewer); run the start_image gate before generating rotations."],
    )
