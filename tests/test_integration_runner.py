from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

RUNNER = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "run_integration_tests.py"
CHANGE_DETECTOR = RUNNER.with_name("detect-changes.sh")


def _sanitizer() -> Callable[[Path], Any]:
    return cast(Callable[[Path], Any], runpy.run_path(str(RUNNER))["_sanitize_and_load_junit"])


def test_junit_sanitizer_removes_failure_details_and_captured_output(tmp_path: Path) -> None:
    sentinel = "JUNIT_SECRET_SENTINEL_42"
    report = tmp_path / "results.xml"
    report.write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite tests="2" failures="1" errors="0" skipped="0">
<testcase classname="contract" name="passed" />
<testcase classname="contract" name="failed">
<failure message="{sentinel}">traceback {sentinel}</failure>
<system-out>stdout {sentinel}</system-out><system-err>stderr {sentinel}</system-err>
</testcase></testsuite></testsuites>
""",
        encoding="utf-8",
    )

    root = _sanitizer()(report)

    assert root is not None
    serialized = report.read_text(encoding="utf-8")
    assert sentinel not in serialized
    assert 'classname="contract"' in serialized
    assert 'name="failed"' in serialized


def test_junit_sanitizer_discards_malformed_reports(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text("<testsuite><failure>secret", encoding="utf-8")

    assert _sanitizer()(report) is None
    assert not report.exists()


def test_junit_sanitizer_discards_reports_with_invalid_counts(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuite tests="not-a-number" failures="0" errors="0" skipped="0" />',
        encoding="utf-8",
    )

    assert _sanitizer()(report) is None
    assert not report.exists()


def test_junit_sanitizer_discards_reports_with_impossible_totals(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuite tests="1" failures="1" errors="1" skipped="0" />',
        encoding="utf-8",
    )

    assert _sanitizer()(report) is None
    assert not report.exists()


def test_code_change_detection_includes_packaged_contract_inputs() -> None:
    detector = CHANGE_DETECTOR.read_text(encoding="utf-8")

    assert "integration_tests/" in detector
    assert "detect-changes\\.sh" in detector
    assert "run_integration_tests\\.py" in detector
    assert "\\.github/workflows/tests\\.yml" in detector
