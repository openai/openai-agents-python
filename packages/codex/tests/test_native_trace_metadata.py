"""Native root metadata ownership, with no host metadata cache."""

from __future__ import annotations

import asyncio
import copy
import os
import subprocess
import sys

from agents import Agent, function_tool, telemetry
from agents.testing import ScriptedResponses, text, tool_call


def test_native_root_metadata_ownership(tmp_path):
    env = dict(os.environ, CODEX_HOME=str(tmp_path / "home"))
    env.pop("OPENAI_API_KEY", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from tests.test_native_trace_metadata import native_case; "
            "asyncio.run(native_case())",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=50,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def native_case():
    class Recorder:
        def on_trace_event(self, _event):
            pass

    await telemetry.configure(processors=[Recorder()])
    try:
        metadata = {
            "number": {"$serde_json::private::Number": "7"},
            "raw": {"$serde_json::private::RawValue": '{"a":1}'},
            "number_literal": {"$serde_json::private::Number": "ordinary text"},
            "raw_literal": {"$serde_json::private::RawValue": "ordinary text"},
            "escaped_string": {"text": "é" * 1000},
            "large_integer": 10**80,
            "flag": False,
            "text": "false",
            "object": {},
            "objectText": "{}",
            "nested": {"values": [1, "1", None]},
            "dotted.key": "original",
        }
        expected = copy.deepcopy(metadata)
        with telemetry.trace("metadata.original", options={"metadata": metadata}) as root:
            metadata["nested"]["values"][0] = "caller mutation"
            assert root.trace_metadata == expected
            snapshot = root.trace_metadata
            assert snapshot is not None
            snapshot["nested"]["values"][0] = "readback mutation"
            assert root.trace_metadata == expected
            with telemetry.span("metadata.child", options={"metadata": {"child": True}}) as child:
                assert child.trace_metadata == expected
            with telemetry.span(
                "metadata.detached", options={"detached": True, "metadata": {"own": True}}
            ) as child:
                assert child.trace_metadata == {"own": True}
        assert root.context.trace_metadata == expected
        with telemetry.trace(
            "metadata.same-id",
            options={"trace_id": root.context.trace_id, "metadata": {"other": True}},
        ) as other:
            assert other.trace_metadata == {"other": True}
            with telemetry.span(
                "metadata.explicit-parent", options={"parent": root.context}
            ) as child:
                assert child.trace_metadata == expected
        assert root.trace_metadata == expected

        gate = asyncio.Event()

        async def delayed():
            await gate.wait()
            context = telemetry.current_context()
            assert context is not None
            assert context.trace_metadata == {"task": "a"}

        with telemetry.trace("metadata.task-a", options={"metadata": {"task": "a"}}):
            task = asyncio.create_task(delayed())
        with telemetry.trace("metadata.task-b", options={"metadata": {"task": "b"}}) as current:
            gate.set()
            await task
            assert current.trace_metadata == {"task": "b"}

        observed = []

        @function_tool
        async def read_metadata() -> str:
            """Inherited native root metadata."""
            context = telemetry.current_context()
            assert context is not None
            observed.append(context.trace_metadata)
            return "done"

        async with ScriptedResponses(
            [text("ready")],
            [tool_call("read_metadata", {}, call_id="metadata-read")],
            [text("restored")],
        ) as peer:
            agent = Agent(
                instructions="Answer.",
                model="gpt-5.5",
                provider=peer.provider,
                tools=[read_metadata],
            )
            with telemetry.trace(
                "metadata.source", options={"metadata": {"canary": "ROOT_METADATA_SOURCE_CANARY"}}
            ):
                async with agent.session() as session:
                    assert (
                        await (await session.submit("first")).get_result()
                    ).final_output == "ready"
                    state = await session.save_state()
            assert "ROOT_METADATA_SOURCE_CANARY" not in state.to_json()
            with telemetry.trace("metadata.restored", options={"metadata": {"owner": "restored"}}):
                result = await agent.run("continue", state=state)
                assert result.final_output == "restored"
            assert observed == [{"owner": "restored"}]
        for key, value in {
            "$serde_json::private::Number": "7",
            "$serde_json::private::RawValue": '{"a":1}',
        }.items():
            magic = {key: value}
            with telemetry.trace("metadata.magic-root", options={"metadata": magic}) as magic_root:
                assert magic_root.trace_metadata == magic
            assert magic_root.trace_metadata == magic
        assert (await telemetry.force_flush()).status == "success"
    finally:
        await telemetry.shutdown(timeout_ms=1000)
