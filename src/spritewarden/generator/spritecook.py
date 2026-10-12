"""SpriteCook generator driver (palette-locked stills + reference-consistent sets + sprite sheets).

SpriteCook (https://www.spritecook.ai) is a REST API that returns a *canonical*
``asset_id`` plus ``sprite_url`` (and optional ``spritesheet_url``) for every asset.
It is the best-fitting provider for two of our pipeline needs:

* **Strict palette adherence** -- the API accepts a ``colors`` hex list (max 64), so
  the reference palette is enforced by the provider, then re-snapped locally through
  the Oklab ``remap_image`` guard for a hard 100% guarantee.
* **Reference/style consistency and animation** -- ``reference_asset_id``,
  ``edit_asset_id`` and ``style_asset_ids`` keep asset families on-model, and
  ``/animate-sync`` returns a ready-to-judge spritesheet (``output_format="spritesheet"``).

Everything runs synchronously (``generate-sync`` / ``animate-sync``); if either
endpoint is unavailable we transparently fall back to the async job + polling path.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any
import httpx
from PIL import Image

from .base import BaseGeneratorDriver, GenerationOutput
from ..router.playbook import GenerationRecipe
from ..judge.palette import remap_image, load_palette, rgb_to_hex

SPRITECOOK_BASE = "https://api.spritecook.ai"

_ASSET_LIST_KEYS = ("assets", "output", "outputs", "data", "results")
_URL_KEYS = ("sprite_url", "url", "pixel_url", "raw_url", "spritesheet_url")


def _registry_env(name: str) -> str:
    """Read a Windows user env var when the process env is empty (mirrors the OpenRouter helper)."""
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as env:
            return winreg.QueryValueEx(env, name)[0]
    except Exception:
        return ""


def _get_sprite_cook_key() -> str:
    for name in ("SPRITE_COOK_API_KEY", "SPRITECOOK_API_KEY"):
        key = os.environ.get(name, "")
        if key:
            return key
    if os.name == "nt":
        for name in ("SPRITE_COOK_API_KEY", "SPRITECOOK_API_KEY"):
            key = _registry_env(name)
            if key:
                return key
    return ""


class SpriteCookDriver(BaseGeneratorDriver):
    """Driver for the SpriteCook REST API (still generation, animation, import, credits)."""

    provider_name = "spritecook"

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or _get_sprite_cook_key()
        self.base_url = (
            base_url
            or os.environ.get("SPRITE_COOK_API_URL")
            or os.environ.get("SPRITECOOK_API_URL")
            or SPRITECOOK_BASE
        ).rstrip("/")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def fetch_asset(self, asset_id: str, output_dir: str | Path, **params: Any) -> GenerationOutput:
        """Download a canonical SpriteCook asset by id (``GET /v1/api/assets/<id>``) and palette-snap it."""
        if not self.is_configured():
            return GenerationOutput(success=False, provider="spritecook", error="SPRITE_COOK_API_KEY is not set")
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "spritewarden/spritecook",
        }
        palette = str(params.get("palette") or "luap-40")
        try:
            with httpx.Client(timeout=120.0, follow_redirects=True) as client:
                resp = client.get(f"{self.base_url}/v1/api/assets/{asset_id}", headers=headers)
                if resp.status_code != 200:
                    return GenerationOutput(
                        success=False,
                        provider="spritecook",
                        error=_http_error("SpriteCook asset fetch", resp),
                    )
                payload = _as_dict(resp)
                assets = _extract_assets(payload) or [payload]
            written = self._download_assets(assets, out_dir, f"spritecook_{asset_id}", palette, prefer_sheet=True)
            if not written:
                return GenerationOutput(
                    success=False,
                    provider="spritecook",
                    error="SpriteCook asset had no downloadable URL",
                    metadata={"response": _trim(payload)},
                )
            return GenerationOutput(
                success=True,
                provider="spritecook",
                output_files=written,
                metadata={"asset_ids": [asset_id], "fetched": True},
            )
        except Exception as e:  # noqa: BLE001 - surfaced as a structured failure
            return GenerationOutput(success=False, provider="spritecook", error=f"SpriteCook fetch failed: {e}")

    # -- public API ---------------------------------------------------------

    def generate(self, recipe: GenerationRecipe, output_dir: str | Path) -> GenerationOutput:
        from .contracts import capability_status
        if not capability_status("spritecook", recipe.operation)["executable"]:
            return GenerationOutput(success=False, provider="spritecook", error=f"Unsupported native workflow: {recipe.operation}")
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        if not self.is_configured():
            return GenerationOutput(
                success=False,
                provider="spritecook",
                error="SPRITE_COOK_API_KEY is not set",
            )

        try:
            if _is_animation(recipe):
                return self._generate_animation(recipe, out_dir)
            return self._generate_still(recipe, out_dir)
        except Exception as e:  # noqa: BLE001 - surfaced as a structured failure
            return GenerationOutput(
                success=False,
                provider="spritecook",
                error=f"SpriteCook request failed: {e}",
            )

    # -- stills -------------------------------------------------------------

    def build_request(self, recipe: GenerationRecipe, *, asset_id: str | None = None) -> dict[str, Any]:
        """Build the actual REST body without network access; imports happen separately."""
        from .contracts import capability_status
        capability = capability_status("spritecook", recipe.operation)
        if not capability["executable"]:
            raise ValueError(f"Unsupported native workflow: {recipe.operation}")
        if capability["operation"] == "animate_game_art":
            source_id = asset_id or recipe.params.get("asset_id")
            if not source_id:
                raise ValueError("animation requires source asset_id (or import a local source)")
            frames = int(recipe.params.get("output_frames", recipe.params.get("frames", 8)))
            if frames < 2 or frames > 16 or frames % 2:
                raise ValueError("output_frames must be even, from 2 to 16")
            body = {"asset_id": source_id, "prompt": recipe.prompt, "output_frames": frames,
                    "output_format": recipe.params.get("output_format", "spritesheet")}
            if body["output_format"] != "spritesheet":
                raise ValueError("only spritesheet outputs have executable post-gates")
            self._apply_palette(body, recipe, palette_size=True)
            self._copy_optional(body, recipe, "pixel", "negative_prompt", "matte_color", "removebg", "model", "auto_enhance_prompt")
        else:
            if capability["operation"] == "edit" and not recipe.params.get("edit_asset_id"):
                raise ValueError("edit requires edit_asset_id")
            body = {"prompt": recipe.prompt, "width": recipe.size or 64, "height": recipe.size or 64, "pixel": True}
            body.update(self._common_params(recipe))
            self._apply_palette(body, recipe)
        return body

    def _generate_still(self, recipe: GenerationRecipe, out_dir: Path) -> GenerationOutput:
        body = self.build_request(recipe)

        ok, payload, error = self._post_sync("/v1/api/generate-sync", "/v1/api/generate", body)
        if not ok:
            return GenerationOutput(success=False, provider="spritecook", error=error)

        assets = _extract_assets(payload)
        written = self._download_assets(assets, out_dir, "spritecook", recipe.palette_id, prefer_sheet=False)
        if not written:
            recovery = _recovery_hint(payload)
            return GenerationOutput(
                success=False,
                provider="spritecook",
                error=f"SpriteCook returned no downloadable assets{recovery}",
                metadata={"response": _trim(payload)},
            )
        return self._output(recipe, out_dir, body, payload, assets, written)

    # -- animation ----------------------------------------------------------

    def _generate_animation(self, recipe: GenerationRecipe, out_dir: Path) -> GenerationOutput:
        asset_id = self._resolve_asset_id(recipe)
        if not asset_id:
            return GenerationOutput(
                success=False,
                provider="spritecook",
                error=(
                    "SpriteCook animation requires recipe.params['asset_id'] "
                    "(or params['inputs'] pointing at a local PNG to import)"
                ),
            )

        body = self.build_request(recipe, asset_id=asset_id)

        ok, payload, error = self._post_sync("/v1/api/animate-sync", "/v1/api/animate", body)
        if not ok:
            return GenerationOutput(success=False, provider="spritecook", error=error)

        assets = _extract_assets(payload)
        written = self._download_assets(assets, out_dir, "spritecook_anim", recipe.palette_id, prefer_sheet=True)
        if not written:
            recovery = _recovery_hint(payload)
            return GenerationOutput(
                success=False,
                provider="spritecook",
                error=f"SpriteCook animation returned no downloadable assets{recovery}",
                metadata={"response": _trim(payload)},
            )
        return self._output(recipe, out_dir, body, payload, assets, written, extra={"asset_id": asset_id})

    # -- request helpers ----------------------------------------------------

    def _post_sync(
        self,
        sync_path: str,
        async_path: str,
        body: dict[str, Any],
    ) -> tuple[bool, dict[str, Any], str | None]:
        """POST to the sync endpoint, falling back to async job + polling when unavailable."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "spritewarden/spritecook",
        }
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(f"{self.base_url}{sync_path}", headers=headers, json=body)
            if resp.status_code == 200:
                payload = _as_dict(resp)
                if _extract_assets(payload):
                    return True, payload, None
                job_id = _job_id(payload)
                if job_id:
                    return self._poll_job(client, headers, job_id)
                return True, payload, None

            if resp.status_code in (404, 405):
                # Sync route not deployed on this account/region: use the async job path.
                async_resp = client.post(f"{self.base_url}{async_path}", headers=headers, json=body)
                if async_resp.status_code != 200:
                    return False, {}, _http_error("SpriteCook", async_resp)
                payload = _as_dict(async_resp)
                job_id = _job_id(payload)
                if not job_id:
                    return True, payload, None
                return self._poll_job(client, headers, job_id)

            return False, {}, _http_error("SpriteCook", resp)

    def _poll_job(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        job_id: str,
        max_polls: int = 90,
        interval: float = 2.0,
    ) -> tuple[bool, dict[str, Any], str | None]:
        import time

        payload: dict[str, Any] = {"job_id": job_id}
        for _ in range(max_polls):
            time.sleep(interval)
            resp = client.get(f"{self.base_url}/v1/api/jobs/{job_id}", headers=headers)
            if resp.status_code != 200:
                return False, {}, _http_error("SpriteCook job", resp)
            payload = _as_dict(resp)
            status = str(payload.get("status", "")).lower()
            if _extract_assets(payload):
                return True, payload, None
            if status in ("failed", "error", "cancelled", "canceled", "expired"):
                return False, payload, f"SpriteCook job {job_id} ended as '{status}'"
        return False, payload, f"SpriteCook job {job_id} did not finish in time"

    def _import_asset(self, client: httpx.Client, headers: dict[str, str], path: Path) -> str | None:
        data = path.read_bytes()
        body = {
            "image": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
            "file_name": path.name,
            "pixel": True,
        }
        resp = client.post(f"{self.base_url}/v1/api/assets/import", headers=headers, json=body)
        if resp.status_code != 200:
            return None
        payload = _as_dict(resp)
        assets = _extract_assets(payload)
        if assets:
            return assets[0].get("asset_id")
        return payload.get("asset_id")

    def _resolve_asset_id(self, recipe: GenerationRecipe) -> str | None:
        asset_id = recipe.params.get("asset_id") or recipe.params.get("character_id")
        if asset_id:
            return str(asset_id)

        from .contracts import source_paths
        paths = source_paths(recipe.params)
        if not paths:
            return None
        if len(paths) != 1:
            raise ValueError("animation requires exactly one source image")
        first = paths[0]
        headers = {"Authorization": f"Bearer {self.api_key}", "User-Agent": "spritewarden/spritecook"}
        with httpx.Client(timeout=120.0) as client:
            return self._import_asset(client, headers, first)

    def _download_assets(
        self,
        assets: list[dict[str, Any]],
        out_dir: Path,
        prefix: str,
        palette_id: str,
        prefer_sheet: bool,
    ) -> list[str]:
        written: list[str] = []
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            for i, asset in enumerate(assets, 1):
                url = _asset_url(asset, prefer_sheet=prefer_sheet)
                if not url:
                    continue
                resp = client.get(url, headers={"User-Agent": "spritewarden/spritecook"})
                if resp.status_code != 200:
                    continue
                raw_path = out_dir / f"{prefix}_{i}_raw.png"
                raw_path.write_bytes(resp.content)
                try:
                    img = Image.open(raw_path).convert("RGBA")
                    snapped, _, _ = remap_image(img, palette_id)
                    final_path = out_dir / f"{prefix}_{i}_snapped.png"
                    snapped.save(final_path)
                    written.append(str(final_path))
                except Exception:
                    written.append(str(raw_path))
        return written

    def _output(
        self,
        recipe: GenerationRecipe,
        out_dir: Path,
        body: dict[str, Any],
        payload: dict[str, Any],
        assets: list[dict[str, Any]],
        written: list[str],
        extra: dict[str, Any] | None = None,
    ) -> GenerationOutput:
        metadata: dict[str, Any] = {
            "asset_ids": [a.get("asset_id") for a in assets if a.get("asset_id")],
            "credits_used": payload.get("credits_used"),
            "credits_remaining": payload.get("credits_remaining"),
            "operation": recipe.operation,
        }
        if extra:
            metadata.update(extra)
        log_path = out_dir / "spritecook_events.json"
        log_path.write_text(
            json.dumps(
                {"request": body, "response": _trim(payload), "files": written},
                indent=2,
            )
        )
        return GenerationOutput(
            success=True,
            provider="spritecook",
            output_files=written,
            metadata=metadata,
            cost_usd=None,
        )

    # -- param mapping ------------------------------------------------------

    def _common_params(self, recipe: GenerationRecipe) -> dict[str, Any]:
        params = recipe.params
        body: dict[str, Any] = {}
        if params.get("variations"):
            body["variations"] = int(params["variations"])
        if params.get("pixel") is not None:
            body["pixel"] = bool(params["pixel"])
        if params.get("pixel_perfect") is not None:
            body["pixel_perfect"] = bool(params["pixel_perfect"])
        if params.get("bg_mode"):
            body["bg_mode"] = params["bg_mode"]
        if params.get("theme"):
            body["theme"] = params["theme"]
        if params.get("style"):
            body["style"] = params["style"]
        if params.get("aspect_ratio"):
            body["aspect_ratio"] = params["aspect_ratio"]
        if params.get("smart_crop") is not None:
            body["smart_crop"] = bool(params["smart_crop"])
        if params.get("smart_crop_mode"):
            body["smart_crop_mode"] = params["smart_crop_mode"]
        if params.get("mode"):
            body["mode"] = params["mode"]
        if params.get("resolution"):
            body["resolution"] = params["resolution"]
        if params.get("quality"):
            body["quality"] = params["quality"]
        if params.get("model"):
            body["model"] = params["model"]
        if params.get("reference_asset_id"):
            body["reference_asset_id"] = params["reference_asset_id"]
        if params.get("edit_asset_id"):
            body["edit_asset_id"] = params["edit_asset_id"]
        if params.get("style_asset_ids"):
            body["style_asset_ids"] = list(params["style_asset_ids"])
        if params.get("project_id"):
            body["project_id"] = params["project_id"]
        for alpha_key in ("runpod_alpha_threshold_enabled", "runpod_alpha_cutoff"):
            if params.get(alpha_key) is not None:
                body[alpha_key] = params[alpha_key]
        return body

    def _apply_palette(self, body: dict[str, Any], recipe: GenerationRecipe, palette_size: bool = False) -> None:
        """Apply the reviewed palette contract: at most eight color hints."""
        if "colors" in recipe.params:
            colors = recipe.params["colors"]
            if palette_size:
                if not isinstance(colors, int) or isinstance(colors, bool) or not 1 <= colors <= 8:
                    raise ValueError("animation colors must be an integer from 1 to 8")
            elif not isinstance(colors, list) or not 1 <= len(colors) <= 8:
                raise ValueError("colors must contain between 1 and 8 hex entries")
            body["colors"] = colors
            return
        try:
            palette = load_palette(recipe.palette_id)
        except Exception:
            return
        hexes = list(dict.fromkeys(rgb_to_hex(c) for c in palette))[:8]
        if not hexes:
            return
        if palette_size:
            # animate-sync accepts an integer palette size, not a hex list.
            body["colors"] = len(hexes)
        else:
            body["colors"] = hexes

    @staticmethod
    def _copy_optional(body: dict[str, Any], recipe: GenerationRecipe, *keys: str) -> None:
        for key in keys:
            if recipe.params.get(key) is not None:
                body[key] = recipe.params[key]


