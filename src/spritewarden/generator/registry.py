"""Provider registry: maps recipe provider strings to concrete generator drivers.

The drivers were previously declared but never dispatched. This module gives the
pipeline a single dispatch point so routing recipes can actually be executed --
including the SpriteCook integration.
"""

from __future__ import annotations

from pathlib import Path

from .base import BaseGeneratorDriver, GenerationOutput
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
    return ["pixellab", "sprite_fusion", "openrouter_image", "spritecook"]


def get_driver(provider: str, **kwargs: object) -> BaseGeneratorDriver | None:
    """Instantiate the driver for a provider slug, or None if unknown."""
    cls = DRIVER_CLASSES.get((provider or "").strip().lower())
    if cls is None:
        return None
    return cls(**kwargs)  # type: ignore[arg-type]


def dispatch(recipe: GenerationRecipe, output_dir: str | Path, **kwargs: object) -> GenerationOutput:
    """Execute a recipe through its provider driver, returning a structured failure if unknown."""
    driver = get_driver(recipe.provider, **kwargs)
    if driver is None:
        return GenerationOutput(
            success=False,
            provider=recipe.provider,
            error=f"No generator driver registered for provider '{recipe.provider}'",
        )
    if not driver.is_configured():
        return GenerationOutput(
            success=False,
            provider=recipe.provider,
            error=f"Provider '{recipe.provider}' is not configured (missing API key)",
        )
    return driver.generate(recipe, output_dir)
