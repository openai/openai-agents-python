from __future__ import annotations

import asyncio
import importlib
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from agents import Agent, Runner, RunResult
from agents.testing import ScriptedModel
from examples import run_examples
from examples.tools import local_shell_skill, shell, shell_human_in_the_loop

from .test_responses import get_text_message
from .utils.hitl import make_shell_call


def use_scripted_shell(
    monkeypatch: pytest.MonkeyPatch, batches: list[list[str]]
) -> list[RunResult]:
    """Exercise the examples' actual agents and approval configuration without an API."""
    real_run = Runner.run
    results: list[RunResult] = []
    agents: dict[str, Agent] = {}

    async def run(agent: Agent, prompt: Any) -> RunResult:
        if agent.name not in agents:
            model = ScriptedModel()
            for index, commands in enumerate(batches):
                model.enqueue([make_shell_call(f"call_shell_{index}", commands=commands)])
            model.enqueue([get_text_message("Done")])
            agents[agent.name] = agent.clone(model=model)
        result = await real_run(agents[agent.name], prompt)
        results.append(result)
        return result

    monkeypatch.setattr(Runner, "run", run)
    return results


@pytest.mark.parametrize("example", ["shell", "local_skill", "hitl"])
@pytest.mark.parametrize("decision", ["yes", "no", "eof", "noninteractive", "auto"])
@pytest.mark.asyncio
async def test_examples_require_interactive_approval(
    monkeypatch: pytest.MonkeyPatch, example: str, decision: str
) -> None:
    monkeypatch.setenv("SHELL_AUTO_APPROVE", "1")
    monkeypatch.setenv("EXAMPLES_INTERACTIVE_MODE", "auto" if decision == "auto" else "manual")
    # The former bypass was read at import time, as when the auto runner starts a process.
    importlib.reload(shell)
    monkeypatch.setenv("SHELL_EXAMPLE_TEST_SECRET", "synthetic-secret")
    monkeypatch.setenv("BASH_ENV", "/synthetic/startup-hook")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: decision != "noninteractive")
    prompt = (
        Mock(side_effect=EOFError)
        if decision == "eof"
        else Mock(return_value="yes" if decision == "auto" else decision)
    )
    monkeypatch.setattr("builtins.input", prompt)
    proc = Mock(returncode=0, communicate=AsyncMock(return_value=(b"approved output", b"")))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", spawn)
    results = use_scripted_shell(monkeypatch, [["printf approved"]])

    if example == "shell":
        await shell.main("Run the command", "unused-model")
    elif example == "hitl":
        await shell_human_in_the_loop.main("Run the command", "unused-model")
    else:
        await local_shell_skill.main("unused-model")

    expected_calls = 2 if example == "local_skill" else 1
    assert len(results) == (2 if example == "hitl" else expected_calls)
    assert results[-1].final_output == "Done"
    assert not results[-1].interruptions
    if example == "hitl":
        assert len(results[0].interruptions) == 1
    if decision == "yes":
        assert prompt.call_count == expected_calls
        assert spawn.await_count == expected_calls
        for call in spawn.await_args_list:
            assert call.args == ("printf approved",)
            assert "OPENAI_API_KEY" not in call.kwargs["env"]
            assert "SHELL_EXAMPLE_TEST_SECRET" not in call.kwargs["env"]
            assert "BASH_ENV" not in call.kwargs["env"]
            assert call.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    else:
        spawn.assert_not_awaited()
        assert prompt.call_count == (
            0 if decision in {"noninteractive", "auto"} else expected_calls
        )


@pytest.mark.asyncio
async def test_hitl_approval_does_not_authorize_future_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXAMPLES_INTERACTIVE_MODE", "manual")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompt = Mock(side_effect=["yes", "no"])
    monkeypatch.setattr("builtins.input", prompt)
    proc = Mock(returncode=0, communicate=AsyncMock(return_value=(b"first output", b"")))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", spawn)
    results = use_scripted_shell(monkeypatch, [["printf first"], ["printf second"]])

    await shell_human_in_the_loop.main("Run both commands", "unused-model")

    assert prompt.call_count == 2
    assert spawn.await_count == 1
    assert spawn.call_args.args == ("printf first",)
    assert len(results) == 3
    assert [len(result.interruptions) for result in results] == [1, 1, 0]
    assert results[-1].final_output == "Done"


@pytest.mark.skipif(os.name == "nt", reason="Uses POSIX shell builtins")
@pytest.mark.asyncio
async def test_approved_child_does_not_inherit_secrets_or_operator_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL_EXAMPLE_TEST_SECRET", "synthetic-secret")
    monkeypatch.setenv("EXAMPLES_INTERACTIVE_MODE", "manual")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    results = use_scripted_shell(
        monkeypatch,
        [
            [
                'printf "%s\\n" "${SHELL_EXAMPLE_TEST_SECRET-unset}"; '
                'if read answer; then printf "read input"; else printf "stdin closed"; fi'
            ]
        ],
    )

    await shell.main("Check child process boundaries", "unused-model")

    outputs = [
        item.raw_item for item in results[0].new_items if item.type == "tool_call_output_item"
    ]
    assert len(outputs) == 1
    assert outputs[0]["output"][0]["stdout"] == "unset\nstdin closed"


@pytest.mark.asyncio
async def test_approval_display_escapes_terminal_controls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EXAMPLES_INTERACTIVE_MODE", "manual")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert not await shell.prompt_shell_approval(["echo safe\r\x1b[2Krm file"])
    output = capsys.readouterr().out
    assert "\\r\\x1b[2Krm file" in output
    assert "\x1b" not in output
    assert "without sandbox isolation" in output


@pytest.mark.parametrize(
    "relpath",
    [
        "examples/tools/shell.py",
        "examples/tools/local_shell_skill.py",
        "examples/tools/shell_human_in_the_loop.py",
    ],
)
def test_auto_runner_skips_interactive_host_shell_examples(
    monkeypatch: pytest.MonkeyPatch, relpath: str
) -> None:
    monkeypatch.delenv("EXAMPLES_AUTO_SKIP", raising=False)
    path = run_examples.ROOT_DIR / relpath
    tags = run_examples.detect_tags(path, path.read_text())
    assert "interactive" in tags
    assert run_examples.should_skip(
        tags, {"interactive"}, run_examples.load_auto_skip(), relpath, True
    ) == (True, {"auto-skip"})
    assert run_examples.should_skip(tags, set(), set(), relpath, False) == (
        True,
        {"interactive"},
    )
