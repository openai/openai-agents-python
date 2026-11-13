"""Native WebSocket send failures do not retain the request in chained frames."""

import traceback

import pytest
from openai import AsyncOpenAI

from agents import Agent, NativeProvider, ResponsesWebSocketModel
from tests._asyncio import timeout


@pytest.mark.parametrize("fail_in_helper", [False, True])
async def test_socket_send_failure_releases_request_from_traceback_locals(fail_in_helper):
    canary = "private-socket-send-" + "synthetic-canary"
    calls = []

    class Socket:
        async def send(self, value):
            assert canary in value
            calls.append("send")
            if fail_in_helper:
                try:
                    fail(value)
                except LookupError as error:
                    raise OSError("socket send failed") from error
            raise OSError("socket send failed")

        async def recv(self):
            pytest.fail("A failed send must not start receiving")

        async def close(self):
            calls.append("close")

    def fail(payload):
        assert canary in payload
        raise LookupError("synthetic send helper failed")

    async def connect(request, options):
        return Socket()

    async with timeout(20), AsyncOpenAI(api_key="synthetic") as client:
        model = ResponsesWebSocketModel(client, socket_factory=connect)
        async with NativeProvider(model_backend=model) as provider:
            async with Agent(
                model="gpt-4.1", instructions="Complete once.", provider=provider
            ).session() as session:
                turn = await session.submit(canary)
                with pytest.raises(Exception) as caught:
                    await turn.get_result()

    failure = caught.value
    diagnostics = []
    seen = set()
    stack = [failure]
    while stack:
        error = stack.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        stack.extend(
            item
            for item in (error.__cause__, error.__context__, getattr(error, "model_error", None))
            if isinstance(item, BaseException)
        )
        frames = (
            (frame, line)
            for frame, line in traceback.walk_tb(error.__traceback__)
            if frame.f_code in (Socket.send.__code__, fail.__code__)
        )
        diagnostics.extend(traceback.StackSummary.extract(frames, capture_locals=True))
    assert calls == ["send", "close"]
    assert diagnostics, "The diagnostic chain must retain the original error and source frames"
    assert all(canary not in repr(frame.locals) for frame in diagnostics)
