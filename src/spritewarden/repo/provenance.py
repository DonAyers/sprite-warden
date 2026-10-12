"""Provenance: register what was generated or composed, and how, in the asset catalog.

Every generated file gets a catalog row whose ``provenance`` says which provider and
operation made it, from which provider asset ids, with which parameters. The asset id
is derived from the file's bytes, so re-registering the same output updates its row
instead of minting a duplicate.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import AssetCatalog

MAX_PARAM_CHARS = 400


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:limit] or "asset"


def _compact(value: Any) -> Any:
    """Params safe to store: long strings (base64, data URLs) are cut."""
    if isinstance(value, str) and len(value) > MAX_PARAM_CHARS:
        return value[:64] + f"...<{len(value)} chars>"
    if isinstance(value, dict):
        return {str(k): _compact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_compact(v) for v in value]
    return value


def _image_size(path: Path) -> list[int] | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return [im.width, im.height]
    except Exception:  # noqa: BLE001 - not an image we can read; the row is still worth keeping
        return None


def register_generation(
    catalog: AssetCatalog,
    recipe: Any,
    output: Any,
    *,
    game: str = "global",
    character_id: str | None = None,
    asset_type: str | None = None,
    category: str = "generated",
) -> list[str]:
    """Catalog completed output bytes, including failed diagnostics; never grant approval."""
    meta = dict(getattr(output, "metadata", None) or {})
    success = bool(getattr(output, "success", False))
    if not success and meta.get("execution") != "completed":
        return []
    provider_ids = [str(a) for a in (meta.get("asset_ids") or []) if a]
    is_anim = "animat" in str(recipe.operation).lower()
    registered: list[str] = []
    files = [Path(f) for f in getattr(output, "output_files", [])]
    for index, path in enumerate(files):
        if not path.is_file():
            continue
        digest = _sha256(path)
        asset_id = f"gen-{_slug(recipe.provider, 16)}-{digest[:12]}"
        provenance = {
            "kind": "generation",
            "provider": recipe.provider,
            "operation": recipe.operation,
            "prompt": recipe.prompt,
            "params": _compact(recipe.params),
            "provider_asset_ids": provider_ids,
            "provider_asset_id": provider_ids[index] if index < len(provider_ids) else (provider_ids[0] if len(provider_ids) == 1 else None),
            "source_asset_id": meta.get("asset_id"),
            "size": recipe.size,
            "sha256": digest,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "execution": meta.get("execution", "completed"),
            "validation": meta.get("validation", "pending"),
            "readiness": "blocked" if not success or meta.get("validation") == "failed" else "needs_review",
            # Registration is bookkeeping, not a human approval boundary.
            "human_decision": "pending",
            "run_id": meta.get("run_id"),
            "gate_records": meta.get("gate_records") or [],
            "error": getattr(output, "error", None),
        }
        for key in ("credits_used", "forced_ungated"):
            if meta.get(key) is not None:
                provenance[key] = meta[key]
        catalog.register_asset(
            asset_id=asset_id,
            name=path.stem,
            file_path=path,
            category=category,
            asset_type=asset_type or ("spritesheet" if is_anim else "sprite"),
            game=game,
            character_id=character_id,
            palette_id=recipe.palette_id,
            perspective=recipe.perspective,
            frame_size=_image_size(path),
            tags=[recipe.provider, recipe.operation, "generated", *_slug(recipe.prompt, 60).split("-")[:8]],
            provenance=provenance,
        )
        registered.append(asset_id)
    return registered


def register_sheet(
    catalog: AssetCatalog,
    report: Any,
    *,
    game: str,
    character_id: str | None = None,
    category: str = "characters",
) -> str:
    """Register a composed spritesheet (a ``SheetReport``); returns its catalog asset id."""
    sheet = Path(report.sheet_path)
    asset_id = f"sheet-{_slug(game, 24)}-{_slug(sheet.stem)}"
    catalog.register_asset(
        asset_id=asset_id,
        name=sheet.stem,
        file_path=sheet,
        category=category,
        asset_type="spritesheet",
        game=game,
        character_id=character_id,
        palette_id=report.palette_id,
        frame_size=[report.cell_size, report.cell_size],
        frame_count=report.frame_count,
        tags=["sheet", *sorted({f.name for f in report.frames})][:24],
        provenance={
            "kind": "sheet",
            "tool": "warden sheet",
            "engine": report.engine,
            "manifest": report.manifest_path,
            "columns": report.columns,
            "rows": report.rows,
            "sources": [f.source for f in report.frames],
            "sha256": _sha256(sheet) if sheet.is_file() else None,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    return asset_id


def register_imports(
    catalog: AssetCatalog,
    *,
    provider: str,
    output: Any,
    game: str = "global",
    character_id: str | None = None,
    category: str = "imported",
) -> list[str]:
    """Register files pulled from a provider by id (``warden fetch``) as catalog rows.

    A ``GenerationOutput`` whose ``metadata["assets"]`` holds one record per output file
    (Sprite Fusion's listing does) keeps the provider's own asset id, prompt, dimensions,
    source url, frame count and fps in ``provenance``; a fetch that only knows one id
    (a PixelLab bundle) falls back to ``metadata``'s asset/character id. Kind is ``import``,
    so the gallery can tell fetched work from what this pipeline generated.
    """
    if not getattr(output, "success", False):
        return []
    files = [Path(f) for f in (getattr(output, "output_files", None) or [])]
    meta = dict(getattr(output, "metadata", None) or {})
    records = meta.get("assets") if isinstance(meta.get("assets"), list) else []
    provider_ids = [str(a) for a in (meta.get("asset_ids") or []) if a]
    fallback_id = meta.get("asset_id") or meta.get("character_id") or (provider_ids[0] if provider_ids else None)
    registered: list[str] = []
    for index, path in enumerate(files):
        if not path.is_file():
            continue
        digest = _sha256(path)
        info = records[index] if index < len(records) and isinstance(records[index], dict) else {}
        is_anim = "anim" in str(info.get("type") or "").lower() or bool(info.get("spritesheet_url"))
        provider_asset_id = info.get("id") or (provider_ids[index] if index < len(provider_ids) else fallback_id)
        existing = catalog.find_by_provider_asset_id(provider, str(provider_asset_id or ""))
        if existing:
            asset_id = str(existing["id"])
        else:
            asset_id = f"import-{_slug(provider, 16)}-{digest[:12]}"
            same_bytes = catalog.find_by_sha(digest)
            if same_bytes and (same_bytes.get("provenance") or {}).get("provider_asset_id") != provider_asset_id:
                unique = _slug(str(provider_asset_id or f"item-{index + 1}"), 28)
                asset_id = f"{asset_id}-{unique}"
        provenance = {
            "kind": "import",
            "provider": provider,
            "provider_asset_id": provider_asset_id,
            "asset_type": info.get("type"),
            "prompt": info.get("prompt"),
            "dimensions": info.get("dimensions"),
            "source_url": info.get("source_url"),
            "spritesheet_url": info.get("spritesheet_url"),
            "frame_count": info.get("frame_count"),
            "fps": info.get("fps"),
            "sha256": digest,
            "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        catalog.register_asset(
            asset_id=asset_id,
            name=str(info.get("name") or path.stem),
            file_path=path,
            category=category,
            asset_type="spritesheet" if is_anim else "sprite",
            game=game,
            character_id=character_id,
            frame_size=_image_size(path),
            frame_count=int(info.get("frame_count") or 1),
            tags=[provider, "import", *_slug(str(info.get("prompt") or ""), 40).split("-")[:6]],
            provenance=provenance,
        )
        registered.append(asset_id)
    return registered
