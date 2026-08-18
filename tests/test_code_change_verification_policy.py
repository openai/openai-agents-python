from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import _running_in_nested_codex_macos_sandbox

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SKILL_PATH = _REPOSITORY_ROOT / ".agents/skills/code-change-verification/SKILL.md"
_PROMPT_PATH = _REPOSITORY_ROOT / ".agents/skills/code-change-verification/agents/openai.yaml"
_CHANGE_DETECTOR_PATH = _REPOSITORY_ROOT / ".github/scripts/detect-changes.sh"


def test_code_change_verification_keeps_codex_execution_sandboxed() -> None:
    skill = _SKILL_PATH.read_text(encoding="utf-8")
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    combined = f"{skill}\n{prompt}"

    assert "sandbox_permissions=require_escalated" not in combined
    assert "persistent command allow rule" not in combined
    assert "outside the Codex sandbox" not in combined
    assert "retry with broader host access" in combined
    assert (
        "/usr/bin/env -u OPENAI_API_KEY OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX=1 "
        "UV_DEFAULT_INDEX=https://pypi.org/simple"
    ) in skill


def test_code_change_detection_includes_verification_skill() -> None:
    detector = _CHANGE_DETECTOR_PATH.read_text(encoding="utf-8")
    code_pattern = re.search(r"^\s*pattern='([^']+)'$", detector, flags=re.MULTILINE)

    assert code_pattern is not None
    assert re.match(
        code_pattern.group(1),
        ".agents/skills/code-change-verification/SKILL.md",
    )


@pytest.mark.parametrize(
    ("platform", "environment", "expected"),
    [
        pytest.param("darwin", {"OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX": "1"}, True),
        pytest.param("darwin", {}, False),
        pytest.param("darwin", {"OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX": "0"}, False),
        pytest.param("linux", {"OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX": "1"}, False),
    ],
)
def test_native_macos_sandbox_skip_requires_explicit_nested_mode(
    platform: str, environment: dict[str, str], expected: bool
) -> None:
    assert (
        _running_in_nested_codex_macos_sandbox(
            platform=platform,
            environment=environment,
        )
        is expected
    )
