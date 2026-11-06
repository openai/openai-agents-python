"""Actual native cancellation, interpreter shutdown and independent ownership."""

import asyncio
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agents import Agent, NativeProvider, _native, _native_async

# The standalone shutdown probe needs only the installed SDK and standard library.
if __name__ != "__main__":
    import pytest

    from tests._asyncio import timeout
    from tests.native_fixtures import (
        assistant_output,
        core_owner as core_owner,
        native_options,
        responses as responses,
    )


@contextmanager
def _on_native_start(callback):
    original = _native_async._await_native_completion
    owner = asyncio.current_task()

    def observe(call):
        if asyncio.current_task() is not owner:
            return original(call)

        class ObservedCall:
            def __getattr__(self, name):
                return getattr(call, name)

            def start(self, writer):
                call.start(writer)
                callback()

        return original(ObservedCall())

    with patch.object(_native_async, "_await_native_completion", observe):
        yield


def agent(responses):
    return Agent(
        instructions="Answer.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-native-async-key"),
    )


async def test_cancellation_immediately_after_native_start(responses, core_owner):
    async with timeout(20), agent(responses).session() as session:
        await session._native._replace_mcp_servers(
            "{}", json.dumps({"generation": 0, "servers": {}})
        )
        started = []

        def cancel():
            started.append("native operation started")
            asyncio.current_task().cancel()

        async def receive():
            with _on_native_start(cancel):
                await session._native._next_native_mcp_result()

        operation = asyncio.create_task(receive())
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert len(started) == 1
        responses.respond(assistant_output("still usable"))
        handle = await session.submit("continue")
        assert (await handle.get_result()).final_output == "still usable"


async def _pending_work_at_exit(options):
    engine = await _native.create_engine(
        json.dumps(options["config"]), json.dumps(options["runtime"])
    )
    thread = await engine.start_replaceable_thread(json.dumps(options), "[]")
    await thread._replace_mcp_servers("{}", json.dumps({"generation": 0, "servers": {}}))
    started = asyncio.Event()

    async def receive():
        with _on_native_start(started.set):
            await thread._next_native_mcp_result()

    pending = asyncio.create_task(receive())
    await started.wait()
    assert not pending.done()
    print("pending-native-work", flush=True)
    # asyncio.run owns cancellation of the pending call at interpreter shutdown.


async def test_interpreter_exits_with_pending_native_work(tmp_path, responses):
    # Python 3.10 ignores PYTHONSAFEPATH; keep tests/_asyncio.py off the import path.
    completed = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            "import runpy, sys; sys.argv.pop(0); runpy.run_path(sys.argv[0], run_name='__main__')",
            __file__,
            json.dumps(native_options(tmp_path, responses)),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(_native.__file__).parents[1]),
            "PYTHONSAFEPATH": "1",
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["pending-native-work", "shutdown-complete"]


async def test_independent_session_survives_other_session_close(responses):
    async with (
        timeout(30),
        agent(responses).session() as first,
        agent(responses).session() as second,
    ):
        assert first.id != second.id
        await first._aclose()
        responses.respond(assistant_output("sibling survived"))
        handle = await second.submit("continue")
        assert (await handle.get_result()).final_output == "sibling survived"


if __name__ == "__main__":
    asyncio.run(_pending_work_at_exit(json.loads(sys.argv[1])))
    print("shutdown-complete", flush=True)
