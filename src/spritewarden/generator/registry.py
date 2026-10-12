"""Provider registry: maps recipe provider strings to concrete generator drivers.

The drivers were previously declared but never dispatched. This module gives the
pipeline a single dispatch point so routing recipes can actually be executed --
including the SpriteCook integration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseGeneratorDriver, GenerationOutput
from .contracts import capability_status
from .openrouter_image import OpenRouterImageDriver
from .pixellab import PixelLabDriver
from .spritecook import SpriteCookDriver
from .spritefusion import SpriteFusionDriver
from ..router.playbook import GenerationRecipe

DRIVER_CLASSES: dict[str, type[BaseGeneratorDriver]] = {
    "pixellab": PixelLabDriver,
    "sprite_fusion": SpriteFusionDriver,
    "spritefusion": SpriteFusionDriver,
    "openrouter_image": OpenRouterImageDriver,
    "openrouter": OpenRouterImageDriver,
    "spritecook": SpriteCookDriver,
}


def available_providers() -> list[str]:
    """Canonical provider slugs that have an executable driver."""
    return ["sprite_fusion", "openrouter_image", "spritecook"]


def get_driver(provider: str, **kwargs: object) -> BaseGeneratorDriver | None:
    """Instantiate the driver for a provider slug, or None if unknown."""
    from ..router.recipes import registry
    cls = DRIVER_CLASSES.get((provider or "").strip().lower()) or DRIVER_CLASSES.get(registry().canonical_provider(provider))
    if cls is None:
        return None
    return cls(**kwargs)  # type: ignore[arg-type]


def ungated_reasons(recipe: GenerationRecipe, project_root: str | Path | None = None) -> list[str]:
    """Why the recipe's input image does not satisfy its ``required_gates_before`` (empty = it does)."""
    from ..enforce import find_project_root, recipe_gate_reasons

    root = Path(project_root) if project_root else find_project_root(Path.cwd())
    return recipe_gate_reasons(recipe.provider, recipe.operation, recipe.params, root)


def dispatch(recipe: GenerationRecipe, output_dir: str | Path, *, force_ungated: bool = False,
             project_root: str | Path | None = None, register: bool = True, catalog: Any = None,
             game: str = "global", character_id: str | None = None, run_id: str | None = None,
             **kwargs: object) -> GenerationOutput:
    """Execute once through the validated boundary; never implicitly approve output.

    force_ungated bypasses input evidence only, not contracts or recorded routes.
    """
    from .execution import execute
    return execute(recipe, output_dir, get_driver=get_driver, force_ungated=force_ungated,
                   project_root=project_root, register=register, catalog=catalog,
                   game=game, character_id=character_id, run_id=run_id, **kwargs)
