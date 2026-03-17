from __future__ import annotations

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


def test_compute_desired_labels_removes_stale_fallback_labels() -> None:
    desired = pr_labels.compute_desired_labels(
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
        changed_files=["src/agents/run_internal/approvals.py"],
        diff_text="",
        codex_ran=True,
        codex_output_valid=False,
        codex_labels=[],
        base_sha=None,
        head_sha=None,
    )

    assert desired == {"feature:core"}


def test_compute_managed_labels_preserves_model_only_labels_without_valid_codex_output() -> None:
    managed = pr_labels.compute_managed_labels(codex_ran=True, codex_output_valid=False)

    assert "bug" not in managed
    assert "enhancement" not in managed
    assert "feature:core" in managed
