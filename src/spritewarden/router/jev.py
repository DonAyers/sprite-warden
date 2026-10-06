"""Jev System One Decision Model client on OpenRouter for fast, typed pipeline routing."""

import json
import os
from typing import Any
import httpx
from pydantic import BaseModel, Field

from .playbook import determine_fallback_recipe, GenerationRecipe

DECISIONS_API_URL = "https://openrouter.ai/api/alpha/decisions"


def _get_openrouter_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key or os.name != "nt":
        return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as env:
            return winreg.QueryValueEx(env, "OPENROUTER_API_KEY")[0]
    except Exception:
        return ""


class DecisionOutcome(BaseModel):
    """The result of a Jev decision query."""
    question_id: str
    decision: str | bool | float
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float = 1.0
    used_fallback: bool = False
    cost_usd: float | None = None


class JevClient:
    """Client for Jev (TypeSafe System One decision model) via OpenRouter."""

    def __init__(self, api_key: str | None = None, timeout: float = 10.0):
        self.api_key = api_key or _get_openrouter_key()
        self.timeout = timeout

    def is_available(self) -> bool:
        return bool(self.api_key)

    def ask_decision(
        self,
        state: dict[str, Any],
        question: str,
        primitive: str = "choice",
        options: list[str] | dict[str, str] | None = None,
        question_id: str = "q1",
    ) -> DecisionOutcome:
        """Call OpenRouter Decisions API with typesafe/jev-1.13 or fallback to local rules."""
        if not self.is_available():
            opts = list(options.keys()) if isinstance(options, dict) else options
            return self._local_fallback(state, question_id, primitive, opts)

        q_type = primitive.lower()
        q_obj: dict[str, Any] = {"type": q_type, "instructions": question}

        if q_type == "choice":
            if isinstance(options, dict):
                q_obj["criteria"] = options
            elif isinstance(options, list):
                q_obj["criteria"] = {opt: f"Select {opt}" for opt in options}
            else:
                q_obj["criteria"] = {"yes": "Affirmative", "no": "Negative"}
        elif q_type == "score":
            q_obj["scale"] = ["1", "2", "3", "4", "5"]

        payload = {
            "model": "typesafe/jev-1.13",
            "state": state,
            "questions": {question_id: q_obj},
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    DECISIONS_API_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    answers = data.get("answers", {})
                    ans = answers.get(question_id, {})
                    cost = (data.get("usage") or {}).get("cost")
                    
                    if q_type == "choice":
                        val = ans.get("choice")
                    elif q_type == "noul":
                        val = ans.get("noul")
                    else:
                        val = ans.get("score")

                    return DecisionOutcome(
                        question_id=question_id,
                        decision=val if val is not None else True,
                        probabilities=ans.get("probabilities", {}),
                        confidence=ans.get("confidence", 1.0),
                        used_fallback=False,
                        cost_usd=cost,
                    )
        except Exception:
            pass  # Fall through to local fallback

        opts = list(options.keys()) if isinstance(options, dict) else options
        return self._local_fallback(state, question_id, primitive, opts)

    def _local_fallback(
        self,
        state: dict[str, Any],
        question_id: str,
        primitive: str,
        options: list[str] | None,
    ) -> DecisionOutcome:
        """Deterministic local decision when offline or unauthenticated."""
        if primitive == "choice" and options:
            # Simple heuristic on state
            req_type = state.get("asset_type", "")
            if "vehicle" in req_type or state.get("is_vehicle"):
                chosen = "sprite_fusion" if "sprite_fusion" in options else options[0]
            elif "tile" in req_type:
                chosen = "pixellab" if "pixellab" in options else options[0]
            elif "turnaround" in req_type or "concept" in req_type:
                chosen = "openrouter_image" if "openrouter_image" in options else options[0]
            elif "icon" in req_type:
                chosen = "spritecook" if "spritecook" in options else options[0]
            else:
                chosen = options[0]
            return DecisionOutcome(
                question_id=question_id,
                decision=chosen,
                probabilities={chosen: 0.9},
                confidence=0.9,
                used_fallback=True,
            )
        elif primitive == "noul":
            return DecisionOutcome(
                question_id=question_id,
                decision=True,
                probabilities={"yes": 0.85, "no": 0.15},
                confidence=0.85,
                used_fallback=True,
            )
        else:
            return DecisionOutcome(
                question_id=question_id,
                decision=0.85,
                confidence=0.85,
                used_fallback=True,
            )

    def route_generation(
        self,
        prompt: str,
        asset_type: str = "character",
        perspective: str = "low top-down",
        size: int = 64,
        is_animation: bool = False,
        is_vehicle_or_rigid: bool = False,
    ) -> GenerationRecipe:
        """Use Jev to choose the provider and assemble the recipe."""
        state = {
            "prompt": prompt,
            "asset_type": asset_type,
            "perspective": perspective,
            "size": size,
            "is_animation": is_animation,
            "is_vehicle": is_vehicle_or_rigid,
        }
        criteria = {
            "sprite_fusion": "Best for rigid 8-direction vehicles, hulls, direction-set from East view with headlights, and props",
            "pixellab": "Best for humanoid characters, 8-direction character v3 from true South reference, and 8-frame walk cycles",
            "retro_diffusion": "Best for on-palette generation and tile variations",
            "openrouter_image": "Best for multi-pose keyframe sheets (load, smear, hit, recover) and concept turnarounds",
            "spritecook": "Palette-locked stills and flat icons only",
        }
        outcome = self.ask_decision(
            state=state,
            question="Which pixel art provider is best suited for this asset?",
            primitive="choice",
            options=criteria,
        )

        recipe = determine_fallback_recipe(
            subject=prompt,
            asset_type=asset_type,
            perspective=perspective,
            size=size,
            is_animation=is_animation,
            is_vehicle_or_rigid=is_vehicle_or_rigid,
        )
        if not outcome.used_fallback:
            recipe.provider = str(outcome.decision)
        return recipe
