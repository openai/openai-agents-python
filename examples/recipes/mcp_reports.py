"""Keep large native MCP report results in application-owned memory.

    MCP_URL=http://localhost:8000/mcp python -m examples.recipes.mcp_reports

The endpoint must advertise a ``report`` tool. See mcp_reports.md for its
contract, bounds and ownership. This command makes live model calls.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from agents import Agent, HttpMCP, NativeProvider, Session

MAX_VISIBLE_BYTES = 4096
MAX_REPORT_BYTES = 64 * 1024
MAX_STORED_BYTES = 128 * 1024
MAX_REPORTS = 4


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def visible_bytes(result: dict[str, Any]) -> int:
    return len(
        encoded(
            {
                "content": [block for block in result["content"] if block["type"] == "text"],
            }
        ).encode("utf-8")
    )


class ReportStore:
    """One authenticated owner's reports, retained only for one Session."""

    def __init__(self, owner: object) -> None:
        self._owner = owner
        self._reports: dict[str, str] = {}
        self._stored_bytes = 0
        self._closed = False

    def _check_owner(self, owner: object) -> None:
        if owner is not self._owner or self._closed:
            raise PermissionError("Report references are unavailable for this owner")

    def references(self, owner: object) -> tuple[str, ...]:
        self._check_owner(owner)
        return tuple(self._reports)

    def read(self, owner: object, reference: str) -> dict[str, Any]:
        self._check_owner(owner)
        if reference not in self._reports:
            raise KeyError("Report reference is unavailable")
        # Decode a fresh copy so callers cannot alter retained reports.
        original: dict[str, Any] = json.loads(self._reports[reference])
        return original

    async def transform(self, _context: object, result: dict[str, Any]) -> dict[str, Any]:
        self._check_owner(self._owner)
        if result.get("isError"):
            return result
        if len(result["content"]) > 8:
            raise ValueError("This example accepts at most eight content blocks")
        original = encoded(result)
        size = len(original.encode("utf-8"))
        content = list(result["content"])
        structured = result.get("structuredContent")
        if structured is not None:
            # Native MCP prefers structuredContent over content, including media.
            # Select content projection and keep structured data in a text slot.
            for index, block in enumerate(content):
                if block["type"] == "text":
                    content[index] = {
                        **block,
                        "text": f"{block['text']}\nStructured content: {encoded(structured)}",
                    }
                    break
            else:
                content.append({"type": "text", "text": encoded(structured)})
        normalized = {**result, "content": content}
        normalized.pop("structuredContent", None)
        if visible_bytes(normalized) <= MAX_VISIBLE_BYTES:
            return normalized
        if (
            size > MAX_REPORT_BYTES
            or len(self._reports) >= MAX_REPORTS
            or self._stored_bytes + size > MAX_STORED_BYTES
        ):
            raise ValueError("Report storage limit reached; no reference was created")

        reference = str(uuid4())
        structured_preview = encoded(structured)[:32] if structured is not None else None
        receipt = f"Stored report ({size} bytes). Reference: {reference}"
        if structured_preview is not None:
            receipt += f"\nStructured preview: {structured_preview}"
        bounded = {
            **normalized,
            "content": [
                {**block, "text": f"{receipt}\nPreview: {block['text'][:32]}"}
                if block["type"] == "text"
                else block
                for block in content
            ],
        }
        if visible_bytes(bounded) > MAX_VISIBLE_BYTES:
            raise ValueError("The bounded report exceeds the visible result limit")
        # No await separates checks, retention and returning the replacement.
        self._reports[reference] = original
        self._stored_bytes += size
        return bounded

    def close(self) -> None:
        self._closed = True
        self._reports.clear()
        self._stored_bytes = 0


@asynccontextmanager
async def report_session(
    url: str, owner: object, *, provider: NativeProvider | None = None
) -> AsyncIterator[tuple[Session[None, str], ReportStore]]:
    reports = ReportStore(owner)
    try:
        agent = Agent[None, str](
            model="gpt-5.5",
            instructions=(
                "Use reports.report to request the example report with key=43. "
                "Summarize its preview and include its reference. "
                "The application retrieves the full report."
            ),
            tools=[
                HttpMCP(
                    name="reports",
                    url=url,
                    tools=["report"],
                    transform_result=reports.transform,
                )
            ],
            provider=provider,
        )
        async with agent.session() as session:
            yield session, reports
    finally:
        # Session exit joins admitted callbacks before their references expire.
        reports.close()


async def main() -> None:
    url = os.environ["MCP_URL"]
    # Authentication supplies the real identity; model arguments never select it.
    owner = object()
    async with report_session(url, owner) as (session, reports):
        result = await (await session.submit("Request the example report.")).get_result()
        print(result.final_output)
        for reference in reports.references(owner):
            original = reports.read(owner, reference)
            print(f"Retrieved {len(encoded(original).encode('utf-8'))} bytes.")


if __name__ == "__main__":
    asyncio.run(main())
