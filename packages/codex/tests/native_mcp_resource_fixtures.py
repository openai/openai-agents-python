"""Synthetic resource protocol shared by HTTP and the real stdio subprocess."""

from __future__ import annotations

import json
from typing import Any

URI = "fixture://private/report"
CURSOR = "opaque+/= cursor:?☃"
TEXT = "private-resource-text-573"
BLOB = "AAH/SGVsbG8="


def resource_result(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "resources/list":
        return {
            "resources": []
            if "cursor" in params
            else [
                {
                    "uri": URI,
                    "name": "report",
                    "title": "Private report",
                    "description": "Synthetic resource",
                    "mimeType": "text/plain",
                    "size": 25,
                    "annotations": {"audience": ["user"], "priority": 0.5},
                    "_meta": {"revision": "one"},
                }
            ],
            **({} if "cursor" in params else {"nextCursor": CURSOR}),
            "_meta": {"page": "resources"},
        }
    if method == "resources/templates/list":
        return {
            "resourceTemplates": []
            if "cursor" in params
            else [
                {
                    "uriTemplate": "fixture://private/{name}",
                    "name": "reports",
                    "title": "Reports",
                    "description": "Synthetic template",
                    "mimeType": "text/plain",
                    "annotations": {"audience": ["assistant"]},
                    "_meta": {"revision": "one"},
                }
            ],
            **({} if "cursor" in params else {"nextCursor": CURSOR}),
        }
    return {
        "contents": [
            {"uri": params["uri"], "mimeType": "text/plain", "text": TEXT, "_meta": {"part": 1}},
            {
                "uri": params["uri"],
                "mimeType": "application/octet-stream",
                "blob": BLOB,
                "_meta": {"part": 2},
            },
        ],
        "resultType": "complete",
        "ttlMs": 0,
        "cacheScope": "private",
        "_meta": {"source": "fixture"},
    }


def main() -> None:
    import argparse
    import atexit
    import os
    import signal
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--hold", action="store_true")
    parser.add_argument("--elicit", action="store_true")
    options = parser.parse_args()
    options.log.mkdir()
    log = options.log / f"{os.getpid()}.jsonl"

    def record(event: str, **values: Any) -> None:
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "pid": os.getpid(), **values}) + "\n")

    def send(message: dict[str, Any]) -> None:
        print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)

    def stop(*_: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    atexit.register(record, "exit")
    record("start")
    pending = None
    for line in sys.stdin.buffer:
        message = json.loads(line)
        record("request", message=message)
        method = message.get("method")
        if method is None and message.get("id") == 573:
            send({"id": pending, "result": resource_result("resources/read", {"uri": URI})})
            continue
        if "id" not in message:
            continue
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"resources": {}},
                "serverInfo": {"name": "resources-only", "version": "1"},
            }
        elif method == "resources/read" and options.hold:
            continue
        elif method == "resources/read" and options.elicit:
            pending = message["id"]
            send(
                {
                    "id": 573,
                    "method": "elicitation/create",
                    "params": {
                        "mode": "form",
                        "message": "Synthetic resource configuration",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {"answer": {"type": "string"}},
                            "required": ["answer"],
                        },
                    },
                }
            )
            continue
        elif method.startswith("resources/"):
            result = resource_result(method, message.get("params", {}))
        else:
            send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported method"}})
            continue
        send({"id": message["id"], "result": result})


if __name__ == "__main__":
    main()
