"""Small protocol checks; --check-delay adds a two-request delay control.

Use the native .venv Python. This checks correctness, not benchmark throughput.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import ClientSession
from openai.types.responses import Response, ResponseStreamEvent
from pydantic import TypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from owned_process import defer_cleanup_signals, read_ready_line, terminate_group
from protocol_peer import final_text


async def validate(check_delay: bool) -> None:
    peer = Path(__file__).with_name("protocol_peer.py")
    process = None
    try:
        with defer_cleanup_signals():
            # Start the sole peer before requests, outside benchmark measurement.
            process = subprocess.Popen(  # noqa: ASYNC220
                [
                    sys.executable,
                    str(peer),
                    "--tool-rounds",
                    "2",
                    "--tools-per-round",
                    "2",
                    "--chunks",
                    "7",
                    "--output-chars",
                    "123",
                ],
                stdout=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        base = json.loads(read_ready_line(process))["base_url"]
        async with ClientSession() as client:

            async def post(items, stream=False, expected_status=200):
                body = {
                    "input": items,
                    "stream": stream,
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "functions",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "echo",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {"value": {"type": "string"}},
                                        "required": ["value"],
                                        "additionalProperties": False,
                                    },
                                }
                            ],
                        }
                    ],
                }
                async with client.post(base + "/responses", json=body) as response:
                    wire = await response.text()
                    assert response.status == expected_status, wire
                if expected_status != 200:
                    return json.loads(wire)
                if stream:
                    events = [
                        json.loads(line[6:])
                        for line in wire.splitlines()
                        if line.startswith("data: ")
                    ]
                    assert [e["sequence_number"] for e in events] == list(range(len(events)))
                    for event in events:
                        TypeAdapter(ResponseStreamEvent).validate_python(event)
                    result = events[-1]["response"]
                    if result["output"][0]["type"] == "message":
                        chunks = [
                            e["delta"] for e in events if e["type"] == "response.output_text.delta"
                        ]
                        assert len(chunks) == 7
                        assert "".join(chunks) == result["output"][0]["content"][0]["text"]
                else:
                    result = json.loads(wire)
                Response.model_validate(result)
                return result

            history = []
            for op_id, stream in (("json", False), ("sse", True)):
                history.append({"role": "user", "content": f"BENCH_RUN={op_id}"})
                for round_no in range(3):
                    result = await post(history, stream)
                    if round_no < 2:
                        for call in result["output"]:
                            assert call["namespace"] == "functions"
                            value = json.loads(call["arguments"])["value"]
                            history.extend(
                                [
                                    call,
                                    {
                                        "type": "function_call_output",
                                        "call_id": call["call_id"],
                                        "output": json.dumps(value) if stream else value,
                                    },
                                ]
                            )
                    else:
                        assert result["output"][0]["content"][0]["text"] == final_text(op_id, 123)
                        history.extend(result["output"])
            # Current output must be present and match exactly, with no arbitrary object unwraps.
            for op_id, bad_value in (
                ("missing", None),
                ("wrong", "wrong"),
                ("object", '{"value":"wrong"}'),
            ):
                items = [{"role": "user", "content": f"BENCH_RUN={op_id}"}]
                result = await post(items)
                for call in result["output"]:
                    items.append(call)
                    if bad_value is not None:
                        items.append(
                            {
                                "type": "function_call_output",
                                "call_id": call["call_id"],
                                "output": bad_value,
                            }
                        )
                await post(items, expected_status=400)
            await post([{"role": "user", "content": "no marker"}], expected_status=400)
            await post(
                [
                    {"role": "user", "content": "BENCH_RUN=unknown-history"},
                    {"role": "user", "content": "BENCH_RUN=new-op"},
                ],
                expected_status=400,
            )
            # Removing an older completed operation's output must reject a new operation.
            damaged = [item for item in history if item.get("type") != "function_call_output"]
            damaged.append({"role": "user", "content": "BENCH_RUN=damaged-history"})
            await post(damaged, expected_status=400)
            async with client.get(base[:-3] + "/stats") as response:
                stats = await response.json()
            assert stats["errors"] == 6
            assert stats["verified_tool_outputs"] == 8
            assert stats["emitted_text_chunks"] == 7
            assert stats["operations"]["sse"]["status"] == "completed"
            assert stats["receipts"][0]["tool_declarations"][0]["name"] == "functions"
            receipt = next(r for r in stats["receipts"] if r["op_id"] == "sse")
            assert receipt["history_user_markers"] == ["json", "sse"]
            assert receipt["history_assistant_sha256s"] == [
                hashlib.sha256(final_text("json", 123).encode()).hexdigest()
            ]
            assert (
                receipt["history_tool_call_ids"] == stats["operations"]["json"]["issued_call_ids"]
            )
            assert receipt["history_tool_output_ids"] == receipt["history_tool_call_ids"]
            assert all(r["start_ns"] <= r["end_ns"] for r in stats["receipts"])
            async with client.get(base[:-3] + "/stats?summary=1") as response:
                summary = await response.json()
            assert "operations" not in summary and "receipts" not in summary
            assert summary["requests"] == stats["requests"]
            assert summary["process_cpu_ns"] >= stats["process_cpu_ns"] > 0
            assert summary["monotonic_ns"] >= stats["monotonic_ns"] > 0
            assert all(r["body_parse_ns"] >= 0 for r in stats["receipts"])
            print(
                "PASS: JSON/full SSE schemas; exact output/chunks; "
                "current/historical tool verification; negative controls; history hashes; "
                "namespace schemas; counters/summary CPU timestamps"
            )
    finally:
        if process is not None:
            with defer_cleanup_signals():
                terminate_group(process)
                process.stdout.close()

    if check_delay:
        for stream in (False, True):
            process = None
            try:
                with defer_cleanup_signals():
                    # Start the sole peer before requests, outside benchmark measurement.
                    process = subprocess.Popen(  # noqa: ASYNC220
                        [sys.executable, str(peer), "--tool-rounds", "0", "--delay-ms", "40"],
                        stdout=subprocess.PIPE,
                        text=True,
                        start_new_session=True,
                    )
                base = json.loads(read_ready_line(process))["base_url"]
                async with ClientSession() as client:
                    start = time.monotonic_ns()
                    async with client.post(
                        base + "/responses",
                        json={
                            "input": "BENCH_RUN=delay",
                            "stream": stream,
                        },
                    ) as response:
                        assert response.status == 200
                        await response.read()
                    elapsed_ms = (time.monotonic_ns() - start) / 1e6
                    assert elapsed_ms >= 39, elapsed_ms
                    print(
                        f"PASS: {'SSE' if stream else 'JSON'} 40ms injected delay; "
                        f"elapsed {elapsed_ms:.1f}ms"
                    )
            finally:
                if process is not None:
                    with defer_cleanup_signals():
                        terminate_group(process)
                        process.stdout.close()


def main():
    if sys.flags.optimize or os.environ.get("PYTHONOPTIMIZE"):
        raise RuntimeError("Run without optimization and clear PYTHONOPTIMIZE")

    def stop_requested(signum, _frame):
        raise KeyboardInterrupt(f"Received signal {signum}")

    signal.signal(signal.SIGTERM, stop_requested)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-delay", action="store_true")
    asyncio.run(validate(parser.parse_args().check_delay))


if __name__ == "__main__":
    main()
