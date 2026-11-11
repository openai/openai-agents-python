"""Restored model attempts use current declaration limits."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from agents import (
    Agent,
    LocalStorage,
    ModelResponse,
    ModelRetrySettings,
    ModelSettings,
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


@pytest.mark.parametrize(
    ("active_profile", "limit"),
    [
        ("root", "retry"),
        ("worker", "retry"),
        ("worker-after-instruction-update", "retry"),
        ("return-to-root", "retry"),
        ("return-to-root", "attempt_timeout_ms"),
        ("worker", "invocation_timeout_ms"),
    ],
)
async def test_restored_graph_uses_current_attempt_limits(active_profile, limit):
    calls = []
    worker_active = active_profile in {"worker", "worker-after-instruction-update"}

    class Backend:
        async def complete(self, request):
            calls.append(request)
            if limit == "retry" and len(calls) == 1:
                raise ConnectionError("synthetic restored graph failure")
            if limit != "retry":
                await asyncio.sleep(2)
            return ModelResponse([assistant_output("unexpected retry")["item"]])

    current_settings: ModelSettings = {
        "retry": ModelRetrySettings(max_retries=0, policy=lambda _: True)
    }
    if limit == "attempt_timeout_ms":
        current_settings["attempt_timeout_ms"] = 1000
    elif limit == "invocation_timeout_ms":
        current_settings["invocation_timeout_ms"] = 1000
    async with timeout(20), NativeProvider(model_backend=Backend()) as provider:
        worker = Agent(name="Worker", instructions="Answer.", model_settings=current_settings)
        root = Agent(
            name="Root",
            instructions="Answer.",
            provider=provider,
            handoffs=[worker],
            model_settings=(
                {"retry": ModelRetrySettings(max_retries=3, policy=lambda _: True)}
                if worker_active
                else current_settings
            ),
        )
        async with root.session() as source:
            if worker_active:
                await source.update(agent=worker)
            saved = json.loads((await source.save_state()).to_json())

        changed = 0
        for row in saved["history"]:
            profile = (row.get("metadata") or {}).get("sdk_agent_profile")
            if profile is not None:
                model = profile["model"]
                controls = model["request_controls"]
                controls["retry"]["max_retries"] = 3
                if limit == "attempt_timeout_ms":
                    controls["attempt_timeout_ms"] = 5000
                elif limit == "invocation_timeout_ms":
                    model["invocation_timeout_ms"] = 5000
                changed += 1
        assert changed > 0

        async with root.session(state=SessionState.from_json(json.dumps(saved))) as restored:
            if active_profile == "return-to-root":
                await restored.update(agent=worker)
                await restored.update(agent=root)
            if active_profile == "worker-after-instruction-update":
                await restored.update(instructions="Answer from the current worker.")
            expected = (
                "synthetic restored graph failure"
                if limit == "retry"
                else "timed out|timeout|deadline"
            )
            with pytest.raises(Exception, match=expected):
                await (await restored.submit("Answer once.")).get_result()
        assert len(calls) == 1


async def test_stored_graph_resume_binds_the_active_worker_attempt_limit(tmp_path):
    calls = []

    class Backend:
        def complete(self, request):
            calls.append(request)
            raise ConnectionError("synthetic restored worker failure")

    storage = LocalStorage(tmp_path / "stored-graph-authority")
    async with timeout(20), NativeProvider(model_backend=Backend()) as provider:
        worker = Agent(
            name="Worker",
            instructions="Answer.",
            model_settings={"retry": ModelRetrySettings(max_retries=0, policy=lambda _: True)},
        )
        root = Agent(
            name="Root",
            instructions="Route.",
            provider=provider,
            handoffs=[worker],
            model_settings={"retry": ModelRetrySettings(max_retries=3, policy=lambda _: True)},
        )
        async with root.session(storage=storage) as source:
            await source.update(agent=worker)
            identity = source.id
        async with root.session(storage=storage, resume=identity) as restored:
            with pytest.raises(Exception, match="synthetic restored worker failure"):
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
