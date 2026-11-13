import pytest

from agents import Agent, NativeAgents, NativeError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, tool_output
from tests.test_native_child_agents import collaboration, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_provider_lineage import history_peer


@pytest.mark.parametrize("quota", ["records", "bytes"])
async def test_history_exhaustion_rejects_before_dispatch_and_further_growth(quota):
    async with (
        timeout(40),
        history_peer(client_max_size=16 * 1024 * 1024) as (provider, model, _reads, _base, wire),
    ):
        model.respond(assistant_output("Before quota."))
        agent = Agent(instructions="Continue.", model="gpt-5.5", provider=provider)
        async with agent.session(
            provider_history={"type": "conversation", "id": "conv_current"}
        ) as session:
            assert (
                await (await session.submit("Small initial input.")).get_result()
            ).final_output == "Before quota."
            if quota == "records":
                initial = await session.history_snapshot()
                model_window = await session.model_context()
                await session.transact_history(
                    expected_revision=initial.revision,
                    keep=initial.total,
                    items=[
                        {
                            "type": "message",
                            "role": "user",
                            "id": f"quota-{index}",
                            "content": [{"type": "input_text", "text": f"retained-{index}"}],
                        }
                        for index in range(4096 - len(model_window.items))
                    ],
                )
                text = "One more input."
            else:
                text = "x" * (8 * 1024 * 1024 + 1)
            before = await session.history_snapshot()
            requests_before = len(model.requests)
            wire_before = list(wire)
            model.respond(assistant_output("Must not dispatch."))
            for _ in range(2):
                with pytest.raises(NativeError, match="Provider-managed history.*limit"):
                    await (await session.submit(text)).get_result()
                after = await session.history_snapshot()
                assert after.items == before.items
                assert after.revision == before.revision
            assert len(model.requests) == requests_before
            assert wire == wire_before
            if quota == "bytes":
                model.replies.clear()
                model.respond(assistant_output("Same cursor retained."))
                assert (
                    await (await session.submit("Small follow-up.")).get_result()
                ).final_output == "Same cursor retained."
        model.replies.clear()
        model.respond(assistant_output("Within budget."))
        async with agent.session(
            provider_history={"type": "conversation", "id": "conv_current"}
        ) as fresh:
            assert (
                await (await fresh.submit("Small input.")).get_result()
            ).final_output == "Within budget."
        assert len(model.requests) == (3 if quota == "bytes" else 2)


