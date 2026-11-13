from __future__ import annotations

import builtins
import logging
import sys
import traceback
from typing import Any, NoReturn

import pytest
from openai import AsyncOpenAI

from agents import Agent, ModelRequest, NativeProvider, ResponsesWebSocketModel
from tests._asyncio import timeout
from tests.test_native_responses_websocket_review import _diagnostics

_PRIVATE = "synthetic-private-receive-frame-4566621806"


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
async def test_failed_custom_receive_releases_private_frames_and_keeps_diagnostics(
    streaming, failure_kind, caplog
):
    failure = ConnectionError("synthetic socket receive failure")

    def fail_leaf(payload: str) -> NoReturn:
        assert _PRIVATE in payload
        raise failure

    class Socket:
        closed = 0
        sends = 0

        async def send(self, value: str) -> None:
            self.sends += 1

        async def recv(self) -> str:
            payload = '{"private":"' + _PRIVATE + '"}'
            try:
                fail_leaf(payload)
            except ConnectionError as leaf:
                if failure_kind == "direct":
                    raise
                if failure_kind == "cause":
                    raise OSError("synthetic receive wrapper") from leaf
                if failure_kind == "context":
                    raise OSError("synthetic receive wrapper")  # noqa: B904 - implicit-context canary
                if sys.version_info >= (3, 11):
                    raise builtins.ExceptionGroup("synthetic receive group", [leaf]) from None
                raise

        async def close(self) -> None:
            self.closed += 1

    socket = Socket()

    async def connect(request: ModelRequest, options: Any) -> Socket:
        return socket

    async with timeout(20), AsyncOpenAI(api_key="synthetic") as client:
        async with NativeProvider(
            model_backend=ResponsesWebSocketModel(client, socket_factory=connect)
        ) as provider:
            async with Agent(
                model="gpt-4.1", instructions="Complete once.", provider=provider
            ).session() as session:
                turn = await session.submit("Public synthetic input.")
                with pytest.raises(Exception) as caught:
                    if streaming:
                        async for _ in turn.events():
                            pass
                    await turn.get_result()
                causes: list[BaseException] = [caught.value]
                report = _diagnostics(caught.value)
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
                        if frame.f_code.co_name in ("recv", "fail_leaf"):
                            report += repr(frame.f_locals)
                logging.getLogger(__name__).error("Native socket receive failure: %s", report)
                assert _PRIVATE not in report and _PRIVATE not in caplog.text
                assert failure in causes
                assert "synthetic socket receive failure" in "".join(
                    traceback.format_exception(caught.value)
                )
                assert socket.closed == socket.sends == 1
        assert not client.is_closed()
