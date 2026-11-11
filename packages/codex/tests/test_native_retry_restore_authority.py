"""Restored model attempts use current declaration limits."""

from __future__ import annotations

import os

import pytest

from agents import Agent, ModelResponse, ModelRetrySettings, NativeProvider, SessionState
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
