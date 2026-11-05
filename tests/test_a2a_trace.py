from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from examples.observability.trace_walkthrough import Collector


def test_exported_native_graph_crosses_real_a2a_http_boundary(tmp_path):
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment["PYTHONPATH"] = os.pathsep.join([str(root), str(root / "src")])
    output = tmp_path / "receipt.json"
    with Collector().serve() as collector:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.a2a_trace_worker",
                collector.url,
                str(output),
                str(tmp_path / "host"),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(output.read_text())
    assert receipt["outgoing"] == receipt["received"]
    spans = [
        span
        for batch in collector.snapshot()
        if batch["signal"] == "traces"
        for resource in batch["data"]["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]

    def attributes(span):
        return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}

    def dispatch(turn):
        found = [
            span
            for span in spans
            if attributes(span).get("submission.id") == turn
            and span["name"].startswith("op.dispatch.")
        ]
        assert len(found) == 1
        return found[0]

    remote = dispatch(receipt["remote_turn"])
    parent = receipt["outgoing"]["traceparent"]
    assert remote["traceId"] == parent[3:35]
    assert remote["parentSpanId"] == parent[36:52]
    by_id = {span["spanId"]: span for span in spans}
    ancestry = [by_id[parent[36:52]]]
    while ancestry[-1].get("parentSpanId") in by_id:
        ancestry.append(by_id[ancestry[-1]["parentSpanId"]])
    assert any(attributes(span).get("call_id") == "outbound-call" for span in ancestry)
    assert any(attributes(span).get("submission.id") == receipt["local_turn"] for span in ancestry)
    assert "alice-token" not in json.dumps(collector.snapshot())
    (tmp_path / "a2a-export.json").write_text(
        json.dumps({"receipt": receipt, "batches": collector.snapshot()})
    )
