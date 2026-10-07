"""Gate records: ``<image>.gate.json`` sidecars written by the judge commands.

A record proves that a specific version of an image (by sha256) passed named
gates, so later stages (rotation, animation, review pages) can refuse inputs
that were never gated or changed since. The shape is shared with the game
repo's generator hook (``tools/generator_hook.py:gate_record``)::

    {
      "version": 1,
      "image": "south.png",
      "sha256": "<hex of the image bytes>",
      "time": "2026-10-07T12:00:00+00:00",
      "spritewarden": "0.1.0",
      "params": {"command": "judge-still", ...},
      "gates": {"pixel_grid": {"passed": true, "score": 1.0, "summary": "..."},
                "still": {"passed": true}}
    }

Umbrella names: ``still`` (judge-still overall), ``start_image`` (the anchor
gate), ``views`` (judge-views overall, alias of ``direction_set``) and ``anim``
(judge-anim overall).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

RECORD_SUFFIX = ".gate.json"
RECORD_VERSION = 1


def _version() -> str:
    try:
        from .. import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def sha256_of(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def record_path(image_path: str | Path) -> Path:
    p = Path(image_path)
    return p.with_name(p.name + RECORD_SUFFIX)


def gates_from_scorecard(card: Any, umbrella: str | None = None) -> dict[str, dict[str, Any]]:
    """Turn a Still/Animation scorecard (or a single GateResult) into the record's gates map."""
    gates: dict[str, dict[str, Any]] = {}
    results = getattr(card, "gates", None)
    if results is None:
        results = [card]
    for g in results:
        gates[g.name] = {
            "passed": bool(g.passed),
            "score": round(float(g.score), 3),
            "summary": g.summary,
            "errors": list(g.errors)[:6],
        }
    if umbrella:
        overall = bool(getattr(card, "passed", all(v["passed"] for v in gates.values())))
        gates[umbrella] = {"passed": overall}
    return gates


