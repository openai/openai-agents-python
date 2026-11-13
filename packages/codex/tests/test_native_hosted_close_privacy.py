from __future__ import annotations

import builtins
import logging
import sys
import traceback
from typing import Any

import pytest
from openai import AsyncOpenAI

from agents import Agent, HostedResponsesWebSocketModel, ModelRequest, NativeProvider
from tests._asyncio import timeout

_PRIVATE = "synthetic-private-hosted-close-owner-4567269567"


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "failure_kind",
    [
        "direct",
        "cause",
        "context",
        pytest.param(
            "group",
            marks=pytest.mark.skipif(sys.version_info < (3, 11), reason="Native exception groups"),
        ),
    ],
)
async def test_failed_hosted_close_releases_socket_owner_from_retained_error_frames(
    streaming, failure_kind, caplog
):
    from _agents_codex._hosted_responses_websocket import _Socket
    from _agents_codex._responses_websocket import _Connection

    primary = OSError("synthetic receive failure")
    cleanup = ConnectionError("synthetic close failure")

    class Socket:
        sends = 0
        closes = 0

        async def send(self, value: str) -> None:
            assert _PRIVATE in value
            self.sends += 1

        async def recv(self) -> str:
            raise primary

        async def close(self) -> None:
            self.closes += 1
            if failure_kind == "direct":
                raise cleanup
            try:
                raise RuntimeError("synthetic close cause")
            except RuntimeError as leaf:
                if failure_kind == "cause":
                    raise cleanup from leaf
                if failure_kind == "context":
                    raise cleanup  # noqa: B904 - implicit-context canary
                if sys.version_info >= (3, 11):
                    raise builtins.ExceptionGroup(
                        "synthetic close group", [cleanup, leaf]
                    ) from None
                raise

    socket = Socket()

    async def connect(request: ModelRequest, options: Any) -> Socket:
        return socket

    async with timeout(20), AsyncOpenAI(api_key="synthetic") as client:
        async with NativeProvider(
            model_backend=HostedResponsesWebSocketModel(client, socket_factory=connect)
        ) as provider:
            async with Agent(
                model="gpt-5.5", instructions="Answer. " + _PRIVATE, provider=provider
            ).session() as session:
                turn = await session.submit("Public synthetic input.")
                with pytest.raises(Exception) as caught:
                    if streaming:
                        async for _ in turn.events():
                            pass
                    await turn.get_result()
                causes: list[BaseException] = [caught.value]
                snapshots = []
                for cause in causes:
                    for nested in (
                        cause.__cause__,
                        cause.__context__,
                        getattr(cause, "model_error", None),
                        *getattr(cause, "exceptions", ()),
                    ):
                        if isinstance(nested, BaseException) and nested not in causes:
                            causes.append(nested)
                    for frame, _ in traceback.walk_tb(cause.__traceback__):
                        for value in tuple(frame.f_locals.values()):
                            if isinstance(value, _Connection):
                                value = value.socket
                            if isinstance(value, _Socket) and value._run.active is not None:
                                snapshots.append(repr(value._run.active.create))
                logging.getLogger(__name__).error("Retained close owner snapshots: %s", snapshots)
                assert _PRIVATE not in repr(snapshots) and _PRIVATE not in caplog.text
                assert primary in causes and cleanup in causes
                assert socket.sends == socket.closes == 1
        assert not client.is_closed()


@pytest.mark.parametrize(
    "failure_kind",
    [
        "direct",
        "cause",
        "context",
        pytest.param(
            "group",
            marks=pytest.mark.skipif(sys.version_info < (3, 11), reason="Native exception groups"),
        ),
    ],
)
async def test_outer_cleanup_releases_hosted_owner_graph(failure_kind):
    from _agents_codex._hosted_responses_websocket import _Active, _Run, _Socket
    from _agents_codex._responses_websocket import _Connection, _Owner
    from agents.models import ModelRunOwner

    cleanup = ConnectionError("synthetic final close failure")
    leaf = RuntimeError("synthetic close cause")

    class Socket:
        closes = 0

        async def close(self):
            self.closes += 1
            if failure_kind == "direct":
                raise cleanup
            try:
                raise leaf
            except RuntimeError:
                if failure_kind == "cause":
                    raise cleanup from leaf
                if failure_kind == "context":
                    raise cleanup  # noqa: B904 - implicit-context canary
                if sys.version_info >= (3, 11):
                    raise builtins.ExceptionGroup(
                        "synthetic close group", [cleanup, leaf]
                    ) from None
                raise

    raw = Socket()
    owner = ModelRunOwner("synthetic-session", "synthetic-turn")
    async with AsyncOpenAI(api_key="synthetic") as client:
        backend = HostedResponsesWebSocketModel(client)
        run = _Run(active=_Active(create={"input": [{"text": _PRIVATE}]}, lineage="synthetic"))
        backend._runs[owner] = run
        backend._transport._owners[owner] = _Owner(
            connection=_Connection(_Socket(raw, run, {}, None), "scope")
        )
        error = None
        try:
            await backend.cleanup_on_run_end(owner)
        except Exception as caught:
            error = caught
        assert error is not None
        pending = [error]
        snapshots = []
        for cause in pending:
            for child in (cause.__cause__, cause.__context__, *getattr(cause, "exceptions", ())):
                if isinstance(child, BaseException) and child not in pending:
                    pending.append(child)
            for frame, _ in traceback.walk_tb(cause.__traceback__):
                if "_agents_codex" not in frame.f_code.co_filename:
                    continue
                for value in tuple(frame.f_locals.values()):
                    if isinstance(value, _Owner):
                        value = value.connection
                    if isinstance(value, _Connection):
                        value = value.socket
                    if isinstance(value, _Socket) and value._run.active is not None:
                        snapshots.append(repr(value._run.active.create))
                    if isinstance(value, HostedResponsesWebSocketModel):
                        snapshots.extend(
                            repr(item.active.create)
                            for item in value._runs.values()
                            if item.active is not None
                        )
        assert _PRIVATE not in repr(snapshots)
        assert cleanup in pending
        if failure_kind != "direct":
            assert leaf in pending
        assert owner not in backend._runs
        assert owner not in backend._transport._owners
        assert raw.closes == 1
        assert not client.is_closed()
