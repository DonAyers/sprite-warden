"""Central Asset Repository & Catalog: SQLite FTS5 search and identity storage."""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .identity import CharacterIdentity, SceneIdentity

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "spritewarden.db"

SCHEMA = """
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
"""


class AssetCatalog:
    """Manages the central art database, character identities, and FTS5 search."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript(SCHEMA)

    # --- Character Identity Methods ---

    def save_identity(self, identity: CharacterIdentity) -> None:
        """Store or update a CharacterIdentity record."""
        dims = identity.dimensions or [64, 64]
        with self._get_conn() as conn:
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
