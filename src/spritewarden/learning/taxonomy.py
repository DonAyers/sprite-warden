"""Canonical perspective/style taxonomy: normalize the messy community + provider terms."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

CANONICAL_PERSPECTIVES = ["top-down", "angled-top-down", "side-on", "isometric"]

PERSPECTIVE_SYNONYMS: dict[str, list[str]] = {
    "top-down": [
        "top-down", "top down", "topdown", "overhead", "overhead view",
        "birds-eye", "birds eye", "birdseye", "bird's-eye", "bird's eye",
        "plan view", "flat top-down",
    ],
    "angled-top-down": [
        "angled top-down", "angled top down", "angled", "three-quarter",
        "three quarter", "three-quarter view", "3/4 view", "oblique", "dimetric",
        "high-angle", "high angle", "2.5d angled",
    ],
    "side-on": [
        "side-on", "side on", "side view", "side", "lateral", "side-scrolling",
        "side scrolling", "side-scroller", "profile",
    ],
    "isometric": [
        "isometric", "iso", "true isometric", "2.5d", "2.5d iso", "isometric 2.5d",
    ],
}

_AMBIGUOUS_TERMS = {"three-quarter", "three quarter", "three-quarter view", "2.5d"}

STYLE_AXES = {
    "palette": ["luap-40", "sweetie-16", "na16", "gameboy", "monochrome", "custom"],
    "outline": ["none", "1px", "hollow", "selective"],
    "lighting": ["flat", "ramp", "lightmap", "dither"],
    "era": ["8-bit", "16-bit", "32-bit", "modern"],
}


class PerspectiveResult(BaseModel):
    """Outcome of normalizing a perspective phrase to the canonical set."""

    raw: str
    canonical: str | None = None
    ambiguous: bool = False
    matched: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.canonical is not None and not self.ambiguous


def _slug(text: str) -> str:
    text = text.strip().lower().replace("'", "").replace("\u2019", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _matches(slug: str, synonym: str) -> bool:
    syn = _slug(synonym)
    if not syn:
        return False
    if syn == slug:
        return True
    in_tokens = set(slug.split())
    return all(tok in in_tokens for tok in syn.split())


def normalize_perspective(term: str, provider: str | None = None) -> PerspectiveResult:
    """Map a raw perspective phrase onto a canonical value; flag ambiguity rather than guess."""
    raw = (term or "").strip()
    slug = _slug(raw)
    if not slug:
        return PerspectiveResult(raw=raw)
    matched = [c for c, syns in PERSPECTIVE_SYNONYMS.items() if any(_matches(slug, s) for s in syns)]
    if not matched:
        return PerspectiveResult(raw=raw)
    ambiguous = len(matched) > 1 or slug in {_slug(t) for t in _AMBIGUOUS_TERMS}
    return PerspectiveResult(raw=raw, canonical=matched[0], ambiguous=ambiguous, matched=matched)


def normalize_style(tags: list[str]) -> list[str]:
    """Lowercase, de-duplicate, and de-slug style tags into a stable ordering."""
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags or []:
        t = _slug(tag)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out
