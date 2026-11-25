from dataclasses import replace

import pytest

from agents import Agent, Runner
from agents.exceptions import UserError
from agents.testing import ScriptedModel, assistant_message, function_call

from .test_plugin_state import config, reader


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True], ids=["handoff", "agent-as-tool"])
@pytest.mark.parametrize("changed", [False, True], ids=["identity", "redact-approved-args"])
async def test_redaction_checks_in_memory_plugin_delegates(nested: bool, changed: bool) -> None:
    effects: list[str] = []
    delegate = reader(effects, [])
    cfg = replace(config(), redact_text=str)
    if nested:
        call = function_call("ask", {"input": "go"}, call_id="x")
        manager = Agent(
            "manager",
            model=ScriptedModel([[call], [assistant_message("done")]]),
            tools=[delegate.as_tool("ask", "ask", run_config=cfg)],
        )
    else:
        call = function_call("transfer_to_reader", {}, call_id="x")
        manager = Agent("manager", model=ScriptedModel([[call]]), handoffs=[delegate])
    result = await Runner.run(manager, "go", context={"allowed": True}, run_config=cfg)
    assert result.interruptions and effects == []
    state = result.to_state()
    state.approve(result.interruptions[0])
    with pytest.raises(UserError, match="could not safely serialize|in memory"):
        state.to_json()
    if changed:
        protected = replace(cfg, redact_text=lambda text: text.replace("N42", "hidden"))
        with pytest.raises(UserError):
            await Runner.run(manager, state, run_config=protected)
        assert effects == []
    else:
        finished = await Runner.run(manager, state, run_config=cfg)
        assert finished.final_output == "done"
        assert effects == ["N42"]
