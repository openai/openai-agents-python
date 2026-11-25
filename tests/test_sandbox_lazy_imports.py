from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def _run_fresh_python(source: str) -> None:
    env = {key: value for key, value in os.environ.items() if "proxy" not in key.lower()}
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ordinary_tool_run_does_not_load_sandbox_implementations() -> None:
    _run_fresh_python(
        """
        import asyncio
        import sys
        from agents import Agent, Runner, RunConfig
        from agents.decorators import tool
        from agents.testing import ScriptedModel, assistant_message, function_call

        @tool
        def increment(x: int) -> str:
            return str(x + 1)

        model = ScriptedModel([
            [function_call("increment", {"x": 1}, call_id="call_1")],
            [assistant_message("2")],
        ])
        result = asyncio.run(Runner.run(
            Agent(name="arithmetic", model=model, tools=[increment]),
            "start",
            run_config=RunConfig(tracing_disabled=True),
        ))
        assert result.final_output == "2"
        assert len(model.calls) == 2
        model.assert_complete()
        assert "agents.sandbox.manifest" not in sys.modules
        assert "agents.sandbox.runtime_session_manager" not in sys.modules
        assert "agents.sandbox.capabilities" not in sys.modules
        assert "agents.testing.sandbox" not in sys.modules

        class CustomAgent(Agent):
            pass

        ordinary = asyncio.run(Runner.run(
            CustomAgent(name="custom", model=ScriptedModel([[assistant_message("ok")]])),
            "start",
            run_config=RunConfig(tracing_disabled=True),
        ))
        assert ordinary.final_output == "ok"
        from agents.sandbox import SandboxAgent
        from agents.exceptions import UserError
        try:
            asyncio.run(Runner.run(
                SandboxAgent(name="sandbox", model=ScriptedModel([[assistant_message("unused")]])),
                "start",
                run_config=RunConfig(tracing_disabled=True),
            ))
        except UserError as error:
            assert "requires `RunConfig(sandbox=...)`" in str(error)
        else:
            raise AssertionError("SandboxAgent ran without sandbox configuration")
        """
    )


def test_sandbox_exports_keep_identity_and_concurrent_first_access() -> None:
    _run_fresh_python(
        """
        from concurrent.futures import ThreadPoolExecutor
        import inspect
        import pickle
        import agents.sandbox as sandbox

        assert set(sandbox.__all__).issubset(dir(sandbox))
        with ThreadPoolExecutor(max_workers=4) as pool:
            exported = list(pool.map(lambda _: sandbox.Manifest, range(8)))
        from agents.sandbox.manifest import Manifest
        assert all(value is Manifest for value in exported)
        manifest = Manifest()
        assert type(pickle.loads(pickle.dumps(manifest))) is Manifest
        assert inspect.getmodule(sandbox.Manifest).__name__ == "agents.sandbox.manifest"
        from agents.run_config import SandboxRunConfig
        assert sandbox.SandboxRunConfig is SandboxRunConfig
        exported = {name: getattr(sandbox, name) for name in sandbox.__all__}
        assert exported["Manifest"] is Manifest

        import agents.testing as testing
        from agents.testing.sandbox import ScriptedSandboxSession, scripted_sandbox_session
        assert testing.ScriptedSandboxSession is ScriptedSandboxSession
        assert testing.scripted_sandbox_session is scripted_sandbox_session
        """
    )
