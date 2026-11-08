"""Synthetic prompt data shared by HTTP tests and the real stdio peer."""

from __future__ import annotations

import json
from typing import Any

CURSOR = "opaque+/= prompt:?☃"
TEXT = "private-prompt-text-574"
PROMPT = {
    "name": "review",
    "title": "Review",
    "description": "Synthetic review prompt",
    "arguments": [
        {"name": "language", "title": "Language", "description": "Choice", "required": True}
    ],
    "icons": [{"src": "https://example.invalid/prompt.svg", "mimeType": "image/svg+xml"}],
    "_meta": {"revision": "one"},
}


def prompt_result(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "prompts/list":
        cursor = params.get("cursor")
        if cursor is None:
            return {"prompts": [PROMPT], "nextCursor": "", "_meta": {"page": 1}}
        if cursor == "":
            return {"prompts": [], "nextCursor": CURSOR, "_meta": {"page": 2}}
        return {"prompts": [PROMPT, {"name": "welcome"}], "_meta": {"page": 3}}
    metadata = {"annotations": {"audience": ["user"], "priority": 0.5}, "_meta": {"content": True}}
    return {
        "description": "Caller-owned preview",
        "messages": [
            {"role": "user", "content": {"type": "text", "text": TEXT, **metadata}},
            {
                "role": "assistant",
                "content": {
                    "type": "image",
                    "data": "AAH/",
                    "mimeType": "image/png",
                    **metadata,
                },
            },
            {
                "role": "user",
                "content": {
                    "type": "audio",
                    "data": "AAH/",
                    "mimeType": "audio/wav",
                    **metadata,
                },
            },
            {
                "role": "user",
                "content": {
                    "type": "resource",
                    "resource": {
                        "uri": "fixture://prompt/text",
                        "text": TEXT,
                        "mimeType": "text/plain",
                        "_meta": {"nested": "text"},
                    },
                    **metadata,
                },
            },
            {
                "role": "assistant",
                "content": {
                    "type": "resource",
                    "resource": {
                        "uri": "fixture://prompt/blob",
                        "blob": "AAH/",
                        "_meta": {"nested": "blob"},
                    },
                    **metadata,
                },
            },
            {
                "role": "user",
                "content": {
                    "type": "resource_link",
                    "uri": "fixture://prompt/link",
                    "name": "link",
                    "title": "Linked resource",
                    "mimeType": "text/plain",
                    **metadata,
                },
            },
        ],
        "_meta": {"request": {key: value for key, value in params.items() if key != "_meta"}},
        **({"resultType": "complete"} if params.get("name") == "complete" else {}),
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
    # Binary stdin preserves UTF-8 protocol JSON on Windows.
    for line in sys.stdin.buffer:
        message = json.loads(line)
        record("request", message=message)
        if "id" not in message or "method" not in message:
            continue
        method = message["method"]
        params = message.get("params", {})
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"prompts": {}},
                "serverInfo": {"name": "prompts-only", "version": "1"},
            }
        elif method == "prompts/list" and "cursor" in params and options.hold:
            continue
        elif method == "prompts/get" and options.elicit:
            send(
                {
                    "id": 574,
                    "method": "elicitation/create",
                    "params": {
                        "mode": "form",
                        "message": "Synthetic prompt configuration",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {"choice": {"type": "string"}},
                        },
                    },
                }
            )
            continue
        elif method.startswith("prompts/"):
            result = prompt_result(method, params)
        else:
            send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported"}})
            continue
        send({"id": message["id"], "result": result})


if __name__ == "__main__":
    main()
