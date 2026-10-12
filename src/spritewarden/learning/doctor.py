"""Health/staleness report for the learning subsystem (`warden learn doctor`, MCP)."""

from __future__ import annotations

from datetime import datetime, timezone

from .db import LearningDB
from .diffing import REVIEW_THRESHOLD
from .summarize import current_period, llm_enabled


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def report(db: LearningDB | None = None) -> dict:
    """Summarize knowledge-base health and surface staleness/drift warnings."""
    db = db or LearningDB()
    now = datetime.now(timezone.utc)
    warnings: list[str] = []

    sources = db.list_sources()
    never_checked = [s.id for s in sources if not s.last_checked]
    overdue: list[str] = []
    for s in sources:
        last = _parse(s.last_checked)
        if last and (now - last).total_seconds() > s.cadence_s * 2:
            overdue.append(s.id)

    events = db.list_events(limit=10000)
    new_events = [e for e in events if e.review_state == "new"]
    significant = [e for e in new_events if e.significance >= REVIEW_THRESHOLD]

    episodes = db.list_episodes(limit=100000)
    outcomes: dict[str, int] = {}
    for e in episodes:
        outcomes[e.outcome] = outcomes.get(e.outcome, 0) + 1

    strategies = db.list_strategies()
    by_status: dict[str, int] = {}
    for s in strategies:
        by_status[s.status] = by_status.get(s.status, 0) + 1
    no_evidence = [s.id for s in strategies if s.status == "active" and not s.evidence]

    proposals = db.list_proposals(status="open")
    tools = db.list_tools()
    budget = db.get_budget(current_period())

    failed = [c for c in db.source_health() if not c["successful"]]
    if failed:
        warnings.append(f"{len(failed)} source(s) unreachable/failed; last good revision retained and potentially stale")
    if not episodes:
        warnings.append("zero episode capture: generation outcomes are unknown; verify event projection/capture")
    if not sources:
        warnings.append("no sources registered — run `warden learn seed`")
    if never_checked:
        warnings.append(f"{len(never_checked)} source(s) never checked")
    if overdue:
        warnings.append(f"{len(overdue)} source(s) overdue for a check")
    if significant:
        warnings.append(f"{len(significant)} significant unreviewed doc change(s)")
    if no_evidence:
        warnings.append(f"{len(no_evidence)} active strategy(ies) with no evidence")
    if budget.spend_usd >= budget.cap_usd:
        warnings.append("monthly LLM budget exhausted (background summaries paused)")

    return {
        "sources": {"total": len(sources), "never_checked": len(never_checked), "overdue": len(overdue), "failed": len(failed), "checks": db.source_health()},
        "events": {"total": len(events), "new": len(new_events), "significant_new": len(significant)},
        "episodes": {"total": len(episodes), "by_outcome": outcomes},
        "strategies": {"total": len(strategies), "by_status": by_status, "active_without_evidence": len(no_evidence)},
        "proposals": {"open": len(proposals)},
        "provider_tools": {"total": len(tools)},
        "budget": {"period": budget.period, "spend_usd": budget.spend_usd, "cap_usd": budget.cap_usd, "calls": budget.calls},
        "llm_enabled": llm_enabled(),
        "db_path": str(db.db_path),
        "warnings": warnings,
    }
