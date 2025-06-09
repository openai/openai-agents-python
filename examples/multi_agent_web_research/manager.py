from __future__ import annotations

import asyncio
import time

from rich.console import Console

from agents import Runner, custom_span, gen_trace_id, trace

from .agents.planner_agent import LegalSearchItem, LegalSearchPlan, planner_agent
from .agents.search_agent import search_agent
from .agents.writer_agent import LegalReportData, writer_agent
from .printer import Printer


class ResearchManager:
    def __init__(self):
        self.console = Console()
        self.printer = Printer(self.console)

    async def run(self, query: str) -> None:
        trace_id = gen_trace_id()
        with trace("Research trace", trace_id=trace_id):
            self.printer.update_item(
                "trace_id",
                f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}",
                is_done=True,
                hide_checkmark=True,
            )

            self.printer.update_item(
                "starting",
                "Starting legal research...",
                is_done=True,
                hide_checkmark=True,
            )
            search_plan = await self._plan_searches(query)
            search_results = await self._perform_searches(search_plan)
            report = await self._write_report(query, search_results)

            final_report = f"Report summary\n\n{report.short_summary}"
            self.printer.update_item("final_report", final_report, is_done=True)

            self.printer.end()

        print("\n\n=====REPORT=====\n\n")
        print(f"Report: {report.markdown_report}")
        print("\n\n=====FOLLOW UP QUESTIONS=====\n\n")
        follow_up_questions = "\n".join(report.follow_up_questions)
        print(f"Follow up questions: {follow_up_questions}")

    async def _plan_searches(self, query: str) -> LegalSearchPlan:
        self.printer.update_item("planning", "Planning legal searches...")
        result = await Runner.run(
            planner_agent,  # Now LegalPlannerAgent
            f"Legal Query: {query}",
        )
        self.printer.update_item(
            "planning",
            f"Will perform {len(result.final_output.searches)} legal searches",
            is_done=True,
        )
        return result.final_output_as(LegalSearchPlan)

    async def _perform_searches(self, search_plan: LegalSearchPlan) -> list[str]:
        with custom_span("Search the web for legal information"):
            self.printer.update_item("searching", "Searching for legal information...")
            num_completed = 0
            tasks = [asyncio.create_task(self._search(item)) for item in search_plan.searches]
            results = []
            for task in asyncio.as_completed(tasks):
                result = await task
                if result is not None:
                    results.append(result)
                num_completed += 1
                self.printer.update_item(
                    "searching", f"Searching... {num_completed}/{len(tasks)} completed"
                )
            self.printer.mark_item_done("searching")
            return results

    async def _search(self, item: LegalSearchItem) -> str | None:
        input = f"Legal Search term: {item.query}\\nReason for searching: {item.reason}"
        try:
            result = await Runner.run(
                search_agent,  # Now LegalSearchAgent
                input,
            )
            return str(result.final_output)
        except Exception:
            return None

    async def _write_report(self, query: str, search_results: list[str]) -> LegalReportData:
        self.printer.update_item("writing", "Thinking about legal report...")
        input = f"Original legal query: {query}\\nSummarized legal search results: {search_results}"
        result = Runner.run_streamed(
            writer_agent,  # Now LegalWriterAgent
            input,
        )
        update_messages = [
            "Thinking about legal report...",
            "Planning legal document structure...",
            "Writing outline for legal brief/memo...",
            "Drafting sections (Facts, Issues, Analysis, Conclusion)...",
            "Reviewing and formatting legal citations...",
            "Finalizing legal document...",
            "Finishing legal report...",
        ]

        last_update = time.time()
        next_message = 0
        async for _ in result.stream_events():
            if time.time() - last_update > 5 and next_message < len(update_messages):
                self.printer.update_item("writing", update_messages[next_message])
                next_message += 1
                last_update = time.time()

        self.printer.mark_item_done("writing")
        return result.final_output_as(LegalReportData)
