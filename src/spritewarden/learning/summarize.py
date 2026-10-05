"""Optional, budget-capped LLM summarization/reflection with deterministic fallbacks.

Disabled by default (`SPRITEWARDEN_LEARN_LLM` off) — the loop is deterministic-first.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from .db import LearningDB

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_LEARN_MODEL = os.environ.get("SPRITEWARDEN_LEARN_MODEL", "google/gemini-2.5-flash-lite")
LLM_ON_VALUES = {"1", "on", "true", "yes"}


def llm_enabled(api_key: str | None = None) -> bool:
    if os.environ.get("SPRITEWARDEN_LEARN_LLM", "off").lower() not in LLM_ON_VALUES:
        return False
    if api_key:
        return True
    from ..router.jev import _get_openrouter_key

    return bool(_get_openrouter_key())


def current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _chat(model: str, system: str, user: str, api_key: str, timeout: float = 45.0) -> tuple[dict[str, Any] | None, float | None]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                OPENROUTER_CHAT_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code != 200:
                return None, None
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            cost = (data.get("usage") or {}).get("cost")
            start, end = content.find("{"), content.rfind("}")
            if start == -1 or end <= start:
                return None, cost
            return json.loads(content[start:end + 1]), cost
    except Exception:
        return None, None


def summarize_doc_event(
    event: Any,
    source: Any,
    db: LearningDB,
    api_key: str | None = None,
    model: str | None = None,
) -> tuple[str, str, str, float | None]:
    """Refine a doc_event's What/How/Why via a tiny model (budget-capped) or keep deterministic."""
    fallback = (event.summary_what, event.summary_how, event.summary_why, None)
    if not llm_enabled(api_key):
        return fallback
    from ..router.jev import _get_openrouter_key

    key = api_key or _get_openrouter_key()
    budget = db.get_budget(current_period())
    if not budget.allows(0.01):
        return fallback

    system = (
        "You summarize a detected change in a pixel-art generation provider's docs/API for a coding agent. "
        "Return ONLY JSON: {\"what\": short, \"how\": concrete action for the agent, \"why\": impact}. "
        "Be terse (one sentence each). Do not invent facts not present in the diff."
    )
    user = (
        f"Provider: {source.provider}\nSource: {source.id} ({source.kind})\n"
        f"Signals: {', '.join(event.signals)}\nSignificance: {event.significance}\n\nDiff:\n{event.diff_text[:6000]}"
    )
    parsed, cost = _chat(model or DEFAULT_LEARN_MODEL, system, user, key)
    db.add_spend(current_period(), cost or 0.0, calls=1)
    if not parsed:
        return fallback
    return (
        str(parsed.get("what", fallback[0])),
        str(parsed.get("how", fallback[1])),
        str(parsed.get("why", fallback[2])),
        cost,
    )


def reflect_cluster(
    stats: Any,
    episodes: list[Any],
    existing: Any | None = None,
    db: LearningDB | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> tuple[str, str, str]:
    """Distill a cluster of episodes into a What/How/Why strategy (GEPA-style, small evidence)."""
    verdict = "prefer" if stats.success_rate >= 0.7 else ("avoid" if stats.success_rate <= 0.3 else "mixed")
    what = f"{verdict} {stats.provider}:{stats.operation} for {stats.asset_type}/{stats.perspective}"
    reason = "; ".join(stats.common_reasons[:3]) if stats.common_reasons else "no dominant failure reason"
    how = (
        f"n={stats.n}, success={stats.success_rate:.0%}"
        + (f", avg gate score={stats.avg_gate_score:.2f}" if stats.avg_gate_score is not None else "")
        + (f". Evidence reasons: {reason}." if stats.common_reasons else ".")
    )
    why = (
        f"Outcome distribution over {stats.n} attempts: "
        f"{getattr(stats, 'accepted', 0)} accepted, {getattr(stats, 'rejected', 0)} rejected, "
        f"{getattr(stats, 'rerolled', 0)} rerolled."
    )

    if not llm_enabled(api_key) or db is None:
        return what, how, why
    from ..router.jev import _get_openrouter_key

    key = api_key or _get_openrouter_key()
    budget = db.get_budget(current_period())
    if not budget.allows(0.01):
        return what, how, why

    system = (
        "You are a pixel-art pipeline strategist. Given outcome statistics and a few generation attempts, "
        "write a durable, provider-specific strategy. Return ONLY JSON "
        "{\"what\": imperative title, \"how\": concrete steps/rules, \"why\": evidence-based rationale}. "
        "Do not invent providers or parameters not present."
    )
    sample = [
        {"provider": e.provider, "operation": e.operation, "params": e.params, "outcome": e.outcome, "reason": e.reason}
        for e in episodes[:8]
    ]
    user = json.dumps(
        {
            "provider": stats.provider, "operation": stats.operation, "asset_type": stats.asset_type,
            "perspective": stats.perspective, "n": stats.n, "success_rate": stats.success_rate,
            "common_reasons": stats.common_reasons, "episodes": sample,
            "existing_strategy": existing.how if existing else None,
        },
        indent=2,
    )
    parsed, cost = _chat(model or DEFAULT_LEARN_MODEL, system, user, key)
    db.add_spend(current_period(), cost or 0.0, calls=1)
    if not parsed:
        return what, how, why
    return str(parsed.get("what", what)), str(parsed.get("how", how)), str(parsed.get("why", why))
