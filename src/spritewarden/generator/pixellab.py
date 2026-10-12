"""PixelLab character bundle downloader and sprite strip compositor."""

import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any
import httpx
from PIL import Image

from .base import BaseGeneratorDriver, GenerationOutput
from ..router.playbook import GenerationRecipe
from ..judge.palette import remap_image, load_palette

PIXELLAB_BASE = "https://api.pixellab.ai/mcp"
GAME_ORDER = [
    ("down", "south"),
    ("down_right", "south-east"),
    ("right", "east"),
    ("up_right", "north-east"),
    ("up", "north"),
    ("up_left", "north-west"),
    ("left", "west"),
    ("down_left", "south-west"),
]


class PixelLabDriver(BaseGeneratorDriver):
    """Driver for fetching and laying out PixelLab character & object bundles."""

    provider_name = "pixellab"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("PIXELLAB_API_KEY", "")

    def is_configured(self) -> bool:
        # Bundles can be fetched without an API key using the bundle download endpoint
        return True

    def fetch_asset(self, asset_id: str, output_dir: str | Path, **params: Any) -> GenerationOutput:
        """Download a PixelLab character/object bundle by id and lay out its 8-direction strip."""
        cell = int(params.get("cell") or 64)
        recipe = GenerationRecipe(
            provider="pixellab",
            operation="download_bundle",
            prompt=f"fetch pixellab {asset_id}",
            size=cell,
            canvas_size=cell,
            perspective=str(params.get("perspective") or "low top-down"),
            palette_id=str(params.get("palette") or "luap-40"),
            params={
                "character_id": asset_id,
                "is_object": bool(params.get("is_object")),
                "ground_row_offset": int(params.get("ground_row_offset") or 7),
            },
        )
        return self._download_bundle(recipe, output_dir)

    def generate(self, recipe: GenerationRecipe, output_dir: str | Path) -> GenerationOutput:
        return GenerationOutput(success=False, provider="pixellab",
                                error="Unsupported execution: PixelLab is import-only; use fetch_asset for existing bundles")

    def _download_bundle(self, recipe: GenerationRecipe, output_dir: str | Path) -> GenerationOutput:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        char_id = recipe.params.get("character_id")
        if not char_id:
            return GenerationOutput(
                success=False,
                provider="pixellab",
                error="PixelLab character_id required in recipe.params",
            )

        is_object = recipe.params.get("is_object", False)
        kind = "objects" if is_object else "characters"
        url = f"{PIXELLAB_BASE}/{kind}/{char_id}/download"

        try:
            with httpx.Client(timeout=120.0) as client:
                headers = {"User-Agent": "spritewarden/pixellab"}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"
                resp = client.get(url, headers=headers)
                if resp.status_code != 200:
                    return GenerationOutput(
                        success=False,
                        provider="pixellab",
                        error=f"PixelLab download failed (HTTP {resp.status_code})",
                    )

                # Unzip bundle
                bundle_dir = out_dir / "bundle"
                bundle_dir.mkdir(exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                    z.extractall(bundle_dir)

                # Lay out 8-direction strip
                cell = recipe.size or 64
                ground_offset = recipe.params.get("ground_row_offset", 7)
                feet_row = cell - ground_offset

                # Collect rotation images
                rotations: dict[str, Image.Image] = {}
                for game_name, pl_name in GAME_ORDER:
                    rot_file = self._find_rotation_file(bundle_dir, pl_name)
                    if rot_file and rot_file.exists():
                        rotations[game_name] = Image.open(rot_file).convert("RGBA")

                if len(rotations) < 8:
                    return GenerationOutput(
                        success=False,
                        provider="pixellab",
                        error=f"Only found {len(rotations)}/8 rotations in bundle",
                    )

                # Align ground row across all directions
                lowest_y = max(img.getbbox()[3] for img in rotations.values() if img.getbbox())
                strip_frames = []
                for game_name, _ in GAME_ORDER:
                    img = rotations[game_name]
                    left = (img.width - cell) // 2
                    top = lowest_y - 1 - feet_row
                    cropped = img.crop((left, top, left + cell, top + cell))
                    snapped, _, _ = remap_image(cropped, recipe.palette_id)
                    strip_frames.append(snapped)

                # Compose single horizontal strip
                strip_img = Image.new("RGBA", (cell * len(strip_frames), cell))
                for i, frame in enumerate(strip_frames):
                    strip_img.paste(frame, (i * cell, 0))

                strip_path = out_dir / f"{char_id}_8dir_strip.png"
                strip_img.save(strip_path)

                return GenerationOutput(
                    success=True,
                    provider="pixellab",
                    output_files=[str(strip_path)],
                    metadata={"character_id": char_id, "directions": 8, "cell_size": cell},
                )
        except Exception as e:
            return GenerationOutput(
                success=False,
                provider="pixellab",
                error=f"PixelLab processing failed: {str(e)}",
            )

    def _find_rotation_file(self, folder: Path, direction: str) -> Path | None:
        for p in folder.rglob(f"{direction}.png"):
            if "rotations" in p.parts:
                return p
        return None
