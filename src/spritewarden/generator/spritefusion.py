"""Sprite Fusion generator driver (direction-set, generate, animate, style-reference).

Also implements the account asset library: ``GET /api/v1/assets`` (cursor-paginated)
and ``GET /api/v1/assets/{assetId}``, so ``warden fetch sprite_fusion --all`` pulls
everything you have made (editor, API, integrations) down and into the gallery.
"""

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Callable
import httpx
from PIL import Image

from .base import BaseGeneratorDriver, GenerationOutput
from ..router.playbook import GenerationRecipe
from ..judge.palette import remap_image

BASE_URL = "https://www.spritefusion.com/api/v1"
GAME_ORDER = ["S", "SE", "E", "NE", "N", "NW", "W", "SW"]
PAGE_LIMIT_MAX = 100


class SpriteFusionDriver(BaseGeneratorDriver):
    """Driver for Sprite Fusion pixel art generator API."""

    provider_name = "sprite_fusion"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("SPRITE_FUSION_API_KEY", "")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "spritewarden/sprite_fusion",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    # -- asset library (fetch by id / list all) -----------------------------

    def list_assets(self, *, limit: int = PAGE_LIMIT_MAX, cursor: str | None = None) -> dict[str, Any]:
        """One page of ``GET /assets``: ``{"assets": [...], "hasMore": bool, "nextCursor": ...}``."""
        if not self.is_configured():
            raise RuntimeError("SPRITE_FUSION_API_KEY is not set")
        params: dict[str, Any] = {"limit": max(1, min(PAGE_LIMIT_MAX, int(limit)))}
        if cursor:
            params["cursor"] = cursor
        with httpx.Client(timeout=60.0) as client:
            resp = client.get(f"{BASE_URL}/assets", headers=self._headers(), params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"Sprite Fusion list assets returned HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        return payload if isinstance(payload, dict) else {"assets": payload}

    def fetch_asset(self, asset_id: str, output_dir: str | Path, **params: Any) -> GenerationOutput:
        """Download one owned asset by id (``GET /assets/{assetId}``)."""
        if not self.is_configured():
            return GenerationOutput(success=False, provider="sprite_fusion", error="SPRITE_FUSION_API_KEY is not set")
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                resp = client.get(f"{BASE_URL}/assets/{asset_id}", headers=self._headers())
                if resp.status_code != 200:
                    return GenerationOutput(
                        success=False, provider="sprite_fusion",
                        error=f"Sprite Fusion asset fetch returned HTTP {resp.status_code}: {resp.text[:300]}",
                    )
                payload = resp.json()
            asset = payload.get("asset") if isinstance(payload, dict) else None
            asset = asset if isinstance(asset, dict) else (payload if isinstance(payload, dict) else {})
            written, records = self._download_many([asset], out_dir, params.get("palette"))
            if not written:
                return GenerationOutput(success=False, provider="sprite_fusion",
                                        error="Sprite Fusion asset had no downloadable URL", metadata={"asset": asset})
            return GenerationOutput(success=True, provider="sprite_fusion", output_files=written,
                                    metadata={"assets": records, "asset_ids": [asset_id], "fetched": True})
        except Exception as e:  # noqa: BLE001 - surfaced as a structured failure
            return GenerationOutput(success=False, provider="sprite_fusion", error=f"Sprite Fusion fetch failed: {e}")

    def fetch_all(
        self,
        output_dir: str | Path,
        *,
        palette: str | None = None,
        max_items: int | None = None,
        page_limit: int = PAGE_LIMIT_MAX,
        on_page: Callable[[int, int], None] | None = None,
    ) -> GenerationOutput:
        """Walk every page of ``GET /assets`` and download each asset into ``output_dir``.

        Preserves the provider's own bytes unless ``palette`` is given (then Oklab-snaps).
        Returns one ``metadata["assets"]`` record per downloaded file, aligned with
        ``output_files``, carrying the provider id, prompt, dimensions and urls.
        """
        if not self.is_configured():
            return GenerationOutput(success=False, provider="sprite_fusion", error="SPRITE_FUSION_API_KEY is not set")
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        assets: list[dict[str, Any]] = []
        try:
            cursor: str | None = None
            for _ in range(1000):  # page safety cap
                payload = self.list_assets(limit=page_limit, cursor=cursor)
                batch = _asset_list(payload)
                assets.extend(batch)
                if on_page:
                    on_page(len(batch), len(assets))
                if max_items and len(assets) >= max_items:
                    assets = assets[:max_items]
                    break
                cursor = payload.get("nextCursor")
                if not cursor or payload.get("hasMore") is False:
                    break
        except Exception as e:  # noqa: BLE001
            return GenerationOutput(success=False, provider="sprite_fusion", error=f"Sprite Fusion listing failed: {e}")

        try:
            written, records = self._download_many(assets, out_dir, palette)
        except Exception as e:  # noqa: BLE001
            return GenerationOutput(success=False, provider="sprite_fusion", error=f"Sprite Fusion download failed: {e}")
        return GenerationOutput(
            success=bool(written),
            provider="sprite_fusion",
            output_files=written,
            metadata={"assets": records, "count": len(written), "listed": len(assets)},
            error=None if written else "Sprite Fusion returned no downloadable assets",
        )

    # -- generation ---------------------------------------------------------

    def build_request(self, recipe: GenerationRecipe) -> dict[str, Any]:
        """Serialize only the selected operation's contract, without making a request."""
        from .contracts import source_paths
        from ..router.recipes import registry

        entry = registry().get("spritefusion", recipe.operation)
        if entry is None:
            raise ValueError(f"Unsupported Sprite Fusion operation: {recipe.operation}")
        operation = entry.operation
        body: dict[str, Any] = {"operation": operation}
        if operation in ("edit", "animate") and "size" in recipe.params:
            raise ValueError(f"{operation} does not accept size")
        if operation == "direction-set":
            if recipe.size not in (16, 32, 64):
                raise ValueError("direction-set size must be 16, 32 or 64")
            if recipe.prompt:
                raise ValueError("direction-set does not accept a prompt")
        if operation not in ("edit", "animate"):
            if not 16 <= recipe.size <= 128:
                raise ValueError("size must be between 16 and 128")
            body["size"] = recipe.size
        if recipe.prompt:
            body["prompt"] = recipe.prompt
        paths = source_paths(recipe.params)
        if operation in ("direction-set", "animate", "edit", "style-reference") and not paths:
            raise ValueError(f"{operation} requires a source image")
        if operation in ("direction-set", "animate") and len(paths) != 1:
            raise ValueError(f"{operation} requires exactly one source image")
        if operation == "style-reference" and not 4 <= len(paths) <= 8:
            raise ValueError("style-reference requires 4 to 8 source images")
        if operation == "edit" and len(paths) > 10:
            raise ValueError("edit accepts one base plus at most 9 supporting images")
        for path in paths:
            try:
                with Image.open(path) as image:
                    image.verify()
                    dimensions = image.size
                    if image.format != "PNG":
                        raise ValueError("source must be a PNG image")
            except Exception as exc:
                raise ValueError(f"invalid source image {path}: {exc}") from exc
            if operation == "direction-set" and (dimensions[0] != dimensions[1] or max(dimensions) > 64):
                raise ValueError("direction-set source must be square and at most 64 pixels; no silent shrinking")
        if paths:
            body["inputs"] = [{"data_url": self._to_data_url(path)} for path in paths]
        if operation == "animate":
            frames = int(recipe.params.get("output_frames", recipe.params.get("frames", 8)))
            if frames < 2 or frames > 16 or frames % 2:
                raise ValueError("output_frames must be even, from 2 to 16")
            body["output_frames"] = frames
        return body

    def generate(self, recipe: GenerationRecipe, output_dir: str | Path) -> GenerationOutput:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        if not self.is_configured():
            return GenerationOutput(
                success=False,
                provider="sprite_fusion",
                error="SPRITE_FUSION_API_KEY is not set",
            )

        try:
            body = self.build_request(recipe)
        except ValueError as exc:
            return GenerationOutput(success=False, provider="sprite_fusion", error=f"Refused: {exc}")

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

    def _download_many(
        self,
        assets: list[dict[str, Any]],
        out_dir: Path,
        palette: str | None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Download each asset to a named PNG; optionally Oklab-snap to ``palette``."""
        written: list[str] = []
        records: list[dict[str, Any]] = []
        used: set[str] = set()
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            for index, asset in enumerate(assets, 1):
                url = _asset_download_url(asset)
                if not url:
                    continue
                try:
                    resp = client.get(url, headers={"User-Agent": "spritewarden/sprite_fusion"})
                except Exception:  # noqa: BLE001 - one bad asset must not stop the import
                    continue
                if resp.status_code != 200:
                    continue
                asset_id = str(asset.get("id") or f"asset_{index}")
                name = _safe_name(asset.get("prompt"), asset_id)
                is_anim = _is_animation_asset(asset)
                path = _unique_path(out_dir / f"{name}{'_sheet' if is_anim else ''}.png", used)
                path.write_bytes(resp.content)
                raw_path = path
                if palette:
                    try:
                        img = Image.open(path).convert("RGBA")
                        snapped, _, _ = remap_image(img, palette)
                        path = _unique_path(path.with_stem(path.stem + "_snapped"), used)
                        snapped.save(path)
                    except Exception:  # noqa: BLE001 - keep the raw bytes if the snap fails
                        pass
                written.append(str(path))
                records.append({
                    "id": asset_id,
                    "raw_file": str(raw_path),
                    "type": asset.get("type"),
                    "name": path.stem,
                    "prompt": asset.get("prompt"),
                    "dimensions": _dimensions(asset),
                    "source_url": url,
                    "spritesheet_url": asset.get("spritesheetUrl"),
                    "frame_count": asset.get("frameCount"),
                    "fps": asset.get("fps"),
                })
        return written, records


# -- module helpers --------------------------------------------------------


def _asset_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The asset list out of ``GET /assets`` (tolerant of a couple of envelope shapes)."""
    value = payload.get("assets") if isinstance(payload, dict) else None
    if isinstance(value, list):
        return [a for a in value if isinstance(a, dict)]
    for key in ("data", "results", "outputs"):
        nested = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(nested, list):
            return [a for a in nested if isinstance(a, dict)]
    return []


def _is_animation_asset(asset: dict[str, Any]) -> bool:
    return "anim" in str(asset.get("type") or "").lower() or bool(asset.get("spritesheetUrl"))


def _asset_download_url(asset: dict[str, Any]) -> str | None:
    """Prefer an animation's spritesheet, else the still's image url."""
    keys = ("spritesheetUrl", "assetUrl", "url", "imageUrl", "pixelUrl") if _is_animation_asset(asset) else \
        ("assetUrl", "url", "imageUrl", "pixelUrl", "spritesheetUrl")
    for key in keys:
        value = asset.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _dimensions(asset: dict[str, Any]) -> Any:
    dims = asset.get("dimensions")
    if dims:
        return dims
    width, height = asset.get("width"), asset.get("height")
    if width and height:
        return [width, height]
    return None


def _safe_name(prompt: Any, asset_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(prompt or "").lower()).strip("-")[:40]
    safe_id = re.sub(r"[^A-Za-z0-9]+", "-", asset_id).strip("-")
    return f"sf_{slug}" if slug else f"sf_{safe_id or 'asset'}"


def _unique_path(path: Path, used: set[str]) -> Path:
    key = path.name.lower()
    if key not in used:
        used.add(key)
        return path
    stem, suffix, n = path.stem, path.suffix, 2
    while f"{stem}_{n}{suffix}".lower() in used:
        n += 1
    out = path.with_name(f"{stem}_{n}{suffix}")
    used.add(out.name.lower())
    return out
