"""Central Asset Repository & Catalog: SQLite FTS5 search and identity storage."""

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from .identity import CharacterIdentity, SceneIdentity

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "spritewarden.db"
DB_ENV = "SPRITEWARDEN_DB"  # path override for the catalog database

ASSET_COLUMNS = (
    "id, name, category, asset_type, game, character_id, palette_id, perspective, "
    "frame_w, frame_h, frame_count, file_path, tags, provenance, created_at"
)
# Provenance is stored as JSON with the default ``": "`` separator, so a provider filter can
# be a portable LIKE (JSON1's json_extract is not on every bundled SQLite).
_NO_PROVIDER = ("", "none", "null", "any", "*")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS identities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    game TEXT NOT NULL,
    palette_id TEXT NOT NULL,
    perspective TEXT NOT NULL,
    dimensions_w INTEGER,
    dimensions_h INTEGER,
    prompt_description TEXT,
    prohibitions TEXT,
    data_json TEXT
);

CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    game TEXT NOT NULL,
    character_id TEXT REFERENCES identities(id),
    palette_id TEXT,
    perspective TEXT,
    frame_w INTEGER,
    frame_h INTEGER,
    frame_count INTEGER DEFAULT 1,
    file_path TEXT NOT NULL,
    tags TEXT,
    provenance TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE VIRTUAL TABLE IF NOT EXISTS assets_fts USING fts5(
    name, category, asset_type, tags, game, character_id, file_path,
    content='assets', content_rowid='rowid', tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS assets_ai AFTER INSERT ON assets BEGIN
    INSERT INTO assets_fts(rowid, name, category, asset_type, tags, game, character_id, file_path)
    VALUES (new.rowid, new.name, new.category, new.asset_type, new.tags, new.game, new.character_id, new.file_path);
END;

-- Keep the external-content FTS index in step with updates and deletes (register_asset upserts):
-- without these a re-registered asset stayed findable by its OLD name/tags and not by the new ones.
CREATE TRIGGER IF NOT EXISTS assets_au AFTER UPDATE ON assets BEGIN
    INSERT INTO assets_fts(assets_fts, rowid, name, category, asset_type, tags, game, character_id, file_path)
    VALUES ('delete', old.rowid, old.name, old.category, old.asset_type, old.tags, old.game, old.character_id, old.file_path);
    INSERT INTO assets_fts(rowid, name, category, asset_type, tags, game, character_id, file_path)
    VALUES (new.rowid, new.name, new.category, new.asset_type, new.tags, new.game, new.character_id, new.file_path);
END;

CREATE TRIGGER IF NOT EXISTS assets_ad AFTER DELETE ON assets BEGIN
    INSERT INTO assets_fts(assets_fts, rowid, name, category, asset_type, tags, game, character_id, file_path)
    VALUES ('delete', old.rowid, old.name, old.category, old.asset_type, old.tags, old.game, old.character_id, old.file_path);
END;
"""


class AssetCatalog:
    """Manages the central art database, character identities, and FTS5 search."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or os.environ.get(DB_ENV) or DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            had_update_trigger = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'assets_au'"
            ).fetchone() is not None
            had_assets = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'assets'"
            ).fetchone() is not None
            conn.executescript(SCHEMA)
            if had_assets and not had_update_trigger:
                # A database from before the update/delete triggers may hold stale index rows;
                # an FTS 'delete' with values that do not match them would corrupt the index.
                conn.execute("INSERT INTO assets_fts(assets_fts) VALUES ('rebuild')")
            for table in ("assets", "identities"):
                rows = conn.execute(f"SELECT DISTINCT game FROM {table} WHERE game IS NOT NULL AND game != ''").fetchall()
                for row in rows:
                    slug = str(row[0])
                    name = re.sub(r"[-_]+", " ", slug).strip().title() or slug
                    conn.execute(
                        "INSERT OR IGNORE INTO projects (slug, name) VALUES (?, ?)",
                        (slug, name),
                    )

    def list_projects(self) -> list[dict[str, Any]]:
        """Gallery groupings keyed by the existing asset ``game`` field."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT p.slug, p.name, p.description, p.created_at, COUNT(a.id) AS asset_count "
                "FROM projects p LEFT JOIN assets a ON a.game = p.slug "
                "GROUP BY p.slug ORDER BY lower(p.name), p.slug"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_project(self, slug: str, name: str, description: str = "") -> dict[str, Any]:
        """Create a project grouping; an identical retry returns the existing project."""
        slug = str(slug).strip().lower()
        name = str(name).strip()
        description = str(description).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", slug):
            raise ValueError("project slug must be 1-64 lowercase letters, digits, hyphens, or underscores")
        if not name or len(name) > 100:
            raise ValueError("project name must be 1-100 characters")
        if len(description) > 500:
            raise ValueError("project description must be at most 500 characters")
        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT slug, name, description, created_at FROM projects WHERE slug = ?", (slug,)
            ).fetchone()
            if existing:
                item = dict(existing)
                if item["name"] != name or item["description"] != description:
                    raise ValueError(f"project slug {slug!r} already exists")
                return item
            conn.execute("INSERT INTO projects (slug, name, description) VALUES (?, ?, ?)",
                         (slug, name, description))
            row = conn.execute(
                "SELECT slug, name, description, created_at FROM projects WHERE slug = ?", (slug,)
            ).fetchone()
        return dict(row)

    def get_project(self, slug: str) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT slug, name, description, created_at FROM projects WHERE slug = ?",
                               (slug,)).fetchone()
        return dict(row) if row else None

    def update_asset_metadata(
        self,
        asset_id: str,
        *,
        tags: list[str],
        game: str,
        character_id: str | None,
        category: str,
    ) -> bool:
        """Edit organization fields and mark provenance as gallery-edited; bytes/gates stay untouched."""
        clean_tags: list[str] = []
        for raw in tags:
            if not isinstance(raw, str):
                raise ValueError("tags must be strings")
            tag = re.sub(r"[^\w-]+", "-", raw.strip().lower(), flags=re.UNICODE).strip("-_")[:48]
            if tag and tag not in clean_tags:
                clean_tags.append(tag)
        if len(clean_tags) > 32:
            raise ValueError("at most 32 tags per asset")
        game = str(game).strip()
        category = str(category).strip()
        if not category or len(category) > 80:
            raise ValueError("category must be 1-80 characters")
        character_id = str(character_id).strip() if character_id else None
        with self._get_conn() as conn:
            row = conn.execute("SELECT provenance FROM assets WHERE id = ?", (asset_id,)).fetchone()
            if row is None:
                return False
            if game and not conn.execute("SELECT 1 FROM projects WHERE slug = ?", (game,)).fetchone():
                raise ValueError(f"unknown project {game!r}")
            if character_id and not conn.execute("SELECT 1 FROM identities WHERE id = ?", (character_id,)).fetchone():
                raise ValueError(f"unknown character identity {character_id!r}")
            try:
                provenance = json.loads(row["provenance"]) if row["provenance"] else {}
            except (ValueError, TypeError):
                provenance = {}
            provenance["gallery_metadata"] = {"edited": True}
            conn.execute(
                "UPDATE assets SET tags = ?, game = ?, character_id = ?, category = ?, provenance = ? WHERE id = ?",
                (" ".join(clean_tags), game, character_id, category, json.dumps(provenance), asset_id),
            )
        return True

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        """One asset row by id (provenance decoded), or None."""
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["provenance"] = json.loads(item["provenance"]) if item.get("provenance") else {}
        except ValueError:
            pass
        return item

    def delete_asset(self, asset_id: str) -> bool:
        with self._get_conn() as conn:
            return conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,)).rowcount > 0

    def find_by_sha(self, digest: str) -> dict[str, Any] | None:
        """The first asset whose provenance records this sha256 (generated/import/sheet/local)."""
        if not digest:
            return None
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE provenance LIKE ? LIMIT 1",
                (f'%\"sha256\": \"{digest}\"%',),
            ).fetchone()
        return self._decode_asset(row) if row is not None else None

    def find_by_provider_asset_id(self, provider: str, provider_asset_id: str) -> dict[str, Any] | None:
        """The first asset imported from one provider-owned asset id, if already catalogued."""
        if not provider or not provider_asset_id:
            return None
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE provenance LIKE ? AND provenance LIKE ? LIMIT 1",
                (f'%\"provider\": \"{provider}\"%', f'%\"provider_asset_id\": \"{provider_asset_id}\"%'),
            ).fetchone()
        return self._decode_asset(row) if row is not None else None

    # --- Character Identity Methods ---

    def save_identity(self, identity: CharacterIdentity) -> None:
        """Store or update a CharacterIdentity record."""
        dims = identity.dimensions or [64, 64]
        with self._get_conn() as conn:
            project_slug = str(identity.game or "global")
            project_name = re.sub(r"[-_]+", " ", project_slug).strip().title() or project_slug
            conn.execute("INSERT OR IGNORE INTO projects (slug, name) VALUES (?, ?)", (project_slug, project_name))
            conn.execute(
                """
                INSERT INTO identities (id, name, game, palette_id, perspective, dimensions_w, dimensions_h,
                                        prompt_description, prohibitions, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, game=excluded.game, palette_id=excluded.palette_id,
                    perspective=excluded.perspective, dimensions_w=excluded.dimensions_w,
                    dimensions_h=excluded.dimensions_h, prompt_description=excluded.prompt_description,
                    prohibitions=excluded.prohibitions, data_json=excluded.data_json
                """,
                (
                    identity.id,
                    identity.name,
                    identity.game,
                    identity.palette_id,
                    identity.perspective,
                    dims[0],
                    dims[1],
                    identity.prompt_description,
                    json.dumps(identity.prohibitions),
                    identity.model_dump_json(),
                ),
            )

    def get_identity(self, identity_id: str) -> CharacterIdentity | None:
        """Fetch a CharacterIdentity by slug."""
        with self._get_conn() as conn:
            row = conn.execute("SELECT data_json FROM identities WHERE id = ?", (identity_id,)).fetchone()
            if not row:
                return None
            return CharacterIdentity.model_validate_json(row["data_json"])

    def list_identities(self, game: str | None = None) -> list[CharacterIdentity]:
        """List all registered character identities."""
        with self._get_conn() as conn:
            if game:
                rows = conn.execute("SELECT data_json FROM identities WHERE game = ? ORDER BY id", (game,)).fetchall()
            else:
                rows = conn.execute("SELECT data_json FROM identities ORDER BY id").fetchall()
            return [CharacterIdentity.model_validate_json(r["data_json"]) for r in rows]

    # --- Asset Indexing & Search Methods ---

    def register_asset(
        self,
        asset_id: str,
        name: str,
        file_path: str | Path,
        category: str = "characters",
        asset_type: str = "sprite",
        game: str = "global",
        character_id: str | None = None,
        palette_id: str | None = None,
        perspective: str | None = None,
        frame_size: list[int] | None = None,
        frame_count: int = 1,
        tags: list[str] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        """Index an asset in the central repository."""
        fs = frame_size or [None, None]
        tags_str = " ".join(tags or [])
        prov_json = json.dumps(provenance or {})
        with self._get_conn() as conn:
            project_slug = str(game or "global")
            project_name = re.sub(r"[-_]+", " ", project_slug).strip().title() or project_slug
            conn.execute("INSERT OR IGNORE INTO projects (slug, name) VALUES (?, ?)", (project_slug, project_name))
            conn.execute(
                """
                INSERT INTO assets (id, name, category, asset_type, game, character_id, palette_id,
                                    perspective, frame_w, frame_h, frame_count, file_path, tags, provenance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, category=excluded.category, asset_type=excluded.asset_type,
                    game=excluded.game, character_id=excluded.character_id, palette_id=excluded.palette_id,
                    perspective=excluded.perspective, frame_w=excluded.frame_w, frame_h=excluded.frame_h,
                    frame_count=excluded.frame_count, file_path=excluded.file_path, tags=excluded.tags,
                    provenance=excluded.provenance
                """,
                (
                    asset_id,
                    name,
                    category,
                    asset_type,
                    game,
                    character_id,
                    palette_id,
                    perspective,
                    fs[0],
                    fs[1],
                    frame_count,
                    str(file_path),
                    tags_str,
                    prov_json,
                ),
            )

    def search(
        self,
        query: str = "",
        game: str | None = None,
        character_id: str | None = None,
        category: str | None = None,
        asset_type: str | None = None,
        perspective: str | None = None,
        max_frame_size: int | None = None,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """Search the central repository for existing assets matching parameters."""
        conditions = []
        params = []

        if query.strip():
            # Clean alphanumeric FTS query
            clean_q = " ".join(re.findall(r"\w+", query))
            if clean_q:
                conditions.append("assets.rowid IN (SELECT rowid FROM assets_fts WHERE assets_fts MATCH ?)")
                params.append(clean_q)

        if game:
            conditions.append("game = ?")
            params.append(game)
        if character_id:
            conditions.append("character_id = ?")
            params.append(character_id)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if asset_type:
            conditions.append("asset_type = ?")
            params.append(asset_type)
        if perspective:
            conditions.append("perspective = ?")
            params.append(perspective)
        if max_frame_size:
            conditions.append("(frame_w IS NULL OR frame_w <= ?)")
            params.append(max_frame_size)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"""
        SELECT id, name, category, asset_type, game, character_id, palette_id,
               perspective, frame_w, frame_h, frame_count, file_path, tags, provenance
        FROM assets
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ?
        """
        params.append(limit)

        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            results = []
            for r in rows:
                item = dict(r)
                if item.get("provenance"):
                    try:
                        item["provenance"] = json.loads(item["provenance"])
                    except Exception:
                        pass
                results.append(item)
            return results

    # --- Gallery listing (paginated, provider-aware) ---

    @staticmethod
    def _decode_asset(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key in ("provenance",):
            if item.get(key):
                try:
                    item[key] = json.loads(item[key])
                except (ValueError, TypeError):
                    pass
        tags = item.get("tags")
        item["tag_list"] = [t for t in str(tags or "").split() if t]
        return item

    def _asset_filters(
        self,
        *,
        query: str = "",
        game: str | None = None,
        character_id: str | None = None,
        category: str | None = None,
        asset_type: str | None = None,
        perspective: str | None = None,
        provider: str | None = None,
    ) -> tuple[list[str], list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if query.strip():
            clean_q = " ".join(re.findall(r"\w+", query))
            if clean_q:
                conditions.append("rowid IN (SELECT rowid FROM assets_fts WHERE assets_fts MATCH ?)")
                params.append(clean_q)
        for column, value in (
            ("game", game), ("character_id", character_id), ("category", category),
            ("asset_type", asset_type), ("perspective", perspective),
        ):
            if value:
                conditions.append(f"{column} = ?")
                params.append(value)
        if provider and provider.strip().lower() not in _NO_PROVIDER:
            # Generated rows carry provenance.provider; sheets/locals carry provenance.kind
            # ("sheet", "local"). Match either, so the gallery's provider filter is complete.
            conditions.append("(provenance LIKE ? OR provenance LIKE ?)")
            name = provider.strip().lower()
            params.extend([f'%"provider": "{name}"%', f'%"kind": "{name}"%'])
        return conditions, params

    def list_assets(
        self,
        *,
        query: str = "",
        game: str | None = None,
        character_id: str | None = None,
        category: str | None = None,
        asset_type: str | None = None,
        perspective: str | None = None,
        provider: str | None = None,
        limit: int = 60,
        offset: int = 0,
    ) -> dict[str, Any]:
        """A page of catalog assets plus the total matching the same filters (for the gallery)."""
        conditions, params = self._asset_filters(
            query=query, game=game, character_id=character_id, category=category,
            asset_type=asset_type, perspective=perspective, provider=provider,
        )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit = max(0, min(int(limit), 500))
        offset = max(0, int(offset))
        with self._get_conn() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM assets {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT {ASSET_COLUMNS} FROM assets {where} ORDER BY created_at DESC, name LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return {"assets": [self._decode_asset(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    def facets(self) -> dict[str, list[str]]:
        """The distinct filter values actually present, for the gallery's dropdowns."""
        out: dict[str, list[str]] = {}
        with self._get_conn() as conn:
            for column in ("game", "category", "asset_type", "perspective", "character_id"):
                rows = conn.execute(
                    f"SELECT DISTINCT {column} FROM assets WHERE {column} IS NOT NULL AND {column} != '' "
                    f"ORDER BY {column}"
                ).fetchall()
                out[column] = [str(r[0]) for r in rows]
            out["providers"] = []
            rows = conn.execute(
                "SELECT DISTINCT provenance FROM assets WHERE provenance IS NOT NULL AND provenance != ''"
            ).fetchall()
        seen: set[str] = set()
        for r in rows:
            try:
                prov = json.loads(r[0])
            except (ValueError, TypeError):
                continue
            name = str(prov.get("provider") or prov.get("kind") or "").strip().lower()
            if name and name not in seen:
                seen.add(name)
                out["providers"].append(name)
        out["providers"].sort()
        return out

    def summary(self) -> dict[str, Any]:
        """Counts for the gallery's header: total assets, per-provider, per-type, identities."""
        with self._get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            by_type = {str(r[0]): r[1] for r in conn.execute(
                "SELECT asset_type, COUNT(*) FROM assets GROUP BY asset_type ORDER BY asset_type")}
            identities = conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0]
        by_provider: dict[str, int] = {}
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT provenance FROM assets WHERE provenance IS NOT NULL AND provenance != ''").fetchall()
        for r in rows:
            try:
                prov = json.loads(r[0])
            except (ValueError, TypeError):
                continue
            name = str(prov.get("provider") or prov.get("kind") or "unknown").strip().lower()
            by_provider[name] = by_provider.get(name, 0) + 1
        return {"assets": total, "identities": identities, "by_type": by_type, "by_provider": by_provider}