@pytest.mark.parametrize("remote,fork", [(True, "all"), (True, "none"), (False, "all")])
async def test_native_child_history_fork_refuses_remote_context_but_preserves_fresh(
    tmp_path, monkeypatch, remote, fork
):
    async with (
        timeout(45),
        history_peer() as (provider, model, _reads, _base, _wire),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        root_requests = []
        child_requests = []

        class Program:
            def __bool__(self):
                return True

            def popleft(self):
                body = model.requests[-1]
                path = metadata(body, model.request_headers[-1])["agent_name"]
                if path != "/root":
                    child_requests.append(body)
                    return [assistant_output("Child complete.")]
                root_requests.append(body)
                if len(root_requests) == 1:
                    return [
                        collaboration(
                            "spawn_agent",
                            {
                                "task_name": "fresh",
                                "message": "Complete a fresh task.",
                                "fork_turns": fork,
                            },
                            "spawn-child",
                        )
                    ]
                return [assistant_output("Parent complete.")]

        model.replies = Program()
        agent = Agent(
            instructions="Delegate once.",
            model="gpt-5.5",
            provider=provider,
            tools=[NativeAgents(max_children=1)],
            programmatic_tool_calling=False,
        )
        options = (
            {"provider_history": {"type": "conversation", "id": "conv_current"}} if remote else {}
        )
        async with agent.session(environment=declaration(executor), **options) as session:
            assert (
                await (await session.submit("Parent-only history.")).get_result()
            ).final_output == "Parent complete."
            children = [child async for child in session.subagents()]
            if remote and fork == "all":
                assert "Cannot fork provider-managed history" in tool_output(
                    root_requests[1], "spawn-child"
                )
                assert children == []
                assert child_requests == []
            else:
                assert len(children) == 1
                # The child can finish concurrently with the parent response.
                [child_turn] = await handles(session, 1)
                assert (await child_turn.get_result()).final_output == "Child complete."
            assert (
                await (await session.submit("Continue parent.")).get_result()
            ).final_output == "Parent complete."


@pytest.mark.parametrize("output_kind", ["provider", "tool"])
async def test_output_admission_preserves_receipt_and_fences_accepted_effects(output_kind):
    import asyncio

    from agents import function_tool
    from tests.native_fixtures import function_call

    effects = []
    release = asyncio.Event()

    @function_tool
    async def effect(label: str) -> str:
        effects.append(label)
        if len(effects) == 2:
            release.set()
        await release.wait()
        return "done"

    async with (
        timeout(60),
        history_peer(client_max_size=16 * 1024 * 1024) as (provider, model, _reads, _base, wire),
    ):
        model.respond(assistant_output("Before quota."))
        agent = Agent(
            instructions="Continue.",
            model="gpt-5.5",
            provider=provider,
            model_settings={"store": True, "parallel_tool_calls": True},
            tools=[effect],
        )
        async with agent.session(provider_history={"type": "previous_response"}) as session:
            await (await session.submit("Small initial input.")).get_result()
            if output_kind == "provider":
                model.respond(assistant_output("y" * (4 * 1024 * 1024)))
                text = "x" * (5 * 1024 * 1024)
            else:
                initial = await session.history_snapshot()
                model_window = await session.model_context()
                await session.transact_history(
                    expected_revision=initial.revision,
                    keep=initial.total,
                    items=[
                        {
                            "type": "message",
                            "role": "user",
                            "id": f"quota-{index}",
                            "content": [{"type": "input_text", "text": f"retained-{index}"}],
                        }
                        for index in range(4092 - len(model_window.items))
                    ],
                )
                model.respond(
                    function_call("effect", {"label": "a"}, "effect-a"),
                    function_call("effect", {"label": "b"}, "effect-b"),
                )
                model.respond(assistant_output("Must not continue."))
                text = "Complete both tools."
            with pytest.raises(NativeError, match="Provider-managed history.*limit"):
                await (await session.submit(text)).get_result()
            state = await session.save_state()
            saved_selection = state.provider_history
            assert saved_selection["type"] == "previous_response"
            assert saved_selection["id"] in (
                {"resp_1"} if output_kind == "provider" else {"resp_1", "resp_2"}
            )
            assert sorted(effects) == ([] if output_kind == "provider" else ["a", "b"])
            authorized = {
                "type": "previous_response",
                "id": "resp_1" if output_kind == "provider" else "resp_2",
            }
            before = list(wire)
            expected_failure = (
                "reconciliation" if output_kind == "provider" else "Provider-managed history.*limit"
            )
            with pytest.raises(NativeError, match=expected_failure):
                await (await session.submit("Must not retry.")).get_result()
            assert wire == before
        with pytest.raises(RuntimeError, match="reconciliation"):
            async with agent.session(state=state, provider_history=authorized):
                pass
        assert wire == before


async def test_sampling_expansion_rejects_before_dispatch_without_losing_the_cursor():
    enabled = False

    def transform(request):
        if enabled:
            return [
                *request.items,
                *(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "extra"}],
                    }
                    for _ in range(4096)
                ),
            ]
        return None

    async with (
        timeout(40),
        history_peer(client_max_size=16 * 1024 * 1024) as (provider, model, _reads, _base, wire),
    ):
        model.respond(assistant_output("Before quota."))
        agent = Agent(
            instructions="Continue.",
            model="gpt-5.5",
            provider=provider,
            transform_context=transform,
        )
        async with agent.session(
            provider_history={"type": "conversation", "id": "conv_current"}
        ) as session:
            await (await session.submit("Before.")).get_result()
            enabled = True
            model.respond(assistant_output("Must not dispatch."))
            before = list(wire)
            with pytest.raises(NativeError, match="Provider-managed history.*limit"):
                await (await session.submit("Keep the original input.")).get_result()
            assert wire == before
            enabled = False
            model.replies.clear()
            model.respond(assistant_output("After quota."))
            assert (
                await (await session.submit("Continue.")).get_result()
            ).final_output == "After quota."
            assert len(model.requests) == 2
