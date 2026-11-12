"""Public environment admission for the typed application editor."""

import pytest

from agents import Agent, Environment, NativeProvider, apply_patch_tool, function_tool
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_apply_patch import Editor, patch_call
from tests.test_native_child_agents import trust
from tests.test_native_executor_tools import declaration


async def test_native_editor_replaces_only_the_workspace_patch_tool(
    responses, tmp_path, monkeypatch
):
    editor = Editor()
    operation = {"type": "create_file", "path": "tenant://custom", "diff": "custom format"}
    responses.respond(patch_call(operation))
    responses.respond(assistant_output("done"))
    async with (
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        NativeProvider(base_url=responses.url, api_key="synthetic") as provider,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Use the editor.",
            model="gpt-5",
            provider=provider,
            tools=[apply_patch_tool(editor)],
        )
        async with timeout(20), agent.session(environment=declaration(executor)) as session:
            assert (await (await session.submit("edit")).get_result()).final_output == "done"
        assert not any(request.get("method") == "process/start" for request in executor.requests)
    assert len(editor.operations) == 1
    assert editor.operations[0].path == operation["path"]
    tools = responses.requests[0]["tools"]
    assert [item for item in tools if item.get("type") == "apply_patch"] == [
        {"type": "apply_patch"}
    ]
    assert {"exec_command", "write_stdin"} <= {item.get("name") for item in tools}
    assert not (tmp_path / "custom").exists()


@pytest.mark.parametrize("name", ["apply_patch", "exec_command"])
async def test_ordinary_application_tool_still_rejects_environment_collision(
    responses, tmp_path, name
):
    @function_tool(name=name)
    def ordinary_tool() -> str:
        pytest.fail("A colliding ordinary tool must not run")

    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Reject the collision.", provider=provider, tools=[ordinary_tool]
        )
        with pytest.raises(
            ValueError,
            match=f"(?:collides with enabled environment tool|uses a reserved name): {name}",
        ):
            async with agent.session(environment=Environment.local(cwd=tmp_path, tools=[name])):
                pytest.fail("A colliding ordinary tool must not open")
    assert not responses.requests
