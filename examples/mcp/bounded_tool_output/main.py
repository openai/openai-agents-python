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

from agents import Agent, RunConfig, Runner, ToolCallHookContext, ToolExecutionConfig, UserError
from agents.decorators import tool
from agents.mcp import MCPServerStdio, MCPToolMetaContext
from agents.testing import ScriptedModel, assistant_message, function_call

MAX_TEXT_BYTES = 128
SERVER = "Inspection reports"


async def site_report() -> str:
    """Read the local inspection report."""
    return "WARN: synthetic site detail\n" * 20


@tool(failure_error_function=None)
async def inspect_site() -> str:
    """Read the local inspection report."""
    return await site_report()


async def summarize(texts: tuple[str, ...]) -> str:
    """Share only the number of warning markers in these synthetic reports."""
    count = await asyncio.to_thread(lambda: sum(text.count("WARN:") for text in texts))
    return f"Inspection report with {count} warning markers."


class Reports:
    """Private storage for one owner and one run. Never put this object in SDK context."""

    def __init__(self, owner: str):
        self._owner = owner
        self._texts: dict[str, tuple[str, ...]] | None = {}

    def close(self) -> None:
        self._texts = None

    def fetch(self, requester: str, reference: str, *, part: int = 0, offset: int = 0) -> str:
        self.authorize(requester)
        if self._texts is None or reference not in self._texts:
            raise PermissionError("Report unavailable")
        if part < 0 or offset < 0:
            raise ValueError("Use a nonnegative part and offset")
        return self._texts[reference][part][offset : offset + 64]

    def authorize(self, requester: str) -> None:
        if self._texts is None or requester != self._owner:
            raise PermissionError("Report unavailable")

    async def prepare(self, call: ToolCallHookContext[str], output: Any) -> Any:
        if call.tool is not inspect_site:
            return output
        self.authorize(call.run_context.context)
        if not isinstance(output, str):
            raise UserError("Expected a report string")
        return await self.bound((output,)) or output

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
        if self._texts is None:
            raise RuntimeError("Report storage closed")
        self._texts[reference] = texts
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
        if call.server_name == SERVER and call.tool_name == "read.report":
            self.reports.authorize(call.run_context.context)

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> CallToolResult:
        result = await super().call_tool(tool_name, arguments, meta=meta)
        if self.name != SERVER or tool_name != "read.report":
            return result
        # Wire aliases are the same in both supported MCP client versions.
        fields = result.model_dump(by_alias=True, exclude={"content", "meta"})
        if fields.get("isError"):
            return result
        if any(not isinstance(block, (TextContent, ImageContent)) for block in result.content):
            raise UserError("Unsupported report media")

        structured = fields.get("structuredContent")
        if structured:
            if structured == {"ticket": "site"}:
                structured = {"report": await site_report()}
            text = json.dumps(structured)
            replacement = await self.reports.bound((text,)) or text
        else:
            texts = tuple(part.text for part in result.content if isinstance(part, TextContent))
            replacement = await self.reports.bound(texts)
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
    owner = "sample-user"  # Use the authenticated principal from your application.
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
    with closing(Reports(owner)) as reports:
        config = RunConfig(
            tracing_disabled=True,
            tool_execution=ToolExecutionConfig(prepare_tool_output=reports.prepare),
        )
        async with ReportServer(reports) as server:
            agent = Agent(
                name="Inspector",
                model=model,
                tools=[inspect_site],
                mcp_servers=[server],
                mcp_config={"include_server_in_tool_names": True},
            )
            result = await Runner.run(
                agent, "Inspect both sites.", context=owner, run_config=config
            )
        for item in result.new_items:
            if item.type == "tool_call_output_item":
                summary = item.output if isinstance(item.output, str) else item.output["text"]
                print(summary)
                reference = summary.split("Reference: ")[1]
                page = reports.fetch(owner, reference)
                print(f"Retrieved {len(page)} characters privately.")


if __name__ == "__main__":
    asyncio.run(main())
