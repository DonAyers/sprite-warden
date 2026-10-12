"""Tipshare bridge: export active strategies as tips and map outcomes back.

Tipshare is an MCP knowledge base, so this produces ready-to-publish payloads
(for `share_tip`) and records what has been exported; `mark_outcome` payloads
are generated from strategy confirmations/rejections.
"""

from __future__ import annotations

from .db import LearningDB



def export_tips(
    db: LearningDB,
    topic: str = "godot-pixel-art",
    mark: bool = False,
    include_exported: bool = False,
) -> list[dict]:
    """Build payloads for active strategies. Blind marking is deliberately refused."""
    if mark:
        raise ValueError("Blind marking disabled: use publish with a verified get_tip receipt")
    payloads: list[dict] = []
    for strategy in db.list_strategies(status="active"):
        if not include_exported and db.is_tip_exported(strategy.id):
            continue
        payloads.append(
            {
                "export_key": strategy.id,
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
    return payloads


def _digest(value):
    import hashlib
    import json
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sync_outcomes(db, data=None, *, dry_run=True, command=None, session_factory=None):
    """Ingest actual exported outcomes or read exported tips through MCP; never infer votes.

    Outcomes are review evidence, not automatic strategy rewards. Each changed revision
    creates a new proposal; older revisions cannot erase newer corrections.
    """
    if data is None:
        from .recipe_tips import tipshare_command, TIPSHARE_AGENT_ID
        from .mcp_probe import StdioSession
        command = command or tipshare_command()
        if not command:
            raise ValueError("Provide a tipshare dump or SPRITEWARDEN_TIPSHARE_CMD")
        factory = session_factory or (lambda cmd: StdioSession(cmd, env={"TIPSHARE_AGENT_ID": TIPSHARE_AGENT_ID}))
        data = {"outcomes": []}
        with db._get_conn() as conn:
            refs = [r[0] for r in conn.execute("SELECT DISTINCT tip_ref FROM tip_exports WHERE tip_ref IS NOT NULL")]
        with factory(command) as session:
            for ref in refs:
                tip = session.call_tool("get_tip", {"tip_id": ref})
                for outcome in tip.get("outcomes", []):
                    data["outcomes"].append(dict(outcome, tip_uuid=tip["uuid"]))
    from .models import Proposal
    proposals = db.list_proposals(limit=100000)
    changed = []
    for outcome in data.get("outcomes", []):
        if not outcome.get("uuid") or not outcome.get("tip_uuid"):
            raise ValueError("Outcome requires UUID and tip_uuid provenance")
        key = "tipshare-outcome:" + outcome["uuid"]
        prior = [p for p in proposals if p.evidence.get("sync_key") == key]
        revision = int(outcome.get("revision", 1))
        if prior and max(int(p.evidence["outcome"].get("revision", 1)) for p in prior) > revision:
            continue
        digest = _digest(outcome)
        if any(p.evidence.get("content_hash") == digest for p in prior):
            continue
        p = Proposal(kind="data_quality", target=key + ":" + digest,
                     what="Review Tipshare outcome correction", how=outcome.get("note", ""),
                     why="External observation; not an independently verified generation outcome",
                     evidence={"sync_key": key, "content_hash": digest, "outcome": outcome,
                               "provenance": "tipshare", "evidence_state": "reported", "automatic_application": False})
        if not dry_run:
            p.id = db.insert_proposal(p)
        proposals.append(p)
        changed.append(p.model_dump())
    return {"dry_run": dry_run, "changed": len(changed), "proposals": changed}


def curated_corrections():
    """Audit-derived proposals, not claims of live provider verification or published tips."""
    evidence = {"source": "sprite-warden-audit-2026-10-11.md", "section": "5", "retrieval": "local audit; no live verification", "review_required": True}
    rows = [
        (34, "scoped_correction", "Replace inline start/end frame base64 guidance with current registry URL fields; retain mid-stride staging only as a scoped observation.", "Preserve the negative outcome: prior success was already mid-stride, not general walking.", "Not a retraction of the observed charge; retract general applicability."),
        (31, "upstream_conflict", "Preserve staging advice; remove blanket animate-size instructions and check the exact operation contract.", "Current local registry/#56 disagree with older advice. Host/transport/version must be established.", "Retract executable generalization, not staging observation."),
        (40, "upstream_conflict", "Preserve edit staging advice; do not send edit --size under the local contract.", "Sprite Fusion operation page and API reference disagree about edit size; unresolved upstream documentation conflict.", "Retract universal size prescription; no claim of live endpoint verification."),
        (60, "causal_hypothesis_unproven", "Keep source-relative transparency-hole checks; describe matte collision as an unproven hypothesis, not established cause.", "Issue log confirms provider-origin holes but not matte causation. Exact pixel comparisons later retracted dramatic texture/shoulder diagnoses.", "Downgrade causal hypothesis; explicitly retract disproven texture-loss interpretation, not observed holes."),
        (63, "coverage_limited", "Bound enforcement assurances to tested interfaces and operations; document MCP/dispatch/classification bypasses and require regression evidence.", "Audit reproduced paths that escaped enforcement; local hooks do not govern arbitrary scripts/other hosts.", "Retract blanket coverage assurance; do not retract working checks."),
    ]
    return [dict(tip_id=i, evidence_state=state, correction=correction, observation=observation,
                 retraction=retraction, provenance=evidence, applicability={"host": "audited local checkout", "provider_version": "unknown"},
                 status="proposed", publish=False) for i,state,correction,observation,retraction in rows]


def import_corrections(db, corrections, *, dry_run=True):
    from .models import Proposal
    existing = {p.target for p in db.list_proposals(limit=100000)}
    changed=[]
    for correction in corrections:
        if not correction.get("provenance") or not correction.get("correction"):
            raise ValueError("Corrections need provenance and proposed correction text")
        target = "tipshare-correction:" + str(correction.get("tip_uuid") or correction.get("tip_id")) + ":" + _digest(correction)
        if target in existing:
            continue
        p=Proposal(kind="data_quality",target=target,what="Review tip correction",how=correction["correction"],
                   why=correction.get("retraction", "Preserve source evidence and applicability"),evidence=correction)
        if not dry_run:
            p.id=db.insert_proposal(p)
        existing.add(target)
        changed.append(p.model_dump())
    return {"dry_run":dry_run,"changed":len(changed),"proposals":changed}


def outcome_payload(tip_id: int, worked: bool, note: str = "") -> dict:
    """Build a `mark_outcome` payload for verifying an exported strategy tip."""
    return {"tip_id": tip_id, "worked": worked, "note": note, "author": "spritewarden"}
