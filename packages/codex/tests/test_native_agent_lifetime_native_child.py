"""Native-only children never borrow the opening Python Agent's display label."""

import subprocess
import sys
from dataclasses import replace

from agents import NativeAgents, telemetry
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.test_native_child_agents import agent_for, child_peer, handles, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_trace_exporter import native_trace_child_env
from tests.test_tracing_native import Recorder


def test_native_only_child_does_not_invent_a_python_agent_span(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, sys; from pathlib import Path; from pytest import MonkeyPatch; "
            "from tests.test_native_agent_lifetime_native_child import native_case; "
            "asyncio.run(native_case(Path(sys.argv[1]), MonkeyPatch()))",
            str(tmp_path),
        ],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def native_case(tmp_path, monkeypatch):
    creation, steps = [], []

    class PolicyRecorder(Recorder):
        def should_create_span(self, name, kind):
            if kind == "agent":
                creation.append(name)
            return True

    async def before_child_step(_step):
        steps.append("child step")
        return None

    recorder = PolicyRecorder()
    await telemetry.configure(processors=[recorder])
    try:
        async with (
            timeout(30),
            child_peer(("default",)) as model,
            ExecutorPeer(
                tmp_path / "remote", synthetic_process_output="accepted native fixture result"
            ).serve() as executor,
        ):
            trust(executor, tmp_path, monkeypatch)
            model.release["alpha"].set()
            agent = agent_for(model, executor)
            [native] = [tool for tool in agent.tools if isinstance(tool, NativeAgents)]
            agent = replace(
                agent,
                name="RootOnly",
                tools=[replace(native, max_children=1, before_step=before_child_step)],
            )
            with telemetry.trace("native-child-agent-label") as workflow:
                trace_id = workflow.context.trace_id
                async with agent.session(environment=declaration(executor)) as session:
                    root = await session.submit("Spawn one native child")
                    [child] = await handles(session, 1)
                    assert (await root.get_result()).final_output == "root complete"
                    assert (await child.get_result()).final_output == "alpha complete"
            assert steps
            assert len(model.child_requests["alpha"]) == 2
            assert len(executor.synthetic_process_starts) == 1
        assert (await telemetry.force_flush()).status == "success"
        spans = [
            row
            for row in recorder.events
            if row["trace_id"] == trace_id and row["attributes"].get("sdk.span.kind") == "agent"
        ]
        assert creation == ["RootOnly"]
        assert [(row["lifecycle"], row["name"]) for row in spans] == [
            ("start", "RootOnly"),
            ("end", "RootOnly"),
        ]
    finally:
        await telemetry.shutdown()
