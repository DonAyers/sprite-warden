"""SQLite-backed knowledge store for the learning subsystem (append-only core)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .models import (
    Budget,
    DocEvent,
    Episode,
    Proposal,
    ProviderTool,
    Snapshot,
    Source,
    Strategy,
)

DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "learning.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    cadence_s INTEGER NOT NULL DEFAULT 86400,
    extract TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    etag TEXT,
    last_modified TEXT,
    last_hash TEXT,
    last_checked TEXT,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    fetched_at TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    body TEXT,
    extracted_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_snapshots_source ON snapshots(source_id, id DESC);

CREATE TABLE IF NOT EXISTS doc_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    provider TEXT DEFAULT '',
    detected_at TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    significance REAL DEFAULT 0,
    signals_json TEXT DEFAULT '[]',
    diff_text TEXT DEFAULT '',
    summary_what TEXT DEFAULT '',
    summary_how TEXT DEFAULT '',
    summary_why TEXT DEFAULT '',
    model TEXT DEFAULT 'deterministic',
    review_state TEXT DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_events_source ON doc_events(source_id, id DESC);

CREATE TABLE IF NOT EXISTS provider_tools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    capability_json TEXT DEFAULT '{}',
    params_json TEXT DEFAULT '{}',
    limits_json TEXT DEFAULT '{}',
    deprecations_json TEXT DEFAULT '[]',
    observed_at TEXT NOT NULL,
    source_event_id INTEGER,
    UNIQUE(provider, tool_name)
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    game TEXT DEFAULT 'global',
    asset_type TEXT DEFAULT 'sprite',
    perspective TEXT DEFAULT 'unknown',
    style_tags_json TEXT DEFAULT '[]',
    character_id TEXT,
    provider TEXT DEFAULT 'unknown',
    operation TEXT DEFAULT 'unknown',
    params_json TEXT DEFAULT '{}',
    prompt TEXT DEFAULT '',
    gates_json TEXT DEFAULT '{}',
    vision_json TEXT DEFAULT '{}',
    outcome TEXT DEFAULT 'pending',
    reason TEXT DEFAULT '',
    cost_usd REAL,
    duration_ms INTEGER,
    asset_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_scope ON episodes(provider, operation, asset_type, perspective);

CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    scope_key TEXT NOT NULL,
    what TEXT NOT NULL,
    how TEXT NOT NULL,
    why TEXT NOT NULL,
    evidence_json TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0,
    confirmations INTEGER DEFAULT 0,
    rejections INTEGER DEFAULT 0,
    status TEXT DEFAULT 'proposed',
    supersedes TEXT,
    source TEXT DEFAULT 'curation',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategies_scope ON strategies(scope_key);

CREATE VIRTUAL TABLE IF NOT EXISTS strategies_fts USING fts5(
    id UNINDEXED, scope_key, what, how, why
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    what TEXT DEFAULT '',
    how TEXT DEFAULT '',
    why TEXT DEFAULT '',
    evidence_json TEXT DEFAULT '{}',
    cost_estimate_usd REAL,
    status TEXT DEFAULT 'open',
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, id DESC);

CREATE TABLE IF NOT EXISTS budget (
    period TEXT PRIMARY KEY,
    spend_usd REAL DEFAULT 0,
    calls INTEGER DEFAULT 0,
    cap_usd REAL DEFAULT 5.0,
    cap_calls INTEGER DEFAULT 500
);

CREATE TABLE IF NOT EXISTS tip_exports (
    strategy_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    exported_at TEXT NOT NULL,
    tip_ref TEXT
);
"""


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _u(text: str | None, fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except Exception:
        return fallback


class LearningDB:
    """Manages sources, snapshots, events, provider tools, episodes, strategies, proposals."""

    def __init__(self, db_path: str | Path | None = None):
        env = os.environ.get("SPRITEWARDEN_LEARNING_DB")
        self.db_path = Path(db_path or env or DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript(SCHEMA)

    # --- Sources ---

    def upsert_source(self, source: Source) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO sources (id, provider, kind, url, cadence_s, extract, enabled,
                                     etag, last_modified, last_hash, last_checked, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    provider=excluded.provider, kind=excluded.kind, url=excluded.url,
                    cadence_s=excluded.cadence_s, extract=excluded.extract, enabled=excluded.enabled,
                    notes=excluded.notes
                """,
                (
                    source.id, source.provider, source.kind, source.url, source.cadence_s,
                    source.extract, 1 if source.enabled else 0, source.etag, source.last_modified,
                    source.last_hash, source.last_checked, source.notes,
                ),
            )

    def get_source(self, source_id: str) -> Source | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            return self._source_from_row(row) if row else None

    def list_sources(self, provider: str | None = None, enabled_only: bool = False) -> list[Source]:
        clauses, params = [], []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if enabled_only:
            clauses.append("enabled = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._get_conn() as conn:
            rows = conn.execute(f"SELECT * FROM sources {where} ORDER BY provider, id", params).fetchall()
            return [self._source_from_row(r) for r in rows]

    def update_source_fetch_state(
        self,
        source_id: str,
        etag: str | None,
        last_modified: str | None,
        last_hash: str | None,
        last_checked: str,
    ) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE sources SET etag = ?, last_modified = ?, last_hash = ?, last_checked = ? WHERE id = ?",
                (etag, last_modified, last_hash, last_checked, source_id),
            )

    def delete_source(self, source_id: str) -> None:
        with self._get_conn() as conn:
            conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> Source:
        return Source(
            id=row["id"], provider=row["provider"], kind=row["kind"], url=row["url"],
            cadence_s=row["cadence_s"], extract=row["extract"], enabled=bool(row["enabled"]),
            etag=row["etag"], last_modified=row["last_modified"], last_hash=row["last_hash"],
            last_checked=row["last_checked"], notes=row["notes"] or "",
        )

    # --- Snapshots ---

    def insert_snapshot(self, snap: Snapshot) -> int:
        with self._get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO snapshots (source_id, fetched_at, http_status, content_hash, body, extracted_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (snap.source_id, snap.fetched_at, snap.http_status, snap.content_hash,
                 snap.body, _j(snap.extracted)),
            )
            return int(cur.lastrowid)

    def latest_snapshot(self, source_id: str) -> Snapshot | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE source_id = ? ORDER BY id DESC LIMIT 1", (source_id,)
            ).fetchone()
            if not row:
                return None
            return Snapshot(
                id=row["id"], source_id=row["source_id"], fetched_at=row["fetched_at"],
                http_status=row["http_status"], content_hash=row["content_hash"],
                body=row["body"] or "", extracted=_u(row["extracted_json"], {}),
            )

    # --- Doc events ---

    def insert_event(self, event: DocEvent) -> int:
        with self._get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO doc_events (source_id, provider, detected_at, change_kind, significance,
                                           signals_json, diff_text, summary_what, summary_how, summary_why,
                                           model, review_state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event.source_id, event.provider, event.detected_at, event.change_kind,
                 event.significance, _j(event.signals), event.diff_text, event.summary_what,
                 event.summary_how, event.summary_why, event.model, event.review_state),
            )
            return int(cur.lastrowid)

    def list_events(self, limit: int = 50, source_id: str | None = None, review_state: str | None = None) -> list[DocEvent]:
        clauses, params = [], []
        if source_id:
            clauses.append("source_id = ?")
            params.append(source_id)
        if review_state:
            clauses.append("review_state = ?")
            params.append(review_state)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._get_conn() as conn:
            rows = conn.execute(f"SELECT * FROM doc_events {where} ORDER BY id DESC LIMIT ?", params).fetchall()
            return [self._event_from_row(r) for r in rows]

    def set_event_review(self, event_id: int, review_state: str) -> None:
        with self._get_conn() as conn:
            conn.execute("UPDATE doc_events SET review_state = ? WHERE id = ?", (review_state, event_id))

    def update_event_summary(self, event_id: int, what: str, how: str, why: str, model: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE doc_events SET summary_what = ?, summary_how = ?, summary_why = ?, model = ? WHERE id = ?",
                (what, how, why, model, event_id),
            )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> DocEvent:
        return DocEvent(
            id=row["id"], source_id=row["source_id"], provider=row["provider"] or "",
            detected_at=row["detected_at"], change_kind=row["change_kind"],
            significance=row["significance"], signals=_u(row["signals_json"], []),
            diff_text=row["diff_text"] or "", summary_what=row["summary_what"] or "",
            summary_how=row["summary_how"] or "", summary_why=row["summary_why"] or "",
            model=row["model"] or "deterministic", review_state=row["review_state"] or "new",
        )

    # --- Provider tools ---

    def upsert_tool(self, tool: ProviderTool) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO provider_tools (provider, tool_name, capability_json, params_json,
                                               limits_json, deprecations_json, observed_at, source_event_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(provider, tool_name) DO UPDATE SET
                       capability_json=excluded.capability_json, params_json=excluded.params_json,
                       limits_json=excluded.limits_json, deprecations_json=excluded.deprecations_json,
                       observed_at=excluded.observed_at, source_event_id=excluded.source_event_id""",
                (tool.provider, tool.tool_name, tool.capability_json, tool.params_json,
                 tool.limits_json, tool.deprecations_json, tool.observed_at, tool.source_event_id),
            )

    def get_tool(self, provider: str, tool_name: str) -> ProviderTool | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM provider_tools WHERE provider = ? AND tool_name = ?", (provider, tool_name)
            ).fetchone()
            return self._tool_from_row(row) if row else None

    def list_tools(self, provider: str | None = None) -> list[ProviderTool]:
        with self._get_conn() as conn:
            if provider:
                rows = conn.execute(
                    "SELECT * FROM provider_tools WHERE provider = ? ORDER BY tool_name", (provider,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM provider_tools ORDER BY provider, tool_name").fetchall()
            return [self._tool_from_row(r) for r in rows]

    @staticmethod
    def _tool_from_row(row: sqlite3.Row) -> ProviderTool:
        return ProviderTool(
            id=row["id"], provider=row["provider"], tool_name=row["tool_name"],
            capability_json=row["capability_json"] or "{}", params_json=row["params_json"] or "{}",
            limits_json=row["limits_json"] or "{}", deprecations_json=row["deprecations_json"] or "[]",
            observed_at=row["observed_at"], source_event_id=row["source_event_id"],
        )

    # --- Episodes ---

    def insert_episode(self, episode: Episode) -> int:
        with self._get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO episodes (ts, game, asset_type, perspective, style_tags_json, character_id,
                                         provider, operation, params_json, prompt, gates_json, vision_json,
                                         outcome, reason, cost_usd, duration_ms, asset_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (episode.ts, episode.game, episode.asset_type, episode.perspective,
                 _j(episode.style_tags), episode.character_id, episode.provider, episode.operation,
                 _j(episode.params), episode.prompt, _j(episode.gates), _j(episode.vision),
                 episode.outcome, episode.reason, episode.cost_usd, episode.duration_ms, episode.asset_id),
            )
            return int(cur.lastrowid)

    def list_episodes(
        self,
        limit: int = 50,
        game: str | None = None,
        provider: str | None = None,
        outcome: str | None = None,
    ) -> list[Episode]:
        clauses, params = [], []
        for col, val in (("game", game), ("provider", provider), ("outcome", outcome)):
            if val:
                clauses.append(f"{col} = ?")
                params.append(val)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._get_conn() as conn:
            rows = conn.execute(f"SELECT * FROM episodes {where} ORDER BY id DESC LIMIT ?", params).fetchall()
            return [self._episode_from_row(r) for r in rows]

    def count_episodes(self, provider: str | None = None, operation: str | None = None, perspective: str | None = None) -> int:
        clauses, params = [], []
        for col, val in (("provider", provider), ("operation", operation), ("perspective", perspective)):
            if val:
                clauses.append(f"{col} = ?")
                params.append(val)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._get_conn() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM episodes {where}", params).fetchone()[0])

    @staticmethod
    def _episode_from_row(row: sqlite3.Row) -> Episode:
        return Episode(
            id=row["id"], ts=row["ts"], game=row["game"], asset_type=row["asset_type"],
            perspective=row["perspective"], style_tags=_u(row["style_tags_json"], []),
            character_id=row["character_id"], provider=row["provider"], operation=row["operation"],
            params=_u(row["params_json"], {}), prompt=row["prompt"] or "",
            gates=_u(row["gates_json"], {}), vision=_u(row["vision_json"], {}),
            outcome=row["outcome"], reason=row["reason"] or "", cost_usd=row["cost_usd"],
            duration_ms=row["duration_ms"], asset_id=row["asset_id"],
        )

    # --- Strategies ---

    def upsert_strategy(self, strategy: Strategy) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO strategies (id, version, scope_key, what, how, why, evidence_json,
                                           confidence, confirmations, rejections, status, supersedes,
                                           source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       version=excluded.version, scope_key=excluded.scope_key, what=excluded.what,
                       how=excluded.how, why=excluded.why, evidence_json=excluded.evidence_json,
                       confidence=excluded.confidence, confirmations=excluded.confirmations,
                       rejections=excluded.rejections, status=excluded.status, supersedes=excluded.supersedes,
                       source=excluded.source, updated_at=excluded.updated_at""",
                (strategy.id, strategy.version, strategy.scope_key, strategy.what, strategy.how,
                 strategy.why, _j(strategy.evidence), strategy.confidence, strategy.confirmations,
                 strategy.rejections, strategy.status, strategy.supersedes, strategy.source,
                 strategy.created_at, strategy.updated_at),
            )
            conn.execute("DELETE FROM strategies_fts WHERE id = ?", (strategy.id,))
            conn.execute(
                "INSERT INTO strategies_fts (id, scope_key, what, how, why) VALUES (?, ?, ?, ?, ?)",
                (strategy.id, strategy.scope_key, strategy.what, strategy.how, strategy.why),
            )

    def get_strategy(self, strategy_id: str) -> Strategy | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
            return self._strategy_from_row(row) if row else None

    def list_strategies(self, scope_key: str | None = None, status: str | None = None) -> list[Strategy]:
        clauses, params = [], []
        if scope_key:
            clauses.append("scope_key = ?")
            params.append(scope_key)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._get_conn() as conn:
            rows = conn.execute(f"SELECT * FROM strategies {where} ORDER BY scope_key, id", params).fetchall()
            return [self._strategy_from_row(r) for r in rows]

    def search_strategies(self, query: str, limit: int = 20) -> list[Strategy]:
        import re as _re

        clean = " ".join(_re.findall(r"\w+", query))
        if not clean:
            return []
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT s.* FROM strategies s
                   WHERE s.id IN (SELECT id FROM strategies_fts WHERE strategies_fts MATCH ?)
                   ORDER BY s.confidence DESC LIMIT ?""",
                (clean, limit),
            ).fetchall()
            return [self._strategy_from_row(r) for r in rows]

    @staticmethod
    def _strategy_from_row(row: sqlite3.Row) -> Strategy:
        return Strategy(
            id=row["id"], version=row["version"], scope_key=row["scope_key"], what=row["what"],
            how=row["how"], why=row["why"], evidence=_u(row["evidence_json"], []),
            confidence=row["confidence"], confirmations=row["confirmations"],
            rejections=row["rejections"], status=row["status"], supersedes=row["supersedes"],
            source=row["source"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    # --- Proposals ---

    def insert_proposal(self, proposal: Proposal) -> int:
        with self._get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO proposals (kind, target, what, how, why, evidence_json,
                                          cost_estimate_usd, status, created_at, reviewed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (proposal.kind, proposal.target, proposal.what, proposal.how, proposal.why,
                 _j(proposal.evidence), proposal.cost_estimate_usd, proposal.status,
                 proposal.created_at, proposal.reviewed_at),
            )
            return int(cur.lastrowid)

    def get_proposal(self, proposal_id: int) -> Proposal | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
            return self._proposal_from_row(row) if row else None

    def list_proposals(self, status: str | None = "open", limit: int = 50) -> list[Proposal]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._get_conn() as conn:
            rows = conn.execute(f"SELECT * FROM proposals {where} ORDER BY id DESC LIMIT ?", params).fetchall()
            return [self._proposal_from_row(r) for r in rows]

    def set_proposal_status(self, proposal_id: int, status: str, reviewed_at: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE proposals SET status = ?, reviewed_at = ? WHERE id = ?",
                (status, reviewed_at, proposal_id),
            )

    @staticmethod
    def _proposal_from_row(row: sqlite3.Row) -> Proposal:
        return Proposal(
            id=row["id"], kind=row["kind"], target=row["target"], what=row["what"] or "",
            how=row["how"] or "", why=row["why"] or "", evidence=_u(row["evidence_json"], {}),
            cost_estimate_usd=row["cost_estimate_usd"], status=row["status"],
            created_at=row["created_at"], reviewed_at=row["reviewed_at"],
        )

    # --- Budget ---

    def get_budget(self, period: str, cap_usd: float | None = None, cap_calls: int | None = None) -> Budget:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM budget WHERE period = ?", (period,)).fetchone()
            if not row:
                env_cap = float(os.environ.get("SPRITEWARDEN_LEARN_MONTHLY_USD", "5.0"))
                conn.execute(
                    "INSERT OR IGNORE INTO budget (period, spend_usd, calls, cap_usd, cap_calls) VALUES (?, 0, 0, ?, ?)",
                    (period, cap_usd if cap_usd is not None else env_cap, cap_calls or 500),
                )
                return Budget(period=period, cap_usd=cap_usd if cap_usd is not None else env_cap, cap_calls=cap_calls or 500)
            return Budget(
                period=row["period"], spend_usd=row["spend_usd"], calls=row["calls"],
                cap_usd=row["cap_usd"], cap_calls=row["cap_calls"],
            )

    def add_spend(self, period: str, usd: float, calls: int = 1) -> None:
        self.get_budget(period)
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE budget SET spend_usd = spend_usd + ?, calls = calls + ? WHERE period = ?",
                (usd, calls, period),
            )

    # --- Tipshare export tracking ---

    def record_tip_export(self, strategy_id: str, topic: str, tip_ref: str | None, exported_at: str) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO tip_exports (strategy_id, topic, exported_at, tip_ref)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(strategy_id) DO UPDATE SET
                       topic=excluded.topic, exported_at=excluded.exported_at, tip_ref=excluded.tip_ref""",
                (strategy_id, topic, exported_at, tip_ref),
            )

    def is_tip_exported(self, strategy_id: str) -> bool:
        with self._get_conn() as conn:
            return conn.execute(
                "SELECT 1 FROM tip_exports WHERE strategy_id = ?", (strategy_id,)
            ).fetchone() is not None

    def list_tip_exports(self) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM tip_exports ORDER BY exported_at DESC").fetchall()
            return [dict(r) for r in rows]
