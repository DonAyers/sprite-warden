"""``warden check-call`` and ``warden hook``: the enforcement core on the command line.

Kept free of rich / PIL / pydantic imports so a hook invocation for a call that is
not a generator call costs little more than interpreter start-up. ``spritewarden.cli``
routes these two subcommands here before it imports anything else.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

COMMANDS = ("hook", "check-call", "rules", "pipeline")


def add_parsers(subparsers: Any) -> None:
    """Register both subcommands on an argparse subparsers object (used for --help by the full CLI too)."""
    p_check = subparsers.add_parser(
        "check-call", help="Check one generator tool call against the recipe registry (exit 2 = deny)")
    p_check.add_argument("--tool", required=True, help="Tool name, e.g. mcp__pixellab__create_character or Bash")
    p_check.add_argument("--input-file", required=True,
                         help="JSON file with the tool input (or a whole {tool_name, tool_input} event); '-' = stdin")
    p_check.add_argument("--project", help="Project root (default: nearest spritewarden.json / .git above the cwd)")
    p_check.add_argument("--json", action="store_true", help="Print the verdict as JSON")
    p_check.set_defaults(func=cmd_check_call)

    p_hook = subparsers.add_parser(
        "hook", help="Claude Code PreToolUse hook: read the event on stdin, deny generator calls that break a recipe")
    p_hook.add_argument("--claude", action="store_true", help="Claude Code hook protocol (the default and only one)")
    p_hook.add_argument("--project", help="Project root (default: nearest spritewarden.json / .git above the event's cwd)")
    p_hook.set_defaults(func=cmd_hook)

    p_rules = subparsers.add_parser(
        "rules", help="Print the standing pipeline rules, the stages, each provider's identity method and where "
                      "every character is (for a session-start hook)")
    p_rules.add_argument("--project", help="Project root (default: nearest spritewarden.json / .git above the cwd)")
    p_rules.add_argument("--no-status", action="store_true", help="Rules only, no per-character status")
    p_rules.set_defaults(func=cmd_rules)

    p_pipe = subparsers.add_parser("pipeline", help="A character's recorded route (pipeline.json): init / set / show / check")
    pipe_sub = p_pipe.add_subparsers(dest="pipeline_command", required=True)
    p_init = pipe_sub.add_parser("init", help="Create pipeline.json in a character's folder")
    p_init.add_argument("folder")
    p_init.add_argument("values", nargs="*", metavar="key=value", help="e.g. anchor=art/bot/south.png locomotion=wheels faces.back=plain")
    p_init.add_argument("--force", action="store_true", help="Overwrite an existing pipeline.json")
    p_set = pipe_sub.add_parser("set", help="Set values: key=value, faces.back=..., identity.pixellab.id=...")
    p_set.add_argument("folder")
    p_set.add_argument("values", nargs="+", metavar="key=value")
    p_show = pipe_sub.add_parser("show", help="Print pipeline.json and the stage it has reached")
    p_show.add_argument("folder")
    p_check = pipe_sub.add_parser("check", help="List what is wrong or still undecided (exit 1 on errors)")
    p_check.add_argument("folder")
    for p in (p_init, p_set, p_show, p_check):
        p.add_argument("--project", help="Project root (default: found from the folder)")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=cmd_pipeline)


def _root(project: str | None, cwd: str | None) -> Path:
    from .config import find_project_root

    if project:
        return Path(project)
    return find_project_root(Path(cwd) if cwd else Path.cwd())


def cmd_check_call(args: argparse.Namespace) -> int:
    from .core import check_call

    raw = sys.stdin.read() if args.input_file == "-" else Path(args.input_file).read_text(encoding="utf-8-sig")
    data = json.loads(raw) if raw.strip() else {}
    if isinstance(data, dict) and isinstance(data.get("tool_input"), dict) and "tool_name" in data:
        data = data["tool_input"]
    verdict = check_call(args.tool, data if isinstance(data, dict) else {}, _root(args.project, None))
    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2))
    else:
        target = f" ({verdict.provider} / {verdict.operation})" if verdict.provider else ""
        print(f"{verdict.decision.upper()}{target}")
        for reason in verdict.reasons:
            print(f"- {reason}")
        if verdict.context:
            print(verdict.context)
    return 2 if verdict.decision == "deny" else 0


def deny_text(verdict: Any, override_hint: str) -> str:
    return (
        f"BLOCKED by sprite-warden ({verdict.provider} / {verdict.operation}):\n- "
        + "\n- ".join(verdict.reasons)
        + f"\nFix the input or the call. To override on purpose, write {override_hint} "
          "{\"reason\": \"...\", \"calls\": 1} and say so to the project owner."
    )


def hook_response(event: dict[str, Any], project: str | None = None) -> dict[str, Any] | None:
    """The Claude Code PreToolUse response for one event, or None to stay silent."""
    from .config import load_config
    from .core import check_call

    root = _root(project, event.get("cwd") if isinstance(event.get("cwd"), str) else None)
    verdict = check_call(str(event.get("tool_name") or ""), event.get("tool_input"), root)
    if verdict.decision == "ignore":
        return None
    out: dict[str, Any] = {"hookEventName": "PreToolUse"}
    if verdict.decision == "deny":
        out["permissionDecision"] = "deny"
        out["permissionDecisionReason"] = deny_text(verdict, load_config(root).override)
        return {"hookSpecificOutput": out}
    if not verdict.context:
        return None
    out["additionalContext"] = verdict.context
    return {"hookSpecificOutput": out}


def cmd_hook(args: argparse.Namespace) -> int:
    """Always exits 0: a broken hook must never block the tool call."""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            return 0
        response = hook_response(event, args.project)
        if response:
            sys.stdout.write(json.dumps(response) + "\n")
    except Exception:  # noqa: BLE001
        return 0
    return 0


def _out(text: str) -> None:
    """Print UTF-8 whatever the console code page is (a hook's stdout is a pipe on Windows)."""
    try:
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
    except (AttributeError, ValueError):
        print(text)


def cmd_rules(args: argparse.Namespace) -> int:
    from .pipeline import rules_text

    root = None if args.no_status else _root(args.project, None)
    _out(rules_text(root))
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    from .config import find_project_root, load_config
    from .pipeline import check_pipeline, load_pipeline, pipeline_file, save_pipeline, set_values, stage_status

    path = pipeline_file(args.folder)
    cfg = load_config(Path(args.project) if args.project else find_project_root(path.parent.resolve()))
    command = args.pipeline_command
    try:
        if command == "init":
            if path.exists() and not args.force:
                _out(f"{path} exists (use `warden pipeline set`, or --force to start over)")
                return 1
            data = {"bot": path.parent.resolve().name, "anchor": None, "anchor_facing": "south",
                    "faces": {"back": "", "left": "", "right": ""}, "rotation_provider": None,
                    "animation_provider": None, "identity": {}, "props": "layers"}
            save_pipeline(path, set_values(data, list(args.values)))
        elif command == "set":
            data = load_pipeline(path) if path.exists() else {}
            save_pipeline(path, set_values(data, list(args.values)))
        else:
            data = load_pipeline(path)
    except (OSError, ValueError) as exc:
        _out(f"pipeline error: {exc}")
        return 1
    problems = check_pipeline(data, cfg, path)
    status = stage_status(data, cfg)
    errors = [p for p in problems if p["level"] == "error"]
    if args.json:
        _out(json.dumps({"path": str(path), "pipeline": data, "status": status, "problems": problems}, indent=2))
        return 1 if (command == "check" and errors) else 0
    if command in ("init", "set"):
        _out(f"wrote {cfg.relative(path)}")
    if command == "show":
        _out(json.dumps(data, indent=2, ensure_ascii=False))
    _out(f"next: {status['next']}")
    for p in problems:
        _out(f"  {p['level'].upper():5} [{p['stage']}] {p['text']}")
    if not problems:
        _out("  nothing missing")
    return 1 if (command == "check" and errors) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="warden", description="sprite-warden enforcement (check-call, hook, rules, pipeline)")
    add_parsers(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
