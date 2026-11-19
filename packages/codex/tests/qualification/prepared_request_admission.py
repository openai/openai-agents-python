"""Prepared-request admission in the installed native wheel."""

import json
import os
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    native_options,
    responses as responses,
)
from tests.native_storage_fixtures import MemorySessionStore

from agents import _native


@pytest.mark.asyncio
async def test_revoked_no_edit_reply_fails_before_dispatch(
    tmp_path: Path, responses: ResponsesFixture
) -> None:
    assert os.environ["TOKIO_WORKER_THREADS"] == "1"
    assert Path(_native.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    responses.finite_http_response = True
    responses.respond(assistant_output("unexpected dispatch"))

    class ReplyStore(MemorySessionStore):
        on_open: Callable[[], None] | None = None

        def acquire(self, session_id):
            if self.on_open is not None:
                callback, self.on_open = self.on_open, None
                callback()
            return super().acquire(session_id)

    store = ReplyStore()
    options = native_options(tmp_path, responses)
    options_json = json.dumps(options)
    async with timeout(20), AsyncExitStack() as cleanup:
        engine = await _native.create_engine(
            json.dumps(options["config"]), json.dumps(options["runtime"]), canonical_store=store
        )
        cleanup.push_async_callback(engine.close)
        thread = await engine.start_replaceable_thread(options_json, "[]")
        cleanup.push_async_callback(thread.close)
        thread._prepare_requests(True)
        submitted = json.loads(
            await thread._submit(
                json.dumps({"mode": "start_if_idle", "input": [{"type": "text", "text": "Try"}]})
            )
        )
        assert submitted["status"] == "started"
        turn_id = submitted["turn_id"]
        events = []

        async def read_until(*kinds: str):
            while True:
                event = json.loads(await thread._next_session_event())
                events.append(event)
                if event["msg"]["type"] in kinds:
                    return event

        request = await read_until("_sdk_prepare_request")
        (attempt,) = (event for event in events if event["msg"]["type"] == "_sdk_model_attempt")
        assert attempt["id"] == attempt["msg"]["turn_id"] == turn_id
        assert attempt["msg"]["data"]["phase"] == "started"
        attempt_id = attempt["msg"]["data"]["attempt_id"]
        assert isinstance(attempt_id, str) and attempt_id
        assert request["id"] == request["msg"]["turn_id"] == turn_id
        request_id = request["msg"]["request_id"]

        def reply_then_revoke() -> None:
            assert thread._reply_prepared_request(request_id, None, False)
            thread._prepare_requests(False)
            assert not thread._reply_prepared_request(request_id, None, False)

        # Opening a second thread invokes the synchronous store on the sole Tokio worker.
        # Keep the worker inside this callback until revocation, before Core can consume the reply.
        store.on_open = reply_then_revoke
        other = await engine.start_replaceable_thread(options_json, "[]")
        cleanup.push_async_callback(other.close)
        assert store.on_open is None
        terminal = await read_until("task_complete", "turn_complete")
        failure = "Fatal error: Native request preparation failed or was cancelled"
        errors = [
            (event["id"], event["msg"]["message"])
            for event in events
            if event["msg"]["type"] == "error"
        ]
        assert errors == [(turn_id, failure)]
        assert terminal["id"] == turn_id
        assert terminal["msg"]["error"]["message"] == failure
        assert not responses.requests
