"""Tipshare bridge: export active strategies as tips and map outcomes back.

Tipshare is an MCP knowledge base, so this produces ready-to-publish payloads
(for `share_tip`) and records what has been exported; `mark_outcome` payloads
are generated from strategy confirmations/rejections.
"""

from __future__ import annotations

from .db import LearningDB
from .models import utcnow


def export_tips(
    db: LearningDB,
    topic: str = "godot-pixel-art",
    mark: bool = False,
    include_exported: bool = False,
) -> list[dict]:
    """Build `share_tip` payloads for active strategies, optionally marking them exported."""
    payloads: list[dict] = []
    for strategy in db.list_strategies(status="active"):
        if not include_exported and db.is_tip_exported(strategy.id):
            continue
        payloads.append(
            {
                "topic_slug": topic,
                "title": strategy.what,
                "summary": strategy.why,
                "problem": f"Scope: {strategy.scope_key} (confidence {strategy.confidence:.2f})",
                "solution": strategy.how,
                "gotchas": [],
                "artifact_code": "",
                "author": "spritewarden",
            }
        )
        if mark:
            db.record_tip_export(strategy.id, topic, None, utcnow())
    return payloads


def outcome_payload(tip_id: int, worked: bool, note: str = "") -> dict:
    """Build a `mark_outcome` payload for verifying an exported strategy tip."""
    return {"tip_id": tip_id, "worked": worked, "note": note, "author": "spritewarden"}
