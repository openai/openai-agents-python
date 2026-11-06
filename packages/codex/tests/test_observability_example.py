"""Fresh processes prove collector receipt without sharing native telemetry owners."""

import json
import os
import socket
import subprocess
import sys
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).parents[3]


def rows(artifact, signal):
    resource_key, scope_key, row_key = (
        ("resourceSpans", "scopeSpans", "spans")
        if signal == "traces"
        else ("resourceLogs", "scopeLogs", "logRecords")
    )
    return [
        row
        for batch in artifact["batches"]
        if batch["signal"] == signal
        for resource in batch["data"][resource_key]
        for scope in resource[scope_key]
        for row in scope[row_key]
    ]


def attributes(row):
    return {value["key"]: next(iter(value["value"].values())) for value in row["attributes"]}


@pytest.mark.parametrize(
    "scenario,outcome",
    [
        ("tool", "tool_callback_exception"),
        ("tool-error", "completed"),
        ("validation", "output_validation_failure"),
        ("provider", "provider_failure"),
    ],
)
def test_native_export_reaches_real_collector(tmp_path, scenario, outcome):
    output = tmp_path / "trace.json"
    env = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "examples.codex.observability.trace_walkthrough",
            "--scenario",
            scenario,
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    artifact = json.loads(output.read_text())
    receipt = artifact["receipt"]
    assert receipt["outcome"] == outcome
    assert receipt["session_id"] and receipt["turn_id"]
    spans = rows(artifact, "traces")
    assert any(
        span["name"] == "run_turn"
        and span["traceId"]
        and attributes(span).get("example.run_id") == artifact["run_id"]
        for span in spans
    )
    workflow = next(span for span in spans if span["name"] == "synthetic walkthrough")
    step = next(span for span in spans if span["name"] == "exercise scenario")
    assert step["traceId"] == workflow["traceId"]
    assert step["parentSpanId"] == workflow["spanId"]
    assert any(
        span["name"] == "run_turn" and span["traceId"] == workflow["traceId"] for span in spans
    )
    logs = [attributes(row) for row in rows(artifact, "logs")]
    requests = [log for log in logs if log.get("event.name") == "codex.api_request"]
    assert requests
    assert all(log["conversation.id"] == receipt["session_id"] for log in requests)
    assert {str(log["http.response.status_code"]) for log in requests} == (
        {"401"} if scenario == "provider" else {"200"}
    )
    if scenario.startswith("tool"):
        assert receipt["tool_calls"] == ["A"]
        assert receipt["history"]["function_results"][0]["name"] == "lookup"
    else:
        assert receipt["tool_calls"] == []
    for secret in ("synthetic-provider-key", "synthetic-agents-testing-key"):
        assert secret not in output.read_text() + result.stdout + result.stderr


async def test_history_reader_preserves_absent_fields_and_unpaired_results():
    from examples.codex.observability.history_reader import read_history

    class RecordedSession:
        async def turns(self):
            yield {
                "id": "old",
                "created_at": None,
                "started_at": None,
                "completed_at": None,
                "usage": None,
            }

        async def items(self):
            yield {
                "type": "function_call_output",
                "id": "output",
                "turn_id": "old",
                "call_id": "unpaired",
                "status": "completed",
                "output": "synthetic",
                "error": None,
            }

    history = await read_history(RecordedSession())
    assert history["turns"][0]["created_at"] is None
    assert history["turns"][0]["usage"] is None
    assert history["function_results"][0]["name"] is None
    assert history["function_results"][0]["output"] == "synthetic"


def test_viewer_escapes_received_content():
    from urllib.request import urlopen

    from examples.codex.observability.trace_walkthrough import Collector

    collector = Collector()
    collector.batches.append({"synthetic": "<script>alert(1)</script>"})
    with collector.serve() as receiver, urlopen(receiver.url) as response:
        page = response.read().decode()
    assert "&lt;script&gt;" in page
    assert "<script>" not in page


def test_numeric_loopback_collector_serves_without_reverse_dns(monkeypatch):
    from examples.codex.observability.trace_walkthrough import Collector

    def forbidden_lookup(*args, **kwargs):
        raise AssertionError("Numeric loopback must not require reverse DNS")

    monkeypatch.setattr(socket, "getfqdn", forbidden_lookup)
    with Collector().serve() as collector:
        url = urlsplit(collector.url)
        with closing(HTTPConnection(url.hostname, url.port, timeout=2)) as connection:
            connection.request("GET", "/")
            response = connection.getresponse()
            assert response.status == 200
            assert b"html" in response.read()
