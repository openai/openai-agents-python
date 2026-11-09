"""Provider-owned tools through real native Sessions and synthetic Responses."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest

from agents import (
    Agent,
    CapabilityError,
    CodeInterpreter,
    CodexLogin,
    FileSearch,
    ImageGeneration,
    NativeError,
    NativeProvider,
    SessionState,
    TurnStoppedError,
    function_tool,
)
from agents.observation import (
    EventGap,
    HostedToolCallProgress,
    ModelOutputItemCompleted,
    OutputTextDelta,
    TurnFinished,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    text_response,
    tool_output,
)

PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def completed(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response.output_item.done", "output_index": 0, "item": item}


def provider(peer: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=peer.url, api_key="synthetic-hosted-tools-key")


# Deliberately include unknown provider fields to detect lossy normalization.
CASES = [
    pytest.param(
        FileSearch(
            vector_store_ids=["vs_fixture"],
            max_num_results=3,
            include_search_results=True,
            ranking_options={"ranker": "auto", "score_threshold": 0.4},
            filters={"type": "eq", "key": "department", "value": "support"},
        ),
        {
            "type": "file_search",
            "vector_store_ids": ["vs_fixture"],
            "max_num_results": 3,
            "ranking_options": {"ranker": "auto", "score_threshold": 0.4},
            "filters": {"type": "eq", "key": "department", "value": "support"},
        },
        {
            "type": "file_search_call",
            "id": "fs_fixture",
            "status": "completed",
            "queries": ["support hours"],
            "results": [
                {
                    "file_id": "file_fixture",
                    "filename": "hours.txt",
                    "text": "Open at nine.",
                    "score": 0.9,
                    "attributes": {"department": "support"},
                }
            ],
            "future_field": {"retained": True},
        },
        ("queries", "results"),
        "file_search_call.results",
        id="file-search",
    ),
    pytest.param(
        CodeInterpreter(
            container={
                "type": "auto",
                "file_ids": ["file_input"],
                "memory_limit": "4g",
                "network_policy": {"type": "disabled"},
            }
        ),
        {
            "type": "code_interpreter",
            "container": {
                "type": "auto",
                "file_ids": ["file_input"],
                "memory_limit": "4g",
                "network_policy": {"type": "disabled"},
            },
        },
        {
            "type": "code_interpreter_call",
            "id": "ci_fixture",
            "status": "completed",
            "container_id": "cntr_fixture",
            "code": "print(6 * 7)",
            "outputs": [
                {"type": "logs", "logs": "42\n"},
                {"type": "image", "url": "https://example.invalid/chart.png"},
            ],
            "future_field": {"retained": True},
        },
        ("container_id", "code", "outputs"),
        "code_interpreter_call.outputs",
        id="code-interpreter",
    ),
    pytest.param(
        ImageGeneration(
            action="generate",
            quality="low",
            size="1024x1024",
            background="transparent",
            output_format="png",
            partial_images=1,
        ),
        {
            "type": "image_generation",
            "action": "generate",
            "quality": "low",
            "size": "1024x1024",
            "background": "transparent",
            "output_format": "png",
            "partial_images": 1,
        },
        {
            "type": "image_generation_call",
            "id": "ig_fixture",
            "status": "completed",
            "result": "ZmluYWwtaW1hZ2U=",
            "revised_prompt": "A blue square.",
            "output_format": "png",
            "size": "1024x1024",
            "future_field": {"retained": True},
        },
        ("result", "revised_prompt"),
        None,
        id="image-generation",
    ),
]


@pytest.mark.parametrize("container", ["cntr_existing", {"type": "auto"}])
@pytest.mark.parametrize(
    "options,expected_callers",
    [
        pytest.param({}, None, id="default"),
        pytest.param({"allowed_callers": None}, None, id="none"),
        pytest.param({"allowed_callers": ["direct"]}, ["direct"], id="direct"),
        pytest.param({"allowed_callers": ["programmatic"]}, ["programmatic"], id="programmatic"),
        pytest.param(
            {"allowed_callers": ("direct", "programmatic")},
            ["direct", "programmatic"],
            id="both",
        ),
    ],
)
async def test_code_interpreter_allowed_callers_reach_native_request(
    responses, container, options, expected_callers
):
    responses.respond(assistant_output("Done."))
    borrowed = provider(responses)
    async with (
        timeout(20),
        borrowed,
        Agent(
            instructions="Use the hosted tool.",
            model="gpt-5.2",
            provider=borrowed,
            tools=[CodeInterpreter(container=container, **options)],
        ).session() as session,
    ):
        assert (await (await session.submit("Run the task.")).get_result()).final_output == "Done."

    assert len(responses.requests) == 1
    expected = {"type": "code_interpreter", "container": container}
    if expected_callers is not None:
        expected["allowed_callers"] = expected_callers
    assert (
        next(tool for tool in responses.requests[0]["tools"] if tool["type"] == "code_interpreter")
        == expected
    )


@pytest.mark.parametrize("tool,wire,raw,fields,include", CASES)
async def test_config_completion_and_portable_history(responses, tool, wire, raw, fields, include):
    responses.respond(completed(raw), assistant_output("Provider finished."))
    borrowed = provider(responses)
    agent = Agent(instructions="Use the tool.", model="gpt-5.2", provider=borrowed, tools=[tool])
    async with timeout(30), borrowed:
        async with agent.session() as session:
            turn = await session.submit("Perform the hosted task.")
            events = [event async for event in turn.events(include_execution_details=True)]
            assert (await turn.get_result()).final_output == "Provider finished."
            items = [item async for item in session.items()]
            calls = [item for item in items if item["type"] == raw["type"]]
            assert len(calls) == 1
            saved_call = calls[0]
            assert saved_call["id"] == raw["id"]
            assert saved_call["status"] == "completed"
            assert saved_call["turn_id"] == turn.turn_id
            assert saved_call["provider_data"] == raw
            for field in fields:
                assert saved_call[field] == raw[field]
            assert [
                event.item
                for event in events
                if isinstance(event, ModelOutputItemCompleted) and event.item["type"] == raw["type"]
            ] == [saved_call]
            state = SessionState.from_json((await session.save_state()).to_json())

        responses.respond(assistant_output("Evidence reused."))
        async with agent.session(state=state) as restored:
            assert [item async for item in restored.items() if item["type"] == raw["type"]] == [
                saved_call
            ]
            assert (
                await (await restored.submit("Continue from the saved result.")).get_result()
            ).final_output == "Evidence reused."

    assert len(responses.requests) == 2  # No function-result round trip for hosted calls.
    for request in responses.requests:
        assert next(entry for entry in request["tools"] if entry["type"] == wire["type"]) == wire
        if include is not None:
            assert include in request["include"]
        assert all(entry.get("type") != "function_call_output" for entry in request["input"])
    assert (
        next(item for item in responses.requests[1]["input"] if item.get("type") == raw["type"])
        == raw
    )


@pytest.mark.parametrize("tool,_wire,raw,fields,_include", CASES)
async def test_hosted_only_response_is_terminal_without_assistant_text(
    responses, tool, _wire, raw, fields, _include
):
    responses.respond(completed(raw))
    borrowed = provider(responses)
    async with (
        timeout(30),
        borrowed,
        Agent(
            instructions="Use the hosted tool.", model="gpt-5.2", provider=borrowed, tools=[tool]
        ).session() as session,
    ):
        turn = await session.submit("Perform the hosted task without a text answer.")
        assert (await turn.get_result()).final_output == ""
        events = [event async for event in turn.events()]
        assert [event.status for event in events if isinstance(event, TurnFinished)] == [
            "completed"
        ]
        items = [item async for item in session.items()]
        calls = [item for item in items if item["type"] == raw["type"]]
        assert len(calls) == 1
        assert calls[0]["turn_id"] == turn.turn_id
        assert calls[0]["status"] == "completed"
        assert calls[0]["provider_data"] == raw
        for field in fields:
            assert calls[0][field] == raw[field]
        assert not [
            item for item in items if item["type"] == "message" and item["role"] == "assistant"
        ]
    assert len(responses.requests) == 1


@pytest.mark.parametrize("include_results", [False, True])
async def test_file_search_results_include_is_request_level(responses, include_results):
    responses.respond(
        completed(
            {"type": "file_search_call", "id": "fs_empty", "status": "completed", "queries": []}
        ),
        assistant_output("No matches."),
    )
    borrowed = provider(responses)
    async with (
        timeout(20),
        borrowed,
        Agent(
            instructions="Search.",
            model="gpt-5.2",
            provider=borrowed,
            tools=[
                FileSearch(vector_store_ids=["vs_fixture"], include_search_results=include_results)
            ],
        ).session() as session,
    ):
        assert (await (await session.submit("Find a record.")).get_result()).final_output == (
            "No matches."
        )
        call = [item async for item in session.items() if item["type"] == "file_search_call"][0]
        assert call["results"] is None
    request = responses.requests[0]
    assert ("file_search_call.results" in request.get("include", [])) == include_results
    assert next(t for t in request["tools"] if t["type"] == "file_search") == {
        "type": "file_search",
        "vector_store_ids": ["vs_fixture"],
    }


async def test_mixed_hosted_and_local_calls_stream_and_keep_citations(responses):
    called = []

    @function_tool
    def receipt(label: str) -> str:
        called.append(label)
        return "local receipt"

    file_call = {
        "type": "file_search_call",
        "id": "fs_mix",
        "status": "completed",
        "queries": ["hours"],
    }
    code_call = {
        "type": "code_interpreter_call",
        "id": "ci_mix",
        "status": "completed",
        "container_id": "cntr_existing",
        "code": "print(42)",
        "outputs": None,
    }
    annotations = [
        {"type": "file_citation", "file_id": "file_hours", "filename": "hours.txt", "index": 0},
        {
            "type": "container_file_citation",
            "container_id": "cntr_existing",
            "file_id": "cfile_report",
            "filename": "report.csv",
            "start_index": 0,
            "end_index": 6,
        },
    ]
    final = text_response("msg_mix", "Report", " ready.")
    final[-1]["item"]["content"][0]["annotations"] = annotations
    responses.respond(
        completed(file_call),
        completed(code_call),
        function_call("receipt", {"label": "done"}, "local"),
    )
    responses.respond(*final)
    borrowed = provider(responses)
    async with (
        timeout(30),
        borrowed,
        Agent(
            instructions="Search, calculate, then record a receipt.",
            model="gpt-5.2",
            provider=borrowed,
            tools=[
                FileSearch(vector_store_ids=["vs_fixture"]),
                CodeInterpreter(container="cntr_existing"),
                receipt,
            ],
        ).session() as session,
    ):
        turn = await session.submit("Prepare a report.")
        events = [event async for event in turn.events(include_execution_details=True)]
        assert (await turn.get_result()).final_output == "Report ready."
        assert "".join(e.delta for e in events if isinstance(e, OutputTextDelta)) == "Report ready."
        messages = [
            item
            async for item in session.items()
            if item["type"] == "message" and item["role"] == "assistant"
        ]
        assert messages[-1]["content"][0]["annotations"] == annotations
        observed = [e.item for e in events if isinstance(e, ModelOutputItemCompleted)]
        assert {item["type"] for item in observed} >= {
            "file_search_call",
            "code_interpreter_call",
            "message",
        }
    assert called == ["done"]
    assert len(responses.requests) == 2
    assert tool_output(responses.requests[1], "local") == "local receipt"
    outputs = [i for i in responses.requests[1]["input"] if i.get("type") == "function_call_output"]
    assert [item["call_id"] for item in outputs] == ["local"]
    assert (
        next(t for t in responses.requests[0]["tools"] if t["type"] == "code_interpreter")[
            "container"
        ]
        == "cntr_existing"
    )


PROGRESS = [
    {"type": "response.file_search_call.searching", "item_id": "fs_progress"},
    {"type": "response.code_interpreter_call.interpreting", "item_id": "ci_progress"},
    {
        "type": "response.code_interpreter_call_code.delta",
        "item_id": "ci_progress",
        "delta": "print(42)",
    },
    {
        "type": "response.code_interpreter_call_code.done",
        "item_id": "ci_progress",
        "code": "print(42)",
    },
    {"type": "response.image_generation_call.generating", "item_id": "ig_edit"},
    {
        "type": "response.image_generation_call.partial_image",
        "item_id": "ig_edit",
        "partial_image_b64": "cGFydGlhbA==",
        "partial_image_index": 0,
        "background": "opaque",
        "output_format": "png",
        "quality": "low",
        "size": "1024x1024",
    },
]


async def test_native_progress_observations_preserve_provider_events_and_large_preview(responses):
    preview_bytes = b"synthetic-preview" * 8192
    preview_b64 = base64.b64encode(preview_bytes).decode("ascii")
    assert len(preview_b64) >= 100 * 1024
    indices = {"fs_progress": 0, "ci_progress": 1, "ig_edit": 2}
    raw_events = [
        {
            **payload,
            "output_index": indices[payload["item_id"]],
            "sequence_number": index,
            "future_field": {"nested": [index, None, {"keep": True}]},
            **(
                {"partial_image_b64": preview_b64}
                if payload["type"] == "response.image_generation_call.partial_image"
                else {}
            ),
        }
        for index, payload in enumerate(PROGRESS, start=1)
    ]
    final_items = [
        {
            "type": "file_search_call",
            "id": "fs_progress",
            "status": "completed",
            "queries": ["square"],
        },
        {
            "type": "code_interpreter_call",
            "id": "ci_progress",
            "status": "completed",
            "container_id": "cntr_progress",
            "code": "print(42)",
            "outputs": [{"type": "logs", "logs": "42"}],
        },
        {
            "type": "image_generation_call",
            "id": "ig_edit",
            "status": "completed",
            "result": "ZmluYWw=",
        },
    ]
    responses.respond(
        *raw_events,
        *[{**completed(item), "output_index": index} for index, item in enumerate(final_items)],
        assistant_output("All hosted tools finished."),
    )
    borrowed = provider(responses)
    async with (
        timeout(30),
        borrowed,
        Agent(
            instructions="Search, calculate, and generate an image.",
            model="gpt-5.2",
            provider=borrowed,
            tools=[
                FileSearch(vector_store_ids=["vs_fixture"]),
                CodeInterpreter(container="cntr_progress"),
                ImageGeneration(partial_images=1),
            ],
        ).session() as session,
    ):
        turn = await session.submit("Create a square and explain the calculation.")
        events = [event async for event in turn.events(include_execution_details=True)]
        assert (await turn.get_result()).final_output == "All hosted tools finished."
        progress = [event for event in events if isinstance(event, HostedToolCallProgress)]
        assert [event.event for event in progress] == raw_events
        assert [event.item_id for event in progress] == [raw["item_id"] for raw in raw_events]
        for event in progress:
            assert event.type == "hosted_tool_call_progress"
            assert event.session_id == session.id
            assert event.turn_id == turn.turn_id
            assert event.submission_id == turn.submission_id
        partial = progress[-1].event
        encoded = partial["partial_image_b64"]
        assert isinstance(encoded, str)
        assert encoded == preview_b64
        assert partial["partial_image_index"] == 0
        assert base64.b64decode(encoded, validate=True) == preview_bytes
        images = [item async for item in session.items() if item["type"] == "image_generation_call"]
        assert len(images) == 1
        assert images[0]["result"] == "ZmluYWw="
        assert images[0]["provider_data"] == final_items[-1]

        # Each public reader owns its payload, including nested provider data.
        future = progress[0].event["future_field"]
        assert isinstance(future, dict)
        nested = future["nested"]
        assert isinstance(nested, list)
        nested.append("reader mutation")
        progress[-1].event["partial_image_b64"] = "reader mutation"
        replay = [event async for event in turn.events(include_execution_details=True)]
        assert [event.event for event in replay if isinstance(event, HostedToolCallProgress)] == (
            raw_events
        )
    assert len(responses.requests) == 1


@pytest.mark.parametrize("overflow", ["metadata", "single-image", "aggregate-images"])
async def test_native_progress_buffer_gaps_preserve_readable_suffix_and_final_history(
    responses, overflow
):
    def partial(index, characters):
        return {
            "type": "response.image_generation_call.partial_image",
            "item_id": "ig_budget",
            "output_index": 0,
            "sequence_number": index + 1,
            "partial_image_index": index,
            "partial_image_b64": "A" * characters,
        }

    if overflow == "metadata":
        previews = [{**partial(0, 128 * 1024), "future_metadata": "m" * 65_537}]
        retained_previews = []
    elif overflow == "single-image":
        previews = [partial(0, 16 * 1024 * 1024 + 4)]
        retained_previews = []
    else:
        # Each preview fits 16 MiB. Together they exceed the 32 MiB image
        # budget, so FIFO eviction removes only the first preview.
        previews = [partial(index, 12 * 1024 * 1024) for index in range(3)]
        retained_previews = previews[1:]
    readable = {
        "type": "response.image_generation_call.generating",
        "item_id": "ig_budget",
        "output_index": 0,
        "sequence_number": len(previews) + 1,
        "future_metadata": {"readable": True},
    }
    final_item = {
        "type": "image_generation_call",
        "id": "ig_budget",
        "status": "completed",
        "result": "ZmluYWw=",
    }
    responses.respond(
        *previews, readable, completed(final_item), assistant_output("Final image retained.")
    )
    borrowed = provider(responses)
    async with (
        timeout(30),
        borrowed,
        Agent(
            instructions="Generate an image.",
            model="gpt-5.2",
            provider=borrowed,
            tools=[ImageGeneration(partial_images=3)],
        ).session() as session,
    ):
        turn = await session.submit("Draw a square.")
        # Read only after completion so the aggregate case tests a slow cursor.
        assert (await turn.get_result()).final_output == "Final image retained."
        events = [event async for event in turn.events(include_execution_details=True)]
        assert isinstance(events[0], EventGap)
        progress = [event for event in events if isinstance(event, HostedToolCallProgress)]
        assert [event.event for event in progress] == [*retained_previews, readable]
        assert all(
            earlier.sequence < later.sequence
            for earlier, later in zip(events, events[1:], strict=False)
        )
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].status == "completed"
        images = [item async for item in session.items() if item["type"] == "image_generation_call"]
        assert len(images) == 1
        assert images[0]["result"] == final_item["result"]
        assert images[0]["provider_data"] == final_item


async def test_image_edit_inline_input_mask_and_partial_do_not_replace_final(responses):
    raw = {
        "type": "image_generation_call",
        "id": "ig_edit",
        "status": "completed",
        "result": "ZmluYWw=",
        "revised_prompt": "Change the square to blue.",
        "action": "edit",
    }
    responses.respond(
        {**PROGRESS[-1], "output_index": 0, "sequence_number": 1},
        completed(raw),
        assistant_output("Edited."),
    )
    borrowed = provider(responses)
    async with (
        timeout(30),
        borrowed,
        Agent(
            instructions="Edit the supplied image.",
            model="gpt-5.2",
            provider=borrowed,
            tools=[
                ImageGeneration(
                    action="edit", input_image_mask={"image_url": PNG}, partial_images=1
                )
            ],
        ).session() as session,
    ):
        turn = await session.submit(
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Make this square blue."},
                    {"type": "input_image", "image_url": PNG},
                ],
            }
        )
        assert (await turn.get_result()).final_output == "Edited."
        images = [item async for item in session.items() if item["type"] == "image_generation_call"]
        assert len(images) == 1
        assert images[0]["result"] == "ZmluYWw="
        assert images[0]["provider_data"] == raw
    request = responses.requests[0]
    assert next(t for t in request["tools"] if t["type"] == "image_generation") == {
        "type": "image_generation",
        "action": "edit",
        "input_image_mask": {"image_url": PNG},
        "partial_images": 1,
    }
    images_sent = [
        part
        for item in request["input"]
        if item.get("role") == "user"
        for part in item["content"]
        if part["type"] == "input_image"
    ]
    assert len(images_sent) == 1
    assert images_sent[0]["image_url"].startswith("data:image/")


async def test_failed_provider_item_remains_inspectable(responses):
    raw = {
        "type": "image_generation_call",
        "id": "ig_failed",
        "status": "failed",
        "result": None,
        "revised_prompt": None,
        "error": {
            "code": "image_generation_failed",
            "message": "Synthetic failure.",
            "type": "server_error",
            "param": None,
        },
    }
    responses.respond(
        completed(raw), assistant_output("The provider could not generate the image.")
    )
    borrowed = provider(responses)
    async with (
        timeout(20),
        borrowed,
        Agent(
            instructions="Generate.", model="gpt-5.2", provider=borrowed, tools=[ImageGeneration()]
        ).session() as session,
    ):
        turn = await session.submit("Draw a square.")
        assert (
            await turn.get_result()
        ).final_output == "The provider could not generate the image."
        item = [item async for item in session.items() if item["type"] == "image_generation_call"][
            0
        ]
        assert item["status"] == "failed" and item["result"] is None
        assert item["provider_data"] == raw


async def test_response_failure_is_not_a_successful_hosted_result(responses):
    responses.respond(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "context_length_exceeded",
                    "type": "invalid_request_error",
                    "message": "This model's maximum context length was exceeded.",
                }
            },
        }
    )
    borrowed = provider(responses)
    async with (
        timeout(20),
        borrowed,
        Agent(
            instructions="Search.",
            model="gpt-5.2",
            provider=borrowed,
            tools=[FileSearch(vector_store_ids=["vs_fixture"])],
        ).session() as session,
    ):
        turn = await session.submit("Find a record.")
        with pytest.raises(NativeError):
            await turn.get_result()
        events = [event async for event in turn.events()]
        assert [e.status for e in events if isinstance(e, TurnFinished)] == ["failed"]
        assert not [item async for item in session.items() if item["type"] == "file_search_call"]


async def test_cancel_during_hosted_generation_has_no_final_image_and_session_can_continue(
    responses,
):
    # Gate the fixture after a real text delta. Cancellation must settle even
    # while the server has not sent the final image or response completion.
    responses.delta_gate = asyncio.Event()
    responses.respond(
        {**PROGRESS[-1], "output_index": 0, "sequence_number": 1},
        *text_response("msg_wait", "Working."),
        completed(
            {
                "type": "image_generation_call",
                "id": "ig_late",
                "status": "completed",
                "result": "bGF0ZQ==",
            }
        ),
    )
    borrowed = provider(responses)
    async with (
        timeout(30),
        borrowed,
        Agent(
            instructions="Generate.", model="gpt-5.2", provider=borrowed, tools=[ImageGeneration()]
        ).session() as session,
    ):
        try:
            turn = await session.submit("Draw a square.")
            async for event in turn.events():
                if isinstance(event, OutputTextDelta):
                    break
            else:
                pytest.fail("No streamed delta reached the caller")
            await turn.cancel()
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            events = [event async for event in turn.events()]
            assert [e.status for e in events if isinstance(e, TurnFinished)] == ["cancelled"]
            assert not [
                item async for item in session.items() if item["type"] == "image_generation_call"
            ]
        finally:
            responses.delta_gate.set()
        responses.respond(assistant_output("Recovered."))
        assert (await (await session.submit("Continue.")).get_result()).final_output == "Recovered."


@pytest.mark.parametrize(
    "tool",
    [
        FileSearch(vector_store_ids=["vs_fixture"]),
        CodeInterpreter(container="cntr_fixture"),
        ImageGeneration(),
    ],
)
def test_duplicate_hosted_declarations_rejected(tool):
    with pytest.raises(ValueError, match="only once"):
        Agent(instructions="Act.", tools=[tool, tool]).validate()


@pytest.mark.parametrize(
    "tool,name",
    [
        (FileSearch(vector_store_ids=["vs_fixture"]), "file_search"),
        (CodeInterpreter(container="cntr_fixture"), "code_interpreter"),
        (ImageGeneration(), "image_generation"),
    ],
)
def test_hosted_names_cannot_collide_with_local_functions(tool, name):
    @function_tool(name=name)
    def local() -> str:
        return "must not execute"

    with pytest.raises(ValueError, match="reserve"):
        Agent(instructions="Act.", tools=[tool, local]).validate()


async def test_open_session_rejects_replacement_hosted_config_before_request(responses):
    tool = FileSearch(vector_store_ids=["vs_fixture"])
    replacement = FileSearch(vector_store_ids=["vs_other"])
    borrowed = provider(responses)
    async with (
        timeout(20),
        borrowed,
        Agent(
            instructions="Search.", model="gpt-5.2", provider=borrowed, tools=[tool]
        ).session() as session,
    ):
        with pytest.raises(CapabilityError, match="captured"):
            await session.submit("Use a different store.", tools=[replacement])
        with pytest.raises(CapabilityError, match="captured"):
            await session.update(tools=[replacement])
        assert not responses.requests


async def test_codex_login_cannot_open_provider_hosted_tools(tmp_path):
    borrowed = NativeProvider(auth=CodexLogin(codex_home=tmp_path / "unused-login"))
    async with timeout(10), borrowed:
        agent = Agent(
            instructions="Generate.", model="gpt-5.2", provider=borrowed, tools=[ImageGeneration()]
        )
        with pytest.raises(ValueError, match="API-key Responses provider"):
            async with agent.session():
                pytest.fail("Codex login must not admit provider-hosted tools")


@pytest.mark.parametrize("value", [-1, 51, True, False, 1.5, "2"])
def test_file_search_rejects_invalid_result_limit(value):
    from _agents_codex.tools import _capture_hosted_tools

    tool = FileSearch(vector_store_ids=["vs_fixture"], max_num_results=value)
    with pytest.raises(ValueError, match="max_num_results"):
        _capture_hosted_tools([tool])


@pytest.mark.parametrize("value", [None, 0, 1, 50])
async def test_file_search_normalizes_result_limit_in_native_request(responses, value):
    responses.respond(assistant_output("Done."))
    borrowed = provider(responses)
    async with (
        timeout(20),
        borrowed,
        Agent(
            instructions="Search.",
            model="gpt-5.2",
            provider=borrowed,
            tools=[FileSearch(vector_store_ids=["vs_fixture"], max_num_results=value)],
        ).session() as session,
    ):
        assert (await (await session.submit("Find a record.")).get_result()).final_output == "Done."
    wire = next(t for t in responses.requests[0]["tools"] if t["type"] == "file_search")
    assert wire == {
        "type": "file_search",
        "vector_store_ids": ["vs_fixture"],
        **({"max_num_results": value} if value else {}),
    }


@pytest.mark.parametrize("include", [False, True])
async def test_native_hosted_provider_fields_and_output_inclusion(responses, include):
    extra = {"future_option": {"nested": [True, None]}}
    responses.respond(assistant_output("Done."))
    borrowed = provider(responses)
    async with (
        timeout(20),
        borrowed,
        Agent(
            instructions="Use hosted tools.",
            model="gpt-5.2",
            provider=borrowed,
            tools=[
                FileSearch(vector_store_ids=["vs_fixture"], provider_data=extra),
                CodeInterpreter(
                    container="cntr_fixture", include_outputs=include, provider_data=extra
                ),
                ImageGeneration(provider_data=extra),
            ],
        ).session() as session,
    ):
        extra["future_option"]["nested"].append("late mutation")
        assert (await (await session.submit("Run.")).get_result()).final_output == "Done."
    request = responses.requests[0]
    for kind in ["file_search", "code_interpreter", "image_generation"]:
        wire = next(t for t in request["tools"] if t["type"] == kind)
        assert wire["future_option"] == {"nested": [True, None]}
        assert "provider_data" not in wire
        assert "include_outputs" not in wire
    assert ("code_interpreter_call.outputs" in request["include"]) == include


def test_hosted_provider_fields_cannot_replace_admitted_identity():
    from _agents_codex.tools import _capture_hosted_tools

    with pytest.raises(ValueError, match="cannot replace"):
        _capture_hosted_tools([ImageGeneration(provider_data={"type": "computer"})])


@pytest.mark.parametrize("provider_data", [False, 0, "", []])
def test_hosted_provider_data_rejects_non_objects(provider_data: Any):
    from _agents_codex.tools import _capture_hosted_tools

    with pytest.raises(TypeError, match="provider_data must be an object"):
        _capture_hosted_tools(
            [FileSearch(vector_store_ids=["vs_fixture"], provider_data=provider_data)]
        )
