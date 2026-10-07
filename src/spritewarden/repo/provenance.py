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
    """Register every file of a successful ``GenerationOutput``; returns the catalog asset ids."""
    if not getattr(output, "success", False):
        return []
    meta = dict(getattr(output, "metadata", None) or {})
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
