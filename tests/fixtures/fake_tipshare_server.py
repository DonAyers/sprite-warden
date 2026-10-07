"""A fake tipshare MCP stdio server for tests: `search` and `share_tip` over a JSON file.

The store path comes from env FAKE_TIPSHARE_STORE: {"tips": [...], "calls": [...]}. Nothing
here talks to the real tipshare knowledge base.
"""

import json
import os
import sys
from pathlib import Path

STORE = Path(os.environ["FAKE_TIPSHARE_STORE"])


def load():
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tips": [], "calls": []}


def save(data):
    STORE.write_text(json.dumps(data), encoding="utf-8")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def text(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:
            continue
        method = msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-tipshare", "version": "0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "search"}, {"name": "share_tip"}]}})
        elif method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments") or {}
            data = load()
            data["calls"].append({"tool": name, "arguments": args, "agent": os.environ.get("TIPSHARE_AGENT_ID")})
            if name == "search":
                if any(ch in args.get("query", "") for ch in ":()\"*"):
                    save(data)
                    send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                        "isError": True, "content": [{"type": "text", "text": "fts5: syntax error"}]}})
                    continue
                words = args.get("query", "").lower().split()
                hits = [{"id": t["id"], "title": t["title"], "topic_slug": t["topic_slug"]}
                        for t in data["tips"]
                        if t["topic_slug"] == args.get("topic_slug", t["topic_slug"])
                        and all(w in t["title"].lower() for w in words)]
                save(data)
                send({"jsonrpc": "2.0", "id": msg["id"], "result": text(hits)})
            elif name == "share_tip":
                if "FAIL" in args.get("title", ""):
                    save(data)
                    send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                        "isError": True, "content": [{"type": "text", "text": "rejected: looks like a secret"}]}})
                    continue
                tip = {"id": 100 + len(data["tips"]), **args}
                data["tips"].append(tip)
                save(data)
                send({"jsonrpc": "2.0", "id": msg["id"], "result": text({"id": tip["id"], "uuid": f"u-{tip['id']}"})})
            else:
                save(data)
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"unknown tool {name}"}})
        else:
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})


if __name__ == "__main__":
    main()
