from __future__ import annotations

import logging
import traceback
from typing import Any

import pytest
from openai import AsyncOpenAI

from agents import Agent, ModelRequest, NativeProvider, ResponsesWebSocketModel
from tests._asyncio import timeout

_PROMPT = "synthetic-private-factory-prompt-4566316533"
_AUTH = "synthetic-private-factory-auth-4566316533"


def _report(error: BaseException) -> str:
    pending, visited, output = [error], set(), []
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        output.append(repr(vars(current)))
        for frame, _ in traceback.walk_tb(current.__traceback__):
            # Inspect the failing application factory and its transport boundary.
            # The caller's own submitted input is outside this boundary.
            if not (
                frame.f_code.co_filename.endswith("_responses_websocket.py")
                or frame.f_code.co_name == "connect"
            ):
                continue
            output.extend(
                repr(value)
                for value in frame.f_locals.values()
                if isinstance(value, (str, bytes, dict, list, tuple))
            )
        pending.extend(
            value
            for value in (
                current.__cause__,
                current.__context__,
                getattr(current, "model_error", None),
            )
            if isinstance(value, BaseException)
        )
    return "\n".join(output)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("chained", [False, True])
async def test_failed_socket_factory_releases_prompt_and_auth_from_native_error_frames(
    streaming, chained, caplog
):
    errors = []

    async def connect(request: ModelRequest, options: Any) -> Any:
        prompt = request.body["input"]
        headers = options["headers"]
        assert _PROMPT in repr(prompt) and _AUTH in repr(headers)
        failure = OSError("synthetic socket opening failure")
        errors.append(failure)
        if chained:
            try:
                raise RuntimeError("synthetic socket opening cause")
            except RuntimeError as cause:
                raise failure from cause
        raise failure

    async with timeout(20), AsyncOpenAI(api_key=_AUTH) as client:
        model = ResponsesWebSocketModel(client, socket_factory=connect)
        async with NativeProvider(model_backend=model) as provider:
            async with Agent(
                model="gpt-4.1", instructions="Complete once.", provider=provider
            ).session() as session:
                turn = await session.submit(_PROMPT)
                with pytest.raises(Exception) as caught:
                    if streaming:
                        async for _ in turn.events():
                            pass
                    await turn.get_result()
                report = _report(caught.value)
                logging.getLogger(__name__).error("Native socket opening failure: %s", report)
                assert _PROMPT not in report and _AUTH not in report
                assert _PROMPT not in caplog.text and _AUTH not in caplog.text
                rendered = "".join(traceback.format_exception(caught.value))
                assert "synthetic socket opening failure" in rendered
                assert ("synthetic socket opening cause" in rendered) == chained
                assert errors
        assert not client.is_closed()
