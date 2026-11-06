"""Real Executor termination plus filesystem cleanup failure; requires sandbox host."""

from pathlib import Path

import pytest

from agents import Agent
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_executor_tools import declaration


@pytest.mark.asyncio
@pytest.mark.requires_native_linux_sandbox
async def test_executor_retirement_retries_home_without_repeating_termination(
    responses, tmp_path, monkeypatch
):
    async with (
        timeout(25),
        ExecutorPeer(tmp_path / "remote", deny_terminate=True).serve() as peer,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        responses.respond(
            function_call("exec_command", {"cmd": "sleep 5", "yield_time_ms": 1000}, "background")
        )
        responses.respond(assistant_output("running"))
        owner = provider(responses)
        async with Agent(instructions="Use tools.", tools=[], provider=owner).session(
            environment=declaration(peer)
        ) as session:
            await (await session.submit("start background process")).get_result()
            assert "Process running with session ID" in tool_output(
                responses.requests[-1], "background"
            )
            home = session._home
            path = Path(home.name)
            backup = path.with_name(path.name + "-retry")

            class FailNativeHomeOnce:
                attempted = False

                def cleanup(self):
                    if not self.attempted:
                        self.attempted = True
                        path.rename(backup)
                        path.write_text("prevent actual native directory removal")
                    home.cleanup()

            session._home = FailNativeHomeOnce()
            try:
                with pytest.raises(Exception) as error:
                    await session._aclose()
                messages = []
                current = error.value
                while current is not None:
                    messages.append(str(current))
                    current = current.__context__
                assert any("termination attempt was not acknowledged" in text for text in messages)
                assert any("directory" in text.lower() for text in messages)
                with pytest.raises(RuntimeError, match="open or opening"):
                    await owner.aclose()
                attempted = sum(r.get("method") == "process/terminate" for r in peer.requests)
            finally:
                if backup.exists():
                    path.unlink()
                    backup.rename(path)
            await session._aclose()
            assert not path.exists()
            assert sum(r.get("method") == "process/terminate" for r in peer.requests) == attempted
        await owner.aclose()
        await peer.wait_disconnected()


@pytest.mark.asyncio
async def test_executor_connection_retirement_retries_real_home_after_ack_failure(
    responses, tmp_path, monkeypatch
):
    """No process starts; only termination acknowledgement is fault-injected."""
    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        responses.respond(assistant_output("connected"))
        owner = provider(responses)
        async with Agent(
            instructions="Answer without tools.",
            tools=[],
            provider=owner,
        ).session(environment=declaration(peer)) as session:
            result = await (await session.submit("confirm")).get_result()
            assert result.final_output == "connected"
            native = session._native
            home = session._home
            path = Path(home.name)
            backup = path.with_name(path.name + "-retry")
            attempts = []

            class FailAcknowledgement:
                def __getattr__(self, name):
                    return getattr(native, name)

                async def _terminate_executor_processes(self):
                    attempts.append("termination")
                    raise RuntimeError("controlled termination acknowledgement failure")

            class FailRealHomeOnce:
                attempted = False

                def cleanup(self):
                    if not self.attempted:
                        self.attempted = True
                        path.rename(backup)
                        path.write_text("prevent actual native directory removal")
                    home.cleanup()

            session._native = FailAcknowledgement()
            session._home = FailRealHomeOnce()
            try:
                with pytest.raises(Exception) as error:
                    await session._aclose()
                messages = []
                current = error.value
                while current is not None:
                    messages.append(str(current))
                    current = current.__context__
                assert any("controlled termination acknowledgement failure" in s for s in messages)
                assert any("directory" in s.lower() for s in messages)
                with pytest.raises(RuntimeError, match="open or opening"):
                    await owner.aclose()
            finally:
                if backup.exists():
                    path.unlink()
                    backup.rename(path)
            await session._aclose()
            assert not path.exists()
            assert attempts == ["termination"]
        await owner.aclose()
        assert peer.attempts > 0, ("Executor transport never connected", peer.requests)
        await peer.wait_disconnected()
        assert len(responses.requests) == 1
        assert not any(request.get("method") == "process/start" for request in peer.requests)
