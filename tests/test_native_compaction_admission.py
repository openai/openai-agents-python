"""Pause automatic compaction before provider admission through a native hook."""

import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest

from agents import Agent, TurnStoppedError, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_executor_policies import OutputEvents
from tests.test_native_provider_websockets import peer as model_peer


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX native hook commands")
@pytest.mark.parametrize("websocket", [False, True])
async def test_stop_during_compaction_setup_fences_provider_admission(
    tmp_path, monkeypatch, websocket
):
    from agents import _native

    entered = tmp_path / "hook-entered"
    release = tmp_path / "hook-release"
    released = tmp_path / "hook-released"
    hook = tmp_path / "hold-compaction.py"
    hook.write_text(
        "import pathlib, time\n"
        f"pathlib.Path({str(entered)!r}).touch()\n"
        f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        f"pathlib.Path({str(released)!r}).touch()\n"
    )
    original_create = _native.create_engine

    async def create_with_compaction_barrier(config_json, runtime_json, **kwargs):
        runtime = json.loads(runtime_json)
        # This test-only hook pauses the existing native setup path.
        runtime["bypass_hook_trust"] = True
        Path(runtime["codex_home"], "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreCompact": [
                            {
                                "matcher": "auto",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": shlex.join([sys.executable, str(hook)]),
                                    }
                                ],
                            }
                        ]
                    }
                }
            )
        )
        return await original_create(config_json, json.dumps(runtime), **kwargs)

    monkeypatch.setattr(_native, "create_engine", create_with_compaction_barrier)
    interrupt_entered, release_interrupt = asyncio.Event(), asyncio.Event()

    class GatedInterrupt(OutputEvents):
        async def _interrupt_session(self, *args, **kwargs):
            interrupt_entered.set()
            await release_interrupt.wait()
            return await self.native._interrupt_session(*args, **kwargs)

    @function_tool
    async def lookup() -> str:
        return "settled-tool-output"

    async with timeout(30), model_peer() as model:

        async def report_usage(event):
            if event["type"] == "response.completed" and len(model.requests) == 1:
                event["response"]["usage"].update(
                    input_tokens=260_000, output_tokens=1_000, total_tokens=261_000
                )

        model.before_event = report_usage
        model.respond(function_call("lookup", {}, "settled"))
        model.respond(assistant_output("compacted history"))
        model.respond(assistant_output("successor"))
        async with Agent(
            model="gpt-5.5",
            instructions="Use lookup.",
            tools=[lookup],
            provider=model.provider(websocket=websocket),
        ).session() as session:
            session._native = GatedInterrupt(session._native)
            turn = await session.submit("Lookup now.")
            while not entered.exists():
                await asyncio.sleep(0.01)
            assert len(model.requests) == 1
            stopping = asyncio.create_task(turn.cancel())
            try:
                await interrupt_entered.wait()
                assert not stopping.done()
                release.touch()
                while not released.exists():
                    await asyncio.sleep(0.01)
                try:
                    async with timeout(2):
                        while len(model.requests) == 1:
                            await asyncio.sleep(0.01)
                except asyncio.TimeoutError:
                    pass
                assert len(model.requests) == 1, "Stopped turn admitted late compaction"
            finally:
                release.touch()
                release_interrupt.set()
                await stopping
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            successor = await session.submit("Continue without tools.")
            assert (await successor.get_result()).final_output == "successor"
            assert len(model.requests) == 3
