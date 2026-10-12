"""``warden sync``: index the art tree into the catalog so the gallery sees everything.

Generated assets are registered by ``register_generation`` / ``register_sheet`` when
they are produced. Hand-drawn art, imported references and provider output that was
never run through a driver are not -- this walks the project (or explicit paths),
hashes every PNG, and upserts a catalog row (``local-<sha12>``) whose provenance
carries the gate record and the nearest ``pipeline.json``. Re-running is idempotent:
the asset id is derived from the file's bytes.

``asset_id`` collisions across files (same bytes in two places) intentionally resolve
to one row -- identical images are one asset.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..enforce.config import find_project_root, load_config
from ..gates.records import load_record, passed_gates
from ..repo.catalog import AssetCatalog

IMAGE_SUFFIXES = (".png",)
SKIP_DIRS = {".git", ".godot", "node_modules", "__pycache__", ".venv", "build", "dist", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
DEFAULT_MAX_BYTES = 32 * 1024 * 1024


@dataclass
class SyncReport:
    """What one ``sync_paths`` run did."""

    roots: list[str] = field(default_factory=list)
    scanned: int = 0
    registered: int = 0
    updated: int = 0
    skipped: int = 0
    already: int = 0
    with_record: int = 0
    rejected: int = 0
    files: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "roots": self.roots,
            "scanned": self.scanned,
            "registered": self.registered,
            "updated": self.updated,
            "skipped": self.skipped,
            "already": self.already,
            "with_record": self.with_record,
            "rejected": self.rejected,
            "files": self.files,
            "errors": self.errors,
        }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_images(root: Path, recursive: bool, max_bytes: int) -> Iterable[Path]:
    if root.is_file():
        if root.suffix.lower() in IMAGE_SUFFIXES:
            yield root
        return
    if not root.is_dir():
        return
    walker = root.rglob("*") if recursive else root.glob("*")
    for entry in sorted(walker):
        if not entry.is_file() or entry.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in entry.parts):
            continue
        try:
            if entry.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        yield entry


def _image_size(path: Path) -> list[int] | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return [im.width, im.height]
    except Exception:  # noqa: BLE001 - a non-image is still worth a row
        return None


def _guess_type(path: Path) -> str:
    stem = path.stem.lower()
    if any(word in stem for word in ("strip", "sheet", "walk", "anim", "_8dir")):
        return "spritesheet"
    return "sprite"


def _pipeline_context(image: Path, root: Path) -> dict[str, Any]:
    """The nearest pipeline.json above the image (bot, providers, anchor), if any."""
    try:
        from ..enforce.core import find_pipeline

        pipeline, where = find_pipeline(image)
        if pipeline and where is not None:
            return {
                "dir": str(where.parent),
                "bot": str(pipeline.get("bot") or where.parent.name),
                "rotation_provider": pipeline.get("rotation_provider"),
                "animation_provider": pipeline.get("animation_provider"),
            }
    except Exception:  # noqa: BLE001 - context is a nicety, never fail a sync
        pass
    return {}


def sync_paths(
    paths: Iterable[str | Path] | None = None,
    catalog: AssetCatalog | None = None,
    *,
    project_root: str | Path | None = None,
    recursive: bool = True,
    game: str = "global",
    character_id: str | None = None,
    category: str = "local",
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> SyncReport:
    """Index every PNG under ``paths`` (default: the project root) into the catalog."""
    root = Path(project_root) if project_root else find_project_root(Path.cwd())
    cfg = load_config(root)
    catalog = catalog or AssetCatalog()
    raw_paths = list(paths) if paths else [root]
    report = SyncReport(roots=[cfg.relative(Path(p) if Path(p).is_absolute() else root / p) for p in raw_paths])
    seen: set[str] = set()

    for raw in raw_paths:
        target = Path(raw)
        if not target.is_absolute():
            target = root / target
        for image in _iter_images(target, recursive, max_bytes):
            key = str(image.resolve())
            if key in seen:
                continue
            seen.add(key)
            report.scanned += 1
            try:
                digest = _sha256(image)
            except OSError as exc:
                report.errors.append(f"{cfg.relative(image)}: {exc}")
                report.skipped += 1
                continue

            asset_id = f"local-{digest[:12]}"
            record = load_record(image)
            gates = sorted(passed_gates(image)) if record else []
            rejected = bool((record or {}).get("rejected"))
            if record:
                report.with_record += 1
            if rejected:
                report.rejected += 1

            # A file already catalogued by a generation / import / sheet (a richer row with
            # provider provenance) is left alone; only ever mint a local-<sha12> row once.
            existing_asset = catalog.get_asset(asset_id)
            if existing_asset is None:
                other = catalog.find_by_sha(digest)
                if other is not None:
                    report.already += 1
                    report.files.append({
                        "asset_id": other.get("id"),
                        "path": cfg.relative(image),
                        "gates": gates,
                        "rejected": rejected,
                        "skipped": "already indexed",
                    })
                    continue

            ctx = _pipeline_context(image, root)
            char = character_id or ctx.get("bot")
            palette_id = None
            perspective = None
            resolved_game = game
            if char:
                identity = catalog.get_identity(str(char))
                if identity is not None:
                    palette_id = identity.palette_id
                    perspective = identity.perspective
                    resolved_game = identity.game or game

            old_provenance = (existing_asset or {}).get("provenance") or {}
            gallery_edited = bool((old_provenance.get("gallery_metadata") or {}).get("edited"))
            tags = ["local", "sync", *_pipeline_tags(ctx), *gates]
            if rejected:
                tags.append("rejected")
            resolved_category = category
            if gallery_edited and existing_asset:
                # Empty values are deliberate gallery edits, not missing inference.
                resolved_game = existing_asset["game"]
                char = existing_asset["character_id"]
                resolved_category = existing_asset.get("category") or category
                palette_id = existing_asset.get("palette_id") or palette_id
                perspective = existing_asset.get("perspective") or perspective
                tags = str(existing_asset.get("tags") or "").split()
            provenance = {
                "kind": "local",
                "sha256": digest,
                "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "relative_path": cfg.relative(image),
                "gates": gates,
                "rejected": rejected,
                "pipeline": ctx or None,
                "command": ((record or {}).get("params") or {}).get("command"),
            }
            if gallery_edited:
                provenance["gallery_metadata"] = {"edited": True}
            existed = existing_asset is not None
            catalog.register_asset(
                asset_id=asset_id,
                name=image.stem,
                file_path=image,
                category=resolved_category,
                asset_type=_guess_type(image),
                game=resolved_game,
                character_id=str(char) if char else None,
                palette_id=palette_id,
                perspective=perspective,
                frame_size=_image_size(image),
                tags=tags,
                provenance=provenance,
            )
            if existed:
                report.updated += 1
            else:
                report.registered += 1
            report.files.append({
                "asset_id": asset_id,
                "path": cfg.relative(image),
                "gates": gates,
                "rejected": rejected,
                "bot": char,
            })
    return report


def _pipeline_tags(ctx: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("rotation_provider", "animation_provider"):
        if ctx.get(key):
            out.append(str(ctx[key]).lower())
    return out
