"""Idle model updates belong to the native conversation and its checkpoints."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from agents import Agent, ModelSettings, function_tool
from tests._asyncio import timeout
from tests.test_native_agent_checkpoints import call, message, peer


async def test_native_model_update_is_acknowledged_and_restored():
    async with timeout(30), peer() as provider:
        provider.replies.extend([message("before"), message("after"), message("restored")])
        agent = Agent(
            instructions="Answer briefly.",
            provider=provider.provider(),
            model="gpt-5.2",
            model_settings={"reasoning_effort": "low", "reasoning_summary": "concise"},
        )
        async with agent.session() as session:
            first = await session.submit("before update")
            assert (await (first).get_result()).final_output == "before"
            await session.update(
                model="gpt-5.2-codex",
                model_settings={
                    "reasoning_effort": "high",
                    "reasoning_summary": "detailed",
                    "service_tier": "flex",
                },
            )
            # Save before a new turn: the acknowledged settings must be enough.
            saved = await session.save_state()
            second = await session.submit("after update")
            assert (await (second).get_result()).final_output == "after"
            assert (await (first).get_result()).final_output == "before"
        async with agent.session(state=saved) as restored:
            third = await restored.submit("after restore")
            assert (await (third).get_result()).final_output == "restored"
        initial, changed, restored_request = provider.requests
        assert initial["model"] == "gpt-5.2"
        assert initial["reasoning"] == {"effort": "low", "summary": "concise"}
        for request in (changed, restored_request):
            assert request["model"] == "gpt-5.2-codex"
            assert request["reasoning"] == {"effort": "high", "summary": "detailed"}
            assert request["service_tier"] == "flex"


async def test_native_settings_patch_keeps_omitted_values_and_clears_tier():
    async with timeout(30), peer() as provider:
        provider.replies.extend([message("patched"), message("cleared")])
        agent = Agent(
            instructions="Answer.",
            provider=provider.provider(),
            model="gpt-5.2",
            model_settings={
                "reasoning_effort": "low",
                "reasoning_summary": "concise",
                "service_tier": "flex",
            },
        )
        async with agent.session() as session:
            await session.update(model_settings={"reasoning_effort": "high"})
            first = await session.submit("patch")
            assert (await (first).get_result()).final_output == "patched"
            await session.update(model_settings={"service_tier": None})
            second = await session.submit("clear")
            assert (await (second).get_result()).final_output == "cleared"
        patched, cleared = provider.requests
        assert patched["service_tier"] == "flex"
        assert cleared.get("service_tier") in (None, "default")
        for request in provider.requests:
            assert request["model"] == "gpt-5.2"
            assert request["reasoning"] == {"effort": "high", "summary": "concise"}


@pytest.mark.parametrize(
    "settings",
    [
        {"unknown": "value"},
        {"temperature": "invalid"},
        {"reasoning_effort": None},
        {"reasoning_summary": None},
        {"reasoning_effort": 2},
        {"reasoning_effort": "persistent"},
        {"reasoning_summary": "unsupported"},
        {"service_tier": 2},
        {"service_tier": ""},
    ],
)
async def test_invalid_mixed_update_cannot_partially_change_model(settings):
    async with timeout(30), peer() as provider:
        provider.replies.append(message("unchanged"))
        agent = Agent(instructions="Answer.", provider=provider.provider(), model="gpt-5.2")
        async with agent.session() as session:
            with pytest.raises((TypeError, ValueError)):
                await session.update(model="gpt-5.2-codex", model_settings=cast(Any, settings))
            submitted = await session.submit("still usable")
            assert (await (submitted).get_result()).final_output == "unchanged"
        assert provider.requests[0]["model"] == "gpt-5.2"


@pytest.mark.parametrize("model", [2, "", "   "])
async def test_invalid_model_rejects_before_other_settings_change(model):
    async with timeout(30), peer() as provider:
        provider.replies.append(message("unchanged"))
        agent = Agent(instructions="Answer.", provider=provider.provider(), model="gpt-5.2")
        async with agent.session() as session:
            with pytest.raises((TypeError, ValueError)):
                await session.update(
                    model=cast(Any, model), model_settings={"service_tier": "flex"}
                )
            submitted = await session.submit("still usable")
            assert (await (submitted).get_result()).final_output == "unchanged"
        assert provider.requests[0]["model"] == "gpt-5.2"
        assert provider.requests[0].get("service_tier") != "flex"


async def test_empty_update_checks_lifetime_but_does_not_send_native_operation(monkeypatch):
    async with timeout(30), peer() as provider:
        agent = Agent(instructions="Answer.", provider=provider.provider())
        async with agent.session() as session:

            def forbidden(*args):
                pytest.fail("empty update reached the mutating Core operation")

            monkeypatch.setattr(type(session._native), "_update_model_settings", forbidden)
            await session.update()
            await session.update(model=None, model_settings=None)
            await session.update(model_settings={})
        with pytest.raises(RuntimeError, match="closed"):
            await session.update()


@pytest.mark.parametrize(
    "changes",
    [
        {"instructions": "unaccepted-instructions"},
        {"model": "gpt-5.2-codex"},
        {"model_settings": {"reasoning_effort": "high"}},
    ],
)
async def test_mixed_tool_update_rejects_before_either_category_changes(
    changes: dict[str, Any],
) -> None:
    calls: list[str] = []

    @function_tool
    def lookup() -> str:
        """Record that the opening tool still runs."""
        calls.append("original")
        return "unchanged"

    async with timeout(30), peer() as provider:
        provider.replies.extend([call("lookup", "original-call"), message("done")])
        agent = Agent(
            instructions="original-instructions",
            tools=[lookup],
            model="gpt-5.2",
            provider=provider.provider(),
        )
        async with agent.session() as session:
            with pytest.raises(TypeError, match="unexpected keyword argument"):
                await session.submit("new", tools=[], **changes)
            result = await (await session.submit("Use the original tool")).get_result()
            assert result.final_output == "done"
        assert calls == ["original"]
        assert len(provider.requests) == 2
        for request in provider.requests:
            assert request["model"] == "gpt-5.2"
            assert "unaccepted-instructions" not in str(request)
            assert request.get("reasoning", {}).get("effort") != "high"


async def test_active_submission_rejects_public_and_native_update():
    entered, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        entered.set()
        await release.wait()
        return "released"

    async with timeout(30), peer() as provider:
        provider.replies.extend([call("hold", "held-call"), message("finished")])
        agent = Agent(
            instructions="Use the tool.",
            tools=[hold],
            provider=provider.provider(),
            model="gpt-5.2",
        )
        async with agent.session() as session:
            submitted = await session.submit("hold")
            await entered.wait()
            try:
                with pytest.raises(RuntimeError, match="busy"):
                    await session.update()
                with pytest.raises(RuntimeError, match="busy"):
                    await session.update(model="gpt-5.2-codex")
                with pytest.raises(RuntimeError, match="busy"):
                    await session.update(instructions="Changed while busy")
                with pytest.raises(ValueError, match="idle|active submission"):
                    await session._native._update_model_settings('{"model":"gpt-5.2-codex"}')
                # Core rejects the update at admission; a thin session rejects
                # the checkpoint needed for promotion before changing its owner.
                with pytest.raises(
                    (ValueError, RuntimeError), match="idle|Active SDK Sessions cannot be saved"
                ):
                    await session._native._update_instructions("Changed while busy")
            finally:
                release.set()
            assert (await (submitted).get_result()).final_output == "finished"
        assert all(request["model"] == "gpt-5.2" for request in provider.requests)


@pytest.mark.parametrize("close", [False, True], ids=["cancel-waiter", "close-session"])
async def test_update_joins_native_acknowledgement_and_captures_settings(monkeypatch, close):
    accepted, release = asyncio.Event(), asyncio.Event()
    async with timeout(30), peer() as provider:
        provider.replies.append(message("changed"))
        agent = Agent(instructions="Answer.", provider=provider.provider(), model="gpt-5.2")
        async with agent.session() as session:
            native_update = type(session._native)._update_model_settings

            async def delayed(native, encoded):
                result = await native_update(native, encoded)
                accepted.set()
                await release.wait()
                return result

            monkeypatch.setattr(type(session._native), "_update_model_settings", delayed)
            settings: ModelSettings = {"service_tier": "flex"}
            update = asyncio.create_task(session.update(model_settings=settings))
            await accepted.wait()
            settings["service_tier"] = None
            for operation in (
                session.submit("busy"),
                session.save_state(),
                session.submit("new", tools=[]),
                session.compact(),
            ):
                with pytest.raises(RuntimeError):
                    await operation
            closing = asyncio.create_task(session._aclose()) if close else None
            update.cancel()
            await asyncio.sleep(0)
            assert not update.done()
            if closing is not None:
                assert not closing.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await update
            if closing is not None:
                await closing
            else:
                submitted = await session.submit("after joined update")
                assert (await (submitted).get_result()).final_output == "changed"
                assert provider.requests[0]["service_tier"] == "flex"


@pytest.mark.parametrize("failure", ["lost", "wrong-receipt"])
async def test_unconfirmed_update_fences_session(monkeypatch, failure):
    async with timeout(30), peer() as provider:
        agent = Agent(instructions="Answer.", provider=provider.provider())
        async with agent.session() as session:
            native_update = type(session._native)._update_model_settings

            async def lose_acknowledgement(native, encoded):
                await native_update(native, encoded)
                if failure == "lost":
                    raise OSError("synthetic lost acknowledgement")
                return False

            monkeypatch.setattr(
                type(session._native), "_update_model_settings", lose_acknowledgement
            )
            with pytest.raises((OSError, RuntimeError), match="acknowledgement"):
                await session.update(model_settings={"service_tier": "flex"})
            for operation in (session.submit("blocked"), session.save_state(), session.update()):
                with pytest.raises(RuntimeError, match="unconfirmed"):
                    await operation
        assert provider.requests == []


async def test_zero_turn_checkpoint_restores_saved_defaults_over_new_agent_settings():
    async with timeout(30), peer() as provider:
        provider.replies.extend([message("original"), message("restored")])
        agent = Agent(instructions="Answer.", provider=provider.provider(), model="gpt-5.2")
        async with agent.session() as session:
            saved = await session.save_state()
            submitted = await session.submit("original defaults")
            assert (await (submitted).get_result()).final_output == "original"
        changed_agent = Agent(
            instructions="Answer.",
            provider=provider.provider("current-credential"),
            model="gpt-5.2-codex",
            model_settings={
                "reasoning_effort": "high",
                "reasoning_summary": "detailed",
                "service_tier": "flex",
            },
        )
        async with changed_agent.session(state=saved) as session:
            submitted = await session.submit("saved defaults")
            assert (await (submitted).get_result()).final_output == "restored"
        original, restored = provider.requests
        for key in ("model", "reasoning", "service_tier"):
            assert restored.get(key) == original.get(key)
        assert provider.authorization == [
            "Bearer synthetic-checkpoint-key",
            "Bearer current-credential",
        ]
