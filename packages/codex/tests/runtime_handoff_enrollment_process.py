"""Separate-process public-path witness; a fresh native binding is required."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from agents import Agent, DecisionHandoff, NativeProvider, function_tool
from agents.state import SessionState
from tests._asyncio import timeout
from tests.test_native_decision_handoff import Decisions
from tests.test_native_manual_tools import next_request


async def run(mode: str, url: str, directory: Path, variant: str) -> None:
    effects = directory / "effects.jsonl"
    factory_calls = 0
    targets = []

    @function_tool(needs_approval=True, approval_key="parity.enrolled.write_once.v1")
    def write_once(value: str) -> str:
        with effects.open("a") as stream:
            stream.write(json.dumps({"owner": mode, "value": value}) + "\n")
        return f"{mode}:{value}"

    def target() -> Agent:
        return Agent(
            name="Source" if variant == "collision" else "Runtime target",
            instructions="Use the exact current write_once callback."
            + (" Wrong owner." if variant == "changed" else ""),
            model="gpt-5.5",
            tools=[write_once],
        )

    def factory(_context, _input):
        nonlocal factory_calls
        factory_calls += 1
        if mode != "write":
            raise AssertionError("Cold restore reran the runtime factory")
        result = target()
        targets.append(result)
        return result

    route = DecisionHandoff(
        name="delegate",
        description="Delegate.",
        question="Choose.",
        targets={"late": factory},
    )
    async with (
        timeout(25),
        NativeProvider(base_url=url, api_key="synthetic", supports_websockets=False) as provider,
    ):
        current_target = target() if mode != "write" else None
        root = Agent(
            name="Source",
            instructions="Delegate.",
            model="gpt-5.5",
            provider=provider,
            decisions=Decisions("late"),
            handoffs=[route]
            + ([current_target] if mode != "write" and variant != "missing" else []),
        )
        if mode == "write":
            async with root.session() as session:
                turn = await session.submit("Delegate and request approval.")
                request = await next_request(session.interactions(), write_once)
                assert request.call_id == "effect"
                assert len(targets) == 1 and factory_calls == 1
                assert not effects.exists()
                if variant == "collision":
                    try:
                        await session.update(agent=targets[0])
                    except ValueError as error:
                        assert "unique across current Agents" in str(error)
                    else:
                        raise AssertionError("Enrollment accepted an opening Agent name collision")
                    await turn.cancel()
                else:
                    await session.update(agent=targets[0])
                    assert await session.get_active_agent() is targets[0]
                    saved = await session.suspend()
                    (directory / "pending.json").write_text(saved.to_json())
        else:
            state_path = directory / ("pending.json" if mode == "read" else "approved.json")
            state = SessionState.from_json(state_path.read_text())
            if variant == "root":
                assert current_target is not None
                root = current_target
            if variant in {"changed", "missing", "root"}:
                try:
                    unexpected = await root.session(state=state)
                except (ValueError, RuntimeError) as error:
                    expected = {
                        "root": "original root",
                        "missing": "currently reachable target",
                        "changed": "target declaration differs",
                    }[variant]
                    assert expected in str(error), str(error)
                else:
                    await unexpected.close()
                    raise AssertionError("Restore accepted the wrong current owner")
                assert not effects.exists()
            else:
                async with root.session(
                    state=state,
                    **({"verify_remembered_approvals": lambda _: True} if mode == "again" else {}),
                ) as session:
                    assert await session.get_active_agent() is current_target
                    if mode == "read":
                        request = await next_request(session.interactions(), write_once)
                        assert request.call_id == "effect"
                        assert not effects.exists()
                        await request.approve(always=True)
                        assert session.restored_turn is not None
                        assert (await session.restored_turn.get_result()).final_output == "done"
                        saved = await session.save_state()
                        assert saved.remembered_approvals
                        (directory / "approved.json").write_text(saved.to_json())
                    else:
                        turn = await session.submit("Run a distinct second call.")
                        assert (await turn.get_result()).final_output == "done again"
        assert factory_calls == (1 if mode == "write" else 0)
        (directory / f"{mode}-{variant}.receipt.json").write_text(
            json.dumps({"factory_calls": factory_calls, "pid": __import__("os").getpid()})
        )


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4]))
