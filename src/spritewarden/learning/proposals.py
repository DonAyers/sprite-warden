"""Proposal review/apply: the suggest-don't-apply gate (nothing mutates state until approved)."""

from __future__ import annotations

from .db import LearningDB
from .models import Proposal, utcnow


def create_proposal(db: LearningDB, proposal: Proposal) -> int | None:
    """Insert a proposal, skipping if an identical open one already exists."""
    for existing in db.list_proposals(status="open"):
        if existing.kind == proposal.kind and existing.target == proposal.target:
            return None
    return db.insert_proposal(proposal)


def review(db: LearningDB, proposal_id: int, approve: bool) -> Proposal | None:
    """Approve or reject a proposal; only approval applies the change to the registry."""
    proposal = db.get_proposal(proposal_id)
    if proposal is None:
        return None
    if proposal.status != "open":
        return proposal
    status = "approved" if approve else "rejected"
    db.set_proposal_status(proposal_id, status, utcnow())
    proposal.status = status
    proposal.reviewed_at = utcnow()
    if approve:
        _apply(db, proposal)
    return proposal


def _apply(db: LearningDB, proposal: Proposal) -> None:
    """Apply an approved proposal to strategies / doc-event review state."""
    if proposal.kind in ("new_strategy", "update_strategy"):
        strategy = db.get_strategy(proposal.target)
        if strategy:
            strategy.status = "active"
            strategy.confirmations += 1
            strategy.updated_at = utcnow()
            db.upsert_strategy(strategy)
    elif proposal.kind == "deprecate_strategy":
        strategy = db.get_strategy(proposal.target)
        if strategy:
            strategy.status = "deprecated"
            strategy.updated_at = utcnow()
            db.upsert_strategy(strategy)
    elif proposal.kind == "review_doc_change":
        event_id = proposal.evidence.get("event_id") if isinstance(proposal.evidence, dict) else None
        if isinstance(event_id, int):
            db.set_event_review(event_id, "acknowledged")
            event = next((e for e in db.list_events(limit=100000) if e.id == event_id), None)
            create_proposal(db, Proposal(kind="change_param", target=f"doc-update:{event_id}",
                what="Proposed contract/test/documentation update after doc review",
                how="Review the attached diff against affected operation contracts; implement and test a bounded update before publishing a replacement tip.",
                why="Acknowledging source drift is not permission to silently rewrite executable policy",
                evidence={"event_id": event_id, "source_id": event.source_id if event else None,
                          "diff": event.diff_text if event else "", "automatic_application": False,
                          "evidence_state": "reviewed_source_pending_contract_verification",
                          "proposed_targets": ["data/recipes.json", "provider serializer tests", "operation documentation", "versioned recipe tips"]}))
