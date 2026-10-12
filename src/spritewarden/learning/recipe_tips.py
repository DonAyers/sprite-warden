"""Recipe tips: one tipshare payload per provider, generated from the recipe registry, and a
publisher that talks to a tipshare MCP stdio server.

The registry is local policy, not proof of live provider behavior or complete interception.
Tips are content-versioned projections. Publishing is opt-in; matching titles require
content readback, and only verified UUID receipts are recorded in ``tip_exports``.

Server command: env ``SPRITEWARDEN_TIPSHARE_CMD``, a JSON array, e.g.
``["node", "C:/path/to/tipshare/dist/server.js"]``. The child gets
``TIPSHARE_AGENT_ID=sprite-warden`` so the tips are attributable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Callable

from .models import utcnow

TIPSHARE_CMD_ENV = "SPRITEWARDEN_TIPSHARE_CMD"
TIPSHARE_AGENT_ID = "sprite-warden"
DEFAULT_TOPIC = "godot-pixel-art"
LABELS = {
    "pixellab": "PixelLab",
    "spritefusion": "Sprite Fusion",
    "retrodiffusion": "Retro Diffusion",
    "spritecook": "SpriteCook",
    "openrouter_image": "Image models (OpenRouter)",
}
TIP_FIELDS = ("topic_slug", "title", "summary", "problem", "solution", "gotchas", "artifact_name", "artifact_code", "author", "supersedes", "applicability", "evidence_state")


def _start_image_line(si: dict[str, Any] | None) -> str | None:
    if not si or not si.get("required"):
        return None
    bits = []
    if si.get("facing") and si["facing"] != "any":
        bits.append(f"faces {si['facing']}")
    if si.get("canvas") and si["canvas"] != "any":
        bits.append(f"{si['canvas']} canvas")
    if si.get("margin_px"):
        bits.append(f">={si['margin_px']}px transparent margin")
    if si.get("min_px"):
        bits.append(f">={si['min_px']}px")
    if si.get("max_px"):
        bits.append(f"<={si['max_px']}px")
    if si.get("multiple_of"):
        bits.append(f"side a multiple of {si['multiple_of']}")
    if si.get("native_pixels", True):
        bits.append("native 1:1 pixels")
    if si.get("pose"):
        bits.append(f"pose: {si['pose']}")
    return "; ".join(bits)


def _readable(pattern: str) -> str:
    """A registry regex as the phrase it forbids (``pixel[\\s-]?perfect`` -> ``pixel-perfect``)."""
    text = pattern.replace("\\b", "").replace("[\\s-]?", "-").replace("\\s+", " ").replace("\\s*", "")
    return text.replace("\\", "").strip()


def _rule_line(rule: dict[str, Any], operation: str = "") -> str:
    kind = rule.get("kind", "?")
    if rule.get("message"):
        fields = _Keep(count="N", value="<value>", operation=operation, **{k: v for k, v in rule.items() if k in ("max", "of")})
        return f"{kind}: {str(rule['message']).format_map(fields)}"
    if kind == "forbidden_words":
        return "forbidden_words: the provider's forbidden phrases in " + " / ".join(rule.get("args", []))
    if kind == "matte_collision":
        return (f"matte_collision: denied when more than {float(rule.get('max_fraction', 0.02)) * 100:g}% of the source's "
                f"opaque pixels are within RGB distance {rule.get('distance', 24)} of the matte colour "
                f"(default {rule.get('default', '#808080')}). {rule.get('advice', '')}").strip()
    return kind


def build_recipe_tips(registry: dict[str, Any] | None = None, topic: str = DEFAULT_TOPIC) -> list[dict[str, Any]]:
    """One ``share_tip`` payload per provider of the registry (plus an ``export_key`` for bookkeeping)."""
    if registry is None:
        from ..enforce.core import load_raw_registry

        registry = load_raw_registry()
    version = registry.get("version", 1)
    tips: list[dict[str, Any]] = []
    for pname, prov in (registry.get("providers") or {}).items():
        label = LABELS.get(pname, pname)
        defaults = prov.get("defaults") or {}
        ops = prov.get("operations") or {}
        enforce = prov.get("enforce") or {}

        projection = {"provider": prov, "pipeline_rules": registry.get("pipeline_rules", []), "stages": registry.get("stages", {}), "version": version}
        digest = hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        lines: list[str] = ["Pipeline rules: " + json.dumps(projection["pipeline_rules"]),
                            "Stages: " + json.dumps(projection["stages"]),
                            "Provider identity: " + json.dumps(prov.get("identity", {}))]
        if defaults.get("prompt_rules"):
            lines.append("Every call:")
            lines += [f"- {r}" for r in defaults["prompt_rules"]]
        for rule in enforce.get("rules", []):
            lines.append(f"- enforced ({_rule_line(rule)})")
        gotchas: list[str] = []
        forbidden = list(dict.fromkeys(_readable(p) for p in defaults.get("forbidden_words", [])))
        if forbidden:
            gotchas.append("Never write in a prompt: " + ", ".join(forbidden) + ".")
        for oname, entry in ops.items():
            lines.append("")
            lines.append(f"{oname}: {entry.get('description', '').strip()}")
            start = _start_image_line(entry.get("start_image"))
            if start:
                lines.append(f"- start image: {start}")
            for rule in entry.get("prompt_rules", []):
                lines.append(f"- {rule}")
            limits = entry.get("arg_limits") or {}
            if limits:
                lines.append("- limits: " + "; ".join(f"{k}={v}" for k, v in limits.items()))
            before = entry.get("required_gates_before") or []
            after = entry.get("required_gates_after") or []
            lines.append(f"- gates before: {', '.join(before) or 'none'}; after: {', '.join(after) or 'none'}")
            for rule in (entry.get("enforce") or {}).get("rules", []):
                lines.append(f"- enforced ({_rule_line(rule, oname)})")
            for note in (entry.get("start_image") or {}).get("notes", []) + entry.get("notes", []):
                gotchas.append(f"{oname}: {note}")
        sources = list(dict.fromkeys(defaults.get("sources", []) + [s for e in ops.values() for s in e.get("sources", [])]))
        lines.append("")
        lines.append("Sources: " + ", ".join(sources))
        lines.append("Machine-readable local policy: `warden recipes " + pname + " --json`. "
                     "Enforcement depends on the actual interface/operation; external paths may bypass hooks. "
                     "Provider docs or live schemas may conflict; verify scoped applicability before use.")

        tips.append({
            "export_key": f"recipe:{pname}:{digest}",
            "content_hash": digest,
            "applicability": {"provider": pname, "registry_version": version, "registry_hash": digest, "transport": "local-contract; verify provider surface"},
            "evidence_state": "local_policy_not_provider_verified",
            "topic_slug": topic,
            "title": f"{label} generator recipes: start image, prompt rules, limits and gates (sprite-warden registry v{version}; sha256 {digest})",
            "summary": (f"{label}'s documented rules for {', '.join(ops)} as data: what the start image must be, what never "
                        f"to write in a prompt, the argument limits, and the gates to pass before and after each call."),
            "problem": (f"Generator calls to {label} that ignore its own documentation (wrong start image, noise words in "
                        f"the prompt, out-of-range arguments, deriving from an un-gated anchor) cost credits and produce "
                        f"oddities that no amount of re-prompting fixes."),
            "solution": "\n".join(lines).strip(),
            "gotchas": gotchas[:16],
            "author": TIPSHARE_AGENT_ID,
        })
    return tips


class _Keep(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def tipshare_command(env: dict[str, str] | None = None) -> list[str] | None:
    """The tipshare server argv from ``SPRITEWARDEN_TIPSHARE_CMD`` (a JSON array), or None."""
    raw = (env if env is not None else os.environ).get(TIPSHARE_CMD_ENV, "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{TIPSHARE_CMD_ENV} must be a JSON array of strings: {exc}") from exc
    if not isinstance(data, list) or not data or not all(isinstance(x, str) for x in data):
        raise ValueError(f"{TIPSHARE_CMD_ENV} must be a non-empty JSON array of strings")
    return data


def _search_query(title: str) -> str:
    """A plain-word query for the server's full-text search (punctuation breaks FTS syntax)."""
    return " ".join(re.findall(r"[A-Za-z0-9]+", title)[:6])


