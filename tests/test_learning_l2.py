"""Tests for the spritewarden.learning L2 (episodes, curation, proposals, router)."""

from types import SimpleNamespace
from pathlib import Path

import pytest

from spritewarden.judge.models import GateResult, StillScorecard
from spritewarden.learning.curation import active_rules_for, aggregate, derive, scope_key, strategy_id_for
from spritewarden.learning.db import LearningDB
from spritewarden.learning.episodes import record_from_refine, record_from_still
from spritewarden.learning.models import Episode, Proposal, Strategy
from spritewarden.learning.proposals import create_proposal, review
from spritewarden.learning.summarize import llm_enabled, reflect_cluster, summarize_doc_event


@pytest.fixture(autouse=True)
def _deterministic_llm(monkeypatch):
    monkeypatch.setenv("SPRITEWARDEN_LEARN_LLM", "off")


def _still(passed: bool, score: float = 0.8) -> StillScorecard:
    return StillScorecard(
        path="x.png",
        passed=passed,
        score=score,
        summary="ok" if passed else "palette_adherence failed",
        gates=[
            GateResult(name="palette_adherence", passed=passed, score=score, summary="s"),
            GateResult(name="clipping", passed=True, score=1.0, summary="s"),
        ],
    )


def test_record_from_still_maps_outcome_and_gates(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    eid = record_from_still(db, _still(True), provider="pixellab", operation="create_character_v3",
                            perspective="top-down", asset_type="character")
    ep = db.list_episodes()[0]
    assert ep.id == eid and ep.outcome == "accepted"
    assert ep.perspective == "top-down"
    assert ep.gates["palette_adherence"]["passed"] is True


def test_record_from_refine(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    from spritewarden.reducer.core import ReduceParams

    report = SimpleNamespace(
        source="in.png", used_vision=True, converged=True,
        best=SimpleNamespace(params=ReduceParams(width=48), det_metrics={"score": 0.82}),
        final_verdict={"recommended_action": "accept", "production_ready_prob": 0.9},
    )
    record_from_refine(db, report)
    ep = db.list_episodes()[0]
    assert ep.provider == "pixelize" and ep.operation == "refine"
    assert ep.outcome == "accepted" and ep.params["width"] == 48


def test_aggregate_and_derive_strategy(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    for _ in range(4):
        record_from_still(db, _still(True), provider="pixellab", operation="create_character_v3",
                          perspective="top-down", asset_type="character")
    clusters = aggregate(db, min_evidence=3)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.n == 4 and c.success_rate == 1.0
    assert c.avg_gate_score is not None

    proposals = derive(db, min_evidence=3)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.kind == "new_strategy"
    strat = db.get_strategy(p.target)
    assert strat.status == "proposed"
    assert strat.evidence and strat.confidence > 0


def test_derive_low_success_proposes_deprecation(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    for _ in range(3):
        record_from_still(db, _still(False, 0.2), provider="sprite_fusion", operation="direction-set",
                          perspective="side-on", asset_type="vehicle")
    proposals = derive(db, min_evidence=3)
    assert proposals and proposals[0].kind == "deprecate_strategy"


def test_min_evidence_respected(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    record_from_still(db, _still(True), provider="p", operation="o", perspective="top-down")
    assert derive(db, min_evidence=3) == []


def test_approve_activates_strategy_and_router_rule(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    for _ in range(3):
        record_from_still(db, _still(True), provider="pixellab", operation="create_character_v3",
                          perspective="top-down", asset_type="character")
    proposals = derive(db, min_evidence=3)
    pid = proposals[0].id
    applied = review(db, pid, approve=True)
    assert applied.status == "approved"
    strat = db.get_strategy(proposals[0].target)
    assert strat.status == "active"

    key = scope_key("pixellab", "create_character_v3", "character", "top-down")
    assert active_rules_for(key, db=db) == [f"[learned] {strat.how}"]


def test_reject_does_not_activate(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    for _ in range(3):
        record_from_still(db, _still(True), provider="p", operation="o", perspective="top-down")
    proposals = derive(db, min_evidence=3)
    review(db, proposals[0].id, approve=False)
    assert db.get_strategy(proposals[0].target).status == "proposed"


def test_create_proposal_dedupes_open(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    p = Proposal(kind="new_strategy", target="t1", what="w", how="h", why="y")
    assert create_proposal(db, p) is not None
    assert create_proposal(db, Proposal(kind="new_strategy", target="t1", what="w2", how="h2", why="y2")) is None
    assert len(db.list_proposals(status="open")) == 1


def test_summarize_deterministic_fallback(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    assert llm_enabled(api_key="") is False
    source = SimpleNamespace(provider="acme", id="acme_docs", kind="md")
    event = SimpleNamespace(summary_what="w", summary_how="h", summary_why="y", signals=[], significance=0.5, diff_text="")
    what, how, why, cost = summarize_doc_event(event, source, db)
    assert (what, how, why, cost) == ("w", "h", "y", None)


def test_reflect_cluster_deterministic(tmp_path: Path):
    db = LearningDB(tmp_path / "l.db")
    stats = SimpleNamespace(
        provider="pixellab", operation="walk", asset_type="character", perspective="top-down",
        n=5, success_rate=0.8, avg_gate_score=0.9, common_reasons=["off-palette"],
    )
    what, how, why = reflect_cluster(stats, [], existing=None, db=None)
    assert "prefer" in what and "80%" in how


def test_router_attaches_learned_rules(tmp_path: Path, monkeypatch):
    from spritewarden.router import PipelineRouter
    from spritewarden.repo.catalog import AssetCatalog

    learning_db = tmp_path / "l.db"
    db = LearningDB(learning_db)
    for _ in range(3):
        record_from_still(db, _still(True), provider="pixellab", operation="create_character_v3",
                          perspective="top-down", asset_type="character")
    proposals = derive(db, min_evidence=3)
    review(db, proposals[0].id, approve=True)

    monkeypatch.setenv("SPRITEWARDEN_LEARNING_DB", str(learning_db))
    router = PipelineRouter(catalog=AssetCatalog(tmp_path / "catalog.db"))
    result = router.route_request(prompt="a fresh robot", asset_type="character", perspective="low top-down")
    assert result.recipe is not None
    assert any(r.startswith("[learned]") for r in result.recipe.rules)
