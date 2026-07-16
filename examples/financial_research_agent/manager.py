from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from datetime import datetime, timezone

from pydantic import BaseModel
from rich.console import Console

from agents import Runner, RunResult, RunResultStreaming, custom_span, gen_trace_id, trace
from examples.web_search_utils import extract_url_citations, extract_web_search_source_urls

from .agents.financials_agent import financials_agent
from .agents.planner_agent import FinancialSearchItem, FinancialSearchPlan, planner_agent
from .agents.risk_agent import risk_agent
from .agents.search_agent import FinancialSearchSummary, search_agent
from .agents.verifier_agent import VerificationResult, verifier_agent
from .agents.writer_agent import REVISION_PROMPT, FinancialReportData, writer_agent
from .printer import Printer


class FinancialSource(BaseModel):
    title: str
    url: str


class FinancialSearchEvidence(BaseModel):
    query: str
    reason: str
    summary: str
    sources: list[FinancialSource]
    retrieved_at: str


def _extract_financial_sources(items: Sequence[object]) -> list[FinancialSource]:
    sources: list[FinancialSource] = []
    seen: set[str] = set()

    for citation in extract_url_citations(items):
        if citation.url in seen:
            continue
        seen.add(citation.url)
        sources.append(FinancialSource(title=citation.title, url=citation.url))

    for url in extract_web_search_source_urls(items):
        if url in seen:
            continue
        seen.add(url)
        sources.append(FinancialSource(title=url, url=url))

    return sources


async def _summary_extractor(run_result: RunResult | RunResultStreaming) -> str:
    """Custom output extractor for sub‑agents that return an AnalysisSummary."""
    # The financial/risk analyst agents emit an AnalysisSummary with a `summary` field.
    # We want the tool call to return just that summary text so the writer can drop it inline.
    return str(run_result.final_output.summary)