def _titles(found: Any) -> set[str]:
    rows = found if isinstance(found, list) else (found.get("results") or found.get("tips") or [] if isinstance(found, dict) else [])
    return {str(r.get("title", "")).strip().lower() for r in rows if isinstance(r, dict)}


def publish_tips(
    payloads: list[dict[str, Any]],
    db: Any = None,
    *,
    command: list[str] | None = None,
    dry_run: bool = True,
    include_exported: bool = False,
    session_factory: Callable[[list[str]], Any] | None = None,
) -> list[dict[str, Any]]:
    """Publish tip payloads through a tipshare MCP stdio server; returns one report row per tip.

    ``dry_run`` (the default) never calls ``share_tip``: with a server command it still
    searches (read-only) and reports ``would_publish`` / ``skipped_existing``; without one it
    reports ``would_publish`` unchecked. A real run requires the command, searches first,
    skips a tip whose title already exists (case-insensitive) or whose ``export_key`` is in
    ``tip_exports`` (unless ``include_exported``), and records each tip it sends.
    """
    command = command if command is not None else tipshare_command()
    if not dry_run and not command:
        raise ValueError(f"publishing needs the tipshare server command: set {TIPSHARE_CMD_ENV} to a JSON array")
    report: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for original in payloads:
        tip = dict(original)
        key = tip.get("export_key")
        if key and db is not None and key.startswith("recipe:"):
            family = ":".join(key.split(":")[:2]) + ":"
            with db._get_conn() as conn:
                previous = conn.execute("SELECT tip_ref FROM tip_exports WHERE strategy_id LIKE ? AND strategy_id != ? AND tip_ref IS NOT NULL ORDER BY exported_at DESC, rowid DESC LIMIT 1", (family + "%", key)).fetchone()
            if previous:
                from uuid import UUID
                UUID(previous["tip_ref"])  # Refuse ambiguous legacy numeric IDs.
                tip["supersedes"] = previous["tip_ref"]
        if key and db is not None and not include_exported and db.is_tip_exported(key):
            report.append({"title": tip["title"], "export_key": key, "action": "skipped_exported"})
        else:
            pending.append(tip)
    if not pending:
        return report
    if not command:
        return report + [{"title": t["title"], "export_key": t.get("export_key"), "action": "would_publish",
                          "checked": False, "supersedes": t.get("supersedes")} for t in pending]

    if session_factory is None:
        from .mcp_probe import StdioSession

        def session_factory(cmd: list[str]) -> Any:
            return StdioSession(cmd, env={"TIPSHARE_AGENT_ID": TIPSHARE_AGENT_ID})

    with session_factory(command) as session:
        for tip in pending:
            row: dict[str, Any] = {"title": tip["title"], "export_key": tip.get("export_key"), "supersedes": tip.get("supersedes")}
            try:
                found = session.call_tool("search", {"query": _search_query(tip["title"]),
                                                     "topic_slug": tip["topic_slug"], "limit": 25})
                if tip["title"].strip().lower() in _titles(found):
                    rows = found if isinstance(found, list) else found.get("results", found.get("tips", []))
                    match = next(r for r in rows if r.get("title", "").strip().lower() == tip["title"].strip().lower())
                    verified = session.call_tool("get_tip", {"tip_id": match["id"]})
                    if verified.get("solution") != tip["solution"]:
                        raise ValueError("Existing title has conflicting content; requires review")
                    from uuid import UUID
                    ref = str(UUID(verified["uuid"]))
                    row.update(action="skipped_existing", tip_ref=ref, checked=True)
                    if not dry_run and db is not None and tip.get("export_key"):
                        db.record_tip_export(tip["export_key"], tip["topic_slug"], ref, utcnow())
                elif dry_run:
                    row.update(action="would_publish", checked=True)
                else:
                    sent = session.call_tool("share_tip", {k: tip[k] for k in TIP_FIELDS if tip.get(k) not in (None, "", [])})
                    if not isinstance(sent, dict) or not sent.get("id"):
                        raise ValueError("No tip id in publish receipt; not marked exported")
                    verified = session.call_tool("get_tip", {"tip_id": sent["id"]})
                    if not isinstance(verified, dict) or verified.get("title") != tip["title"] or verified.get("solution") != tip["solution"]:
                        raise ValueError("Publish readback mismatch; not marked exported")
                    from uuid import UUID
                    ref = str(UUID(verified["uuid"]))
                    row.update(action="published", tip_ref=ref)
                    if db is not None and tip.get("export_key"):
                        db.record_tip_export(tip["export_key"], tip["topic_slug"], row["tip_ref"], utcnow())
            except Exception as exc:  # noqa: BLE001 - one tip failing must not stop or hide the others
                row.update(action="error", error=f"{type(exc).__name__}: {exc}")
            report.append(row)
    return report


def mark_exported(payloads: list[dict[str, Any]], db: Any, tip_ref: str | None = None) -> int:
    """Legacy entrypoint retained only to reject unverified export marking."""
    raise ValueError("Blind marking is disabled; publish with a verified get_tip receipt instead")
