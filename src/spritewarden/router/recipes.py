"""Per-provider recipe registry: each provider's own documented rules as data.

The registry (``spritewarden/data/recipes.json``) holds one entry per
(provider, operation): start-image requirements, prompt rules, forbidden words,
argument limits, the gates that must have passed before the call and the gates
that must run after it. ``playbook.sanitize_prompt_for_provider`` and
``determine_fallback_recipe`` read it, as do the CLI (``warden recipes``) and
the MCP resource ``spritewarden://recipes``.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class StartImageRequirements(BaseModel):
    """What the input image to an operation must look like."""

    required: bool = False
    facing: str | None = None  # south | east | any
    pose: str | None = None
    canvas: str | None = None  # square | any
    margin_px: int | None = None
    min_px: int | None = None
    max_px: int | None = None
    multiple_of: int | None = None
    native_pixels: bool = True
    symmetric: bool | None = None
    transparent: bool | None = None
    notes: list[str] = Field(default_factory=list)


class Recipe(BaseModel):
    """One (provider, operation) entry."""

    provider: str
    operation: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    start_image: StartImageRequirements | None = None
    prompt_rules: list[str] = Field(default_factory=list)
    forbidden_words: list[str] = Field(default_factory=list, description="Regex patterns stripped from prompts")
    strip_dimensions: bool = False
    strip_counts: bool = False
    arg_limits: dict[str, Any] = Field(default_factory=dict)
    required_gates_before: list[str] = Field(default_factory=list)
    required_gates_after: list[str] = Field(default_factory=list)
    asset_id_params: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)

    def forbidden_patterns(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.forbidden_words]

    def rules_text(self) -> list[str]:
        """Prompt rules plus the start-image requirement as human-readable rules."""
        out = list(self.prompt_rules)
        si = self.start_image
        if si and si.required:
            bits = []
            if si.facing and si.facing != "any":
                bits.append(f"faces {si.facing}")
            if si.canvas and si.canvas != "any":
                bits.append(f"{si.canvas} canvas")
            if si.margin_px:
                bits.append(f">={si.margin_px}px transparent margin")
            if si.max_px:
                bits.append(f"<={si.max_px}px")
            if si.multiple_of:
                bits.append(f"side a multiple of {si.multiple_of}")
            if si.native_pixels:
                bits.append("native 1:1 pixels")
            if si.symmetric:
                bits.append("symmetric")
            if si.pose:
                bits.append(f"pose: {si.pose}")
            out.insert(0, "Start image: " + "; ".join(bits) + ".")
        return out


class ProviderRecipes(BaseModel):
    provider: str
    aliases: list[str] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)
    operations: dict[str, Recipe] = Field(default_factory=dict)

    def find(self, operation: str) -> Recipe | None:
        key = _norm(operation)
        for name, rec in self.operations.items():
            if _norm(name) == key or key in {_norm(a) for a in rec.aliases}:
                return rec
        return None


class RecipeRegistry(BaseModel):
    version: int = 1
    source_document: str = ""
    gate_names: list[str] = Field(default_factory=list)
    providers: dict[str, ProviderRecipes] = Field(default_factory=dict)

    def canonical_provider(self, provider: str) -> str | None:
        key = _norm(provider)
        for name, prov in self.providers.items():
            if _norm(name) == key or key in {_norm(a) for a in prov.aliases}:
                return name
        return None

    def provider(self, provider: str) -> ProviderRecipes | None:
        name = self.canonical_provider(provider)
        return self.providers[name] if name else None

    def get(self, provider: str, operation: str) -> Recipe | None:
        prov = self.provider(provider)
        return prov.find(operation) if prov else None

    def select(self, provider: str | None = None) -> list[Recipe]:
        if provider:
            prov = self.provider(provider)
            return list(prov.operations.values()) if prov else []
        return [r for p in self.providers.values() for r in p.operations.values()]

    def forbidden_patterns(self, provider: str, operation: str | None = None) -> list[re.Pattern[str]]:
        """Provider defaults plus the operation's own patterns (deduplicated, in order)."""
        prov = self.provider(provider)
        if not prov:
            return []
        pats: list[str] = list(prov.defaults.get("forbidden_words", []))
        rec = prov.find(operation) if operation else None
        if rec:
            pats += [p for p in rec.forbidden_words if p not in pats]
        return [re.compile(p, re.IGNORECASE) for p in pats]

    def strip_dimensions(self, provider: str, operation: str | None = None) -> bool:
        prov = self.provider(provider)
        if not prov:
            return False
        rec = prov.find(operation) if operation else None
        return bool(prov.defaults.get("strip_dimensions")) or bool(rec and rec.strip_dimensions)

    def strip_counts(self, provider: str, operation: str | None = None) -> bool:
        prov = self.provider(provider)
        if not prov:
            return False
        rec = prov.find(operation) if operation else None
        return bool(prov.defaults.get("strip_counts")) or bool(rec and rec.strip_counts)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def default_registry_path() -> Path:
    return Path(str(resources.files("spritewarden").joinpath("data", "recipes.json")))


