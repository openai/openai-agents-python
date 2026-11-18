from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Sequence
from datetime import datetime, timezone

from pydantic import BaseModel
from rich.console import Console

from agents import (
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
    custom_span,
    gen_trace_id,
    trace,
)
from agents.models.openai_provider import OpenAIProvider
from examples.web_search_utils import (
    extract_url_citations,
    extract_web_search_source_urls,
    get_field,
)

from .agents.financials_agent import financials_agent
from .agents.planner_agent import (
    MAX_SEARCHES,
    FinancialSearchItem,
    FinancialSearchPlan,
    planner_agent,
)
from .agents.primary_source_auditor import (
    AuditedVerificationResult,
    PrimarySourceAudit,
    auditor_agent,
)
from .agents.risk_agent import risk_agent
from .agents.search_agent import FinancialSearchSummary, search_agent
from .agents.verifier_agent import VerificationIssue, VerificationResult, verifier_agent
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

    def __init__(self, run_config: RunConfig | None = None) -> None:
        self.run_config = run_config or RunConfig()
        self.console = Console()
        self.printer = Printer(self.console)
        self.research_cutoff = datetime.now(timezone.utc).date().isoformat()

    async def run(self, query: str) -> None:
        trace_id = gen_trace_id()
        try:
            with trace("Financial research trace", trace_id=trace_id):
                self.printer.update_item(
                    "trace_id",
                    f"View trace: https://platform.openai.com/logs/trace?trace_id={trace_id}",
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
        result = await Runner.run(planner_agent, f"Query: {query}", run_config=self.run_config)
        self.printer.update_item(
            "planning",
            f"Will perform {min(len(result.final_output.searches), MAX_SEARCHES)} searches",
            is_done=True,
        )
        return result.final_output_as(FinancialSearchPlan)

    async def _perform_searches(
        self, search_plan: FinancialSearchPlan
    ) -> Sequence[FinancialSearchEvidence]:
        with custom_span("Search the web"):
            self.printer.update_item("searching", "Searching...")
            # Enforce the host's search budget before scheduling model-generated work.
            tasks = [
                asyncio.create_task(self._search(item))
                for item in search_plan.searches[:MAX_SEARCHES]
            ]
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
            if not results:
                raise RuntimeError(
                    "Financial research requires at least one sourced search result."
                )
            return results

    async def _search(self, item: FinancialSearchItem) -> FinancialSearchEvidence | None:
        input_data = f"Search term: {item.query}\nReason: {item.reason}"
        try:
            # The stock Codex App Server does not expose the retrieved-source metadata.
            # Keep this search on Responses in both modes rather than trusting generated URLs.
            result = await Runner.run(
                search_agent,
                input_data,
                run_config=RunConfig(
                    model_provider=OpenAIProvider(use_responses=True, use_responses_websocket=False)
                ),
            )
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
            run_config=self.run_config,
        )
        risk_tool = risk_agent.as_tool(
            tool_name="risk_analysis",
            tool_description="Use to get a short write‑up of potential red flags",
            custom_output_extractor=_summary_extractor,
            run_config=self.run_config,
        )
        writer_with_tools = writer_agent.clone(tools=[fundamentals_tool, risk_tool])
        self.printer.update_item("writing", "Thinking about report...")
        input_data = self._report_input(query, search_results)
        result = Runner.run_streamed(writer_with_tools, input_data, run_config=self.run_config)
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
        result = await Runner.run(revision_agent, input_data, run_config=self.run_config)
        self.printer.mark_item_done("revising")
        return result.final_output_as(FinancialReportData)

    async def _audit_primary_sources(
        self, query: str, report: FinancialReportData
    ) -> tuple[PrimarySourceAudit, set[str]]:
        audit_result = await Runner.run(
            auditor_agent,
            json.dumps(
                {
                    "original_query": query,
                    "research_cutoff": self.research_cutoff,
                    "report": report.model_dump(mode="json"),
                }
            ),
            run_config=RunConfig(
                model_provider=OpenAIProvider(use_responses=True, use_responses_websocket=False)
            ),
            max_turns=3,
        )
        audit = audit_result.final_output_as(PrimarySourceAudit)
        opened_urls: set[str] = set()
        for item in audit_result.new_items:
            raw = get_field(item, "raw_item")
            if get_field(raw, "type") != "web_search_call":
                continue
            action = get_field(raw, "action")
            url = get_field(action, "url")
            if (
                get_field(raw, "status") == "completed"
                and get_field(action, "type") == "open_page"
                and isinstance(url, str)
            ):
                opened_urls.add(url)
        return audit, opened_urls

    async def _verify_report(
        self,
        query: str,
        report: FinancialReportData,
        search_results: Sequence[FinancialSearchEvidence],
    ) -> VerificationResult:
        self.printer.update_item("verifying", "Reading primary documents and verifying report...")
        audit, opened_urls = await self._audit_primary_sources(query, report)
        audit_issues: list[VerificationIssue] = []
        if not audit.checks:
            audit_issues.append(
                VerificationIssue(
                    claim="Primary-source audit",
                    category="unsupported",
                    explanation="No material claims were checked against opened primary documents.",
                    source_urls=[],
                )
            )
        for check in audit.checks:
            if (
                not check.supported
                or check.source_url not in opened_urls
                or not check.supporting_extract.strip()
            ):
                audit_issues.append(
                    VerificationIssue(
                        claim=check.claim,
                        category="unsupported",
                        explanation=(
                            f"Primary-source audit: {check.explanation} "
                            f"Document extract: {check.supporting_extract}. "
                            f"Source opened: {check.source_url in opened_urls}."
                        ),
                        source_urls=[check.source_url] if check.source_url in opened_urls else [],
                    )
                )
        verification_input = json.loads(self._verification_input(query, report, search_results))
        verification_input["primary_source_audit"] = audit.model_dump(mode="json")
        verification_input["opened_source_urls"] = sorted(opened_urls)
        verification_input["allowed_source_urls"] = sorted(
            set(verification_input["allowed_source_urls"]) | opened_urls
        )

        result = await Runner.run(
            verifier_agent,
            json.dumps(verification_input),
            run_config=self.run_config,
        )
        self.printer.mark_item_done("verifying")
        verification = result.final_output_as(VerificationResult)
        # Require a complete supplied URL token; a lookalike hostname is not a citation.
        has_source_url = any(
            re.search(
                r"(?<![\w/:])" + re.escape(source_url) + r"(?=$|[\s<>\"'`()\[\],;])",
                report.markdown_report,
            )
            for source_url in verification_input["allowed_source_urls"]
        )
        issues = [*verification.issues, *audit_issues]
        if not has_source_url:
            issues.append(
                VerificationIssue(
                    claim="Report source citations",
                    category="unsupported",
                    explanation="The report does not include a complete supplied source URL.",
                    source_urls=[],
                )
            )
        return AuditedVerificationResult(
            verified=verification.verified and not issues,
            issues=issues,
            primary_source_checks=audit.checks,
            opened_source_urls=sorted(opened_urls),
        )

    def _verification_input(
        self,
        query: str,
        report: FinancialReportData,
        search_results: Sequence[FinancialSearchEvidence],
    ) -> str:
        return json.dumps(
            {
                "original_query": query,
                "research_cutoff": self.research_cutoff,
                "report": report.model_dump(mode="json"),
                "evidence": [item.model_dump(mode="json") for item in search_results],
                "allowed_source_urls": sorted(
                    {source.url for item in search_results for source in item.sources}
                ),
            },
            ensure_ascii=False,
        )

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
