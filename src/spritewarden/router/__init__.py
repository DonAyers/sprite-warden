"""Smart Routing: Jev Decision Model, OpenRouter Fusion, and Playbook Guardrails."""

from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field

from .playbook import GenerationRecipe, sanitize_prompt_for_provider, determine_fallback_recipe
from .jev import JevClient, DecisionOutcome
from .fusion import FusionSynthesizer, DeliberationResult
from ..repo.catalog import AssetCatalog
from ..repo.identity import CharacterIdentity


class TriageRoutingResult(BaseModel):
    """The complete routing decision: deduplication check, recipe, and advice."""
    action: str  # "reuse_existing" | "generate_new" | "inpaint_edit"
    existing_asset_match: dict[str, Any] | None = None
    recipe: GenerationRecipe | None = None
    deliberation: DeliberationResult | None = None
    explanation: str


class PipelineRouter:
    """Orchestrates asset search, Jev dispatch, and Fusion deliberation."""

    def __init__(self, catalog: AssetCatalog | None = None):
        self.catalog = catalog or AssetCatalog()
        self.jev = JevClient()
        self.fusion = FusionSynthesizer()

    def route_request(
        self,
        prompt: str,
        asset_type: str = "character",
        character_identity: CharacterIdentity | str | None = None,
        game: str = "global",
        perspective: str = "low top-down",
        size: int = 64,
        is_animation: bool = False,
        is_vehicle_or_rigid: bool = False,
        force_fusion: bool = False,
    ) -> TriageRoutingResult:
        """Evaluate request: check for existing assets, then route to the optimal provider."""
        char_id = character_identity.id if isinstance(character_identity, CharacterIdentity) else character_identity

        # 1. Search existing art catalog (Deduplication)
        existing_matches = self.catalog.search(
            query=prompt,
            game=game,
            character_id=char_id,
            asset_type=asset_type,
            perspective=perspective,
            limit=3,
        )

        if existing_matches:
            best_match = existing_matches[0]
            # If the user prompt closely matches an existing file, recommend reuse
            return TriageRoutingResult(
                action="reuse_existing",
                existing_asset_match=best_match,
                explanation=f"Found existing asset matching query: {best_match['name']} ({best_match['file_path']}).",
            )

        # 2. Format prompt with CharacterIdentity if provided
        final_prompt = prompt
        palette_id = "luap-40"
        if isinstance(character_identity, CharacterIdentity):
            final_prompt = character_identity.format_generation_prompt(prompt)
            palette_id = character_identity.palette_id

        # 3. If novel/ambiguous or explicitly requested, deliberate with OpenRouter Fusion
        if force_fusion:
            delib = self.fusion.deliberate_pipeline(
                task_description=final_prompt,
                game_context=game,
                palette_id=palette_id,
                perspective=perspective,
            )
            return TriageRoutingResult(
                action="generate_new",
                recipe=delib.recipe,
                deliberation=delib,
                explanation="Formulated generation recipe via multi-model OpenRouter Fusion deliberation.",
            )

        # 4. Standard Fast Path via Jev Decision Model
        recipe = self.jev.route_generation(
            prompt=final_prompt,
            asset_type=asset_type,
            perspective=perspective,
            size=size,
            is_animation=is_animation,
            is_vehicle_or_rigid=is_vehicle_or_rigid,
        )
        recipe.palette_id = palette_id

        return TriageRoutingResult(
            action="generate_new",
            recipe=recipe,
            explanation=f"Routed to {recipe.provider} ({recipe.operation}) using proven Playbook constraints.",
        )
