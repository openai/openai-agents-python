import asyncio
import importlib.util
import json
import sys
from pathlib import Path

# The parent has been selecting this exact native product for both processes.
state_path, effects_path, url, native_path = sys.argv[1:]
spec = importlib.util.spec_from_file_location("agents._native", native_path)
assert spec is not None and spec.loader is not None
native = importlib.util.module_from_spec(spec)
sys.modules["agents._native"] = native
spec.loader.exec_module(native)

from agents import Agent, NativeProvider, function_tool  # noqa: E402
from agents.state import SessionState  # noqa: E402


async def main():
    @function_tool(needs_approval=True, approval_key="fixture.risky.v1")
    def risky(value: str) -> str:
        with Path(effects_path).open("a") as stream:
            stream.write(value + "\n")
        return value

    agent = Agent(
        instructions="Use the tool.",
        model="fixture",
        tools=[risky],
        provider=NativeProvider(base_url=url, api_key="synthetic-manual-test"),
    )
    state = SessionState.from_json(Path(state_path).read_text())
    async with agent.session(state=state) as session:
        turn = await session.submit("continue")
        result = await asyncio.wait_for(turn.get_result(), timeout=10)
        assert result.final_output == "restored"
        assert await session.pending_interactions() == ()
    print(json.dumps({"effects": Path(effects_path).read_text().splitlines()}))


asyncio.run(main())
