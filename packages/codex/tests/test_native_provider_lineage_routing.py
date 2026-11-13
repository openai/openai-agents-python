import pytest

from agents import Agent, ModelSettings, NativeError
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_provider_lineage import history_peer


@pytest.mark.parametrize("kind", ["header", "query", "cold"])
@pytest.mark.parametrize("permitted", [False, True])
async def test_history_rebind_uses_current_request_route(kind, permitted):
    routing = []
    async with (
        timeout(30),
        history_peer(
            routing=routing,
            allow_changed_route=permitted,
        ) as (provider, model, _reads, _base, _wire),
    ):
        model.respond(assistant_output("Before."))
        model.respond(assistant_output("After."))
        original: ModelSettings = {
            "store": True,
            "extra_headers": {"x-history-route": "A"},
            "extra_query": {"tenant": "A"},
        }
        agent = Agent(
            instructions="Continue.", model="gpt-5.5", provider=provider, model_settings=original
        )
        changed: ModelSettings = {}
        if kind != "query":
            changed["extra_headers"] = {"x-history-route": "B"}
        if kind != "header":
            changed["extra_query"] = {"tenant": "B"}

        async def finish(active, override=None):
            if permitted:
                await (await active.submit("After.", model_settings_override=override)).get_result()
            else:
                with pytest.raises(NativeError, match="anchor validation failed"):
                    await (
                        await active.submit("After.", model_settings_override=override)
                    ).get_result()
                assert len(model.requests) == 1
                await (
                    await active.submit(
                        "Retry the original route.", model_settings_override=original
                    )
                ).get_result()

        async with agent.session(provider_history={"type": "previous_response"}) as source:
            await (await source.submit("Before.")).get_result()
            if kind == "cold":
                state = await source.save_state()
                await source.aclose()
                current = Agent(
                    instructions="Continue.",
                    model="gpt-5.5",
                    provider=provider,
                    model_settings={**original, **changed},
                )
                async with current.session(
                    state=state,
                    provider_history={
                        "type": "previous_response",
                        "id": "resp_1",
                    },
                ) as restored:
                    await finish(restored)
            else:
                await finish(source, changed)
        assert (
            "GET",
            "A" if kind == "query" else "B",
            "A" if kind == "header" else "B",
            "Bearer current-owner",
            None,
        ) in routing
        assert len(model.requests) == 2
        assert [row for row in routing if row[0] == "POST"] == [
            ("POST", "A", "A", "Bearer current-owner", None),
            (
                "POST",
                "B" if permitted and kind != "query" else "A",
                "B" if permitted and kind != "header" else "A",
                "Bearer current-owner",
                None,
            ),
        ]


@pytest.mark.parametrize("current_route", [None, "A"])
async def test_manual_history_restore_uses_current_root_route(current_route):
    from agents import FunctionTool
    from tests.native_fixtures import function_call
    from tests.test_native_manual_tools import LookupArgs

    lookup = FunctionTool(
        name="lookup", description="Read a record.", input_type=LookupArgs, output_type=str
    )
    routing = []
    async with (
        timeout(30),
        history_peer(routing=routing) as (
            provider,
            model,
            _reads,
            _base,
            _wire,
        ),
    ):
        model.respond(function_call("lookup", {"key": "original"}, "manual"))
        model.respond(assistant_output("After."))
        saved_settings: ModelSettings = {
            "store": True,
            "extra_headers": {"x-history-route": "B"},
            "extra_query": {"tenant": "B"},
        }
        current_settings: ModelSettings = {"store": True}
        if current_route is not None:
            current_settings.update(
                extra_headers={"x-history-route": current_route},
                extra_query={"tenant": current_route},
            )

        async def pending(active):
            async for event in active.interactions():
                request = lookup.match(event)
                if request is not None:
                    return request
            raise AssertionError("Missing manual request")

        original = Agent(
            instructions="Read.",
            model="gpt-5.5",
            provider=provider,
            tools=[lookup],
            model_settings=saved_settings,
        )
        async with original.session(provider_history={"type": "previous_response"}) as source:
            await source.submit("Read.")
            await pending(source)
            state = await source.suspend()
        current = Agent(
            instructions="Read.",
            model="gpt-5.5",
            provider=provider,
            tools=[lookup],
            model_settings=current_settings,
        )
        async with current.session(
            state=state,
            provider_history={
                "type": "previous_response",
                "id": "resp_1",
            },
        ) as restored:
            request = await pending(restored)
            await request.respond("accepted")
            assert (await restored.restored_turn.get_result()).final_output == "After."
        assert ("GET", current_route, current_route, "Bearer current-owner", None) in routing
        assert [row for row in routing if row[0] == "POST"] == [
            ("POST", "B", "B", "Bearer current-owner", None),
            ("POST", current_route, current_route, "Bearer current-owner", None),
        ]
        assert len(model.requests) == 2
        assert (
            sum(
                item.get("type") == "function_call_output" and item.get("call_id") == "manual"
                for item in model.requests[1]["input"]
            )
            == 1
        )
