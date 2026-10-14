"""The gallery's read layer: turn catalog rows + project files into JSON-ready views.

Import-light at module level (json/pathlib + the SQLite catalog). Pillow is only
imported inside the thumbnail path in ``server.py``; nothing here loads PIL, httpx
or pydantic-heavy router code, so the gallery can also be imported from a script.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..enforce.config import find_project_root, load_config
from ..enforce.core import PROVIDER_LABELS, load_raw_registry
from ..enforce.pipeline import check_pipeline, find_pipelines, load_pipeline, stage_status
from ..gates.rejections import read_rejections, summarize
from ..gates.records import load_record, passed_gates, review_problem, sha256_of
from ..repo.catalog import AssetCatalog


def animation_info(asset: dict[str, Any]) -> dict[str, int] | None:
    provenance = asset.get("provenance") if isinstance(asset.get("provenance"), dict) else {}
    try:
        frame_count = int(asset.get("frame_count") or provenance.get("frame_count") or 1)
    except (TypeError, ValueError):
        return None
    if frame_count < 2 or frame_count > 512:
        return None

    kind = str(provenance.get("kind") or "")
    dimensions = provenance.get("dimensions")
    if kind == "sheet":
        frame_width = asset.get("frame_w")
        frame_height = asset.get("frame_h")
    elif isinstance(dimensions, (list, tuple)) and len(dimensions) >= 2:
        frame_width, frame_height = dimensions[:2]
    else:
        frame_width = provenance.get("frame_width")
        frame_height = provenance.get("frame_height")
        if not frame_width or not frame_height:
            return None

    try:
        frame_width, frame_height = int(frame_width), int(frame_height)
    except (TypeError, ValueError):
        return None
    if frame_width < 1 or frame_height < 1:
        return None

    try:
        columns = int(provenance.get("columns") or 0)
        rows = int(provenance.get("rows") or 0)
    except (TypeError, ValueError):
        columns, rows = 0, 0
    if columns < 1:
        sheet_width = asset.get("frame_w")
        columns = max(1, int(sheet_width) // frame_width) if sheet_width else frame_count
    columns = min(columns, frame_count)
    if rows < 1:
        rows = (frame_count + columns - 1) // columns
    try:
        fps = int(provenance.get("fps") or (provenance.get("params") or {}).get("fps") or 8)
    except (TypeError, ValueError):
        fps = 8
    fps = max(1, min(fps, 60))
    return {
        "frame_count": frame_count,
        "frame_width": frame_width,
        "frame_height": frame_height,
        "columns": columns,
        "rows": rows,
        "fps": fps,
    }


class GalleryData:
    """The gallery facade for catalog queries and constrained organization updates."""

    def __init__(
        self,
        catalog: AssetCatalog | None = None,
        *,
        project_root: str | Path | None = None,
        registry_path: str | Path | None = None,
    ):
        self.catalog = catalog or AssetCatalog()
        self.project_root = Path(project_root) if project_root else find_project_root(Path.cwd())
        self.cfg = load_config(self.project_root)
        self._registry_path = registry_path
        self._registry: dict[str, Any] | None = None

    def repair_source(self, asset_id):
        from PIL import Image
        from ..repair import digest, MAX_PIXELS
        asset = self.asset(asset_id)
        if not asset or not asset.get('exists'):
            raise ValueError('repair asset missing')
        path = Path(asset['resolved_path']).resolve()
        if not path.is_relative_to(self.project_root.resolve()):
            raise ValueError('repair asset outside project')
        with Image.open(path) as opened:
            if opened.width * opened.height > MAX_PIXELS:
                raise ValueError('image exceeds repair limit')
            image = opened.convert('RGBA')
        colors = sorted(set(image.getdata()))
        return {'asset_id': asset_id, 'source_digest': digest(image), 'width': image.width, 'height': image.height,
                'palette': ['#%02x%02x%02x' % c[:3] for c in colors if c[3]], 'gate': asset.get('gate'), 'path': str(path)}

    def repair(self, payload):
        from ..repair import save_candidate
        source = self.repair_source(payload['asset_id'])
        result = save_candidate(self.project_root, source['path'], payload['source_digest'], payload.get('mask', []),
                                payload.get('mapping', {}), actor=payload.get('actor'), reason=payload.get('reason'), patch=payload.get('patch'),
                                repair_plan=payload.get('repair_plan'))
        original = self.asset(payload['asset_id'])
        result['asset_id'] = 'repair-' + result['run_id']
        self.catalog.register_asset(result['asset_id'], 'Repair of ' + original['name'], result['path'], category='repair',
                                    game=original.get('game') or 'global', character_id=original.get('character_id'),
                                    palette_id=original.get('palette_id'), provenance={'kind': 'repair', 'source_asset_id': payload['asset_id'],
                                    'source_digest': result['source_digest'], 'run_id': result['run_id'], 'approval': 'pending',
                                    'changed_pixels': result['changed_pixels'], 'validation': result['validation'],
                                    'preflight':result.get('preflight'), 'repair_plan':result.get('repair_plan'),
                                    'pose_review_status':result.get('preflight',{}).get('pose_review_status','not_assessed')})
        return result

    @property
    def registry(self) -> dict[str, Any]:
        if self._registry is None:
            self._registry = load_raw_registry(self._registry_path)
        return self._registry

    # --- helpers -----------------------------------------------------------

    def resolve(self, path: str | Path) -> Path:
        """A catalog ``file_path`` resolved to an existing file, or the best guess."""
        p = Path(str(path))
        if p.is_file():
            return p
        if not p.is_absolute():
            candidate = self.project_root / p
            if candidate.is_file():
                return candidate
        return p

    def _gate_info(self, image: Path) -> dict[str, Any]:
        record = load_record(image)
        if not record:
            return {"has_record": False, "passed": [], "gates": {}, "params": {}, "rejected": None,
                    "stale": None, "review_problem": None, "command": None, "time": None}
        stale: bool | None = None
        try:
            stale = image.is_file() and record.get("sha256") != sha256_of(image)
        except OSError:
            stale = True
        params = record.get("params") if isinstance(record.get("params"), dict) else {}
        return {
            "has_record": True,
            "passed": sorted(passed_gates(image)),
            "gates": record.get("gates", {}),
            "params": params,
            "command": params.get("command"),
            "time": record.get("time"),
            "rejected": record.get("rejected"),
            "stale": stale,
            "review_problem": review_problem(image),
        }

    def _enrich(self, asset: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve(asset.get("file_path") or "")
        asset["resolved_path"] = str(path)
        asset["exists"] = path.is_file()
        asset["relative_path"] = self.cfg.relative(path)
        asset["gate"] = self._gate_info(path) if path.is_file() else {"has_record": False, "passed": []}
        asset["kind"] = (asset.get("provenance") or {}).get("kind") or "asset"
        asset["provider"] = (asset.get("provenance") or {}).get("provider")
        asset["animation"] = animation_info(asset)
        return asset

    # --- endpoints ---------------------------------------------------------

    def review(self) -> dict[str, Any]:
        from hashlib import sha256
        from ..workflow_state import inspect_state
        pipelines = []
        root = self.project_root.resolve()
        for path in find_pipelines(root):
            if not path.resolve().is_relative_to(root):
                continue
            relative = path.parent.relative_to(root).as_posix()
            pipelines.append({'id': sha256(relative.encode()).hexdigest()[:24], 'folder': relative,
                              'state': inspect_state(root, path.parent)})
        return {'pipelines': pipelines}

    def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        from ..workflow_state import record_decision
        allowed = {'pipeline_id', 'revision_id', 'decision', 'actor', 'reason', 'expected_version', 'gate', 'operation', 'run_id'}
        if set(payload) - allowed:
            raise ValueError('unknown decision fields')
        pipeline = next((p for p in self.review()['pipelines'] if p['id'] == payload.get('pipeline_id')), None)
        if not pipeline:
            raise ValueError('unknown pipeline ID')
        fields = {k: v for k, v in payload.items() if k != 'pipeline_id'}
        return record_decision(self.project_root, pipeline['folder'], **fields)

    def revision_image(self, pipeline_id, revision_id, facing):
        from ..workflow_state import _hash, _safe_file
        pipeline = next((p for p in self.review()['pipelines'] if p['id'] == pipeline_id), None)
        if not pipeline:
            raise ValueError('unknown pipeline ID')
        try:
            item = pipeline['state']['revisions'][revision_id]['files'][facing]
        except KeyError:
            raise ValueError('unknown revision or facing') from None
        digest = item['sha256']
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('invalid artifact hash')
        path = _safe_file(self.project_root.resolve(), '.spritewarden/objects/' + digest)
        if _hash(path) != digest:
            raise ValueError('archived revision corrupted')
        return path

    def compare(self, before, after):
        from PIL import Image
        a, b = self.asset(before), self.asset(after)
        if not a or not b or not a['exists'] or not b['exists']:
            raise ValueError('two existing catalog asset IDs required')
        with Image.open(a['resolved_path']) as left, Image.open(b['resolved_path']) as right:
            left, right = left.convert('RGBA'), right.convert('RGBA')
        width = max(left.width, right.width); height = max(left.height, right.height)
        if width * height > 16_000_000:
            raise ValueError('comparison image too large')
        mask = Image.new('RGBA', (width, height))
        count = 0
        for y in range(height):
            for x in range(width):
                old = left.getpixel((x, y)) if x < left.width and y < left.height else None
                new = right.getpixel((x, y)) if x < right.width and y < right.height else None
                if old != new:
                    mask.putpixel((x, y), (255, 0, 160, 255)); count += 1
        return {'before': before, 'after': after, 'changed_pixels': count,
                'before_size': list(left.size), 'after_size': list(right.size)}, mask

    def trace(self, run_id=None, limit=100, offset=0):
        from ..events import EventStore, default_path
        path = default_path(self.project_root)
        return EventStore(path).list_events(run_id=run_id, limit=min(500, max(1, int(limit))), offset=max(0, int(offset))) if path.exists() else []

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "db": str(self.catalog.db_path),
            "project_root": str(self.project_root),
            "config_error": self.cfg.error,
        }

    def summary(self) -> dict[str, Any]:
        data = self.catalog.summary()
        data.update({
            "version": __version__,
            "project_root": str(self.project_root),
            "db": str(self.catalog.db_path),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        return data

    def facets(self) -> dict[str, Any]:
        return self.catalog.facets()

    def assets(self, **filters: Any) -> dict[str, Any]:
        page = self.catalog.list_assets(**filters)
        page["assets"] = [self._enrich(a) for a in page["assets"]]
        return page

    def asset_sequence(self, **filters: Any) -> dict[str, Any]:
        allowed = {"query", "game", "character_id", "category", "asset_type", "perspective", "provider"}
        criteria = {key: value for key, value in filters.items() if key in allowed}
        limit = 500
        maximum = 5000
        assets: list[dict[str, Any]] = []
        total = 0
        while len(assets) < maximum:
            page = self.catalog.list_assets(**criteria, limit=min(limit, maximum - len(assets)), offset=len(assets))
            total = page["total"]
            if not page["assets"]:
                break
            assets.extend({
                "id": item["id"],
                "name": item["name"],
                "animation": animation_info(item),
            } for item in page["assets"])
            if len(assets) >= total:
                break
        return {"assets": assets, "total": total, "truncated": total > len(assets)}

    def asset(self, asset_id: str) -> dict[str, Any] | None:
        row = self.catalog.get_asset(asset_id)
        if row is None:
            return None
        return self._enrich(row)

    def identities(self, game: str | None = None) -> list[dict[str, Any]]:
        return [i.model_dump() for i in self.catalog.list_identities(game)]

    def projects(self) -> list[dict[str, Any]]:
        return self.catalog.list_projects()

    def create_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"name", "slug", "description"}
        if set(payload) - allowed:
            raise ValueError("unknown project fields")
        name = payload.get("name")
        if not isinstance(name, str):
            raise ValueError("project name is required")
        slug = payload.get("slug")
        if slug is None:
            import re

            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64]
        if not isinstance(slug, str):
            raise ValueError("project slug must be a string")
        description = payload.get("description", "")
        if not isinstance(description, str):
            raise ValueError("project description must be a string")
        project = self.catalog.create_project(slug, name, description)
        return next((item for item in self.catalog.list_projects() if item["slug"] == project["slug"]), project)

    def update_asset_metadata(self, asset_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"tags", "project", "character_id", "category"}
        if set(payload) != allowed:
            raise ValueError("asset updates require tags, project, character_id, and category only")
        tags = payload["tags"]
        if not isinstance(tags, list):
            raise ValueError("tags must be a list")
        if any(not isinstance(tag, str) for tag in tags):
            raise ValueError("tags must be strings")
        project = payload["project"]
        character_id = payload["character_id"]
        category = payload["category"]
        if not isinstance(project, str) or not isinstance(category, str):
            raise ValueError("project and category must be strings")
        if character_id is not None and not isinstance(character_id, str):
            raise ValueError("character_id must be a string or null")
        if not self.catalog.update_asset_metadata(asset_id, tags=tags, game=project,
                                                  character_id=character_id, category=category):
            raise ValueError(f"asset {asset_id!r} not found")
        updated = self.asset(asset_id)
        if updated is None:
            raise ValueError(f"asset {asset_id!r} not found")
        return updated

    def pipelines(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in find_pipelines(self.project_root):
            rel = self.cfg.relative(path.parent)
            try:
                data = load_pipeline(path)
            except (OSError, ValueError) as exc:
                out.append({"dir": rel, "bot": path.parent.name, "error": str(exc), "status": None, "problems": []})
                continue
            out.append({
                "dir": rel,
                "bot": data.get("bot") or path.parent.name,
                "data": data,
                "status": stage_status(data, self.cfg),
                "problems": check_pipeline(data, self.cfg, path, self.registry),
            })
        return out

    def rejections(self, provider: str | None = None, limit: int = 200) -> dict[str, Any]:
        entries = read_rejections(self.cfg.rejections_path, limit=limit)
        if provider:
            want = "".join(ch for ch in provider.lower() if ch.isalnum())
            entries = [e for e in entries
                       if "".join(ch for ch in str(e.get("provider") or "").lower() if ch.isalnum()) == want]
        return {
            "log": str(self.cfg.rejections_path),
            "exists": self.cfg.rejections_path.is_file(),
            "count": len(entries),
            "groups": summarize(entries),
            "entries": entries,
        }

    def providers(self) -> dict[str, Any]:
        reg = self.registry
        providers = []
        for name, prov in (reg.get("providers") or {}).items():
            enforce = prov.get("enforce") or {}
            providers.append({
                "name": name,
                "label": PROVIDER_LABELS.get(name, name),
                "identity": prov.get("identity") or {},
                "operations": sorted((prov.get("operations") or {}).keys()),
                "mcp_prefixes": enforce.get("mcp_prefixes") or [],
                "read_only": enforce.get("read_only") or [],
                "spends": enforce.get("spends") or [],
                "cli": enforce.get("cli") or {},
            })
        return {
            "providers": providers,
            "pipeline_rules": reg.get("pipeline_rules") or [],
            "stages": reg.get("stages") or [],
            "registry_version": reg.get("version"),
        }
