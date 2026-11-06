"""Run a trusted local stdio MCP server through the native client.

    PYTHONPATH=packages/codex python -m examples.recipes.local_mcp

Install the matching SDK wheel; this server uses only the Python standard
library, so there is no optional MCP dependency to install. Codex launches the
server lazily on submission and Session exit closes its owned connection and
process. The application selects the raw name ``lookup``; it does not select
the provider's projected namespace. Treat server selection as a trust decision:
a readOnlyHint is metadata, not an operating-system restriction.
"""

import asyncio
import sys
from pathlib import Path

from agents import Agent, NativeError, NativeProvider, StdioMCP

SERVER = Path(__file__).with_name("fixtures") / "catalog_mcp.py"


def catalog_agent(
    provider: NativeProvider | None = None,
    *,
    selected: str = "lookup",
    audit: Path | None = None,
    fail_start: bool = False,
) -> Agent[None, str]:
    args = ["-I", "-B", str(SERVER)]
    if audit is not None:
        args.extend(["--audit", str(audit)])
    if fail_start:
        args.append("--fail-start")
    return Agent(
        instructions="Use catalog lookup to read the price of SKU PAPER.",
        tools=[
            StdioMCP(
                name="catalog",
                command=sys.executable,
                args=args,
                tools=[selected],
                startup_timeout=3,
            )
        ],
        provider=provider,
    )


async def main() -> None:
    try:
        async with catalog_agent().session() as session:
            result = await (await session.submit("How much is PAPER?")).get_result()
            print(result.final_output)
    except NativeError as error:
        # Native setup diagnostics distinguish missing raw names from startup failure.
        print(f"Catalog request failed: {error}", file=sys.stderr)
        raise


if __name__ == "__main__":
    asyncio.run(main())
