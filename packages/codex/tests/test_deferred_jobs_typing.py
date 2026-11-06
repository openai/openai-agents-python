# pyright: strict
"""Experimental registration retains the existing manual grant, output and Session context types."""

from typing import Any

from pydantic import BaseModel, ConfigDict
from typing_extensions import assert_type

from agents import ActionRequested, Completed, FunctionTool, PromptedOutput, Session, Turn
from agents.experimental.deferred import DeferredTools, JobJournal, JobProvider
from agents.state import SessionState


class ReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pages: int


class ReportOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rows: int


async def typed_job(
    session: Session[str, str],
    journal: JobJournal,
    provider: JobProvider[ReportInput],
    event: object,
) -> None:
    jobs = DeferredTools(journal=journal, tenant="tenant", run_id="run")
    tool = FunctionTool(
        name="report",
        description="Report.",
        input_type=ReportInput,
        output_type=ReportOutput,
    )
    jobs.register(
        tool,
        provider_id="report/v1",
        provider=provider,
        project=ReportOutput.model_validate,
    )
    assert_type(tool, FunctionTool[ReportInput, ReportOutput])
    request = tool.match(event)
    if request is not None:
        assert_type(request, ActionRequested[ReportInput, ReportOutput])
        assert_type(await jobs.prepare(session, request), SessionState)


async def typed_start(
    jobs: DeferredTools,
    session: Session[str, str],
    request: ActionRequested[ReportInput, ReportOutput],
) -> None:
    async with jobs.start(session, request) as (restored, outcome):
        assert_type(restored, Session[str, str])
        if isinstance(outcome, Turn):
            assert_type(outcome, Turn[Any])


async def typed_prompted_restore(
    jobs: DeferredTools,
    session: Session[str, ReportOutput],
    request: ActionRequested[ReportInput, ReportOutput],
) -> None:
    assert_type(await jobs.prepare(session, request), SessionState)
    advanced = await jobs.advance(session, request)
    if isinstance(advanced, Turn):
        assert_type(advanced, Turn[Any])
    async with jobs.start(session, request) as (restored, outcome):
        assert_type(restored, Session[str, ReportOutput])
        if isinstance(outcome, Turn):
            assert_type(outcome, Turn[Any])
        assert_type(await restored.submit("Next"), Turn[ReportOutput])


async def prompted_root_with_text_default(
    jobs: DeferredTools,
    session: Session[str, str],
    request: ActionRequested[ReportInput, ReportOutput],
) -> None:
    chosen = await session.submit("Report", output_type=PromptedOutput(ReportOutput))
    assert_type(chosen, Turn[ReportOutput])
    assert_type(await chosen.get_result(), Completed[ReportOutput])
    async with jobs.start(session, request) as (restored, outcome):
        assert_type(restored, Session[str, str])
        assert_type(restored.restored_turn, Turn[Any] | None)
        if isinstance(outcome, Turn):
            assert_type(outcome, Turn[Any])
            assert_type(await outcome.get_result(), Completed[Any])
        advanced = await jobs.advance(restored, request)
        if isinstance(advanced, Turn):
            assert_type(advanced, Turn[Any])
        assert_type(await restored.submit("Next text default"), Turn[str])
