"""Trusted read-only stdio MCP catalog; no third-party dependencies.

The native SDK starts this process. stdout carries JSON-RPC only. This small
peer implements just initialization, listing, ping and one synthetic lookup.
It is an example server, not a general-purpose MCP framework.
"""

import argparse
import atexit
import json
import os
import signal
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--fail-start", action="store_true")
    args = parser.parse_args()

    def record(event: str) -> None:
        if args.audit is not None:
            with args.audit.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, "pid": os.getpid()}) + "\n")

    def stop(_signal: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    atexit.register(record, "exit")
    record("start")
    if args.fail_start:
        raise SystemExit("Synthetic startup failure")
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-catalog", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "lookup",
                        "description": "Read a synthetic catalog item by SKU.",
                        "annotations": {"readOnlyHint": True},
                        "inputSchema": {
                            "type": "object",
                            "properties": {"sku": {"type": "string"}},
                            "required": ["sku"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        elif method == "tools/call":
            params = message.get("params", {})
            if params.get("name") == "lookup" and params.get("arguments") == {"sku": "PAPER"}:
                record("lookup")
                result = {
                    "content": [{"type": "text", "text": "PAPER: 5 dollars"}],
                    "isError": False,
                }
            else:
                result = {
                    "content": [{"type": "text", "text": "Unknown synthetic SKU"}],
                    "isError": True,
                }
        elif method == "ping":
            result = {}
        else:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                ),
                flush=True,
            )
            continue
        print(
            json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}),
            flush=True,
        )


if __name__ == "__main__":
    main()
