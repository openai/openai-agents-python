import json
from dataclasses import replace

import pytest

from agents import Agent, RunConfig, Runner, UserError
from agents.items import ToolCallOutputItem
from agents.testing import ScriptedModel, assistant_message, function_call
from examples.plugins.permissions.src.agents_permissions import permissions
from examples.plugins.permissions.src.agents_permissions.example import Records, records, resolve

KEY = ("acme", "pat", "R42")


def desk(*ids: str) -> Agent[Records]:
    calls = [
        [function_call("read_record", {"record_id": id_}, call_id=f"read-{i}")]
        for i, id_ in enumerate(ids)
    ]
    return Agent(
        name="Desk",
        model=ScriptedModel([*calls, [assistant_message("done")]]),
        plugins=[permissions(), records],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant", "subject", "owner", "resource", "expected"),
    [
        ("acme", "pat", "records", "R42", ["R42"]),
        ("other", "pat", "records", "R42", []),
        ("acme", "other", "records", "R42", []),
        ("acme", "pat", "other", "R42", []),
        ("acme", "pat", "records", "latest", []),
    ],
    ids=["allow", "tenant", "subject", "plugin-owner", "alias-only"],
)
async def test_grant_must_match_identity_and_final_target(
    tenant: str, subject: str, owner: str, resource: str, expected: list[str]
) -> None:
    app = Records(tenant, subject, {("acme", "pat", resource): "allow"})
    agent = desk("latest")
    if owner != "records":
        agent = desk("R42").clone(plugins=[permissions(), replace(records, name=owner)])
    result = await Runner.run(
        agent,
        "I'm pat from acme. Read the record.",
        context=app,
        run_config=RunConfig(policy_context=resolve, tracing_disabled=True),
    )
    assert not result.interruptions
    assert app.reads == expected
    (output,) = [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    assert output == ("Record R42" if expected else "Tool call refused.")


@pytest.mark.asyncio
async def test_approval_is_per_call_and_cannot_override_revocation() -> None:
    app = Records("acme", "pat", {KEY: "review"})
    agent = desk("latest", "R42")
    config = RunConfig(policy_context=resolve, tracing_disabled=True)
    first = await Runner.run(agent, "Read twice", context=app, run_config=config)
    (item,) = first.interruptions
    assert item.arguments and json.loads(item.arguments) == {"record_id": "R42"}
    assert not app.reads
    state = first.to_state()
    state.approve(item, always_approve=True)

    second = await Runner.run(agent, state, run_config=config)
    assert app.reads == ["R42"]
    (next_item,) = second.interruptions
    assert next_item.call_id != item.call_id
    state = second.to_state()
    state.approve(next_item)
    del app.grants[KEY]

    finished = await Runner.run(agent, state, run_config=config)
    assert not finished.interruptions
    assert app.reads == ["R42"]
    assert any(
        isinstance(item, ToolCallOutputItem)
        and item.call_id == next_item.call_id
        and item.output == "Tool call refused."
        for item in finished.new_items
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields", [None, {"decision": "yes"}, {"decision": "allow", "error": "timeout"}]
)
async def test_missing_or_invalid_permission_refuses_call(fields: dict[str, str] | None) -> None:
    app = Records("acme", "pat", {KEY: "allow"})
    config = RunConfig(tracing_disabled=True)
    if fields is not None:
        config.policy_context = lambda *_: fields
    result = await Runner.run(desk("R42"), "Read", context=app, run_config=config)
    assert result.final_output == "done" and not app.reads


@pytest.mark.asyncio
async def test_unavailable_policy_service_blocks_dispatch() -> None:
    app = Records("acme", "pat", {KEY: "allow"})

    async def unavailable(*_: object) -> dict[str, str]:
        raise TimeoutError("Private policy service")

    with pytest.raises(UserError, match="required plugin callback failed") as exc:
        await Runner.run(
            desk("R42"),
            "Read",
            context=app,
            run_config=RunConfig(policy_context=unavailable, tracing_disabled=True),
        )
    assert "Private policy service" not in str(exc.value)
    assert not app.reads


@pytest.mark.asyncio
async def test_approved_read_cannot_move_to_another_signed_in_user() -> None:
    grants = {KEY: "review", ("acme", "lee", "R42"): "review"}
    pat = Records("acme", "pat", grants)
    lee = Records("acme", "lee", grants)
    config = RunConfig(policy_context=resolve, tracing_disabled=True)
    agent = desk("R42")
    paused = await Runner.run(agent, "Read", context=pat, run_config=config)
    state = paused.to_state()
    state.approve(paused.interruptions[0])
    with pytest.raises(UserError, match="original application context"):
        await Runner.run(agent, state, context=lee, run_config=config)
    assert not pat.reads and not lee.reads
