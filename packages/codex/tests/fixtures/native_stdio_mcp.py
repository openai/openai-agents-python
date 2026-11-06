"""Small stdlib MCP peer launched only by the native test client."""

import argparse
import atexit
import json
import os
import signal
import sys
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--label", default="private-stdio-result")
    parser.add_argument("--field", default="key")
    parser.add_argument("--env-var", default="N19_STDIO_CURRENT")
    parser.add_argument("--hold", action="store_true")
    parser.add_argument("--writable", action="store_true")
    parser.add_argument("--invalid-init", action="store_true")
    parser.add_argument("--missing-tool", action="store_true")
    parser.add_argument("--background-form-on-second-call", action="store_true")
    parser.add_argument("--list-release", type=Path)
    parser.add_argument("--form-schema", type=Path)
    options = parser.parse_args()
    # Concurrent descendants must not share Windows append-file positioning.
    options.log.mkdir(exist_ok=True)
    log = options.log / f"{os.getpid()}.jsonl"

    def record(event: str, **values: object) -> None:
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "pid": os.getpid(), **values}) + "\n")

    def stop(signum: int, _frame: object) -> None:
        record("signal", signal=signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    atexit.register(record, "exit")
    record("start", cwd=os.getcwd(), selected_env=os.environ.get(options.env_var))
    calls = 0
    pending_call = None
    for line in sys.stdin:
        message = json.loads(line)
        record("request", message=message)
        method = message.get("method")
        if (
            (options.background_form_on_second_call or options.form_schema is not None)
            and method is None
            and message.get("id") == 873
        ):
            assert pending_call is not None
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": pending_call,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(message)
                                    if options.form_schema is not None
                                    else options.label,
                                }
                            ],
                        },
                    }
                ),
                flush=True,
            )
            pending_call = None
            continue
        if "id" not in message:
            continue
        if method == "initialize":
            result = (
                {}
                if options.invalid_init
                else {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "native-stdio-fixture", "version": "1"},
                }
            )
        elif method == "tools/list":
            if options.list_release is not None:
                while not options.list_release.exists():
                    time.sleep(0.01)
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": "Read a synthetic stdio record",
                        "annotations": (
                            {"readOnlyHint": False, "destructiveHint": True}
                            if options.writable
                            else {"readOnlyHint": True}
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {options.field: {"type": "string"}},
                            "required": [options.field],
                            "additionalProperties": False,
                        },
                    }
                    for name in (("other",) if options.missing_tool else ("search", "excluded"))
                ]
            }
        elif method == "tools/call":
            calls += 1
            if options.background_form_on_second_call and calls == 2:
                pending_call = message["id"]
                # A background request associated with the earlier call cannot
                # authorize stopping the second read on this same connection.
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 873,
                            "method": "elicitation/create",
                            "params": {
                                "mode": "form",
                                "message": "Configuration for earlier background task",
                                "requestedSchema": {
                                    "type": "object",
                                    "properties": {"answer": {"type": "string"}},
                                    "required": ["answer"],
                                },
                            },
                        }
                    ),
                    flush=True,
                )
                continue
            if options.form_schema is not None:
                pending_call = message["id"]
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 873,
                            "method": "elicitation/create",
                            "params": {
                                "mode": "form",
                                "message": "Synthetic stdio form",
                                "requestedSchema": json.loads(options.form_schema.read_text()),
                            },
                        }
                    ),
                    flush=True,
                )
                continue
            if options.hold:
                continue
            result = {
                "content": [{"type": "text", "text": options.label}],
                "structuredContent": {"receipt": options.label},
                "isError": False,
            }
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
