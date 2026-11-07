"""Standalone, loopback-only synthetic Responses peer for SDK benchmarks.

Run with the native checkout's Python (aiohttp is its only dependency). Receipt
storage is bounded; operation counters and output verification remain exact.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

MARKER = re.compile(r"\bBENCH_RUN=([A-Za-z0-9-]+)(?![A-Za-z0-9-])")
RECEIPT_LIMIT = 4096


def final_text(op_id: str, size: int) -> str:
    seed = f"verified:{op_id}|"
    return (seed * ((size + len(seed) - 1) // len(seed)))[:size]


def encode(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def user_markers(items: list[dict[str, Any]]) -> list[str]:
    markers = []
    for item in items:
        if item.get("role") != "user":
            continue
        content = item.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in {"input_text", "text"}
            )
        if not isinstance(content, str):
            raise ValueError("user content must be text or a content list")
        markers.extend(MARKER.findall(content))
    return markers


def echo_identity(body: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, str]:
    declarations = list(body.get("tools", []))
    for item in items:
        if item.get("type") == "additional_tools":
            declarations.extend(item.get("tools", []))
    matches = []
    for declaration in declarations:
        if declaration.get("type") == "function" and declaration.get("name") == "echo":
            identity = {"name": "echo"}
            if declaration.get("namespace"):
                identity["namespace"] = declaration["namespace"]
            matches.append(identity)
        elif declaration.get("type") == "namespace":
            for tool in declaration.get("tools", []):
                if tool.get("type") == "function" and tool.get("name") == "echo":
                    matches.append({"name": "echo", "namespace": declaration["name"]})
    if len(matches) != 1:
        raise ValueError("exactly one echo function must be declared")
    return matches[0]


def assistant_hashes(items: list[dict[str, Any]]) -> list[str]:
    hashes = []
    for item in items:
        if item.get("role") != "assistant":
            continue
        content = item.get("content", "")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "output_text"
            )
        if not isinstance(content, str):
            raise ValueError("assistant content must be text or a content list")
        hashes.append(hashlib.sha256(content.encode()).hexdigest())
    return hashes


def user_hashes(items: list[dict[str, Any]]) -> list[str]:
    hashes = []
    for item in items:
        if item.get("role") != "user":
            continue
        content = item.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in {"input_text", "text"}
            )
        if isinstance(content, str) and MARKER.search(content):
            hashes.append(hashlib.sha256(content.encode()).hexdigest())
    return hashes


def output_text(value: Any) -> str:
    # Native accepts text content lists; v0 normally sends a raw string.
    if isinstance(value, list):
        if not all(
            isinstance(x, dict)
            and x.get("type") in {"input_text", "text", "output_text"}
            and isinstance(x.get("text"), str)
            for x in value
        ):
            raise ValueError("invalid tool output content list")
        return "".join(x["text"] for x in value)
    if not isinstance(value, str):
        raise ValueError("tool output must be a string or text content list")
    return value


@dataclass
class Operation:
    next_round: int = 0
    requests: int = 0
    expected: dict[str, str] = field(default_factory=dict)
    verified: set[str] = field(default_factory=set)
    status: str = "running"


class Peer:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.operations: dict[str, Operation] = {}
        self.receipts: deque[dict[str, Any]] = deque(maxlen=RECEIPT_LIMIT)
        self.requests = self.errors = self.body_bytes = self.response_bytes = 0
        self.verified_tool_outputs = self.emitted_text_chunks = 0

    def snapshot(self, *, summary: bool = False) -> dict[str, Any]:
        counters = {
            "requests": self.requests,
            "errors": self.errors,
            "body_bytes": self.body_bytes,
            "response_bytes": self.response_bytes,
            "verified_tool_outputs": self.verified_tool_outputs,
            "emitted_text_chunks": self.emitted_text_chunks,
            "receipts_dropped": max(0, self.requests - len(self.receipts)),
            "process_cpu_ns": time.process_time_ns(),
            "monotonic_ns": time.monotonic_ns(),
        }
        if summary:
            return counters
        return {
            **counters,
            "operations": {
                op_id: {
                    "requests": op.requests,
                    "status": op.status,
                    "next_round": op.next_round,
                    "issued_call_ids": list(op.expected),
                    "verified_tool_outputs": len(op.verified),
                }
                for op_id, op in self.operations.items()
            },
            "receipts": list(self.receipts),
        }

    def response(
        self, response_id: str, output: list[dict[str, Any]], status: str
    ) -> dict[str, Any]:
        return {
            "id": response_id,
            "object": "response",
            "created_at": 1,
            "model": "synthetic-benchmark",
            "status": status,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "temperature": 1.0,
            "top_p": 1.0,
            "usage": {
                "input_tokens": 11,
                "output_tokens": 3,
                "total_tokens": 14,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }

    def events(self, response: dict[str, Any], chunks: list[str]) -> list[dict[str, Any]]:
        events = [
            {
                "type": "response.created",
                "response": {
                    **response,
                    "status": "in_progress",
                    "output": [],
                    "usage": None,
                },
            },
            {
                "type": "response.in_progress",
                "response": {
                    **response,
                    "status": "in_progress",
                    "output": [],
                    "usage": None,
                },
            },
        ]
        for index, item in enumerate(response["output"]):
            base = {"item_id": item["id"], "output_index": index}
            added = {**item, "status": "in_progress"}
            if item["type"] == "function_call":
                added["arguments"] = ""
            else:
                added["content"] = []
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": added,
                }
            )
            if item["type"] == "function_call":
                events.extend(
                    [
                        {
                            "type": "response.function_call_arguments.delta",
                            **base,
                            "delta": item["arguments"],
                        },
                        {
                            "type": "response.function_call_arguments.done",
                            **base,
                            "arguments": item["arguments"],
                        },
                    ]
                )
            else:
                content_base = {**base, "content_index": 0}
                part = item["content"][0]
                events.append(
                    {
                        "type": "response.content_part.added",
                        **content_base,
                        "part": {**part, "text": ""},
                    }
                )
                events.extend(
                    {
                        "type": "response.output_text.delta",
                        **content_base,
                        "delta": chunk,
                        "logprobs": [],
                    }
                    for chunk in chunks
                )
                events.extend(
                    [
                        {
                            "type": "response.output_text.done",
                            **content_base,
                            "text": part["text"],
                            "logprobs": [],
                        },
                        {
                            "type": "response.content_part.done",
                            **content_base,
                            "part": part,
                        },
                    ]
                )
            events.append(
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": item,
                }
            )
        events.append({"type": "response.completed", "response": response})
        for sequence, event in enumerate(events):
            event["sequence_number"] = sequence
        return events

    async def handle(self, request):
        from aiohttp import web

        self.requests += 1
        request_number = self.requests
        receipt = {
            "request": request_number,
            "start_ns": time.monotonic_ns(),
            "op_id": None,
            "round": None,
            "input_item_count": 0,
            "history_user_markers": [],
            "status": "error",
        }
        self.receipts.append(receipt)
        op = None
        try:
            raw = await request.read()
            self.body_bytes += len(raw)
            receipt["body_bytes"] = len(raw)
            receipt["request_body_sha256"] = hashlib.sha256(raw).hexdigest()
            parse_start = time.monotonic_ns()
            body = json.loads(raw)
            receipt["body_parse_ns"] = time.monotonic_ns() - parse_start
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            items = body.get("input")
            if isinstance(items, str):
                items = [{"role": "user", "content": items}]
            if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
                raise ValueError("input must be text or a list of objects")
            receipt["input_item_count"] = len(items)
            if request_number == 1:
                receipt["tool_declarations"] = list(body.get("tools", [])) + [
                    tool
                    for item in items
                    if item.get("type") == "additional_tools"
                    for tool in item.get("tools", [])
                ]
            receipt["history_assistant_sha256s"] = assistant_hashes(items)
            receipt["history_tool_call_ids"] = [
                item.get("call_id") for item in items if item.get("type") == "function_call"
            ]
            receipt["history_tool_output_ids"] = [
                item.get("call_id") for item in items if item.get("type") == "function_call_output"
            ]
            markers = user_markers(items)
            receipt["history_user_markers"] = markers
            receipt["history_user_sha256s"] = user_hashes(items)
            if not markers:
                raise ValueError("missing user BENCH_RUN marker")
            op_id = markers[-1]
            receipt["op_id"] = op_id
            op = self.operations.setdefault(op_id, Operation())
            op.requests += 1
            round_number = op.next_round
            receipt["round"] = round_number
            if op.status != "running":
                raise ValueError("operation is already completed or failed")
            retained_expected = {}
            for historical_id in dict.fromkeys(markers[:-1]):
                historical = self.operations.get(historical_id)
                if historical is None or historical.status != "completed":
                    raise ValueError("history references an unknown or incomplete operation")
                retained_expected.update(historical.expected)
            retained_expected.update(op.expected)
            outputs = {}
            for item in items:
                if (
                    item.get("type") == "function_call_output"
                    and item.get("call_id") in retained_expected
                ):
                    call_id = item["call_id"]
                    if call_id in outputs:
                        raise ValueError("duplicate tool output")
                    outputs[call_id] = output_text(item.get("output"))
            for call_id, expected in retained_expected.items():
                actual = outputs.get(call_id)
                if actual != expected:
                    # Typed native string returns can be JSON-encoded strings.
                    try:
                        actual = json.loads(actual) if isinstance(actual, str) else None
                    except (ValueError, TypeError):
                        actual = None
                    if actual != expected:
                        raise ValueError(f"missing or wrong echo output for {call_id}")
            newly_verified = set(op.expected) - op.verified
            op.verified.update(newly_verified)
            self.verified_tool_outputs += len(newly_verified)
            receipt["verified_tool_outputs"] = len(newly_verified)
            response_id = f"resp_bench_{request_number}"
            chunks = []
            if round_number < self.args.tool_rounds:
                identity = echo_identity(body, items)
                output = []
                for index in range(self.args.tools_per_round):
                    call_id = f"call_{op_id}_{round_number}_{index}"
                    value = f"echo:{op_id}:{round_number}:{index}"
                    op.expected[call_id] = value
                    output.append(
                        {
                            "type": "function_call",
                            "id": f"fc_{call_id}",
                            "call_id": call_id,
                            **identity,
                            "status": "completed",
                            "arguments": json.dumps({"value": value}, separators=(",", ":")),
                        }
                    )
            else:
                text = final_text(op_id, self.args.output_chars)
                chunks = [
                    text[
                        len(text) * i // self.args.chunks : len(text) * (i + 1) // self.args.chunks
                    ]
                    for i in range(self.args.chunks)
                ]
                output = [
                    {
                        "type": "message",
                        "id": f"msg_{response_id}",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": text,
                                "annotations": [],
                                "logprobs": [],
                            }
                        ],
                    }
                ]
            response = self.response(response_id, output, "completed")
            streaming = body.get("stream", False)
            if not isinstance(streaming, bool):
                raise ValueError("stream must be boolean")
            if self.args.delay_ms:
                await asyncio.sleep(self.args.delay_ms / 1000)
            if streaming:
                result = web.StreamResponse(
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                    }
                )
                await result.prepare(request)
                for event in self.events(response, chunks):
                    frame = (
                        b"event: " + event["type"].encode() + b"\ndata: " + encode(event) + b"\n\n"
                    )
                    await result.write(frame)
                    self.response_bytes += len(frame)
                    if event["type"] == "response.output_text.delta":
                        self.emitted_text_chunks += 1
                await result.write_eof()
            else:
                payload = encode(response)
                result = web.Response(body=payload, content_type="application/json")
                self.response_bytes += len(payload)
            op.next_round += 1
            if round_number == self.args.tool_rounds:
                op.status = "completed"
            receipt["status"] = "ok"
            receipt["stream"] = streaming
            receipt["text_chunks"] = len(chunks) if streaming else 0
            return result
        except Exception as error:
            self.errors += 1
            if op is not None:
                op.status = "error"
            receipt["error"] = f"{type(error).__name__}: {error}"
            payload = encode(
                {
                    "error": {
                        "message": receipt["error"],
                        "type": "benchmark_protocol_error",
                    }
                }
            )
            self.response_bytes += len(payload)
            return web.Response(status=400, body=payload, content_type="application/json")
        finally:
            receipt["end_ns"] = time.monotonic_ns()


async def serve(args: argparse.Namespace) -> None:
    from aiohttp import web

    peer = Peer(args)
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_post("/v1/responses", peer.handle)

    async def stats(request):
        return web.json_response(peer.snapshot(summary=request.query.get("summary") == "1"))

    async def health(request):
        return web.json_response({"ok": True})

    app.router.add_get("/stats", stats)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port)
    await site.start()
    port = runner.addresses[0][1]
    print(json.dumps({"base_url": f"http://127.0.0.1:{port}/v1"}), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--tools-per-round", type=int, default=1)
    parser.add_argument("--tool-rounds", type=int, default=1)
    parser.add_argument("--chunks", type=int, default=8)
    parser.add_argument("--output-chars", type=int, default=256)
    parser.add_argument("--delay-ms", type=float, default=0)
    args = parser.parse_args()
    if not (
        0 <= args.port <= 65535
        and args.tools_per_round >= 0
        and args.tool_rounds >= 0
        and args.chunks > 0
        and args.output_chars >= 0
        and args.delay_ms >= 0
    ):
        parser.error("invalid negative count, delay, port, or zero chunks")
    if args.tool_rounds and not args.tools_per_round:
        parser.error("tool rounds require at least one tool per round")
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
