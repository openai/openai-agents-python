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
MAX_REPORTS = 4


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ReportStore:
    """One authenticated owner's reports, retained only for one Session."""

    def __init__(self, owner: object) -> None:
        self._owner = owner
        self._reports: dict[str, str] = {}
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
        content = result["content"]
        if (
            result.get("structuredContent") is not None
            or len(content) != 1
            or content[0]["type"] != "text"
        ):
            raise ValueError("This example expects one text block and no structuredContent")

        original = encoded(result)
        size = len(original.encode("utf-8"))
        if size <= MAX_VISIBLE_BYTES:
            return result
        if size > MAX_REPORT_BYTES or len(self._reports) >= MAX_REPORTS:
            raise ValueError("Report storage limit reached; no reference was created")

        reference = str(uuid4())
        preview = {
            **content[0],
            "text": f"Stored report ({size} bytes). Reference: {reference}\n{content[0]['text'][:500]}",
        }
        replacement = {**result, "content": [preview]}
        if len(encoded(replacement).encode("utf-8")) > MAX_VISIBLE_BYTES:
            raise ValueError("Report metadata leaves too little room for a preview")
        # No await separates checks, retention and returning the replacement.
        self._reports[reference] = original
        return replacement

    def close(self) -> None:
        self._closed = True
        self._reports.clear()


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
