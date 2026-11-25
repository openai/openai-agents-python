import pytest

from agents import Agent
from agents.plugin_session import Command, command_catalog, run_command
from agents.plugins import Plugin


@pytest.mark.asyncio
async def test_only_unambiguous_local_commands_receive_application_input() -> None:
    calls: list[str] = []

    def first(ctx: list[str], args: str) -> str:
        ctx.append(args)
        return "first"

    async def second(ctx: list[str], args: str) -> str:
        ctx.append(args)
        return "second"

    agent: Agent[list[str]] = Agent(
        name="test",
        plugins=[
            Plugin(name="one", commands={"show": Command(first, "Show the first result", "f2")}),
            Plugin(name="two", commands={"show": second, "view": second}),
        ],
    )
    catalog = command_catalog(agent)
    assert tuple(catalog) == ("/one:show", "/two:show", "/two:view")
    assert catalog["/one:show"].description == "Show the first result"
    assert catalog["/one:show"].shortcut == "f2"
    assert catalog["/two:show"].shortcut is None
    for line in ("show", "/missing", "/show"):
        with pytest.raises(ValueError):
            await run_command(agent, line, context=calls)
    assert calls == []
    assert await run_command(agent, "/one:show a  b", context=calls) == "first"
    assert await run_command(agent, "/view", context=calls) == "second"
    assert calls == ["a  b", ""]
