"""An application validator can request repair without publishing invalid XML."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

from agents import Agent
from agents.testing import ScriptedResponses, text, tool_call
from examples.codex.validated_artifact import ArtifactContext, publish_xml, read_memory
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import function_declarations, provider


@pytest.mark.parametrize("encoding", [None, "utf-8", "utf-16", "iso-8859-1"])
async def test_published_xml_preserves_text_across_encoding_declarations(
    tmp_path: Path, encoding: str | None
) -> None:
    declaration = f'<?xml version="1.0" encoding="{encoding}"?>' if encoding else ""
    value = "café&#13;line2&#10;line3&#9;tail"
    expected = "café\rline2\nline3\ttail"
    xml = declaration + f'<presentation><slide label="{value}">{value}</slide></presentation>'
    output_path = tmp_path / "presentation.xml"
    async with ScriptedResponses(
        [tool_call("publish_xml", {"xml": xml}, call_id="publish")], [text("Published")]
    ) as peer:
        await Agent(instructions="Publish.", tools=[publish_xml], provider=peer.provider).run(
            "Publish the presentation.",
            context=ArtifactContext(memory={}, output_path=output_path),
        )
    slide = ElementTree.parse(output_path).getroot().find("slide")
    assert slide is not None and slide.text == expected and slide.attrib["label"] == expected


@pytest.mark.parametrize(
    ("invalid_xml", "feedback"),
    [
        ("<presentation><slide></presentation>", "XML is not well formed"),
        ("<presentation />", "at least one slide"),
    ],
)
async def test_memory_and_artifact_validation_repair_in_one_native_turn(
    responses: ResponsesFixture, tmp_path: Path, invalid_xml: str, feedback: str
) -> None:
    valid_xml = "<presentation><slide>Use plain language.</slide></presentation>"
    output_path = tmp_path / "presentation.xml"
    context = ArtifactContext(memory={"audience": "Use plain language."}, output_path=output_path)
    responses.respond(function_call("read_memory", {"key": "audience"}, "memory-note"))
    responses.respond(function_call("publish_xml", {"xml": invalid_xml}, "invalid-draft"))
    responses.respond(function_call("publish_xml", {"xml": valid_xml}, "repaired-draft"))
    responses.respond(assistant_output("The validated presentation is ready."))

    observed_rejection = False

    async def before_event(event: dict[str, Any]) -> None:
        nonlocal observed_rejection
        if event["type"] == "response.created" and len(responses.requests) == 3:
            # The native loop has received the validator's failure, but the next
            # model response has not yet supplied the repaired document.
            assert feedback in json.dumps(responses.requests[-1]["input"])
            assert not output_path.exists()
            observed_rejection = True

    responses.before_event = before_event
    agent = Agent[ArtifactContext, str](
        instructions="Read memory, publish the XML, and repair validation failures.",
        tools=[read_memory, publish_xml],
        provider=provider(responses),
    )
    async with timeout(30), agent.session(context=context) as session:
        handle = await session.submit("Publish a presentation for the saved audience.")
        completed = await (handle).get_result()
        history = [entry async for entry in session.items()]

    assert completed.final_output == "The validated presentation is ready."
    assert observed_rejection
    assert output_path.read_text(encoding="utf-8") == valid_xml
    slide = ElementTree.parse(output_path).getroot().find("slide")
    assert slide is not None and slide.text == "Use plain language."
    assert "Use plain language." in json.dumps(responses.requests[1]["input"])
    assert "Published validated XML: presentation.xml" in json.dumps(responses.requests[3]["input"])
    assert [item["call_id"] for item in history if item["type"] == "function_call"] == [
        "memory-note",
        "invalid-draft",
        "repaired-draft",
    ]
    declarations = function_declarations(responses.requests[0])
    memory = next(tool for tool in declarations if tool["name"] == "read_memory")
    assert set(memory["parameters"]["properties"]) == {"key"}
    assert str(tmp_path) not in json.dumps(responses.requests)
