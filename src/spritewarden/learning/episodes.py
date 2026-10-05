"""Episode capture: record generation/judge/refine attempts as the learning reward signal."""

from __future__ import annotations

from typing import Any

from .db import LearningDB
from .models import Episode, utcnow
from .taxonomy import normalize_perspective


def scope_key(provider: str, operation: str, asset_type: str, perspective: str) -> str:
    """Stable join key linking episodes -> strategies -> router playbook rules."""
    return f"{provider}|{operation}|{asset_type}|{perspective}"


def record_episode(db: LearningDB, episode: Episode) -> int:
    """Persist an episode and return its id."""
    return db.insert_episode(episode)


def _gates_from_scorecard(scorecard: Any) -> dict[str, Any]:
    gates: dict[str, Any] = {}
    for g in getattr(scorecard, "gates", []) or []:
        gates[g.name] = {"passed": g.passed, "score": g.score, "summary": g.summary}
    return gates


def record_from_still(
    db: LearningDB,
    scorecard: Any,
    provider: str,
    operation: str,
    game: str = "global",
    asset_type: str = "sprite",
    perspective: str = "unknown",
    character_id: str | None = None,
    style_tags: list[str] | None = None,
    asset_id: str | None = None,
    cost_usd: float | None = None,
    verdict: Any | None = None,
) -> int:
    """Record a still/QA evaluation as an episode (accepted when all gates passed)."""
    norm = normalize_perspective(perspective)
    vision: dict[str, Any] = {}
    if verdict is not None and hasattr(verdict, "model_dump"):
        vision = verdict.model_dump()
    outcome = "accepted" if getattr(scorecard, "passed", False) else "rejected"
    episode = Episode(
        ts=utcnow(),
        game=game,
        asset_type=asset_type,
        perspective=norm.canonical or (perspective or "unknown"),
        style_tags=style_tags or [],
        character_id=character_id,
        provider=provider,
        operation=operation,
        params={},
        prompt=getattr(scorecard, "path", ""),
        gates=_gates_from_scorecard(scorecard),
        vision=vision,
        outcome=outcome,
        reason=getattr(scorecard, "summary", ""),
        cost_usd=cost_usd,
        asset_id=asset_id,
    )
    return record_episode(db, episode)


def record_from_refine(
    db: LearningDB,
    report: Any,
    game: str = "global",
    perspective: str = "unknown",
    style_tags: list[str] | None = None,
    asset_id: str | None = None,
) -> int:
    """Record a `pixelize --refine` tuning run as an episode."""
    norm = normalize_perspective(perspective)
    best = getattr(report, "best", None)
    verdict = getattr(report, "final_verdict", None) or {}
    action = (verdict or {}).get("recommended_action", "") if isinstance(verdict, dict) else ""
    if action == "accept":
        outcome = "accepted"
    elif action in ("reroll", "inpaint_part"):
        outcome = "rerolled"
    else:
        outcome = "pending"
    params = best.params.model_dump() if best is not None and hasattr(best, "params") else {}
    episode = Episode(
        ts=utcnow(),
        game=game,
        asset_type="sprite",
        perspective=norm.canonical or (perspective or "unknown"),
        style_tags=style_tags or [],
        provider="pixelize",
        operation="refine",
        params=params,
        prompt=getattr(report, "source", ""),
        gates=getattr(best, "det_metrics", {}) or {},
        vision=verdict if isinstance(verdict, dict) else {},
        outcome=outcome,
        reason=f"converged={getattr(report, 'converged', False)} uses_vision={getattr(report, 'used_vision', False)}",
        asset_id=asset_id,
    )
    return record_episode(db, episode)
