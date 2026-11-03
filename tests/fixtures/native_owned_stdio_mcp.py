"""A native stdio peer with process state, external state, and observable retirement."""

import argparse
import atexit
import json
import os
import signal
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--stop-release", type=Path)
    parser.add_argument("--startup-at", type=int)
    parser.add_argument("--startup-mode", choices=("prompt", "catalog", "hold"))
    parser.add_argument("--form-schema", type=Path)
    parser.add_argument(
        "--form-method",
        choices=("elicitation/create", "openai/form"),
        default="elicitation/create",
    )
    parser.add_argument("--url-flow", action="store_true")
    parser.add_argument("--url-batch", action="store_true")
    parser.add_argument("--url-early", action="store_true")
    parser.add_argument("--completion-release", type=Path)
    options = parser.parse_args()
    schema = (
        json.loads(options.form_schema.read_text())
        if options.form_schema is not None
        else {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        }
    )
    options.log.mkdir(exist_ok=True)
    generation = 1
    while True:
        try:
            (options.log / f"generation-{generation}").mkdir()
            break
        except FileExistsError:
            generation += 1
    log = options.log / f"{os.getpid()}.jsonl"
    calls = 0
    pending = None

    def record(event, **values):
        with log.open("a") as stream:
            stream.write(json.dumps({"event": event, "pid": os.getpid(), **values}) + "\n")

    def emit(value):
        print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)

    def form(reason):
        emit(
            {
                "id": 873,
                "method": "elicitation/create" if options.url_flow else options.form_method,
                "params": {
                    "mode": "url",
                    "message": f"{reason}:{os.getpid()}",
                    "url": "https://example.invalid/owned-child-never-open",
                    "elicitationId": "owned-child-url-A",
                }
                if options.url_flow
                else {
                    "mode": "form",
                    "message": f"{reason}:{os.getpid()}",
                    "requestedSchema": schema,
                },
            }
        )
        record("question", reason=reason)
        if options.url_flow and options.url_early:
            emit(
                {
                    "method": "notifications/elicitation/complete",
                    "params": {"elicitationId": "owned-child-url-A"},
                }
            )
            record("early_url_complete")

    def retire(trigger):
        record("retirement", trigger=trigger)
        if calls and options.stop_release is not None:
            # This is old buffered/background stdout emitted after retirement began.
            try:
                form("retired-stdout")
            except BrokenPipeError:
                # Native transport close may already have retired its stdout reader.
                sys.stdout = open(os.devnull, "w")
                record("old_stdout", delivered=False)
            else:
                record("old_stdout", delivered=True)
            record("cleanup_wait", trigger=trigger)
            while not (options.stop_release / str(os.getpid())).exists():
                time.sleep(0.01)

    def stop(signum, _frame):
        record("signal", signal=signum)
        retire("signal")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    atexit.register(record, "exit")
    record("start", generation=generation)

    def catalog():
        return {
            "tools": []
            if options.startup_mode == "catalog" and generation == options.startup_at
            else [
                {
                    "name": name,
                    "description": "Read process and external state",
                    "annotations": {"readOnlyHint": True},
                    "inputSchema": {
                        "type": "object",
                        "properties": {"key": {"type": "string"}},
                        "required": ["key"],
                    },
                }
                for name in ("search", "state")
            ]
        }

    for line in sys.stdin:
        message = json.loads(line)
        record("request", message=message)
        method = message.get("method")
        if method is None and message.get("id") == 873 and pending is not None:
            record("answer", message=message)
            kind, request_id = pending
            pending = None
            if kind == "startup":
                result = catalog()
            else:
                answer = message.get("result", {}).get("content")
                if message.get("result", {}).get("action") == "accept":
                    if options.url_flow:
                        record("url_accepted")
                        if not options.url_early:
                            assert options.completion_release is not None
                            while not (options.completion_release / str(os.getpid())).exists():
                                time.sleep(0.01)
                            emit(
                                {
                                    "method": "notifications/elicitation/complete",
                                    "params": {"elicitationId": "owned-child-url-A"},
                                }
                            )
                            record("url_complete")
                    with options.state.open("a") as stream:
                        stream.write(
                            json.dumps(
                                {
                                    "pid": os.getpid(),
                                    "local_calls": calls,
                                    "answer": answer,
                                }
                            )
                            + "\n"
                        )
                    record("effect", local_calls=calls, answer=answer)
                result = {"content": [{"type": "text", "text": f"local-calls:{calls}"}]}
            emit({"id": request_id, "result": result})
            continue
        if "id" not in message:
            continue
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25"
                if options.url_flow or options.url_batch
                else "2025-06-18",
                "capabilities": {
                    "tools": {},
                    "experimental": {"codex/tool-catalog-cache": {"cacheable": False}},
                },
                "serverInfo": {"name": "owned-stdio", "version": "1"},
            }
        elif method == "tools/list":
            if generation == options.startup_at:
                if options.startup_mode == "hold":
                    record("startup_wait")
                    while True:
                        time.sleep(0.01)
                if options.startup_mode == "prompt":
                    pending = ("startup", message["id"])
                    form("startup")
                    continue
            result = catalog()
        elif method == "tools/call":
            calls += 1
            if message["params"]["name"] == "state":
                record("state", local_calls=calls)
                result = {"content": [{"type": "text", "text": f"local-calls:{calls}"}]}
            else:
                if options.url_batch:
                    identities = ("owned-batch-A", "owned-batch-B")
                    emit(
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32042,
                                "message": "Owned URL batch required",
                                "data": {
                                    "elicitations": [
                                        {
                                            "mode": "url",
                                            "elicitationId": identity,
                                            "url": "https://example.invalid/owned-child-never-open",
                                            "message": "Owned synthetic batch",
                                        }
                                        for identity in identities
                                    ]
                                },
                            },
                        }
                    )
                    record("batch_error")
                    assert options.completion_release is not None
                    while not (options.completion_release / str(os.getpid())).exists():
                        time.sleep(0.01)
                    for identity in reversed(identities):
                        emit(
                            {
                                "method": "notifications/elicitation/complete",
                                "params": {"elicitationId": identity},
                            }
                        )
                    record("batch_complete")
                    continue
                pending = ("tool", message["id"])
                form("tool")
                continue
        else:
            result = {}
        emit({"id": message["id"], "result": result})

    # Support the inner transport's close-input path as well as the outer
    # wrapper's signal-first retirement without assuming their order.
    retire("eof")


if __name__ == "__main__":
    main()
