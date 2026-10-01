"""Structured diagnostic models for slaygent quality assurance judges."""

from typing import Any
from pydantic import BaseModel, Field


class GateResult(BaseModel):
    """The result of a single verification gate."""
    name: str
    passed: bool
    score: float = Field(default=1.0, ge=0.0, le=1.0, description="Normalized score 0.0 to 1.0")
    summary: str
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class StillScorecard(BaseModel):
    """Overall quality evaluation for a still image or sprite frame."""
    path: str
    passed: bool
    score: float = Field(default=1.0, ge=0.0, le=1.0)
    summary: str
    gates: list[GateResult] = Field(default_factory=list)
    mask_paths: dict[str, str] = Field(default_factory=dict)

    def gate(self, name: str) -> GateResult | None:
        for g in self.gates:
            if g.name == name:
                return g
        return None


class AnimationScorecard(BaseModel):
    """Quality evaluation for an animation sequence or spritesheet."""
    path_or_count: str
    frame_count: int
    passed: bool
    score: float = Field(default=1.0, ge=0.0, le=1.0)
    summary: str
    gates: list[GateResult] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)

    def gate(self, name: str) -> GateResult | None:
        for g in self.gates:
            if g.name == name:
                return g
        return None
