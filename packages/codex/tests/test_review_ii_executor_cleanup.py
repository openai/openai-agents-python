"""Entered executor callbacks remain owned and sanitized through shutdown."""

from __future__ import annotations

import asyncio
import json
import threading
import traceback
from contextlib import AsyncExitStack
from typing import Any

import pytest

from agents import Agent, ExecutorContext, NativeError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_code_mode import code_call
from tests.test_native_executor_tools import declaration


@pytest.mark.parametrize(
    "policy",
    [
        "approval",
        "transform_arguments",
        "code_transform_arguments",
        pytest.param("transform_result", marks=pytest.mark.requires_native_linux_sandbox),
    ],
)
@pytest.mark.parametrize(
    ("kind", "ending"),
    [("sync", "force"), ("async", "force"), ("sync", "close"), ("async", "close")],
)
async def test_root_shutdown_reports_executor_cleanup_failure(
    responses, tmp_path, monkeypatch, caplog, policy, kind, ending
) -> None:
    entered = asyncio.Event()
    sync_release = threading.Event()
    async_release = asyncio.Event()
    exited = threading.Event()
    loop = asyncio.get_running_loop()
    secret = "root-executor-cleanup-private-811a"

    def run_sync(ctx: ExecutorContext[None], value: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        try:
            sync_release.wait()
            raise RuntimeError(secret)
        finally:
            exited.set()

    async def run_async(ctx: ExecutorContext[None], value: Any) -> Any:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            try:
                await async_release.wait()
                raise RuntimeError(secret)
            finally:
                exited.set()

    async with timeout(35), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        after = policy == "transform_result"
        programmatic = after or policy == "code_transform_arguments"
        patch = "*** Begin Patch\n*** Add File: result-owned-once\n+effect\n*** End Patch"
        responses.respond(
            code_call("text(await tools.apply_patch(" + json.dumps(patch) + "));", "held-result")
            if after
            else code_call(
                "text(await tools.exec_command({cmd:'touch root-denied-811a'}));", "held-before"
            )
            if programmatic
            else function_call("exec_command", {"cmd": "touch root-denied-811a"}, "held-before")
        )
        async with AsyncExitStack() as sessions:
            session = await sessions.enter_async_context(
                Agent(
                    instructions="Use selected tools.",
                    tools=[],
                    provider=provider(responses),
                    programmatic_tool_calling=programmatic,
                ).session(
                    environment=declaration(
                        peer,
                        **{
                            "transform_arguments"
                            if policy == "code_transform_arguments"
                            else policy: run_sync if kind == "sync" else run_async
                        },
                    )
                )
            )
            turn = await session.submit("Run the requested tool.")
            stop = None
            cancelling = None
            try:
                await entered.wait()
                if after:
                    assert (peer.root / "result-owned-once").read_text() == "effect\n"
                else:
                    assert not (peer.root / "root-denied-811a").exists()
                if ending == "force":
                    cancelling = asyncio.create_task(turn.cancel())
                    await asyncio.sleep(0)
                    assert not exited.is_set()
                stop = asyncio.create_task(sessions.aclose())
                done, _ = await asyncio.wait((stop,), timeout=0.1)
                assert not done, "shutdown returned before its entered callback exited"
                assert not exited.is_set()
                sync_release.set()
                async_release.set()
                with pytest.raises(NativeError, match="cleanup") as caught:
                    await stop
                if cancelling is not None:
                    await asyncio.gather(cancelling, return_exceptions=True)
                assert exited.is_set()
                assert secret not in "".join(traceback.format_exception(caught.value))
                assert secret not in caplog.text
                assert len(responses.requests) == 1
                assert not (peer.root / "root-denied-811a").exists()
                if after:
                    # The result callback cannot undo the already-recorded effect.
                    assert (peer.root / "result-owned-once").read_text() == "effect\n"
                else:
                    assert not any(
                        req.get("method") == "process/start" and "root-denied-811a" in str(req)
                        for req in peer.requests
                    )
                # The old handle retains its own sanitized completion.
                with pytest.raises((NativeError, asyncio.CancelledError)):
                    await turn.get_result()
            finally:
                sync_release.set()
                async_release.set()
                if stop is not None:
                    await asyncio.gather(stop, return_exceptions=True)


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_application_failure_stays_primary_when_close_joins_failed_executor_policy(
    responses, tmp_path, monkeypatch, caplog, kind
) -> None:
    entered = asyncio.Event()
    leave_body = asyncio.Event()
    sync_release = threading.Event()
    async_release = asyncio.Event()
    exited = threading.Event()
    loop = asyncio.get_running_loop()
    secret = "application-close-callback-private-d498"
    primary = ValueError("application failure")

    def run_sync(ctx: ExecutorContext[None], value: Any) -> bool:
        loop.call_soon_threadsafe(entered.set)
        try:
            sync_release.wait()
            raise RuntimeError(secret)
        finally:
            exited.set()

    async def run_async(ctx: ExecutorContext[None], value: Any) -> bool:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            try:
                await async_release.wait()
                raise RuntimeError(secret)
            finally:
                exited.set()

    async with timeout(35), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        responses.respond(
            function_call("exec_command", {"cmd": "touch application-denied-d498"}, "held")
        )

        async def application() -> None:
            async with Agent(
                instructions="Use tools.", tools=[], provider=provider(responses)
            ).session(
                environment=declaration(
                    peer,
                    approval=run_sync if kind == "sync" else run_async,
                )
            ) as session:
                await session.submit("Run tool.")
                await leave_body.wait()
                raise primary

        owner = asyncio.create_task(application())
        try:
            await entered.wait()
            leave_body.set()
            done, _ = await asyncio.wait((owner,), timeout=0.1)
            assert not done, "close must join the application's already-entered callback"
            assert not exited.is_set()
            sync_release.set()
            async_release.set()
            with pytest.raises(ValueError, match="application failure") as caught:
                await owner
            assert caught.value is primary
            formatted = "".join(traceback.format_exception(caught.value))
            assert "cleanup" in formatted
            assert secret not in formatted
            assert secret not in caplog.text
            assert exited.is_set()
            assert len(responses.requests) == 1
            assert not (peer.root / "application-denied-d498").exists()
            assert not any(
                req.get("method") == "process/start" and "application-denied-d498" in str(req)
                for req in peer.requests
            )
        finally:
            leave_body.set()
            sync_release.set()
            async_release.set()
            await asyncio.gather(owner, return_exceptions=True)
