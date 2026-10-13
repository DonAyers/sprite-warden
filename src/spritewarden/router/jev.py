"""Jev System One Decision Model client on OpenRouter for fast, typed pipeline routing."""

import json
import os
from typing import Any
import httpx
from pydantic import BaseModel, Field

from .playbook import determine_fallback_recipe, GenerationRecipe, recompile_recipe

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
    decision: str | bool | float | None = None
    probabilities: dict[str, float] = Field(default_factory=dict)
    confidence: float | None = None
    error: str | None = None
    used_fallback: bool = False
    cost_usd: float | None = None


class JevClient:
    """Client for Jev (TypeSafe System One decision model) via OpenRouter."""

    def __init__(self, api_key: str | None = None, timeout: float = 10.0):
        self.api_key = api_key or _get_openrouter_key()
        self.timeout = timeout

    def is_available(self) -> bool:
        return bool(self.api_key) and os.environ.get("SPRITEWARDEN_OFFLINE") != "1"

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

                    import math
                    def number(value, maximum):
                        return (isinstance(value,(int,float)) and not isinstance(value,bool)
                                and math.isfinite(value) and 0 <= value <= maximum)
                    allowed = q_obj.get('criteria', {})
                    valid = (isinstance(val,str) and val in allowed) if q_type == 'choice' else number(val,1 if q_type=='noul' else 4)
                    confidence = ans.get('confidence')
                    probabilities = ans.get('probabilities', {})
                    valid = (valid and (confidence is None or number(confidence,1))
                             and isinstance(probabilities,dict)
                             and all(number(p,1) for p in probabilities.values()))
                    if not valid:
                        return DecisionOutcome(question_id=question_id,cost_usd=cost,error='missing or invalid typed decision answer')
                    return DecisionOutcome(
                        question_id=question_id,
                        decision=val,
                        probabilities=ans.get("probabilities", {}) if val is not None else {},
                        confidence=ans.get("confidence") if val is not None else None,
                        error='missing decision answer' if val is None else None,
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
        """Unknown is not a model decision. Routing rules remain separate in route_generation."""
        return DecisionOutcome(question_id=question_id, used_fallback=True,
                               error='decision service unavailable; no approval or model confidence')

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
        if not outcome.used_fallback and not outcome.error and outcome.decision in criteria:
            recipe = recompile_recipe(recipe, str(outcome.decision))
        return recipe
