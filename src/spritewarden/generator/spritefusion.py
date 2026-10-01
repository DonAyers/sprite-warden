"""Sprite Fusion generator driver (direction-set, generate, animate, style-reference)."""

import base64
import json
import os
from pathlib import Path
from typing import Any
import httpx
from PIL import Image

from .base import BaseGeneratorDriver, GenerationOutput
from ..router.playbook import GenerationRecipe
from ..judge.palette import remap_image

BASE_URL = "https://www.spritefusion.com/api/v1"
GAME_ORDER = ["S", "SE", "E", "NE", "N", "NW", "W", "SW"]


class SpriteFusionDriver(BaseGeneratorDriver):
    """Driver for Sprite Fusion pixel art generator API."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("SPRITE_FUSION_API_KEY", "")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def generate(self, recipe: GenerationRecipe, output_dir: str | Path) -> GenerationOutput:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        if not self.is_configured():
            return GenerationOutput(
                success=False,
                provider="sprite_fusion",
                error="SPRITE_FUSION_API_KEY is not set",
            )

        body: dict[str, Any] = {"operation": recipe.operation}
        if recipe.prompt:
            body["prompt"] = recipe.prompt
        if recipe.size:
            body["size"] = min(recipe.size, 64)  # 64 max in Sprite Fusion

        if "inputs" in recipe.params:
            body["inputs"] = [
                {"data_url": self._to_data_url(p)} for p in recipe.params["inputs"]
            ]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "spritewarden/sprite_fusion",
        }

        try:
            with httpx.Client(timeout=180.0) as client:
                with client.stream("POST", f"{BASE_URL}/generate", headers=headers, json=body) as response:
                    if response.status_code != 200:
                        err_body = response.read().decode(errors="replace")[:300]
                        return GenerationOutput(
                            success=False,
                            provider="sprite_fusion",
                            error=f"Sprite Fusion returned HTTP {response.status_code}: {err_body}",
                        )

                    outputs = []
                    completed = None
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        event = json.loads(line[5:].strip())
                        kind = event.get("type") or event.get("event") or event.get("status")
                        if kind == "output":
                            outputs.append(event)
                        elif kind == "completed":
                            completed = event

                written_files = []
                for i, event in enumerate(outputs, 1):
                    # Extract image URL
                    img_url = self._extract_url(event)
                    if not img_url:
                        continue
                    r = client.get(img_url)
                    raw_path = out_dir / f"sf_raw_{i}.png"
                    raw_path.write_bytes(r.content)

                    # Palette snap
                    img = Image.open(raw_path).convert("RGBA")
                    snapped, _, _ = remap_image(img, recipe.palette_id)
                    final_path = out_dir / f"sf_{i}_snapped.png"
                    snapped.save(final_path)
                    written_files.append(str(final_path))

                log_path = out_dir / "sf_events.json"
                log_path.write_text(json.dumps({"request": body, "outputs": outputs, "completed": completed}, indent=2))

                return GenerationOutput(
                    success=len(written_files) > 0,
                    provider="sprite_fusion",
                    output_files=written_files,
                    metadata={"completed": completed, "outputs_count": len(outputs)},
                )
        except Exception as e:
            return GenerationOutput(
                success=False,
                provider="sprite_fusion",
                error=f"Sprite Fusion request failed: {str(e)}",
            )

    def _to_data_url(self, path: str | Path) -> str:
        data = Path(path).read_bytes()
        return "data:image/png;base64," + base64.b64encode(data).decode("ascii")

    def _extract_url(self, event: dict[str, Any]) -> str | None:
        if "spritesheetUrl" in event:
            return event["spritesheetUrl"]
        if "assetUrl" in event:
            return event["assetUrl"]
        for v in event.values():
            if isinstance(v, str) and v.startswith("http") and ".png" in v.lower():
                return v
        return None
