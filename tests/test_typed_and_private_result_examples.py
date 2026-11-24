from contextlib import closing

import pytest

from agents import Agent, RunConfig, Runner, SQLiteSession
from agents.testing import ScriptedModel, assistant_message, function_call
from examples.basic.typed_answer_repair import RepairLimitError, Verdict, repair
from examples.mcp.bounded_tool_output import main as reports_recipe


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_typed_semantic_repair_and_bounded_failure(streamed):
    invalid = assistant_message('{"decision":"approve","evidence_count":0}')
    valid = assistant_message('{"decision":"reject","evidence_count":2}')
    model = ScriptedModel([[invalid], [valid]])
    result, review = await repair(model, streamed=streamed)
    assert result == Verdict(decision="reject", evidence_count=2)
    assert review.attempts == len(model.calls) == 2
    assert model.calls[1].system_instructions is not None
    assert "positive evidence_count" in model.calls[1].system_instructions
    failed = ScriptedModel([[invalid], [invalid]])
    with pytest.raises(RepairLimitError, match="two candidates"):
        await repair(failed, streamed=streamed)
    assert len(failed.calls) == 2


@pytest.mark.asyncio
async def test_large_private_source_and_sensitive_error_never_enter_history(monkeypatch):
    secret = "credential-secret " * 100

    async def source():
        return secret

    monkeypatch.setattr(reports_recipe, "site_report", source)
    with (
        closing(reports_recipe.Reports("owner")) as reports,
        closing(SQLiteSession("private")) as session,
    ):
        model = ScriptedModel(
            [
                [function_call("inspect_site", {}, call_id="one")],
                [assistant_message("Public summary")],
            ]
        )
        result = await Runner.run(
            Agent(name="demo", tools=[reports.site_tool()], model=model),
            "Synthetic prompt",
            context="owner",
            session=session,
            run_config=RunConfig(tracing_disabled=True),
        )
        assert "credential-secret" not in str(await session.get_items())
        assert "credential-secret" not in str(model.calls[1].input)
        assert "credential-secret" not in str(result.final_output)
        reference, _, _ = reports.saved("owner")[0]
        assert reports.fetch("owner", reference).startswith("credential-secret")
        with pytest.raises(PermissionError):
            reports.fetch("other", reference)
    with pytest.raises(PermissionError):
        reports.fetch("owner", reference)

    async def failure():
        raise RuntimeError("credential-secret")

    monkeypatch.setattr(reports_recipe, "site_report", failure)
    with closing(reports_recipe.Reports("owner")) as reports:
        model = ScriptedModel(
            [
                [function_call("inspect_site", {}, call_id="one")],
                [assistant_message("Public error summary")],
            ]
        )
        result = await Runner.run(
            Agent(name="demo", tools=[reports.site_tool()], model=model),
            "Synthetic prompt",
            context="owner",
            run_config=RunConfig(tracing_disabled=True),
        )
        assert "credential-secret" not in str(result.new_items)
        assert "Inspection report unavailable" in str(model.calls[1].input)
        assert reports.saved("owner") == ()


@pytest.mark.asyncio
async def test_schema_invalid_repair_never_replays_sensitive_candidate():
    from examples.basic.typed_answer_repair import repair_schema

    model = ScriptedModel(
        [
            [assistant_message('{"decision":"credential-secret","evidence_count":"bad"}')],
            [assistant_message('{"decision":"reject","evidence_count":1}')],
        ]
    )
    answer, attempts = await repair_schema(model)
    assert answer == Verdict(decision="reject", evidence_count=1) and attempts == 2
    assert "credential-secret" not in str(model.calls[1].input)
    failed = ScriptedModel([[assistant_message("credential-secret")]] * 2)
    with pytest.raises(RepairLimitError, match="two attempts") as error:
        await repair_schema(failed)
    assert "credential-secret" not in str(error.value) and len(failed.calls) == 2
