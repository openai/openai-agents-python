from unittest.mock import AsyncMock, Mock

import pytest

from agents import Runner
from examples.financial_research_agent.agents.planner_agent import (
    FinancialSearchItem,
    FinancialSearchPlan,
    planner_agent,
)
from examples.financial_research_agent.agents.search_agent import (
    FinancialSearchSummary,
    search_agent,
)
from examples.financial_research_agent.agents.verifier_agent import VerificationResult
from examples.financial_research_agent.agents.writer_agent import FinancialReportData
from examples.financial_research_agent.manager import FinancialResearchManager


@pytest.mark.asyncio
@pytest.mark.parametrize(("planned_count", "expected_count"), [(5, 5), (15, 15), (18, 15)])
async def test_financial_research_bounds_planned_searches(
    monkeypatch: pytest.MonkeyPatch, planned_count: int, expected_count: int
) -> None:
    plan = FinancialSearchPlan(
        searches=[
            FinancialSearchItem(query=f"query {index}", reason=f"reason {index}")
            for index in range(planned_count)
        ]
    )
    planner_result = Mock(final_output=plan, final_output_as=Mock(return_value=plan))
    search_result = Mock(
        final_output_as=Mock(return_value=FinancialSearchSummary(summary="Supported summary")),
        new_items=[
            {
                "raw_item": {
                    "type": "web_search_call",
                    "action": {"sources": [{"type": "url", "url": "https://example.com/report"}]},
                }
            }
        ],
    )
    run_agent = AsyncMock(side_effect=[planner_result] + [search_result] * planned_count)
    monkeypatch.setattr(Runner, "run", run_agent)
    manager = object.__new__(FinancialResearchManager)
    manager.printer = Mock()
    manager.research_cutoff = "2026-09-21"
    produce_report = AsyncMock(
        return_value=(
            FinancialReportData(
                short_summary="Summary", markdown_report="Report", follow_up_questions=[]
            ),
            VerificationResult(verified=True, issues=[]),
        )
    )
    monkeypatch.setattr(manager, "_produce_verified_report", produce_report)

    await manager.run("Analyze a company")

    assert run_agent.await_count == 1 + expected_count
    assert run_agent.await_args_list[0].args == (planner_agent, "Query: Analyze a company")
    searches = run_agent.await_args_list[1:]
    assert all(call.args[0] is search_agent for call in searches)
    assert {call.args[1] for call in searches} == {
        f"Search term: query {index}\nReason: reason {index}" for index in range(expected_count)
    }
    produce_report.assert_awaited_once()
    assert produce_report.await_args is not None
    evidence = produce_report.await_args.args[1]
    assert len(evidence) == expected_count
    assert {item.query for item in evidence} == {
        f"query {index}" for index in range(expected_count)
    }
    manager.printer.update_item.assert_any_call(
        "planning", f"Will perform {expected_count} searches", is_done=True
    )
    manager.printer.update_item.assert_any_call(
        "searching",
        f"Searches finished: {expected_count}/{expected_count} succeeded",
        is_done=True,
    )
    manager.printer.end.assert_called_once()