def write_record(
    image_path: str | Path,
    gates: dict[str, dict[str, Any] | bool],
    *,
    params: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write the sidecar next to the image and return its path."""
    image_path = Path(image_path)
    norm: dict[str, dict[str, Any]] = {}
    for name, verdict in gates.items():
        norm[name] = {"passed": bool(verdict)} if isinstance(verdict, bool) else dict(verdict)
    record: dict[str, Any] = {
        "version": RECORD_VERSION,
        "image": image_path.name,
        "sha256": sha256_of(image_path),
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "spritewarden": _version(),
        "params": dict(params or {}),
        "gates": norm,
    }
    if extra:
        record.update(extra)
    previous = load_record(image_path)
    if previous and previous.get("rejected") and previous.get("sha256") == record["sha256"]:
        record["rejected"] = previous["rejected"]  # judging the same bytes again does not undo a person's "no"
    out = record_path(image_path)
    out.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    return out


def mark_rejected(image_path: str | Path, *, by: str, reason: str) -> Path:
    """Record a person's "no" on the image's sidecar (created if absent).

    From then on ``passed_gates`` returns nothing for these bytes: the image cannot be a source
    for a rotation or animation and cannot go on a review page again, whatever the judges said.
    Changing the image (a new sha256) makes it a new candidate that must be gated afresh.
    """
    image_path = Path(image_path)
    record = load_record(image_path) or {
        "version": RECORD_VERSION, "image": image_path.name, "spritewarden": _version(), "params": {}, "gates": {},
    }
    record["sha256"] = sha256_of(image_path)
    record["rejected"] = {"by": by, "reason": reason,
                          "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    out = record_path(image_path)
    out.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    return out


def load_record(image_or_record_path: str | Path) -> dict[str, Any] | None:
    """Load the sidecar (given the image or the record path) regardless of whether it is current."""
    p = Path(image_or_record_path)
    rec = p if p.name.endswith(RECORD_SUFFIX) else record_path(p)
    try:
        return json.loads(rec.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def passed_gates(image_path: str | Path) -> set[str]:
    """Names of the gates the current image bytes passed; empty when there is no current record."""
    image_path = Path(image_path)
    record = load_record(image_path)
    if not record or not image_path.is_file():
        return set()
    try:
        if record.get("sha256") != sha256_of(image_path):
            return set()
    except OSError:
        return set()
    if record.get("rejected"):
        return set()  # a person said no: no gate result outlives that
    gates = record.get("gates", {})
    if isinstance(gates, list):
        return {str(g) for g in gates}
    out = set()
    for name, verdict in gates.items():
        ok = verdict if isinstance(verdict, bool) else (isinstance(verdict, dict) and bool(verdict.get("passed", False)))
        if ok:
            out.add(str(name))
    return out


def is_gated(image_path: str | Path, gate_name: str) -> bool:
    """True when a current record (sha256 matches the file) says ``gate_name`` passed."""
    return gate_name in passed_gates(image_path)


def _expand(paths: Iterable[str | Path]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(f for f in p.iterdir() if f.suffix.lower() == ".png"))
        elif any(ch in str(raw) for ch in "*?["):
            out.extend(sorted(Path().glob(str(raw))))
        else:
            out.append(p)
    return [p for p in out if not p.name.endswith(RECORD_SUFFIX)]


_SOUTH_NAMES = ("south", "s", "down", "front")


def review_problem(image_path: str | Path) -> str | None:
    """Why a gated image is still not fit to be SHOWN to a person, or None.

    The pixel gates are necessary, not sufficient: the flaws people actually reject are a turned
    view that grew a part the design does not have, and an animation that lost a part of its
    still. So a record written by ``judge-views`` must carry a passed ``vision_reference`` (the
    comparison with the anchor) unless the image IS the anchor view, and one written by
    ``judge-anim`` must have been run with ``--source`` (``frame_integrity`` against the still).
    Records without a ``params.command`` (written by other tools) are not second-guessed.
    """
    record = load_record(image_path)
    if not record:
        return None
    params = record.get("params") if isinstance(record.get("params"), dict) else {}
    gates = record.get("gates") if isinstance(record.get("gates"), dict) else {}
    command = params.get("command")
    if command == "judge-views":
        vision = gates.get("vision_reference")
        if vision is None:
            facing = str(params.get("facing") or "").lower()
            anchor_facing = str(params.get("anchor_facing") or "south").lower()
            is_anchor_view = bool(facing) and not params.get("anchor") and (
                facing == anchor_facing or (facing in _SOUTH_NAMES and anchor_facing in _SOUTH_NAMES))
            if not is_anchor_view:
                return "never compared with the anchor by vision (no vision_reference gate)"
        elif not (vision if isinstance(vision, bool) else bool((vision or {}).get("passed"))):
            return "the vision comparison with the anchor failed"
    elif command == "judge-anim":
        if not params.get("source"):
            return "judged without --source (frames never compared with the still they came from)"
        integrity = gates.get("frame_integrity")
        if isinstance(integrity, dict) and not integrity.get("passed", False):
            return "frame_integrity failed"
    return None


def require_passed(paths: Iterable[str | Path], gates: Iterable[str], *, strict: bool = True) -> dict[str, Any]:
    """Review gatekeeper: which files lack a current passed record for ANY of ``gates``.

    Returns ``{"ok": bool, "required": [...], "checked": n, "missing": [{"path", "reason", "passed"}], "passed": [...]}``.
    A file is accepted when at least one of the required gate names is in its
    current passed set (name exactly what you need; pass several names to accept
    any of them) AND, with ``strict`` (the default), ``review_problem`` finds it
    fit to be shown.
    """
    required = [g for g in gates if g]
    files = _expand(paths)
    missing: list[dict[str, Any]] = []
    ok_files: list[str] = []
    for f in files:
        if not f.is_file():
            missing.append({"path": str(f), "reason": "file not found", "passed": []})
            continue
        record = load_record(f)
        if record is None:
            missing.append({"path": str(f), "reason": f"no {RECORD_SUFFIX} record", "passed": []})
            continue
        have = passed_gates(f)
        if record.get("sha256") != sha256_of(f):
            missing.append({"path": str(f), "reason": "image changed since it was gated (sha256 mismatch)", "passed": []})
            continue
        if not any(g in have for g in required):
            missing.append({
                "path": str(f),
                "reason": f"record has no passed {' / '.join(required)} gate",
                "passed": sorted(have),
            })
            continue
        problem = review_problem(f) if strict else None
        if problem:
            missing.append({"path": str(f), "reason": problem, "passed": sorted(have)})
            continue
        ok_files.append(str(f))
    return {
        "ok": not missing and bool(files),
        "required": required,
        "checked": len(files),
        "missing": missing,
        "passed": ok_files,
    }
