"""Deterministic change detection: hashing, diffs, structural JSON diff, significance."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from typing import Any

KEYWORD_SIGNALS: list[tuple[str, str, float]] = [
    (r"deprecat", "deprecation", 0.40),
    (r"\bbreak", "breaking_change", 0.50),
    (r"remov", "removal", 0.40),
    (r"\bnew\b", "new_capability", 0.25),
    (r"add(ed|s)?\b", "addition", 0.20),
    (r"limit", "limit_change", 0.30),
    (r"default", "default_change", 0.30),
    (r"param", "param_change", 0.25),
    (r"required", "required_change", 0.30),
    (r"version", "version_bump", 0.20),
    (r"pric|cost|credit|billing", "pricing_change", 0.40),
    (r"\bmodel\b", "model_change", 0.25),
    (r"endpoint|\bapi\b", "api_change", 0.30),
]

REVIEW_THRESHOLD = 0.35


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _norm(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def diff_text(old: str, new: str, name: str = "source") -> str:
    old_lines = _norm(old).splitlines(keepends=True)
    new_lines = _norm(new).splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(old_lines, new_lines, fromfile=f"{name}@old", tofile=f"{name}@new", n=2)
    )


def _added_lines(old: str, new: str) -> list[str]:
    old_lines = _norm(old).splitlines()
    new_lines = _norm(new).splitlines()
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines)
    added: list[str] = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(new_lines[j1:j2])
    return added


def json_structural_diff(old: Any, new: Any, path: str = "") -> list[str]:
    """Recursively diff JSON, returning human-readable structural signal strings."""
    out: list[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in old.keys() - new.keys():
            out.append(f"removed {path}{key}")
        for key in new.keys() - old.keys():
            out.append(f"added {path}{key}")
        for key in old.keys() & new.keys():
            out.extend(json_structural_diff(old[key], new[key], f"{path}{key}."))
    elif isinstance(old, list) and isinstance(new, list):
        if old != new:
            out.append(f"changed {path or '<root>'} (list len {len(old)} -> {len(new)})")
    elif old != new:
        out.append(f"changed {path.rstrip('.') or '<root>'}")
    return out


def _try_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except Exception:
        return None


def _signals_from_text(added: list[str]) -> list[str]:
    blob = " ".join(added).lower()
    signals = []
    for pattern, label, _weight in KEYWORD_SIGNALS:
        if re.search(pattern, blob):
            signals.append(label)
    return signals


def _significance(signals: list[str], change_kind: str, added_count: int, is_json: bool) -> float:
    score = 0.10 if change_kind == "changed" else 0.15
    if is_json:
        score += 0.05
    for label in signals:
        for _p, lbl, weight in KEYWORD_SIGNALS:
            if lbl == label:
                score += weight
                break
    if added_count == 0:
        score = min(score, 0.2)
    return round(min(1.0, score), 3)


def analyze_change(
    old: str,
    new: str,
    old_json: Any | None = None,
    new_json: Any | None = None,
    name: str = "source",
) -> dict[str, Any] | None:
    """Return a change record (kind, signals, significance, diff, structural) or None if identical."""
    old_n, new_n = _norm(old), _norm(new)
    if old_n == new_n:
        return None

    if not old_n.strip():
        change_kind = "added"
    elif not new_n.strip():
        change_kind = "removed"
    else:
        change_kind = "changed"

    is_json = old_json is not None or new_json is not None
    structural: list[str] = []
    if is_json and old_json is not None and new_json is not None:
        structural = json_structural_diff(old_json, new_json)

    added = _added_lines(old_n, new_n)
    signals = _signals_from_text(added)
    for s in structural:
        if s.startswith("added"):
            signals.append("addition")
        elif s.startswith("removed"):
            signals.append("removal")
    signals = list(dict.fromkeys(signals))

    return {
        "change_kind": change_kind,
        "signals": signals,
        "significance": _significance(signals, change_kind, len(added), is_json),
        "diff_text": diff_text(old_n, new_n, name),
        "structural": structural,
    }


def deterministic_summary(record: dict[str, Any], source: Any) -> tuple[str, str, str]:
    """Best-effort What/How/Why without an LLM (LLM refinement is layered on in L2/L3)."""
    kind = record["change_kind"]
    signals = ", ".join(record["signals"]) or "content update"
    what = f"{kind} change in {source.provider}/{source.id} ({signals})"
    structural = record.get("structural") or []
    top = "; ".join(structural[:5]) if structural else ""
    how = f"Review the diff and update the {source.provider} registry/strategy if relevant."
    why = (
        f"Structural signals detected: {top}."
        if top
        else f"Provider surface changed; likely affects generation recipes for {source.provider}."
    )
    return what, how, why
