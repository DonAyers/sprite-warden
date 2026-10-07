"""The rejection log: every failed gate and every human "no", appended to one JSONL file.

Nobody has to remember to write a failure down. The judge commands append a line when an
image fails (``by: "gate"``), ``warden reject`` appends the owner's verdict on something
the gates let through (``by: "<name>"``), and the hook shows the newest entries for a
provider before the next call to it spends. ``warden rejections`` prints the log grouped,
which is the raw material for new rules: a reason that keeps coming back should become a
gate or a recipe rule.

One line per rejection::

    {"time": "...", "by": "gate" | "<person>", "image": "art/bot/east.png", "command": "judge-views",
     "stage": "rotation", "provider": "retrodiffusion", "bot": "bildbot",
     "failed": [{"name": "vision_reference", "errors": ["invented: round boss ..."]}],
     "reason": "free text (human rejections)", "sha256": "<hex of the image>"}

Import-light on purpose (json + pathlib): ``enforce.core`` reads it inside the hook.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# The stage a judge command checks the OUTPUT of, i.e. the provider to blame in pipeline.json.
COMMAND_STAGE = {"judge-views": "rotation", "judge-anim": "animation"}


def _norm(name: Any) -> str:
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def describe_source(image: str | Path, command: str | None = None) -> dict[str, Any]:
    """Project root, log path, bot, stage and provider for an image, from its spritewarden.json / pipeline.json."""
    from ..enforce.config import find_project_root, load_config
    from ..enforce.core import find_pipeline

    image = Path(image)
    root = find_project_root(image.parent if image.suffix else image)
    cfg = load_config(root)
    pipeline, where = find_pipeline(image)
    stage = COMMAND_STAGE.get(command or "")
    out: dict[str, Any] = {"root": root, "log": cfg.rejections_path, "image": _relative(image, root), "stage": stage}
    if pipeline and where is not None:
        out["bot"] = str(pipeline.get("bot") or where.parent.name)
        if stage and pipeline.get(f"{stage}_provider"):
            out["provider"] = str(pipeline[f"{stage}_provider"])
    return out


def log_rejection(
    image: str | Path,
    *,
    by: str = "gate",
    command: str | None = None,
    failed: Iterable[dict[str, Any]] | None = None,
    reason: str = "",
    provider: str | None = None,
    log_path: str | Path | None = None,
) -> Path | None:
    """Append one rejection; returns the log path, or None when it could not be written (never raises)."""
    try:
        image = Path(image)
        info = describe_source(image, command)
        entry: dict[str, Any] = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "by": by,
            "image": info["image"],
        }
        if command:
            entry["command"] = command
        for key in ("stage", "bot"):
            if info.get(key):
                entry[key] = info[key]
        if provider or info.get("provider"):
            entry["provider"] = provider or info["provider"]
        failures = [{"name": str(f.get("name")), "errors": [str(e) for e in (f.get("errors") or [])[:3]]}
                    for f in (failed or [])]
        if failures:
            entry["failed"] = failures
        if reason:
            entry["reason"] = reason
        if image.is_file():
            from .records import sha256_of

            entry["sha256"] = sha256_of(image)
        path = Path(log_path) if log_path else Path(info["log"])
        # the same file failing the same way twice in a row is one fact, not two
        last = next(iter(read_rejections(path, limit=1)), None)
        if last and all(last.get(k) == entry.get(k) for k in ("by", "image", "sha256", "failed", "reason", "command")):
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path
    except Exception:  # noqa: BLE001 - bookkeeping must never fail a judge command
        return None


def failed_gates(card: Any) -> list[dict[str, Any]]:
    """The failed gates of a scorecard (or a single GateResult) as ``[{"name", "errors"}]``."""
    results = getattr(card, "gates", None)
    if results is None:
        results = [card]
    return [{"name": g.name, "errors": list(g.errors)[:3] or [g.summary]} for g in results if not g.passed]


def read_rejections(log_path: str | Path, limit: int | None = None) -> list[dict[str, Any]]:
    """Entries newest first; unreadable lines are skipped."""
    try:
        lines = Path(log_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
            if limit and len(out) >= limit:
                break
    return out


def recent_rejections(
    log_path: str | Path,
    *,
    provider: str | None = None,
    bot: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    out = []
    for entry in read_rejections(log_path):
        if provider and _norm(entry.get("provider")) != _norm(provider):
            continue
        if bot and _norm(entry.get("bot")) != _norm(bot):
            continue
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def summarize(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group by (provider, stage, what failed): the repeat offenders first."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in entries:
        names = [f.get("name", "?") for f in entry.get("failed", [])] or ["human: " + str(entry.get("reason", ""))[:60]]
        for name in names:
            key = (str(entry.get("provider") or "-"), str(entry.get("stage") or "-"), str(name))
            g = groups.setdefault(key, {"provider": key[0], "stage": key[1], "what": key[2], "count": 0,
                                        "last": entry.get("time"), "example": ""})
            g["count"] += 1
            if not g["example"]:
                errors = next((f.get("errors") for f in entry.get("failed", []) if f.get("name") == name), None)
                g["example"] = str((errors or [entry.get("reason", "")])[0])[:200]
    return sorted(groups.values(), key=lambda g: (-g["count"], g["provider"], g["what"]))
