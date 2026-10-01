"""Tipshare Learning Adapter: feeds proven recipes and gotchas into shared agent memory."""

import json
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field

from ..judge.models import StillScorecard, AnimationScorecard


class TipshareAdapter:
    """Interacts with the local Tipshare knowledge base to query and record generation lessons."""

    def __init__(self, topic_slug: str = "godot-pixel-art"):
        self.topic_slug = topic_slug

    def record_success_tip(
        self,
        title: str,
        problem: str,
        solution: str,
        summary: str,
        gotchas: list[str] | None = None,
        artifact_code: str | None = None,
        author: str = "spritewarden",
    ) -> dict[str, Any]:
        """Format a verified tip payload ready for Tipshare publishing."""
        return {
            "topic_slug": self.topic_slug,
            "title": title,
            "summary": summary,
            "problem": problem,
            "solution": solution,
            "gotchas": gotchas or [],
            "artifact_code": artifact_code or "",
            "author": author,
        }

    def format_evaluation_summary(
        self,
        task: str,
        provider: str,
        scorecard: StillScorecard | AnimationScorecard,
    ) -> str:
        """Create a concise summary of the QA validation outcome."""
        status = "PASSED" if scorecard.passed else "FAILED"
        gates_summary = ", ".join(f"{g.name}: {'ok' if g.passed else 'FAIL'}" for g in scorecard.gates)
        return f"[{status}] {task} via {provider} (Score: {scorecard.score}) -> {gates_summary}"
