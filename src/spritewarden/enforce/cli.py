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

COMMANDS = ("hook", "check-call")


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="warden", description="sprite-warden enforcement (check-call, hook)")
    add_parsers(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
