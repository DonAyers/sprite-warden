"""``pipeline.json``: the recorded route of one character, and the standing rules as text.

A character's folder holds one ``pipeline.json``. It is where decisions live so that they
are not carried in anyone's memory::

    {
      "bot": "bildbot",
      "body_class": "rigid",             # figure | rigid
      "locomotion": "wheels",            # legs | wheels | treads
      "size_tier": "48-64",
      "canvas": 96,                      # square canvas of every image in the set
      "palette": "luap-40",
      "anchor": "art_source/bots/bildbot/sc/anchor/bare_1.png",   # project-relative, gated start_image
      "anchor_facing": "south",
      "faces": {"back": "...", "left": "plain", "right": "plain"},  # what the anchor cannot show
      "rotation_provider": "retrodiffusion",
      "animation_provider": null,
      "identity": {"pixellab": {"id": "<character_id>", "also": []}},  # the provider's own character entity
      "views_dir": "art_source/bots/bildbot/turn/views",
      "props": "layers"
    }

``check`` says what is missing for the next stage; ``status`` reads the gate records to say
which stage the character has actually reached. ``rules_text`` is the digest a session-start
hook prints, generated from the registry so there is one source.

Import-light (json + pathlib only): used from the hook's fast path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import ProjectConfig, load_config
from .core import (PROVIDER_LABELS, UNSEEN_FACES, _canonical_provider, _facing_files, faces_declared,
                   load_raw_registry, pipeline_identity)

PIPELINE_NAME = "pipeline.json"
KNOWN_KEYS = ("bot", "body_class", "locomotion", "size_tier", "canvas", "palette", "anchor", "anchor_facing",
              "faces", "rotation_provider", "animation_provider", "identity", "views_dir", "props", "notes",
              "description", "char_id", "align", "route")
SKIP_DIRS = {".git", ".godot", "node_modules", "__pycache__", ".venv", "build", "tmp"}


def pipeline_file(target: str | Path) -> Path:
    p = Path(target)
    return p if p.name == PIPELINE_NAME else p / PIPELINE_NAME


def load_pipeline(target: str | Path) -> dict[str, Any]:
    data = json.loads(pipeline_file(target).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{pipeline_file(target)} must hold a JSON object")
    return data


def save_pipeline(target: str | Path, data: dict[str, Any]) -> Path:
    path = pipeline_file(target)
    if path.exists() and '_workflow' in load_pipeline(target):
        raise ValueError('revisioned pipelines require workflow decisions/promote, not legacy set')
    if '_workflow' in data:
        raise ValueError('revisioned state must be created through workflow_state')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _coerce(value: str) -> Any:
    text = value.strip()
    if text.lower() in ("null", "none", ""):
        return None
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if text.lstrip("-").isdigit():
        return int(text)
    if text[:1] in "{[":
        return json.loads(text)
    return text


def set_values(data: dict[str, Any], assignments: list[str]) -> dict[str, Any]:
    """Apply ``key=value`` / ``faces.back=...`` / ``identity.pixellab.id=...`` assignments in place."""
    for item in assignments:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"expected key=value, got {item!r}")
        parts = [p for p in key.strip().split(".") if p]
        node = data
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = _coerce(raw)
    return data


def check_pipeline(data: dict[str, Any], cfg: ProjectConfig, where: Path, reg: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Problems as ``[{"level": "error" | "todo", "stage", "text"}]``: errors are wrong, todos block a later stage."""
    reg = reg if reg is not None else load_raw_registry()
    out: list[dict[str, str]] = []

    def add(level: str, stage: str, text: str) -> None:
        out.append({"level": level, "stage": stage, "text": text})

    from ..gates.records import passed_gates

    if '_workflow' in data:
        from ..workflow_state import _readiness
        state = data['_workflow']
        for stage in ('anchor', 'views', 'anim'):
            rid = state['current'].get(stage)
            if not rid:
                add('todo', stage, 'No current revision: register, review and explicitly promote a candidate')
            else:
                ready = _readiness(cfg.root.resolve(), state, state['revisions'][rid])
                for reason in ready['reasons']:
                    add('error', stage, reason)
        return out

    anchor = data.get("anchor")
    if not isinstance(anchor, str) or not anchor.strip():
        add("todo", "anchor", "no anchor recorded: gate the south image with `warden stage anchor <image>` and set anchor=<image>")
    else:
        path = cfg.resolve(anchor)
        if not path.is_file():
            add("error", "anchor", f"anchor {anchor} does not exist")
        elif "start_image" not in passed_gates(path):
            add("error", "anchor", f"anchor {anchor} has no current passed start_image gate: run `warden stage anchor {anchor}`")
    for face in faces_declared(data):
        add("todo", "views", f"faces.{face} is not described (what is on the {face} of the design? \"plain\" is an answer)")
    for stage in ("rotation", "animation"):
        chosen = data.get(f"{stage}_provider")
        if not chosen:
            add("todo", "views" if stage == "rotation" else "motion", f"{stage}_provider is not decided")
            continue
        canon = _canonical_provider(reg, chosen)
        if canon is None:
            add("error", stage, f"{stage}_provider {chosen!r} is not a provider in the registry ({', '.join(reg.get('providers', {}))})")
            continue
        ident = (reg["providers"][canon].get("identity") or {})
        if stage == "animation" and ident.get("entity"):
            declared = pipeline_identity(data, canon, reg)
            if not declared or not str(declared.get("id") or "").strip():
                add("todo", "motion", f"identity.{canon}.id is not recorded: {PROVIDER_LABELS.get(canon, canon)} animates through "
                                      f"its own character ({', '.join(ident.get('create_with', [])) or 'see `warden recipes`'})")
    for key in data:
        if key not in KNOWN_KEYS and not key.startswith("_") and not key.endswith(("_notes", "_reason", "_provider")):
            add("todo", "-", f"unknown key {key!r} (kept, but nothing reads it)")
    return out


