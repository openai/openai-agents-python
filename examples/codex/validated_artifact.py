"""Use application memory and validate an XML artifact before publishing it.

For owner-scoped artifact IDs, expiry and an authenticated download, see
examples/codex/web_starter/README.md. That service stores and serves the validated bytes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree

from agents import Agent, ToolContext, ToolOutput, function_tool


@dataclass
class ArtifactContext:
    """The application chooses the memory scope and output path."""

    memory: Mapping[str, str]
    output_path: Path


@function_tool
async def read_memory(ctx: ToolContext[ArtifactContext], key: str) -> str:
    """Read a note from the current application's memory scope."""
    return ctx.context.memory.get(key, "No saved note for that key.")


@function_tool
async def publish_xml(ctx: ToolContext[ArtifactContext], xml: str) -> ToolOutput:
    """Return expected validation failures to the model for another attempt.

    This example checks XML syntax and a small application schema. A real
    presentation application should call its own format and content validators.
    Unexpected exceptions still fail the turn rather than requesting a retry.
    """
    try:
        document = ElementTree.fromstring(xml)
    except ElementTree.ParseError as error:
        line, column = error.position
        problem = f"XML is not well formed at line {line}, column {column}."
    else:
        problem = ""
        if document.tag != "presentation" or not document.findall("slide"):
            problem = "Use a presentation root containing at least one slide."
    if problem:
        return ToolOutput.error(f"{problem} Fix the XML and call publish_xml again.")

    # Serialize the validated tree so its declaration matches the stored bytes.
    # Escape CR so an XML reader does not normalize referenced CR text to LF.
    content = ElementTree.tostring(document, encoding="utf-8").replace(b"\r", b"&#13;")
    ctx.context.output_path.write_bytes(content)
    return ToolOutput.text(f"Published validated XML: {ctx.context.output_path.name}")


async def main() -> None:
    agent = Agent[ArtifactContext, str](
        instructions=(
            "Read the audience memory note, then publish a short presentation as XML. "
            "Use a presentation root with slide children. If validation fails, repair "
            "the XML and call publish_xml again before reporting completion."
        ),
        tools=[read_memory, publish_xml],
    )
    with TemporaryDirectory() as directory:
        context = ArtifactContext(
            memory={"audience": "Explain the result in plain language."},
            output_path=Path(directory) / "presentation.xml",
        )
        result = await agent.run("Create one slide about our new report.", context=context)
        print(result.final_output)
        if context.output_path.exists():
            print(context.output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    asyncio.run(main())
