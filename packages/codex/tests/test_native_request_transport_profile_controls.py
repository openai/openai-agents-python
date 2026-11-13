from __future__ import annotations

import pytest

from agents import Agent, LocalStorage, ToolRef, WebSearch, function_tool
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_provider_websockets import WebSocketPeer, peer


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("none", "none"),
        ("auto", "auto"),
        (ToolRef("lookup"), "required"),
    ],
    ids=["none", "auto", "named"],
)
async def test_operation_tool_choice_replaces_hosted_default(choice, expected):
    @function_tool
    def lookup() -> str:
        return "answer"

    async with timeout(20), peer() as server:
        server.respond(assistant_output("default"))
        server.respond(assistant_output("override"))
        agent = Agent(
            model="gpt-5.5",
            instructions="Answer.",
            provider=server.provider(websocket=False),
            tools=[WebSearch(route="hosted"), lookup],
            model_settings={"tool_choice": {"type": "web_search"}},
        )
        async with agent.session() as session:
            await (await session.submit("Use the default.")).get_result()
            await (
                await session.submit(
                    "Use this operation's choice.",
                    model_settings_override={"tool_choice": choice},
                )
            ).get_result()
        assert server.requests[0]["tool_choice"] == {"type": "web_search"}
        request = server.requests[1]
        assert request["tool_choice"] == expected
        if isinstance(choice, ToolRef):
            assert [(tool["type"], tool.get("name")) for tool in request["tools"]] == [
                ("function", "lookup")
            ]


@pytest.mark.parametrize("capture", ["save_state", "fork", "local_storage"])
@pytest.mark.parametrize(
    ("source_transport", "restored_transport"),
    [(True, True), (False, False), (True, False)],
    ids=["rebound", "unset", "removed"],
)
async def test_handoff_profile_rebinds_transport_without_persisting_it(
    monkeypatch, tmp_path, capture, source_transport, restored_transport
):
    routes: list[tuple[str | None, str | None]] = []
    original = WebSocketPeer.handle

    async def record(self, request):
        routes.append((request.headers.get("x-profile-route"), request.query.get("profile")))
        return await original(self, request)

    monkeypatch.setattr(WebSocketPeer, "handle", record)

    async with timeout(30), peer() as server:
        provider = server.provider(websocket=False)

        def graph(prefix: str, worker_transport: bool):
            worker = Agent(
                name="Worker",
                model="gpt-5.5",
                instructions="Answer the handoff.",
                provider=provider,
                model_settings={
                    "extra_headers": {"x-profile-route": f"{prefix}_WORKER_HEADER"},
                    "extra_query": {"profile": f"{prefix}_WORKER_QUERY"},
                }
                if worker_transport
                else None,
            )
            return Agent(
                name="Root",
                model="gpt-5.5",
                instructions="Hand off to Worker.",
                provider=provider,
                handoffs=[worker],
                model_settings={
                    "extra_headers": {"x-profile-route": f"{prefix}_ROOT_HEADER"},
                    "extra_query": {"profile": f"{prefix}_ROOT_QUERY"},
                },
            )

        server.respond(function_call("transfer_to_worker", {}, "handoff"))
        server.respond(assistant_output("worker answer"))
        storage = LocalStorage(tmp_path / "sessions") if capture == "local_storage" else None
        async with graph("SYNTHETIC_SOURCE", source_transport).session(storage=storage) as source:
            turn = await source.submit("Start.")
            assert (await turn.get_result()).final_output == "worker answer"
            session_id = source.id
            state = await getattr(source, capture)() if storage is None else None
        assert routes == [
            ("SYNTHETIC_SOURCE_ROOT_HEADER", "SYNTHETIC_SOURCE_ROOT_QUERY"),
            ("SYNTHETIC_SOURCE_WORKER_HEADER", "SYNTHETIC_SOURCE_WORKER_QUERY")
            if source_transport
            else (None, None),
        ]
        if storage is None:
            encoded = state.to_json()
        else:
            encoded = "".join(path.read_text() for path in storage.path.rglob("*.jsonl"))
            assert encoded
        assert "SYNTHETIC_SOURCE" not in encoded

        server.respond(assistant_output("restored worker answer"))
        async with graph("SYNTHETIC_DESTINATION", restored_transport).session(
            state=SessionState.from_json(encoded) if storage is None else None,
            storage=storage,
            resume=session_id if storage is not None else None,
        ) as restored:
            result = await (await restored.submit("Continue.")).get_result()
            assert result.final_output == "restored worker answer"
            if storage is None:
                assert "SYNTHETIC_DESTINATION" not in (await restored.save_state()).to_json()
        assert routes[-1] == (
            ("SYNTHETIC_DESTINATION_WORKER_HEADER", "SYNTHETIC_DESTINATION_WORKER_QUERY")
            if restored_transport
            else (None, None)
        )
        if storage is not None:
            persisted = "".join(path.read_text() for path in storage.path.rglob("*.jsonl"))
            assert persisted
            assert "SYNTHETIC_SOURCE" not in persisted
            assert "SYNTHETIC_DESTINATION" not in persisted