def stage_status(data: dict[str, Any], cfg: ProjectConfig) -> dict[str, Any]:
    """Which stages the gate records say are done: ``{"anchor": bool, "views": {...}, "next": str}``."""
    from ..gates.records import passed_gates, review_problem

    if '_workflow' in data:
        from ..workflow_state import _readiness
        state = data['_workflow']
        readiness = {stage: _readiness(cfg.root.resolve(), state, state['revisions'][rid])
                     for stage, rid in state['current'].items()}
        anchor_ok = readiness.get('anchor', {}).get('ready', False)
        views_ok = readiness.get('views', {}).get('ready', False)
        selected = state['revisions'].get(state['current'].get('views'), {})
        total = len(selected.get('files', {}))
        views = {'dir': data.get('views_dir'), 'total': total, 'passed': total if views_ok else 0,
                 'shown_ready': total if views_ok else 0}
        nxt = 'review/promote anchor revision' if not anchor_ok else ('review/promote views revision' if not views_ok else 'motion / in game')
        return {'anchor': anchor_ok, 'views': views, 'views_done': views_ok, 'next': nxt,
                'readiness': readiness, 'version': state['version']}

    anchor_ok = False
    anchor = data.get("anchor")
    if isinstance(anchor, str) and anchor.strip():
        anchor_ok = "start_image" in passed_gates(cfg.resolve(anchor))
    views: dict[str, Any] = {"dir": data.get("views_dir"), "total": 0, "passed": 0, "shown_ready": 0}
    if isinstance(data.get("views_dir"), str) and data["views_dir"].strip():
        files = _facing_files(cfg.resolve(data["views_dir"]))
        views["total"] = len(files)
        for f in files:
            have = passed_gates(f)
            if "views" in have or "direction_set" in have:
                views["passed"] += 1
                if review_problem(f) is None:
                    views["shown_ready"] += 1
    views_ok = views["total"] >= 4 and views["passed"] == views["total"] == views["shown_ready"]
    if not anchor_ok:
        nxt = "1 anchor: `warden stage anchor <south image>`"
    elif faces_declared(data) or not data.get("rotation_provider"):
        nxt = "2 views: describe the unseen faces and record rotation_provider first (`warden pipeline check`)"
    elif not views_ok:
        nxt = "2 views: turn the anchor with the recorded rotation provider, then `warden stage views <folder>`"
    elif not data.get("animation_provider"):
        nxt = "4 motion: decide and record animation_provider"
    else:
        nxt = "4 motion / 5 in game"
    return {"anchor": anchor_ok, "views": views, "views_done": views_ok, "next": nxt}


