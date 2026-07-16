from __future__ import annotations

import importlib
from typing import Any

import pytest

from agents.sandbox.capabilities.memory import Memory


def _load_usaspending_agent_module() -> Any:
    try:
        return importlib.import_module(
            "examples.sandbox.extensions.daytona.usaspending_text2sql.agent"
        )
    except SystemExit as exc:
        pytest.skip(str(exc))


def _memory_capability(agent: Any) -> Memory:
    memories = [capability for capability in agent.capabilities if isinstance(capability, Memory)]
    assert len(memories) == 1
    return memories[0]


def test_usaspending_auto_mode_disables_memory_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLES_INTERACTIVE_MODE", "auto")
    module = _load_usaspending_agent_module()

    memory = _memory_capability(module.build_agent())

    assert memory.read is not None
    assert memory.generate is None


def test_usaspending_prompt_mode_keeps_memory_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXAMPLES_INTERACTIVE_MODE", raising=False)
    module = _load_usaspending_agent_module()

    memory = _memory_capability(module.build_agent())

    assert memory.read is not None
    assert memory.generate is not None
