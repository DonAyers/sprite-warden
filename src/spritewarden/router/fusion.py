"""OpenRouter Fusion Deliberation Synthesizer: multi-model recipe formulation for novel assets."""

import json
import os
from typing import Any
import httpx
from pydantic import BaseModel, Field

from .jev import _get_openrouter_key
from .playbook import GenerationRecipe

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"


class DeliberationResult(BaseModel):
    """The outcome of an OpenRouter Fusion deliberation run."""
    recipe: GenerationRecipe
    consensus_points: list[str] = Field(default_factory=list)
    pitfalls_flagged: list[str] = Field(default_factory=list)
    model_used: str = "openrouter/fusion"
    cost_usd: float | None = None
    deliberated: bool = True


class FusionSynthesizer:
    """Invokes OpenRouter Fusion router to formulate and verify novel generation pipelines."""

    def __init__(self, api_key: str | None = None, timeout: float = 45.0):
        self.api_key = api_key or _get_openrouter_key()
        self.timeout = timeout

    def is_available(self) -> bool:
        return bool(self.api_key) and os.environ.get("SPRITEWARDEN_OFFLINE") != "1"

    def deliberate_pipeline(
        self,
        task_description: str,
        game_context: str = "pixel art game",
        palette_id: str = "luap-40",
        perspective: str = "low top-down",
    ) -> DeliberationResult:
        """Run multi-model deliberation via OpenRouter Fusion to synthesize an optimal pipeline recipe."""
        if not self.is_available():
            # Return baseline fallback recipe when offline
            fallback = GenerationRecipe(
                provider="pixellab",
                operation="create_character_v3",
                prompt=task_description,
                size=64,
                canvas_size=64,
                perspective=perspective,
                palette_id=palette_id,
                rules=["Offline fallback: ensure square canvas and true south start pose"],
                suggested_gates=["clipping", "pixel_grid", "palette_adherence"],
            )
            return DeliberationResult(
                recipe=fallback,
                consensus_points=["Use square canvas with margin", "Palette snap with Oklab"],
                pitfalls_flagged=["Non-square reference causes slicing"],
                deliberated=False,
            )

        system_prompt = f"""You are an elite technical pixel art director.
A game project ({game_context}) requires a new asset pipeline.
You must deliberate and return a valid JSON object matching this schema:
{{
  "provider": "pixellab" | "sprite_fusion" | "retro_diffusion" | "openrouter_image" | "spritecook",
  "operation": string,
  "prompt": string (sanitized, no noise words like 'pixel-perfect' or 'transparent background'),
  "size": integer (16, 32, 64, or 96),
  "canvas_size": integer,
  "perspective": "{perspective}",
  "palette_id": "{palette_id}",
  "rules": [string],
  "suggested_gates": [string],
  "consensus": [string],
  "pitfalls": [string]
}}
Only output the raw JSON."""

        payload = {
            "model": "openrouter/fusion-flash",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Formulate generation pipeline for: {task_description}"},
            ],
            "response_format": {"type": "json_object"},
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    OPENROUTER_CHAT_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    parsed = json.loads(content)
                    cost = (data.get("usage") or {}).get("cost")
                    model_name = data.get("model", "openrouter/fusion")

                    recipe = GenerationRecipe(
                        provider=parsed.get("provider", "pixellab"),
                        operation=parsed.get("operation", "generate"),
                        prompt=parsed.get("prompt", task_description),
                        size=parsed.get("size", 64),
                        canvas_size=parsed.get("canvas_size", 64),
                        perspective=parsed.get("perspective", perspective),
                        palette_id=parsed.get("palette_id", palette_id),
                        rules=parsed.get("rules", []),
                        suggested_gates=parsed.get("suggested_gates", ["pixel_grid", "palette_adherence"]),
                    )

                    return DeliberationResult(
                        recipe=recipe,
                        consensus_points=parsed.get("consensus", []),
                        pitfalls_flagged=parsed.get("pitfalls", []),
                        model_used=model_name,
                        cost_usd=cost,
                        deliberated=True,
                    )
        except Exception:
            pass

        # Fallback on any connection/parsing error
        fallback = GenerationRecipe(
            provider="pixellab",
            operation="create_character_v3",
            prompt=task_description,
            size=64,
            canvas_size=64,
            perspective=perspective,
            palette_id=palette_id,
            rules=["Offline/error fallback: ensure square canvas and true south start pose"],
            suggested_gates=["clipping", "pixel_grid", "palette_adherence"],
        )
        return DeliberationResult(
            recipe=fallback,
            consensus_points=["Use square canvas with margin"],
            pitfalls_flagged=["Avoid non-square inputs"],
            deliberated=False,
        )
