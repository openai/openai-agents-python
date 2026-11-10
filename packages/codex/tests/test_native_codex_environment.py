"""Native CLI defaults and application-owned environment overrides."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agents.codex import Codex, CodexOptions, codex_tool


@pytest.mark.skipif(os.name == "nt", reason="Synthetic executable uses a Unix shebang")
@pytest.mark.parametrize(
    "configuration", ["default", "mapping", "empty", "explicit", "inherited", "provided"]
)
async def test_native_cli_environment_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configuration: str
) -> None:
    selected = {
        "SDK_UNRELATED_TOKEN": "synthetic-unrelated-token",
        "CODEX_API_KEY": "synthetic-codex-key",
        "HOME": str(tmp_path),
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "PYTHONPATH": "/synthetic-module-search-path",
    }
    for name, value in selected.items():
        monkeypatch.setenv(name, value)
    executable = tmp_path / "synthetic-codex"
    executable.write_text(
        f"#!{sys.executable}\nimport json, os, sys\nsys.stdin.read()\n"
        f"observed = {{name: os.environ.get(name) for name in {list(selected)!r}}}\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'environment-thread'}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'id': 'message', "
        "'type': 'agent_message', 'text': json.dumps(observed)}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': "
        "{'input_tokens': 1, 'cached_input_tokens': 0, 'output_tokens': 1}}))\n"
    )
    executable.chmod(0o700)
    options = CodexOptions(codex_path_override=str(executable))
    if configuration == "mapping":
        declaration = codex_tool(codex_options={"codex_path_override": str(executable)})
    elif configuration == "empty":
        declaration = codex_tool(
            codex_options=CodexOptions(codex_path_override=str(executable), env={})
        )
    elif configuration == "explicit":
        declaration = codex_tool(
            codex_options=CodexOptions(
                codex_path_override=str(executable),
                env={"SDK_UNRELATED_TOKEN": "explicit-token", "CODEX_API_KEY": "explicit-key"},
            )
        )
    elif configuration == "inherited":
        declaration = codex_tool(
            codex_options=CodexOptions(codex_path_override=str(executable), env=dict(os.environ))
        )
    elif configuration == "provided":
        declaration = codex_tool(codex=Codex(options))
    else:
        declaration = codex_tool(codex_options=options)
    binding = declaration._bind(None, None, declaration)
    result = await binding.callback(
        None, prompt="Inspect the selected test variables", thread_id=None
    )
    observed = json.loads(result["response"])
    if configuration == "empty":
        assert observed == dict.fromkeys(selected)
    elif configuration == "explicit":
        assert observed == {
            "SDK_UNRELATED_TOKEN": "explicit-token",
            "CODEX_API_KEY": "explicit-key",
            "HOME": None,
            "HTTPS_PROXY": None,
            "PYTHONPATH": None,
        }
    else:
        assert observed == {
            **selected,
            "SDK_UNRELATED_TOKEN": (
                selected["SDK_UNRELATED_TOKEN"]
                if configuration in {"inherited", "provided"}
                else None
            ),
            "PYTHONPATH": (
                selected["PYTHONPATH"] if configuration in {"inherited", "provided"} else None
            ),
        }
