"""Restored model attempts use current declaration limits."""

from __future__ import annotations

import asyncio
import os

import pytest

from agents import (
    Agent,
    LocalStorage,
    ModelResponse,
    ModelRetrySettings,
    NativeProvider,
    SessionState,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output

pytestmark = pytest.mark.skipif(
    os.getenv("OPENAI_AGENTS_NATIVE_TESTS") != "1", reason="Requires the native runtime"
)


async def test_restored_retry_limit_does_not_inherit_saved_attempt_budget():
    calls = []

    class Backend:
        def complete(self, request):
            calls.append(request)
            if len(calls) == 1:
                raise ConnectionError("synthetic first-attempt failure")
            return ModelResponse([assistant_output("unexpected replay")["item"]])

    async with timeout(20), NativeProvider(model_backend=Backend()) as provider:
        seed = Agent(
            instructions="Answer.",
            provider=provider,
            model_settings={"retry": ModelRetrySettings(max_retries=3, policy=lambda _: True)},
        )
        async with seed.session() as source:
            saved = SessionState.from_json((await source.save_state()).to_json())
        current = Agent(
            instructions="Answer.",
            provider=provider,
            model_settings={"retry": ModelRetrySettings(max_retries=0, policy=lambda _: True)},
        )
        async with current.session(state=saved) as restored:
            with pytest.raises(Exception, match="synthetic first-attempt failure"):
                await (await restored.submit("Answer once.")).get_result()
        assert len(calls) == 1


@pytest.mark.parametrize("mode", ["omitted", "numeric", "timeout"])
async def test_stored_resume_uses_current_attempt_controls(tmp_path, mode):
    calls = []
    restoring = False

    class Backend:
        async def complete(self, request):
            calls.append(request)
            if not restoring:
                return ModelResponse([assistant_output("seed settings persisted")["item"]])
            if mode == "timeout":
                await asyncio.sleep(2)
                return ModelResponse([assistant_output("current timeout cleared")["item"]])
            raise ConnectionError("stored-resume provider failure")

    storage = LocalStorage(tmp_path / "stored-attempt-authority")
    async with timeout(20), NativeProvider(model_backend=Backend()) as provider:
        seed = Agent(
            instructions="Answer.",
            provider=provider,
            model_settings={
                "retry": ModelRetrySettings(max_retries=3, policy=lambda _: True),
                "attempt_timeout_ms": 1000,
            },
        )
        async with seed.session(storage=storage) as source:
            identity = source.id
            assert (
                await (await source.submit("Persist seed settings.")).get_result()
            ).final_output == ("seed settings persisted")
        assert len(calls) == 1
        calls.clear()
        restoring = True
        current = Agent(
            instructions="Answer.",
            provider=provider,
            model_settings={"retry": ModelRetrySettings(max_retries=1)}
            if mode == "numeric"
            else {},
        )
        async with current.session(storage=storage, resume=identity) as restored:
            turn = await restored.submit("Use current controls.")
            if mode == "timeout":
                assert (await turn.get_result()).final_output == "current timeout cleared"
            else:
                with pytest.raises(Exception, match="stored-resume provider failure"):
                    await turn.get_result()
        assert len(calls) == 1
