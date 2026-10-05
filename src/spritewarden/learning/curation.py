"""Curation: aggregate episodes into strategy clusters and derive reviewable proposals."""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .db import DEFAULT_DB_PATH, LearningDB
from .diffing import REVIEW_THRESHOLD
from .models import Proposal, Strategy, utcnow
from .proposals import create_proposal
from .summarize import reflect_cluster


@dataclass
class ClusterStats:
    """Aggregated outcome statistics for one (provider, operation, asset_type, perspective) scope."""

    scope_key: str
    provider: str
    operation: str
    asset_type: str
    perspective: str
    n: int = 0
    accepted: int = 0
    rejected: int = 0
    rerolled: int = 0
    pending: int = 0
    success_rate: float = 0.0
    avg_gate_score: float | None = None
    common_reasons: list[str] = field(default_factory=list)
    avg_cost: float | None = None
    episode_ids: list[int] = field(default_factory=list)


def scope_key(provider: str, operation: str, asset_type: str, perspective: str) -> str:
    return f"{provider}|{operation}|{asset_type}|{perspective}"


def strategy_id_for(scope_key_value: str) -> str:
    return "learn-" + hashlib.sha1(scope_key_value.encode("utf-8")).hexdigest()[:12]


def _avg_gate_score(episodes: list) -> float | None:
    scores: list[float] = []
    for e in episodes:
        for value in (e.gates or {}).values():
            if isinstance(value, dict) and isinstance(value.get("score"), (int, float)):
                scores.append(float(value["score"]))
    return round(sum(scores) / len(scores), 3) if scores else None


def aggregate(db: LearningDB, min_evidence: int = 1, limit: int = 5000) -> list[ClusterStats]:
    """Group episodes into scope clusters with outcome stats (deterministic)."""
    episodes = db.list_episodes(limit=limit)
    groups: dict[str, list] = {}
    for e in episodes:
        groups.setdefault(scope_key(e.provider, e.operation, e.asset_type, e.perspective), []).append(e)

    clusters: list[ClusterStats] = []
    for key, eps in groups.items():
        if len(eps) < min_evidence:
            continue
        counts = Counter(e.outcome for e in eps)
        decided = counts["accepted"] + counts["rejected"] + counts["rerolled"]
        reasons = Counter(r for r in (e.reason for e in eps) if r)
        costs = [e.cost_usd for e in eps if e.cost_usd is not None]
        clusters.append(
            ClusterStats(
                scope_key=key,
                provider=eps[0].provider,
                operation=eps[0].operation,
                asset_type=eps[0].asset_type,
                perspective=eps[0].perspective,
                n=len(eps),
                accepted=counts["accepted"],
                rejected=counts["rejected"],
                rerolled=counts["rerolled"],
                pending=counts["pending"],
                success_rate=round(counts["accepted"] / decided, 3) if decided else 0.0,
                avg_gate_score=_avg_gate_score(eps),
                common_reasons=[r for r, _ in reasons.most_common(3)],
                avg_cost=round(sum(costs) / len(costs), 4) if costs else None,
                episode_ids=[e.id for e in eps if e.id is not None],
            )
        )
    return sorted(clusters, key=lambda c: c.n, reverse=True)


def derive(
    db: LearningDB,
    min_evidence: int = 3,
    use_llm: bool = False,
    api_key: str | None = None,
) -> list[Proposal]:
    """Turn episode clusters into proposed strategies + proposals (nothing auto-applied)."""
    created: list[Proposal] = []
    for stats in aggregate(db, min_evidence=min_evidence):
        sid = strategy_id_for(stats.scope_key)
        existing = db.get_strategy(sid)
        what, how, why = reflect_cluster(
            stats,
            episodes=[e for e in db.list_episodes(limit=5000) if e.id in set(stats.episode_ids)],
            existing=existing,
            db=db if use_llm else None,
            api_key=api_key,
        )
        confidence = round(min(1.0, abs(stats.success_rate - 0.5) * 2 + 0.3), 3)
        strategy = Strategy(
            id=sid,
            version=(existing.version + 1) if existing else 1,
            scope_key=stats.scope_key,
            what=what,
            how=how,
            why=why,
            evidence=stats.episode_ids,
            confidence=confidence,
            status="proposed",
            source="curation" if not use_llm else "curation+llm",
            created_at=existing.created_at if existing else utcnow(),
            updated_at=utcnow(),
        )
        db.upsert_strategy(strategy)
        if stats.success_rate <= 0.3:
            kind = "deprecate_strategy"
        else:
            kind = "update_strategy" if existing else "new_strategy"
        proposal = Proposal(
            kind=kind,
            target=sid,
            what=what,
            how=how,
            why=why,
            evidence={
                "scope_key": stats.scope_key,
                "n": stats.n,
                "success_rate": stats.success_rate,
                "common_reasons": stats.common_reasons,
                "episode_ids": stats.episode_ids,
            },
        )
        pid = create_proposal(db, proposal)
        if pid is not None:
            proposal.id = pid
            created.append(proposal)
    return created


def active_rules_for(scope_key_value: str, db: LearningDB | None = None) -> list[str]:
    """Return router-consumable rules from approved (active) strategies for a scope."""
    if db is None:
        path = Path(os.environ.get("SPRITEWARDEN_LEARNING_DB") or DEFAULT_DB_PATH)
        if not path.exists():
            return []
        db = LearningDB(path)
    return [f"[learned] {s.how}" for s in db.list_strategies(scope_key=scope_key_value, status="active")]


def proposals_from_events(db: LearningDB, min_significance: float = REVIEW_THRESHOLD) -> list[Proposal]:
    """Create reviewable proposals for significant new doc/tool changes (idempotent)."""
    created: list[Proposal] = []
    for event in db.list_events(limit=1000, review_state="new"):
        if event.significance < min_significance or event.id is None:
            continue
        proposal = Proposal(
            kind="review_doc_change",
            target=f"doc_event:{event.id}",
            what=event.summary_what,
            how=event.summary_how or "Review the changed provider surface and update strategies if relevant.",
            why=event.summary_why,
            evidence={
                "event_id": event.id,
                "source_id": event.source_id,
                "provider": event.provider,
                "signals": event.signals,
                "significance": event.significance,
            },
        )
        pid = create_proposal(db, proposal)
        if pid is not None:
            proposal.id = pid
            created.append(proposal)
    return created