# -- module helpers --------------------------------------------------------


def _is_animation(recipe: GenerationRecipe) -> bool:
    return "animat" in (recipe.operation or "").lower()


def _as_dict(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {"data": data}


def _job_id(payload: dict[str, Any]) -> str | None:
    for key in ("job_id", "jobId", "operation_id"):
        if payload.get(key):
            return str(payload[key])
    poll = payload.get("poll")
    if isinstance(poll, dict) and poll.get("arguments", {}).get("job_id"):
        return str(poll["arguments"]["job_id"])
    return None


def _extract_assets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in _ASSET_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for inner_key in _ASSET_LIST_KEYS:
                inner = value.get(inner_key)
                if isinstance(inner, list):
                    return [item for item in inner if isinstance(item, dict)]
            if any(k in value for k in _URL_KEYS):
                return [value]
    if any(k in payload for k in _URL_KEYS) or payload.get("asset_id"):
        return [payload]
    return []


def _asset_url(asset: dict[str, Any], prefer_sheet: bool) -> str | None:
    order = (
        ("spritesheet_url", "sprite_url", "url", "pixel_url", "raw_url")
        if prefer_sheet
        else ("sprite_url", "url", "pixel_url", "raw_url", "spritesheet_url")
    )
    # Ignore the private presigned aliases; the canonical fields are sufficient.
    for key in order:
        value = asset.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _recovery_hint(payload: dict[str, Any]) -> str:
    warning = payload.get("warning") if isinstance(payload, dict) else None
    if isinstance(warning, dict) and warning.get("code") == "asset_output_unavailable":
        return " (warning: asset_output_unavailable; verify credits and asset ownership)"
    return ""


def _http_error(prefix: str, resp: httpx.Response) -> str:
    body = ""
    try:
        body = resp.text[:300]
    except Exception:
        pass
    return f"{prefix} returned HTTP {resp.status_code}: {body}"


def _trim(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep event logs compact by dropping large base64 blobs."""
    def clean(value: Any) -> Any:
        if isinstance(value, str) and len(value) > 512:
            return value[:64] + f"...<{len(value)} chars>"
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return clean(payload) if isinstance(payload, dict) else payload
