"""Inject a fake writer before simulating tools; a scripted model still runs tools.

The ledger belongs to the application. It deduplicates within this fake writer,
not across processes or restarts. Production writes need their own durable key
and transaction. Cancellation does not undo a completed write.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Literal, Protocol

from agents import Agent, ToolContext, ToolOutput, function_tool
from agents.testing import ScriptedResponses, text, tool_call


class Writer(Protocol):
    async def write(self, key: str) -> None: ...


@dataclass
class FakeWriter:
    ledger: list[str] = field(default_factory=list)

    async def write(self, key: str) -> None:
        if key not in self.ledger:
            self.ledger.append(key)


@dataclass
class Application:
    writer: Writer
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_started: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_release: asyncio.Event = field(default_factory=asyncio.Event)
    cleaned: asyncio.Event = field(default_factory=asyncio.Event)


@function_tool
async def write_record(
    context: ToolContext[Application], key: str, mode: Literal["ok", "error", "raise", "wait"]
) -> ToolOutput:
    """Write once per key, then demonstrate a selected failure or cleanup path."""
    app = context.context
    await app.writer.write(key)
    if mode == "error":
        return ToolOutput.error("Synthetic failure after write")
    if mode == "raise":
        raise ValueError("Synthetic exception after write")
    if mode == "wait":
        app.entered.set()
        try:
            await asyncio.Future()
        finally:
            app.cleanup_started.set()
            await app.cleanup_release.wait()
            app.cleaned.set()
    return ToolOutput.text("written")


async def main() -> None:
    fake = FakeWriter()
    app = Application(fake)
    async with ScriptedResponses(
        [tool_call("write_record", {"key": "synthetic-a", "mode": "ok"}, call_id="first")],
        [tool_call("write_record", {"key": "synthetic-a", "mode": "ok"}, call_id="retry")],
        [text("Done.")],
    ) as peer:
        await Agent(
            instructions="Use the declared tools.", tools=[write_record], provider=peer.provider
        ).run("Write.", context=app)
        assert peer.tool_output("first") == peer.tool_output("retry") == "written"
    assert fake.ledger == ["synthetic-a"]
    print("The injected fake writer recorded one effect.")


if __name__ == "__main__":
    asyncio.run(main())
