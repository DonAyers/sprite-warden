"""Minimal MCP stdio server fixture for tests (initialize + tools/list only)."""

import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if "id" not in msg:
            continue
        method = msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo-fixture", "version": "0"},
            }})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                {"name": "make_sprite", "description": "make a sprite",
                 "inputSchema": {"type": "object", "properties": {"size": {"type": "integer"}}}},
                {"name": "deprecated_op", "description": "old and deprecated"},
            ]}})
        else:
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})


if __name__ == "__main__":
    main()