def find_pipelines(root: str | Path, max_depth: int = 6) -> list[Path]:
    """Every pipeline.json under a project root (bounded walk; build and VCS folders skipped)."""
    root = Path(root)
    found: list[Path] = []

    def walk(folder: Path, depth: int) -> None:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name == PIPELINE_NAME and entry.is_file():
                found.append(entry)
            elif entry.is_dir() and depth < max_depth and entry.name not in SKIP_DIRS and not entry.name.startswith("."):
                walk(entry, depth + 1)

    walk(root, 0)
    return found


def rules_text(project_root: str | Path | None = None, reg: dict[str, Any] | None = None) -> str:
    """The standing rules, the stages, each provider's identity line and every character's next step."""
    reg = reg if reg is not None else load_raw_registry()
    lines = ["ART PIPELINE RULES (sprite-warden registry; the hook enforces them, this is so nobody has to rediscover them):"]
    lines += [f"{i}. {rule}" for i, rule in enumerate(reg.get("pipeline_rules", []), 1)]
    stages = reg.get("stages") or []
    if stages:
        lines.append("")
        lines.append("STAGES (one command each; it stops at the first failed gate):")
        for s in stages:
            cmd = f"  [{s['command']}]" if s.get("command") else ""
            lines.append(f"- {s.get('stage')}: {s.get('what')}. Gate: {s.get('gate')}.{cmd}")
    lines.append("")
    lines.append("CHARACTER IDENTITY, EACH PROVIDER'S OWN WAY (use it as they prescribe; build on top, never around):")
    for pname, prov in (reg.get("providers") or {}).items():
        ident = prov.get("identity") or {}
        if ident.get("concept"):
            lines.append(f"- {PROVIDER_LABELS.get(pname, pname)}: {ident['concept']}")
    if project_root is not None:
        cfg = load_config(project_root)
        pipelines = find_pipelines(cfg.root)
        if pipelines:
            lines.append("")
            lines.append("WHERE EACH CHARACTER IS (from pipeline.json + gate records):")
        for path in pipelines:
            rel = cfg.relative(path.parent)
            try:
                data = load_pipeline(path)
            except (OSError, ValueError) as exc:
                lines.append(f"- {rel}: pipeline.json unreadable ({exc})")
                continue
            status = stage_status(data, cfg)
            route = f"rotation by {data.get('rotation_provider') or 'UNDECIDED'}, animation by {data.get('animation_provider') or 'UNDECIDED'}"
            lines.append(f"- {data.get('bot') or path.parent.name} ({rel}): {route}. Next: {status['next']}")
            for p in check_pipeline(data, cfg, path, reg):
                if p["level"] == "error":
                    lines.append(f"    ERROR: {p['text']}")
        try:
            from ..gates.rejections import read_rejections, summarize

            top = [g for g in summarize(read_rejections(cfg.rejections_path, limit=200)) if g["count"] >= 2][:5]
            if top:
                lines.append("")
                lines.append("REPEATED REJECTIONS (the same thing failing more than once: fix the input or add a rule):")
                lines += [f"- {g['count']}x {g['provider']} / {g['stage']} / {g['what']}: {g['example']}" for g in top]
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines)


__all__ = ["PIPELINE_NAME", "UNSEEN_FACES", "check_pipeline", "find_pipelines", "load_pipeline", "pipeline_file",
           "rules_text", "save_pipeline", "set_values", "stage_status"]
