"""Keep large reports out of model input and SDK history.

Run offline from the checkout with `uv run python examples/mcp/bounded_tool_output/main.py`.
"""

import asyncio
import json
import secrets
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

from agents import Agent, FunctionTool, RunConfig, RunContextWrapper, Runner, UserError
from agents.decorators import tool
from agents.mcp import MCPServerStdio, MCPToolMetaContext
from agents.testing import ScriptedModel, assistant_message, function_call

MAX_TEXT_BYTES = 128
SERVER = "Inspection reports"


async def site_report() -> str:
    """Read the local inspection report."""
    return "WARN: synthetic site detail\n" * 20


async def summarize(texts: tuple[str, ...]) -> str:
    """Share only the number of warning markers in these synthetic reports."""
    count = await asyncio.to_thread(lambda: sum(text.count("WARN:") for text in texts))
    return f"Inspection report with {count} warning markers."


class Reports:
    """Private storage for one owner and one run. Never put this object in SDK context."""

    def __init__(self, owner: str):
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("Report owner must be a nonblank string")
        self._owner = owner
        self._reports: dict[str, tuple[str, tuple[str, ...]]] | None = {}

    def close(self) -> None:
        self._reports = None

    def fetch(self, requester: str, reference: str, *, part: int = 0, offset: int = 0) -> str:
        self.authorize(requester)
        if self._reports is None or reference not in self._reports:
            raise PermissionError("Report unavailable")
        if part < 0 or offset < 0:
            raise ValueError("Use a nonnegative part and offset")
        _, texts = self._reports[reference]
        return texts[part][offset : offset + 64]

    def saved(self, requester: str) -> tuple[tuple[str, str, int], ...]:
        """List the owner's references, display summaries and text part counts."""
        if requester != self._owner or not self._reports:
            return ()
        return tuple(
            (reference, summary, len(texts))
            for reference, (summary, texts) in self._reports.items()
        )

    def show(self, requester: str) -> None:
        """Display only summaries issued by this store to its owner."""
        saved = self.saved(requester)
        if not saved:
            print("No saved reports available.")
            return
        for reference, summary, _ in saved:
            page = self.fetch(requester, reference)
            print(summary)
            print("Retrieved the first page privately." if page else "The first page is empty.")

    def ensure_open(self) -> None:
        if self._reports is None:
            raise PermissionError("Report unavailable")

    def authorize(self, requester: str) -> None:
        self.ensure_open()
        if requester != self._owner:
            raise PermissionError("Report unavailable")

    def site_tool(self) -> FunctionTool:
        @tool(failure_error_function=None)
        async def inspect_site(ctx: RunContextWrapper[str]) -> str:
            """Read the local inspection report."""
            self.authorize(ctx.context)
            try:
                text = await site_report()
            except Exception:
                # Source errors may contain private data. Return only a fixed status.
                return "Inspection report unavailable."
            if not isinstance(text, str):
                raise UserError("Expected a report string")
            replacement = await self.bound((text,))
            self.authorize(ctx.context)
            return replacement or text

        return inspect_site

    async def bound(self, texts: tuple[str, ...]) -> str | None:
        if sum(len(text.encode()) for text in texts) <= MAX_TEXT_BYTES:
            return None

        summary = await summarize(texts)
        if not isinstance(summary, str) or not summary.strip():
            raise UserError("Expected a nonempty report summary")
        reference = secrets.token_urlsafe(24)
        replacement = f"{summary} Reference: {reference}"
        if len(replacement.encode()) > MAX_TEXT_BYTES:
            raise UserError("Report summary exceeds the text budget")
        if self._reports is None:
            raise RuntimeError("Report storage closed")
        self._reports[reference] = replacement, texts
        return replacement


class ReportServer(MCPServerStdio):
    """Shape the peer result before the SDK converts, logs or traces it."""

    def __init__(self, reports: Reports, name: str = SERVER):
        self.reports = reports
        super().__init__(
            name=name,
            params={
                "command": sys.executable,
                "args": [str(Path(__file__).with_name("server.py"))],
            },
            tool_meta_resolver=self.authorize_call,
            failure_error_function=None,
        )

    def authorize_call(self, call: MCPToolMetaContext) -> None:
        if call.tool_name == "read.report":
            self.reports.authorize(call.run_context.context)

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> CallToolResult:
        result = await super().call_tool(tool_name, arguments, meta=meta)
        if tool_name != "read.report":
            return result
        # Wire aliases are the same in both supported MCP client versions.
        fields = result.model_dump(by_alias=True, exclude={"content", "meta"})
        if fields.get("isError"):
            return result
        self.reports.ensure_open()
        if any(not isinstance(block, (TextContent, ImageContent)) for block in result.content):
            raise UserError("Unsupported report media")

        structured = fields.get("structuredContent")
        if structured:
            if structured == {"ticket": "site"}:
                structured = {"report": await site_report()}
            text = json.dumps(structured)
            replacement: str | None = await self.reports.bound((text,)) or text
        else:
            texts = tuple(part.text for part in result.content if isinstance(part, TextContent))
            replacement = await self.reports.bound(texts)

        self.reports.ensure_open()
        if replacement is None:
            return result

        # Start a new result so neither structuredContent nor _meta carries the original.
        parts = list(result.content)
        slots = [i for i, part in enumerate(parts) if isinstance(part, TextContent)]
        if not slots:
            parts.append(TextContent(type="text", text=replacement))
        for i in slots:
            parts[i] = TextContent(type="text", text=replacement if i == slots[0] else "")
        return CallToolResult(content=parts)


async def main() -> None:
    requester = "sample-user"  # Use the authenticated principal from your application.
    model = ScriptedModel(
        [
            [
                function_call("inspect_site", {}, call_id="local"),
                function_call(
                    "mcp_Inspection_reports__read_report", {"kind": "structured"}, call_id="remote"
                ),
            ],
            [assistant_message("Reports ready.")],
        ]
    )
    with closing(Reports(owner="sample-user")) as reports:
        async with ReportServer(reports) as server:
            agent = Agent(
                name="Inspector",
                model=model,
                tools=[reports.site_tool()],
                mcp_servers=[server],
                mcp_config={"include_server_in_tool_names": True},
            )
            await Runner.run(
                agent,
                "Inspect both sites.",
                context=requester,
                run_config=RunConfig(tracing_disabled=True),
            )
        reports.show(requester)


if __name__ == "__main__":
    asyncio.run(main())
