"""Verify configuration capture and refresh at Session ownership boundaries."""

import json
from contextlib import asynccontextmanager

import pytest

from agents import _native
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, native_options, responses as responses


@asynccontextmanager
async def opened(tmp_path, responses, *, managed, capture=False, initial_effort="low"):
    options = native_options(tmp_path, responses)
    source = tmp_path / "settings.toml"
    source.write_text(f'model_reasoning_effort = "{initial_effort}"\n')
    options["runtime"]["loader"].update(user_config_path=str(source), ignore_user_config=False)
    owner = _native.NativeProviderOwner() if managed else None
    lease = owner.acquire() if owner is not None else None
    engine = None
    try:
        engine = await _native.create_engine(
            json.dumps(options["config"]),
            json.dumps(options["runtime"]),
            **({"provider_lease": lease} if lease is not None else {}),
            **({"_capture_initial_config": True} if capture else {}),
        )
        yield engine, source
    finally:
        if engine is not None:
            await engine.close()
        if lease is not None:
            # Exercise the real owner cleanup even if startup or the assertion fails.
            lease.orphan()
            await owner.close()


async def request_effort(engine, responses, options=None, *, private=False):
    start = engine._start_initial_replaceable_thread if private else engine.start_replaceable_thread
    thread = await start(json.dumps(options or {}), "[]")
    try:
        responses.respond(assistant_output("done"))
        result = json.loads(
            await thread._submit(
                json.dumps({"mode": "start_if_idle", "input": [{"type": "text", "text": "Go"}]})
            )
        )
        assert result["status"] == "started"
        async with timeout(15):
            while True:
                event = json.loads(await thread.next_event())
                assert event is not None
                assert event["msg"]["type"] != "error", event
                if event["msg"]["type"] == "task_complete":
                    break
        return responses.requests[-1]["reasoning"]["effort"]
    finally:
        await thread.close()


async def test_session_owned_first_open_captures_then_refreshes(tmp_path, responses):
    async with opened(tmp_path, responses, managed=True, capture=True) as (engine, source):
        source.write_text('model_reasoning_effort = "high"\n')
        assert await request_effort(engine, responses, private=True) == "low"
        assert await request_effort(engine, responses, private=True) == "high"


@pytest.mark.parametrize("private", [False, True])
async def test_raw_engine_keeps_refresh_semantics(tmp_path, responses, private):
    async with opened(tmp_path, responses, managed=False) as (engine, source):
        source.write_text('model_reasoning_effort = "high"\n')
        assert await request_effort(engine, responses, private=private) == "high"


async def test_changed_runtime_reloads_first_open(tmp_path, responses):
    async with opened(tmp_path, responses, managed=True, capture=True) as (engine, source):
        source.write_text('model_reasoning_effort = "high"\n')
        # A changed runtime declaration must avoid the acquisition snapshot.
        other = tmp_path / "other.toml"
        other.write_text('model_reasoning_effort = "high"\n')
        assert (
            await request_effort(
                engine,
                responses,
                {"runtime": {"loader": {"user_config_path": str(other)}}},
                private=True,
            )
            == "high"
        )
        assert await request_effort(engine, responses, private=True) == "high"


async def test_changed_config_applies_first_open(tmp_path, responses):
    async with opened(tmp_path, responses, managed=True, capture=True) as (engine, source):
        assert (
            await request_effort(
                engine, responses, {"config": {"model_reasoning_effort": "high"}}, private=True
            )
            == "high"
        )


async def test_failed_start_consumes_capture(tmp_path, responses):
    async with opened(tmp_path, responses, managed=True, capture=True) as (engine, source):
        with pytest.raises(RuntimeError):
            await engine._start_initial_replaceable_thread(
                json.dumps({"config": {"sandbox_mode": "invalid-synthetic-mode"}}), "[]"
            )
        source.write_text('model_reasoning_effort = "high"\n')
        assert await request_effort(engine, responses, private=True) == "high"


async def test_each_managed_acquisition_captures_fresh_settings(tmp_path, responses):
    async with opened(tmp_path, responses, managed=True, capture=True) as (engine, source):
        assert await request_effort(engine, responses, private=True) == "low"
    async with opened(tmp_path, responses, managed=True, capture=True, initial_effort="high") as (
        engine,
        source,
    ):
        assert await request_effort(engine, responses, private=True) == "high"


@pytest.mark.parametrize("private", [False, True])
async def test_leased_raw_engine_keeps_custom_file_refresh(tmp_path, responses, private):
    async with opened(tmp_path, responses, managed=True) as (engine, source):
        source.write_text('model_reasoning_effort = "high"\n')
        assert await request_effort(engine, responses, private=private) == "high"


async def test_initial_capture_requires_managed_lease():
    with pytest.raises(
        RuntimeError, match="Initial Session configuration capture requires a Provider lease"
    ):
        await _native.create_engine(_capture_initial_config=True)
