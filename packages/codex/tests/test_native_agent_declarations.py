"""Declaration capture, actual admission checks, and one-Session resource ownership."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace

import pytest

from agents import (
    Agent,
    ModelSettings,
    NativeAgents,
    NativeProvider,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses  # noqa: F401
from tests.test_native_agent_checkpoints import message, peer


async def test_agent_captures_defaults_and_settings_cannot_mutate_live_or_future_runs(
    responses,  # noqa: F811
):
    calls = []

    @function_tool
    def lookup() -> str:
        calls.append("accepted")
        return "accepted"

    settings: ModelSettings = {"reasoning_effort": "low", "service_tier": "flex"}
    tools = [lookup]
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-declaration-key")
    agent = Agent(
        instructions="Use lookup.",
        tools=tools,
        model="gpt-5.2",
        model_settings=settings,
        provider=provider,
    )
    tools.clear()
    settings["reasoning_effort"] = "high"
    settings["service_tier"] = None
    with pytest.raises(TypeError):
        agent.model_settings["service_tier"] = None
    for name, value in (
        ("model", "gpt-5.5"),
        ("instructions", "changed"),
        ("tools", ()),
        ("provider", None),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(agent, name, value)
        with pytest.raises(FrozenInstanceError):
            delattr(agent, name)
    assert agent.model_settings == {"reasoning_effort": "low", "service_tier": "flex"}
    async with provider:
        for i in range(2):
            responses.respond(function_call("lookup", {}, f"lookup-{i}"))
            responses.respond(assistant_output("done"))
            assert (await agent.run("read")).final_output == "done"
        assert calls == ["accepted", "accepted"]
        assert all(
            r["reasoning"]["effort"] == "low" and r["service_tier"] == "flex"
            for r in responses.requests
        )


async def test_session_and_runner_override_provider_model_and_merge_settings():
    async with timeout(20), peer() as first, peer() as second:
        default = first.provider()
        override = second.provider()
        agent = Agent(
            instructions="Answer.",
            provider=default,
            model="gpt-5.2",
            model_settings={
                "reasoning_effort": "low",
                "reasoning_summary": "concise",
                "service_tier": "flex",
            },
        )
        first.replies.extend([message("agent"), message("again")])
        second.replies.extend([message("session"), message("runner"), message("borrowed")])
        async with default, override:
            assert (await agent.run("default")).final_output == "agent"
            async with agent.session(
                provider=override,
                model="gpt-5.2-codex",
                model_settings={"reasoning_effort": "high"},
            ) as session:
                assert (
                    await (await session.submit("changed")).get_result()
                ).final_output == "session"
            result = await agent.run(
                "clear",
                provider=override,
                model="gpt-5.2-codex",
                model_settings={"service_tier": None},
            )
            assert result.final_output == "runner"
            assert (await agent.run("default again")).final_output == "again"
            assert (
                await Agent(instructions="Answer.").run("reuse", provider=override)
            ).final_output == "borrowed"
        for request in first.requests:
            assert request["model"] == "gpt-5.2"
            assert request["reasoning"] == {"effort": "low", "summary": "concise"}
            assert request["service_tier"] == "flex"
        retained, cleared = second.requests[:2]
        assert retained["model"] == cleared["model"] == "gpt-5.2-codex"
        assert retained["reasoning"] == {"effort": "high", "summary": "concise"}
        assert retained["service_tier"] == "flex"
        assert cleared["reasoning"] == {"effort": "low", "summary": "concise"}
        assert cleared.get("service_tier") is None
        for provider in (default, override):
            with pytest.raises(RuntimeError, match="closed"):
                async with agent.session(provider=provider):
                    pytest.fail("closed provider reopened")


async def test_provider_context_rejects_open_lease_then_remains_usable(responses):  # noqa: F811
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-context-key")
    agent = Agent(instructions="Answer.", provider=provider)
    manager = provider.__aenter__
    assert await manager() is provider
    async with agent.session() as session:
        with pytest.raises(RuntimeError, match="open or opening"):
            await provider.__aexit__(None, None, None)
        responses.respond(assistant_output("lease survived"))
        assert (await (await session.submit("hello")).get_result()).final_output == "lease survived"
    responses.respond(assistant_output("usable until closed"))
    assert (await agent.run("again")).final_output == "usable until closed"
    await provider.__aexit__(None, None, None)
    with pytest.raises(RuntimeError, match="closed"):
        async with provider:
            pytest.fail("context reentered after close")


def _observe_implicit(monkeypatch, url):
    # Only substitute the implicit connection's destination; Core owns all
    # acquisitions, native publication, responses and retirement in these tests.
    original = NativeProvider.__post_init__
    created = []

    def local(self):
        if self.base_url is None and self.api_key is None:
            object.__setattr__(self, "base_url", url)
            object.__setattr__(self, "api_key", "synthetic-implicit-key")
            created.append(self)
        original(self)

    monkeypatch.setattr(NativeProvider, "__post_init__", local)
    return created


async def test_one_agent_owns_fresh_implicit_provider_per_session_and_run(responses, monkeypatch):  # noqa: F811
    created = _observe_implicit(monkeypatch, responses.url)
    agent = Agent(instructions="Answer.")
    assert created == []
    async with agent.session() as first, agent.session() as sibling:
        assert len(created) == 2
        assert created[0] is not created[1]
        assert first.id != sibling.id
        with pytest.raises(RuntimeError, match="open or opening"):
            await created[0].aclose()
        for session in (first, sibling):
            responses.respond(assistant_output("open"))
            assert (await (await session.submit("hello")).get_result()).final_output == "open"
    responses.respond(assistant_output("later"))
    assert (await agent.run("later")).final_output == "later"
    assert len(created) == 3
    for provider in created:
        with pytest.raises(RuntimeError, match="closed"):
            async with agent.session(provider=provider):
                pytest.fail("implicit provider survived Session retirement")
    assert agent.provider is None


async def test_failed_open_closes_only_implicit_provider_and_keeps_declaration_reusable(
    responses,  # noqa: F811
    monkeypatch,
):
    from agents import _native

    explicit = NativeProvider(base_url=responses.url, api_key="synthetic-explicit-key")
    created = _observe_implicit(monkeypatch, responses.url)
    agent = Agent(instructions="Answer.")
    original = _native.create_engine

    def fail_creation(*args, **kwargs):
        raise RuntimeError("synthetic-opening-error")

    monkeypatch.setattr(_native, "create_engine", fail_creation)
    for provider in (None, explicit):
        with pytest.raises(RuntimeError, match="synthetic-opening-error"):
            async with agent.session(provider=provider):
                pytest.fail("opening unexpectedly succeeded")
    assert len(created) == 1
    monkeypatch.setattr(_native, "create_engine", original)
    with pytest.raises(RuntimeError, match="closed"):
        async with agent.session(provider=created[0]):
            pytest.fail("implicit failed opening survived")
    responses.respond(assistant_output("borrower recovered"))
    assert (await agent.run("explicit", provider=explicit)).final_output == "borrower recovered"
    responses.respond(assistant_output("implicit recovered"))
    assert (await agent.run("implicit")).final_output == "implicit recovered"
    assert len(created) == 2
    await explicit.aclose()


async def test_run_failure_retires_implicit_provider_after_callback_join(responses, monkeypatch):  # noqa: F811
    created = _observe_implicit(monkeypatch, responses.url)
    entered, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @function_tool
    async def held() -> str:
        entered.set()
        try:
            await release.wait()
            return "unused"
        finally:
            joined.set()

    agent = Agent(instructions="Use held.", tools=[held])
    responses.respond(function_call("held", {}, "held"))
    async with timeout(20):
        running = asyncio.create_task(agent.run("read"))
        await entered.wait()
        assert len(created) == 1
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert joined.is_set()
        with pytest.raises(RuntimeError, match="closed"):
            async with agent.session(provider=created[0]):
                pytest.fail("failed run kept its implicit provider")


@pytest.mark.parametrize(
    "overrides",
    [{"model": "gpt-5.2"}, {"model_settings": {}}, {"model_settings": {"service_tier": None}}],
)
async def test_restore_rejects_opening_overrides_before_credentials_or_lease(
    responses,  # noqa: F811
    monkeypatch,
    overrides,
):
    from agents import _native

    provider = NativeProvider(base_url=responses.url, api_key="synthetic-restore-key")
    agent = Agent(instructions="Keep the saved instructions.", provider=provider)
    async with agent.session() as session:
        saved = await session.save_state()

    created = _observe_implicit(monkeypatch, responses.url)

    def forbidden(*args, **kwargs):
        pytest.fail(
            "restore reached credential, lease, or native opening before rejecting overrides"
        )

    monkeypatch.setattr(_native, "_read_openai_api_key_from_env", forbidden)
    monkeypatch.setattr(NativeProvider, "_acquire_lease", forbidden)
    monkeypatch.setattr(_native, "create_checkpoint_engine", forbidden)
    implicit = replace(agent, provider=None)
    with pytest.raises(ValueError, match="cannot be used with state"):
        async with implicit.session(state=saved, **overrides):
            pytest.fail("restore allowed incompatible overrides")
    assert created == []
    assert responses.requests == []


async def test_validation_uses_real_admission_and_does_not_create_implicit_provider(
    responses,  # noqa: F811
    monkeypatch,
):
    created = _observe_implicit(monkeypatch, responses.url)
    valid = Agent(instructions="Answer.", tools=[NativeAgents()])
    # An Environment and explicit model can be supplied at opening.
    assert valid.validate() is None
    invalid = Agent(instructions="Answer.", tools=[NativeAgents(), NativeAgents()])
    with pytest.raises(ValueError, match="exactly one NativeAgents") as offline:
        invalid.validate()
    with pytest.raises(ValueError) as failure:
        async with invalid.session():
            pytest.fail("admitted duplicate NativeAgents grants")
    assert str(failure.value) == str(offline.value)
    assert created == []
