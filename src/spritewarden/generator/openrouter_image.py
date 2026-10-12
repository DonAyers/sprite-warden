"""OpenRouter Image API driver (Gemini 3.1 Flash / Nano Banana 2, etc.)."""

import base64
import json
import os
from pathlib import Path
from typing import Any
import httpx
from PIL import Image

from .base import BaseGeneratorDriver, GenerationOutput
from ..router.playbook import GenerationRecipe
from ..router.jev import _get_openrouter_key
from ..judge.palette import remap_image, load_palette

OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"


class OpenRouterImageDriver(BaseGeneratorDriver):
    """Generates concept turnarounds and keyframes via OpenRouter's Image API."""

    def __init__(self, api_key: str | None = None, default_model: str = "google/gemini-3.1-flash-image"):
        self.api_key = api_key or _get_openrouter_key()
        self.default_model = default_model

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def generate(self, recipe: GenerationRecipe, output_dir: str | Path) -> GenerationOutput:
        from .contracts import capability_status
        if not capability_status("openrouter_image", recipe.operation)["executable"]:
            return GenerationOutput(success=False, provider="openrouter_image", error=f"Unsupported workflow: {recipe.operation}")
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        if not self.is_configured():
            return GenerationOutput(
                success=False,
                provider="openrouter_image",
                error="OPENROUTER_API_KEY is not set",
            )

        model = recipe.params.get("model", self.default_model)
        body: dict[str, Any] = {
            "model": model,
            "prompt": recipe.prompt,
            "n": recipe.params.get("n", 1),
        }
        if recipe.params.get("aspect_ratio"):
            body["aspect_ratio"] = recipe.params["aspect_ratio"]
        if recipe.params.get("size"):
            body["size"] = recipe.params["size"]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(OPENROUTER_IMAGES_URL, headers=headers, json=body)
                if resp.status_code != 200:
                    return GenerationOutput(
                        success=False,
                        provider="openrouter_image",
                        error=f"OpenRouter Image API returned HTTP {resp.status_code}: {resp.text[:300]}",
                    )

                data = resp.json()
                items = data.get("data", [])
                cost = (data.get("usage") or {}).get("cost")

                written_files = []
                for i, item in enumerate(items, 1):
                    b64_data = item.get("b64_json")
                    if not b64_data:
                        continue
                    raw_bytes = base64.b64decode(b64_data)
                    raw_path = out_dir / f"raw_gen_{i}.png"
                    raw_path.write_bytes(raw_bytes)

                    # Snap to target palette
                    img = Image.open(raw_path).convert("RGBA")
                    snapped, _, _ = remap_image(img, recipe.palette_id)
                    final_path = out_dir / f"gen_{i}_snapped.png"
                    snapped.save(final_path)
                    written_files.append(str(final_path))

                # Write event record
                event_log = out_dir / "generation_events.json"
                event_log.write_text(json.dumps({"request": body, "usage": data.get("usage"), "files": written_files}, indent=2))

                return GenerationOutput(
                    success=True,
                    provider="openrouter_image",
                    output_files=written_files,
                    cost_usd=cost,
                    metadata={"model": model, "usage": data.get("usage")},
                )
        except Exception as e:
            return GenerationOutput(
                success=False,
                provider="openrouter_image",
                error=f"Exception during OpenRouter image generation: {str(e)}",
            )
