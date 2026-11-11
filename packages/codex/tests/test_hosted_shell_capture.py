"""Provider-owned shell declaration and item projection contracts."""

from __future__ import annotations

from typing import Any

import pytest

from _agents_codex._projection import project_hosted_response_item
from _agents_codex.tools import _capture_hosted_tools
from agents import HostedShell


@pytest.mark.parametrize(
    "environment",
    [
        {
            "type": "container_auto",
            "file_ids": ["file-script"],
            "memory_limit": "4g",
            "skills": [{"type": "skill_reference", "skill_id": "skill-test", "version": "2"}],
            "network_policy": {"type": "disabled"},
        },
        {"type": "container_reference", "container_id": "cntr-existing"},
    ],
)
def test_hosted_shell_captures_provider_config_without_local_execution(environment: Any) -> None:
    tool = HostedShell(
        environment=environment,
        allowed_callers=["direct"],
        provider_data={"future_option": {"enabled": True}},
    )
    captured = _capture_hosted_tools([tool])
    assert captured == [
        {
            "type": "shell",
            "environment": environment,
            "allowed_callers": ["direct"],
            "future_option": {"enabled": True},
        }
    ]
    environment.clear()
    assert captured[0]["environment"]["type"] in {"container_auto", "container_reference"}


@pytest.mark.parametrize(
    "provider_data", [{"type": "local_shell"}, {"environment": {"type": "local"}}]
)
def test_hosted_shell_provider_fields_cannot_replace_its_owner(provider_data: Any) -> None:
    with pytest.raises(ValueError, match="cannot replace declared fields"):
        _capture_hosted_tools(
            [HostedShell(environment={"type": "container_auto"}, provider_data=provider_data)]
        )


@pytest.mark.parametrize(
    "kind,fields",
    [
        ("shell_call", {"action": {"commands": ["printf provider"], "timeout_ms": 1000}}),
        (
            "shell_call_output",
            {
                "output": [
                    {
                        "stdout": "provider",
                        "stderr": "",
                        "outcome": {"type": "exit", "exit_code": 0},
                    }
                ]
            },
        ),
    ],
)
def test_hosted_shell_projects_complete_provider_items(kind: str, fields: dict[str, Any]) -> None:
    raw = {
        "type": kind,
        "id": "shell-item",
        "call_id": "shell-call",
        "status": "completed",
        **fields,
        "future_field": {"keep": [True, None]},
        "internal_chat_message_metadata_passthrough": {"private": True},
    }
    projected = project_hosted_response_item(raw, "turn-test")
    assert projected is not None
    assert projected["type"] == kind
    assert projected["call_id"] == "shell-call"
    assert projected["id"] == "shell-item"
    assert projected["turn_id"] == "turn-test"
    for name, value in fields.items():
        assert projected[name] == value
    assert projected["provider_data"] == {
        name: value
        for name, value in raw.items()
        if name != "internal_chat_message_metadata_passthrough"
    }
