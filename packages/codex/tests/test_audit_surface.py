"""Source auditing extracts dispatch names without unbounded backtracking."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('"start" => start(),', {"start"}),
        ('"start" | "stop" => handle(),', {"start", "stop"}),
        ('\n  "start"\n | "stop"\t=> handle(),', {"start", "stop"}),
        ('"start" | => handle(),', {"start"}),
        ('"start" "stop" => handle(),', {"start", "stop"}),
        ('"outer" => {\n"inner" => nested(),\n},', {"outer"}),
        ('"start" | "stop"', set()),
    ],
)
def test_dispatched_operations(source, expected, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from audit_surface import dispatched_operations

    assert dispatched_operations(source) == expected


def test_dispatch_audit_finishes_for_long_incomplete_match():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from audit_surface import dispatched_operations; "
            "assert dispatched_operations('  ' + '\"_\"  ' * 10000) == set()",
            str(scripts),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
