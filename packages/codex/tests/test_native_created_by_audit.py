from __future__ import annotations

import pytest

from agents import Agent
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_manual_tools import provider


@pytest.mark.parametrize(
    ("kind", "item_id", "fields"),
    [
        ("file_search_call", "fs_external", {"queries": ["a"], "results": []}),
        (
            "code_interpreter_call",
            "ci_external",
            {"container_id": "cntr_test", "code": "synthetic"},
        ),
        ("image_generation_call", "ig_external", {"result": None, "revised_prompt": None}),
        ("mcp_list_tools", "mcpl_external", {"server_label": "peer", "tools": []}),
        ("program", "pgm_external", {"call_id": "prior_program", "code": "fixture()"}),
        ("program_output", "pgmo_external", {"call_id": "prior_program", "result": "settled"}),
    ],
)
async def test_replay_drops_created_by_only_on_provider_wire(responses, kind, item_id, fields):
    original = {"type": kind, "id": item_id, "created_by": "sdk-private", **fields}
    if kind != "program":
        original["status"] = "completed"
    program = {
        "type": "program",
        "id": "pgm_external",
        "call_id": "prior_program",
        "code": "fixture()",
    }
    result = {
        "type": "program_output",
        "id": "pgmo_external",
        "call_id": "prior_program",
        "status": "completed",
        "result": "settled",
    }
    imported = (
        [original, result]
        if kind == "program"
        else [program, original]
        if kind == "program_output"
        else [original]
    )
    responses.respond(assistant_output("continued"))
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with agent.session() as source:
        initial = await source.history_snapshot()
        await source.transact_history(expected_revision=initial.revision, keep=0, items=imported)
        snapshot = await source.history_snapshot()
        assert snapshot.items == tuple(imported)
        state = await source.save_state()
    async with agent.session(state=state) as restored:
        assert await restored.history_snapshot() == snapshot
        assert (await (await restored.submit("continue")).get_result()).final_output == "continued"
    sent = next(item for item in responses.requests[0]["input"] if item.get("id") == item_id)
    assert "created_by" not in sent
    assert {field: sent[field] for field in fields} == fields
