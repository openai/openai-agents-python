from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Any, cast


def load_pr_labels_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "pr_labels.py"
    spec = spec_from_file_location("pr_labels", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    assert isinstance(module, ModuleType)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(Any, module)


pr_labels = load_pr_labels_module()


def test_infer_fallback_labels_for_chat_completions() -> None:
    labels = pr_labels.infer_fallback_labels(["src/agents/models/chatcmpl_converter.py"])

    assert labels == {"feature:chat-completions"}


def test_infer_fallback_labels_ignores_tests_only_feature_touches() -> None:
    labels = pr_labels.infer_fallback_labels(["tests/realtime/test_openai_realtime.py"])

    assert labels == set()


def test_infer_fallback_labels_marks_core_for_runtime_changes() -> None:
    labels = pr_labels.infer_fallback_labels(["src/agents/run_internal/approvals.py"])

    assert labels == {"feature:core"}


def test_infer_fallback_labels_marks_extensions_for_extensions_memory_changes() -> None:
    labels = pr_labels.infer_fallback_labels(
        ["src/agents/extensions/memory/advanced_sqlite_session.py"]
    )

    assert labels == {"feature:extensions"}


def test_infer_fallback_labels_marks_extensions_for_litellm_changes() -> None:
    labels = pr_labels.infer_fallback_labels(["src/agents/extensions/models/litellm_model.py"])

    assert labels == {"feature:extensions"}


def test_infer_fallback_labels_marks_extensions_for_any_llm_changes() -> None:
    labels = pr_labels.infer_fallback_labels(["src/agents/extensions/models/any_llm_model.py"])

    assert labels == {"feature:extensions"}


def test_compute_desired_labels_removes_stale_fallback_labels() -> None:
    desired = pr_labels.compute_desired_labels(
        pr_context=pr_labels.PRContext(),
        changed_files=["src/agents/models/chatcmpl_converter.py"],
        diff_text="",
        codex_ran=False,
        codex_output_valid=False,
        codex_labels=[],
        base_sha=None,
        head_sha=None,
    )

    assert desired == {"feature:chat-completions"}


def test_compute_desired_labels_falls_back_when_codex_output_is_invalid() -> None:
    desired = pr_labels.compute_desired_labels(
        pr_context=pr_labels.PRContext(),
        changed_files=["src/agents/run_internal/approvals.py"],
        diff_text="",
        codex_ran=True,
        codex_output_valid=False,
        codex_labels=[],
        base_sha=None,
        head_sha=None,
    )

    assert desired == {"feature:core"}


def test_compute_desired_labels_uses_fallback_feature_labels_when_codex_valid_but_empty() -> None:
    desired = pr_labels.compute_desired_labels(
        pr_context=pr_labels.PRContext(),
        changed_files=["src/agents/run_internal/approvals.py"],
        diff_text="",
        codex_ran=True,
        codex_output_valid=True,
        codex_labels=[],
        base_sha=None,
        head_sha=None,
    )

    assert desired == {"feature:core"}


def test_compute_desired_labels_infers_bug_from_fix_title() -> None:
    desired = pr_labels.compute_desired_labels(
        pr_context=pr_labels.PRContext(title="fix: stop streamed tool execution"),
        changed_files=["src/agents/run_internal/approvals.py"],
        diff_text="",
        codex_ran=True,
        codex_output_valid=True,
        codex_labels=[],
        base_sha=None,
        head_sha=None,
    )

    assert desired == {"bug", "feature:core"}


def test_compute_desired_labels_infers_extensions_for_extensions_memory_fix() -> None:
    desired = pr_labels.compute_desired_labels(
        pr_context=pr_labels.PRContext(title="fix(memory): honor custom table names"),
        changed_files=[
            "src/agents/extensions/memory/advanced_sqlite_session.py",
            "tests/extensions/memory/test_advanced_sqlite_session.py",
        ],
        diff_text="",
        codex_ran=True,
        codex_output_valid=True,
        codex_labels=[],
        base_sha=None,
        head_sha=None,
    )

    assert desired == {"bug", "feature:extensions"}


def test_compute_managed_labels_preserves_model_only_labels_without_signal() -> None:
    managed = pr_labels.compute_managed_labels(
        pr_context=pr_labels.PRContext(),
        codex_ran=True,
        codex_output_valid=True,
        codex_labels=[],
    )

    assert "bug" not in managed
    assert "enhancement" not in managed
    assert "feature:core" in managed


def test_compute_managed_labels_manages_model_only_labels_with_fix_title() -> None:
    managed = pr_labels.compute_managed_labels(
        pr_context=pr_labels.PRContext(title="fix: stop streamed tool execution"),
        codex_ran=True,
        codex_output_valid=True,
        codex_labels=[],
    )

    assert "bug" in managed
    assert "enhancement" in managed
