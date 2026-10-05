"""Minimal MCP stdio client to capture a provider's `tools/list` surface for drift detection.

Provider MCP servers change their tools/params/limits over time; probing tools/list
gives a deterministic, machine-readable snapshot we can diff (the "MCP tools don't
keep up" problem).

Configure probe commands via env SPRITEWARDEN_MCP_PROBES, a JSON object mapping a
provider name to an argv list, e.g.:
    {"pixellab": ["python", "-m", "pixellab_mcp"]}
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from typing import Any

from .models import Source

PROTOCOL_VERSION = "2025-06-18"


def probe_config() -> dict[str, list[str]]:
    raw = os.environ.get("SPRITEWARDEN_MCP_PROBES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return {str(k): list(v) for k, v in data.items() if isinstance(v, (list, tuple)) and v}


def _reader(proc: subprocess.Popen, out: "queue.Queue[str | None]") -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        out.put(line)
    out.put(None)


def probe_stdio(
    command: list[str],
    timeout: float = 25.0,
    cwd: str | None = None,
) -> list[dict[str, Any]] | None:
    """Spawn an MCP stdio server, run initialize + tools/list, and return the tool list."""
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=cwd,
        )
    except Exception:
        return None

    lines: "queue.Queue[str | None]" = queue.Queue()
    threading.Thread(target=_reader, args=(proc, lines), daemon=True).start()

    def send(obj: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def await_id(target_id: int) -> dict[str, Any] | None:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == target_id:
                return msg
        return None

    try:
        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "spritewarden", "version": "0.1"},
            },
        })
        if await_id(1) is None:
            return None
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        resp = await_id(2)
        if not resp:
            return None
        tools = (resp.get("result") or {}).get("tools")
        return tools if isinstance(tools, list) else None
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def probe_source(source: Source, config: dict[str, list[str]] | None = None) -> list[dict[str, Any]] | None:
    """Probe the MCP server configured for this source's provider, or None if unconfigured."""
    cfg = config if config is not None else probe_config()
    command = cfg.get(source.provider)
    if not command:
        return None
    return probe_stdio(command)


def normalize_tools(tools: list[dict[str, Any]]) -> str:
    """Stable JSON rendering of a tool list for hashing/diffing (sorted by name)."""
    slim = []
    for t in tools:
        slim.append({
            "name": t.get("name"),
            "description": t.get("description", ""),
            "inputSchema": t.get("inputSchema") or t.get("input_schema") or {},
        })
    slim.sort(key=lambda t: str(t["name"]))
    return json.dumps(slim, indent=2, sort_keys=True, ensure_ascii=False)
