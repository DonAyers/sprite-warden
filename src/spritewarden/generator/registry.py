"""Provider registry: maps recipe provider strings to concrete generator drivers.

The drivers were previously declared but never dispatched. This module gives the
pipeline a single dispatch point so routing recipes can actually be executed --
including the SpriteCook integration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


def ungated_reasons(recipe: GenerationRecipe, project_root: str | Path | None = None) -> list[str]:
    """Why the recipe's input image does not satisfy its ``required_gates_before`` (empty = it does)."""
    from ..enforce import find_project_root, recipe_gate_reasons

    root = Path(project_root) if project_root else find_project_root(Path.cwd())
    return recipe_gate_reasons(recipe.provider, recipe.operation, recipe.params, root)


def dispatch(
    recipe: GenerationRecipe,
    output_dir: str | Path,
    *,
    force_ungated: bool = False,
    project_root: str | Path | None = None,
    register: bool = True,
    catalog: Any = None,
    game: str = "global",
    character_id: str | None = None,
    **kwargs: object,
) -> GenerationOutput:
    """Execute a recipe through its provider driver, returning a structured failure if unknown.

    A recipe that derives from an image (rotation / animation) is refused unless that image
    carries a current passed gate record for one of the recipe's ``required_gates_before``
    (or an umbrella gate) and belongs to the provider its ``pipeline.json`` names.
    ``force_ungated=True`` (``warden generate --force-ungated``) runs it anyway and records
    the skipped reasons in the output metadata.

    Outputs of a successful run are registered in the asset catalog (``catalog``, default the
    shared one) with provider, operation, provider asset ids and params; their catalog ids
    come back in ``metadata["catalog_asset_ids"]``. ``register=False`` skips that.
    """
    driver = get_driver(recipe.provider, **kwargs)
    if driver is None:
        return GenerationOutput(
            success=False,
            provider=recipe.provider,
            error=f"No generator driver registered for provider '{recipe.provider}'",
        )
    reasons = ungated_reasons(recipe, project_root)
    if reasons and not force_ungated:
        return GenerationOutput(
            success=False,
            provider=recipe.provider,
            error=(
                f"Refused: {recipe.provider}/{recipe.operation} requires gate(s) "
                f"{', '.join(recipe.required_gates_before) or 'on its input'} before it runs: "
                + " | ".join(reasons)
                + " (gate the input first, or pass --force-ungated / force_ungated=True to run anyway)"
            ),
            metadata={"refused": "ungated_input", "reasons": reasons},
        )
    if not driver.is_configured():
        return GenerationOutput(
            success=False,
            provider=recipe.provider,
            error=f"Provider '{recipe.provider}' is not configured (missing API key)",
        )
    output = driver.generate(recipe, output_dir)
    if reasons:
        output.metadata["forced_ungated"] = reasons
    if register and output.success and output.output_files:
        # Provenance: every output file gets a catalog row (provider, operation, asset ids, params).
        try:
            from ..repo.provenance import register_generation

            if catalog is None:
                from ..repo.catalog import AssetCatalog

                catalog = AssetCatalog()
            output.metadata["catalog_asset_ids"] = register_generation(
                catalog, recipe, output, game=game, character_id=character_id
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail a paid generation
            output.metadata["catalog_error"] = f"{type(exc).__name__}: {exc}"
    return output