class FinancialResearchManager:
    """
    Orchestrates the full flow: planning, searching, sub‑analysis, writing, and verification.
    """

    def __init__(self) -> None:
        self.console = Console()
        self.printer = Printer(self.console)
        self.research_cutoff = datetime.now(timezone.utc).date().isoformat()

    async def run(self, query: str) -> None:
        trace_id = gen_trace_id()
        try:
            with trace("Financial research trace", trace_id=trace_id):
                self.printer.update_item(
                    "trace_id",
                    f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}",
                    is_done=True,
                    hide_checkmark=True,
                )
                self.printer.update_item("start", "Starting financial research...", is_done=True)
                search_plan = await self._plan_searches(query)
                search_results = await self._perform_searches(search_plan)
                report, verification = await self._produce_verified_report(query, search_results)

                final_report = f"Report summary\n\n{report.short_summary}"
                self.printer.update_item("final_report", final_report, is_done=True)
        finally:
            self.printer.end()

        # Print to stdout
        print("\n\n=====REPORT=====\n\n")
        print(f"Report:\n{report.markdown_report}")
        print("\n\n=====FOLLOW UP QUESTIONS=====\n\n")
        print("\n".join(report.follow_up_questions))
        print("\n\n=====VERIFICATION=====\n\n")
        print(verification)

    async def _produce_verified_report(
        self,
        query: str,
        search_results: Sequence[FinancialSearchEvidence],
    ) -> tuple[FinancialReportData, VerificationResult]:
        report = await self._write_report(query, search_results)
        verification = await self._verify_report(query, report, search_results)
        if verification.verified:
            return report, verification

        report = await self._revise_report(query, report, search_results, verification)
        verification = await self._verify_report(query, report, search_results)
        if not verification.verified:
            raise RuntimeError(
                "Financial report failed evidence verification after one revision: "
                f"{verification.model_dump_json()}"
            )
        return report, verification

    async def _plan_searches(self, query: str) -> FinancialSearchPlan:
        self.printer.update_item("planning", "Planning searches...")
        result = await Runner.run(planner_agent, f"Query: {query}")
        self.printer.update_item(
            "planning",
            f"Will perform {len(result.final_output.searches)} searches",
            is_done=True,
        )
        return result.final_output_as(FinancialSearchPlan)

    async def _perform_searches(
        self, search_plan: FinancialSearchPlan
    ) -> Sequence[FinancialSearchEvidence]:
        with custom_span("Search the web"):
            self.printer.update_item("searching", "Searching...")
            tasks = [asyncio.create_task(self._search(item)) for item in search_plan.searches]
            results: list[FinancialSearchEvidence] = []
            num_completed = 0
            num_succeeded = 0
            num_failed = 0
            for task in asyncio.as_completed(tasks):
                result = await task
                if result is not None:
                    results.append(result)
                    num_succeeded += 1
                else:
                    num_failed += 1
                num_completed += 1
                status = f"Searching... {num_completed}/{len(tasks)} finished"
                if num_failed:
                    status += f" ({num_succeeded} succeeded, {num_failed} failed)"
                self.printer.update_item(
                    "searching",
                    status,
                )
            summary = f"Searches finished: {num_succeeded}/{len(tasks)} succeeded"
            if num_failed:
                summary += f", {num_failed} failed"
            self.printer.update_item("searching", summary, is_done=True)
            return results

    async def _search(self, item: FinancialSearchItem) -> FinancialSearchEvidence | None:
        input_data = f"Search term: {item.query}\nReason: {item.reason}"
        try:
            result = await Runner.run(search_agent, input_data)
            search_summary = result.final_output_as(FinancialSearchSummary)
            sources = _extract_financial_sources(result.new_items)
            if not sources:
                return None
            return FinancialSearchEvidence(
                query=item.query,
                reason=item.reason,
                summary=search_summary.summary,
                sources=sources,
                retrieved_at=self.research_cutoff,
            )
        except Exception:
            return None

    async def _write_report(
        self,
        query: str,
        search_results: Sequence[FinancialSearchEvidence],
    ) -> FinancialReportData:
        # Expose the specialist analysts as tools so the writer can invoke them inline
        # and still produce the final FinancialReportData output.
        fundamentals_tool = financials_agent.as_tool(
            tool_name="fundamentals_analysis",
            tool_description="Use to get a short write‑up of key financial metrics",
            custom_output_extractor=_summary_extractor,
        )
        risk_tool = risk_agent.as_tool(
            tool_name="risk_analysis",
            tool_description="Use to get a short write‑up of potential red flags",
            custom_output_extractor=_summary_extractor,
        )
        writer_with_tools = writer_agent.clone(tools=[fundamentals_tool, risk_tool])
        self.printer.update_item("writing", "Thinking about report...")
        input_data = self._report_input(query, search_results)
        result = Runner.run_streamed(writer_with_tools, input_data)
        update_messages = [
            "Planning report structure...",
            "Writing sections...",
            "Finalizing report...",
        ]
        last_update = time.time()
        next_message = 0
        async for _ in result.stream_events():
            if time.time() - last_update > 5 and next_message < len(update_messages):
                self.printer.update_item("writing", update_messages[next_message])
                next_message += 1
                last_update = time.time()
        self.printer.mark_item_done("writing")
        return result.final_output_as(FinancialReportData)

    async def _revise_report(
        self,
        query: str,
        report: FinancialReportData,
        search_results: Sequence[FinancialSearchEvidence],
        verification: VerificationResult,
    ) -> FinancialReportData:
        self.printer.update_item("revising", "Revising report from verification feedback...")
        revision_agent = writer_agent.clone(instructions=REVISION_PROMPT)
        input_data = (
            f"{self._report_input(query, search_results)}\n"
            f"Existing report:\n{report.model_dump_json()}\n"
            f"Verification feedback:\n{verification.model_dump_json()}"
        )
        result = await Runner.run(revision_agent, input_data)
        self.printer.mark_item_done("revising")
        return result.final_output_as(FinancialReportData)

    async def _verify_report(
        self,
        query: str,
        report: FinancialReportData,
        search_results: Sequence[FinancialSearchEvidence],
    ) -> VerificationResult:
        self.printer.update_item("verifying", "Verifying report...")
        input_data = json.dumps(
            {
                "original_query": query,
                "research_cutoff": self.research_cutoff,
                "report": report.model_dump(mode="json"),
                "evidence": [item.model_dump(mode="json") for item in search_results],
            },
            ensure_ascii=False,
        )
        result = await Runner.run(verifier_agent, input_data)
        self.printer.mark_item_done("verifying")
        return result.final_output_as(VerificationResult)

    def _report_input(
        self,
        query: str,
        search_results: Sequence[FinancialSearchEvidence],
    ) -> str:
        return json.dumps(
            {
                "original_query": query,
                "research_cutoff": self.research_cutoff,
                "evidence": [item.model_dump(mode="json") for item in search_results],
            },
            ensure_ascii=False,
        )
