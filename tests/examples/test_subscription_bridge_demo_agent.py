from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from examples.subscription_bridge import demo_agent


def test_default_model_for_backend() -> None:
    assert demo_agent.default_model_for_backend("codex") == "codex/gpt-5.4"
    assert demo_agent.default_model_for_backend("claude") == "claude/claude-sonnet-4-6"


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://127.0.0.1:8787", "http://127.0.0.1:8787/v1"),
        ("http://127.0.0.1:8787/", "http://127.0.0.1:8787/v1"),
        ("http://127.0.0.1:8787/v1", "http://127.0.0.1:8787/v1"),
        ("http://127.0.0.1:8787/v1/", "http://127.0.0.1:8787/v1"),
    ],
)
def test_normalize_api_base_url(base_url: str, expected: str) -> None:
    assert demo_agent.normalize_api_base_url(base_url) == expected


def test_resolve_model_prefers_explicit_override() -> None:
    assert demo_agent.resolve_model("codex", "codex/gpt-5.4-mini") == "codex/gpt-5.4-mini"
    assert demo_agent.resolve_model("claude", None) == "claude/claude-sonnet-4-6"


def test_demo_agent_module_can_be_loaded_from_file_path() -> None:
    path = Path("examples/subscription_bridge/demo_agent.py").resolve()
    spec = importlib.util.spec_from_file_location("subscription_bridge_demo_agent_file", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.default_model_for_backend("codex") == "codex/gpt-5.4"