def _build(raw: dict[str, Any]) -> RecipeRegistry:
    providers: dict[str, ProviderRecipes] = {}
    for pname, pdata in (raw.get("providers") or {}).items():
        defaults = dict(pdata.get("defaults") or {})
        ops: dict[str, Recipe] = {}
        for oname, odata in (pdata.get("operations") or {}).items():
            merged = dict(odata)
            merged.setdefault("provider", pname)
            merged.setdefault("operation", oname)
            # Provider-wide prompt rules and sources are inherited after the operation's own.
            merged["prompt_rules"] = list(odata.get("prompt_rules", [])) + [
                r for r in defaults.get("prompt_rules", []) if r not in odata.get("prompt_rules", [])
            ]
            merged["sources"] = list(odata.get("sources", [])) + [
                s for s in defaults.get("sources", []) if s not in odata.get("sources", [])
            ]
            merged.setdefault("strip_dimensions", bool(defaults.get("strip_dimensions", False)))
            merged.setdefault("strip_counts", bool(defaults.get("strip_counts", False)))
            ops[oname] = Recipe(**merged)
        providers[pname] = ProviderRecipes(
            provider=pname, aliases=list(pdata.get("aliases", [])), defaults=defaults, operations=ops
        )
    return RecipeRegistry(
        version=int(raw.get("version", 1)),
        source_document=str(raw.get("source_document", "")),
        gate_names=list(raw.get("gate_names", [])),
        providers=providers,
    )


def load_registry(path: str | Path | None = None) -> RecipeRegistry:
    """Load and validate the registry from JSON (default: the packaged recipes.json)."""
    p = Path(path) if path else default_registry_path()
    raw = json.loads(p.read_text(encoding="utf-8"))
    return _build(raw)


@lru_cache(maxsize=1)
def registry() -> RecipeRegistry:
    """The packaged registry, loaded once."""
    return load_registry()


def get_recipe(provider: str, operation: str) -> Recipe | None:
    return registry().get(provider, operation)


def list_recipes(provider: str | None = None) -> list[Recipe]:
    return registry().select(provider)


def registry_json(provider: str | None = None, operation: str | None = None) -> str:
    """JSON rendering for the MCP resource / CLI ``--json``."""
    reg = registry()
    if provider and operation:
        rec = reg.get(provider, operation)
        return json.dumps(rec.model_dump() if rec else {"error": f"unknown recipe {provider}/{operation}"}, indent=2)
    recs = reg.select(provider)
    return json.dumps(
        {
            "version": reg.version,
            "source_document": reg.source_document,
            "gate_names": reg.gate_names,
            "recipes": [r.model_dump() for r in recs],
        },
        indent=2,
    )
