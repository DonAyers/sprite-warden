"""Pydantic models for the sprite-warden learning knowledge base."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

SourceKind = Literal[
    "llms_txt", "md", "openapi", "mcp_tools", "rss", "changelog", "html", "json_api", "file"
]
ChangeKind = Literal["added", "removed", "changed"]
ReviewState = Literal["new", "reviewed", "acknowledged", "dismissed"]
EpisodeOutcome = Literal["accepted", "rejected", "rerolled", "pending"]
ProposalKind = Literal[
    "review_doc_change",
    "change_param",
    "new_strategy",
    "update_strategy",
    "deprecate_strategy",
    "enable_provider",
    "data_quality",
]
ProposalStatus = Literal["open", "approved", "rejected"]
StrategyStatus = Literal["proposed", "active", "deprecated", "superseded"]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Source(BaseModel):
    """A watched documentation/API/tool surface for a provider."""

    id: str
    provider: str
    kind: SourceKind
    url: str
    cadence_s: int = 86400
    extract: str | None = None
    enabled: bool = True
    etag: str | None = None
    last_modified: str | None = None
    last_hash: str | None = None
    last_checked: str | None = None
    notes: str = ""


class Snapshot(BaseModel):
    """An append-only raw capture of a source (the anti-context-collapse evidence)."""

    id: int | None = None
    source_id: str
    fetched_at: str
    http_status: int
    content_hash: str
    body: str = ""
    extracted: dict[str, Any] = Field(default_factory=dict)


class DocEvent(BaseModel):
    """A detected change in a watched source, with deterministic What/How/Why."""

    id: int | None = None
    source_id: str
    provider: str = ""
    detected_at: str = Field(default_factory=utcnow)
    change_kind: ChangeKind = "changed"
    significance: float = 0.0
    signals: list[str] = Field(default_factory=list)
    diff_text: str = ""
    summary_what: str = ""
    summary_how: str = ""
    summary_why: str = ""
    model: str = "deterministic"
    review_state: ReviewState = "new"


class ProviderTool(BaseModel):
    """An observed capability/param surface of a provider tool or API operation."""

    id: int | None = None
    provider: str
    tool_name: str
    capability_json: str = "{}"
    params_json: str = "{}"
    limits_json: str = "{}"
    deprecations_json: str = "[]"
    observed_at: str = Field(default_factory=utcnow)
    source_event_id: int | None = None


class Episode(BaseModel):
    """A single generation/judge/refine attempt with its outcome — the reward signal."""

    id: int | None = None
    ts: str = Field(default_factory=utcnow)
    game: str = "global"
    asset_type: str = "sprite"
    perspective: str = "unknown"
    style_tags: list[str] = Field(default_factory=list)
    character_id: str | None = None
    provider: str = "unknown"
    operation: str = "unknown"
    params: dict[str, Any] = Field(default_factory=dict)
    prompt: str = ""
    gates: dict[str, Any] = Field(default_factory=dict)
    vision: dict[str, Any] = Field(default_factory=dict)
    outcome: EpisodeOutcome = "pending"
    reason: str = ""
    cost_usd: float | None = None
    duration_ms: int | None = None
    asset_id: str | None = None


class Strategy(BaseModel):
    """A distilled What/How/Why lesson scoped to a provider/operation/asset context."""

    id: str
    version: int = 1
    scope_key: str
    what: str
    how: str
    why: str
    evidence: list[int] = Field(default_factory=list)
    confidence: float = 0.0
    confirmations: int = 0
    rejections: int = 0
    status: StrategyStatus = "proposed"
    supersedes: str | None = None
    source: str = "curation"
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)


class Proposal(BaseModel):
    """A suggested change awaiting review; never auto-applied."""

    id: int | None = None
    kind: ProposalKind
    target: str
    what: str
    how: str
    why: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    cost_estimate_usd: float | None = None
    status: ProposalStatus = "open"
    created_at: str = Field(default_factory=utcnow)
    reviewed_at: str | None = None


class Budget(BaseModel):
    """Token/cost ledger for background LLM usage per billing period."""

    period: str
    spend_usd: float = 0.0
    calls: int = 0
    cap_usd: float = 5.0
    cap_calls: int = 500

    def allows(self, estimated_usd: float = 0.0) -> bool:
        return self.spend_usd + estimated_usd <= self.cap_usd and self.calls < self.cap_calls
