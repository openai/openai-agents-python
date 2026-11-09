"""Checked artifacts use the public tools and the real native correction loop."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field, field_serializer
from pydantic_core import PydanticSerializationError

from agents import Agent, function_tool
from agents.artifacts import json_artifact_tool
from agents.testing import ScriptedResponses, text, tool_call
from tests._asyncio import timeout


class Report(BaseModel):
    title: str = Field(min_length=1)


async def test_packaged_artifact_repairs_in_same_native_session(tmp_path: Path) -> None:
    destination = tmp_path / "report.json"
    previous = '{"title":"Previous"}\n'
    destination.write_text(previous, encoding="utf-8")

    @function_tool
    def inspect_previous() -> str:
        return destination.read_text(encoding="utf-8")

    async with (
        ScriptedResponses(
            [tool_call("report", {"json": "{"}, call_id="bad-json")],
            [tool_call("inspect_previous", {}, call_id="after-json")],
            [tool_call("report", {"json": '{"title":""}'}, call_id="bad-schema")],
            [tool_call("inspect_previous", {}, call_id="after-schema")],
            [tool_call("report", {"json": '{"title":"Corrected café"}'}, call_id="corrected")],
            [text("Published")],
        ) as peer,
        timeout(30),
    ):
        agent = Agent(
            instructions="Publish the report, repairing rejected JSON.",
            provider=peer.provider,
            tools=[
                json_artifact_tool(name="report", path=destination, schema=Report),
                inspect_previous,
            ],
        )
        async with agent.session() as session:
            turn = await session.submit("Publish the report.")
            assert (await turn.get_result()).final_output == "Published"
            calls = [item async for item in session.items() if item["type"] == "function_call"]
            assert {item["turn_id"] for item in calls} == {turn.turn_id}
            assert len(calls) == 5
        assert "Artifact report.json: JSON syntax check failed" in (
            peer.tool_output("bad-json") or ""
        )
        assert "Artifact report.json: schema check failed: title:" in (
            peer.tool_output("bad-schema") or ""
        )
        assert peer.tool_output("after-json") == previous
        assert peer.tool_output("after-schema") == previous
        assert "Published validated JSON artifact report" in (peer.tool_output("corrected") or "")
        assert "JSON syntax check failed" in json.dumps(peer.requests[1])
        assert "schema check failed" in json.dumps(peer.requests[3])
    assert json.loads(destination.read_text(encoding="utf-8")) == {"title": "Corrected café"}
    assert list(tmp_path.iterdir()) == [destination]


async def test_packaged_artifact_bounds_diagnostics_and_preserves_previous(tmp_path: Path) -> None:
    class Titles(BaseModel):
        titles: list[int]

    destination = tmp_path / "report.json"
    destination.write_text("previous", encoding="utf-8")
    async with (
        ScriptedResponses(
            [tool_call("report", {"json": json.dumps({"titles": ["bad"] * 40})}, call_id="bad")],
            [text("Rejected")],
        ) as peer,
        timeout(30),
    ):
        agent = Agent(
            instructions="Publish the requested JSON artifact.",
            provider=peer.provider,
            tools=[json_artifact_tool(name="report", path=destination, schema=Titles)],
        )
        await agent.run("Try the report.")
        feedback = peer.tool_output("bad") or ""
        assert "schema check failed" in feedback
        assert len(feedback) < 2048
    assert destination.read_text(encoding="utf-8") == "previous"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize(
    ("schema", "invalid_json", "valid_json", "expected"),
    [
        (float, "1e400", "1.25", 1.25),
        (list[float], "[-1e400]", "[1.25]", [1.25]),
        (float, '"1e400"', "1.25", 1.25),
        (str, '"\\ud800"', '"Corrected café 🦦"', "Corrected café 🦦"),
        (list[str], '["\\udfff"]', '["Corrected café 🦦"]', ["Corrected café 🦦"]),
    ],
)
async def test_packaged_artifact_repairs_invalid_values_in_same_native_session(
    tmp_path: Path, schema: type[Any], invalid_json: str, valid_json: str, expected: Any
) -> None:
    destination = tmp_path / "report.json"
    previous = '{"retained":true}\n'
    destination.write_text(previous, encoding="utf-8")

    @function_tool
    def inspect_previous() -> str:
        return destination.read_text(encoding="utf-8")

    async with (
        ScriptedResponses(
            [tool_call("report", {"json": invalid_json}, call_id="invalid-value")],
            [tool_call("inspect_previous", {}, call_id="previous")],
            [tool_call("report", {"json": valid_json}, call_id="corrected")],
            [text("Published")],
        ) as peer,
        timeout(30),
    ):
        agent = Agent(
            instructions="Publish the report, repairing rejected values.",
            provider=peer.provider,
            tools=[
                json_artifact_tool(name="report", path=destination, schema=schema),
                inspect_previous,
            ],
        )
        async with agent.session() as session:
            turn = await session.submit("Publish the report.")
            assert (await turn.get_result()).final_output == "Published"
            calls = [item async for item in session.items() if item["type"] == "function_call"]
            assert len(calls) == 3
            assert {item["turn_id"] for item in calls} == {turn.turn_id}
        feedback = peer.tool_output("invalid-value") or ""
        assert "JSON value check failed" in feedback
        assert len(feedback) < 2048
        assert "JSON value check failed" in json.dumps(peer.requests[1])
        assert peer.tool_output("previous") == previous
        assert "Published validated JSON artifact report" in (peer.tool_output("corrected") or "")
    assert json.loads(destination.read_text(encoding="utf-8")) == expected
    assert list(tmp_path.iterdir()) == [destination]


async def test_packaged_artifact_serializer_error_fails_without_model_repair(
    tmp_path: Path,
) -> None:
    class BrokenSerializer(BaseModel):
        value: float

        @field_serializer("value")
        def serialize_value(self, value: float) -> float:
            raise ValueError("application serializer failed")

    destination = tmp_path / "report.json"
    async with (
        ScriptedResponses(
            [tool_call("report", {"json": '{"value":1}'}, call_id="valid")],
        ) as peer,
        timeout(30),
    ):
        agent = Agent(
            instructions="Publish the requested JSON artifact.",
            provider=peer.provider,
            tools=[json_artifact_tool(name="report", path=destination, schema=BrokenSerializer)],
        )
        with pytest.raises(PydanticSerializationError, match="application serializer failed"):
            await agent.run("Publish the report.")
        assert len(peer.requests) == 1
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_packaged_artifact_io_error_fails_without_model_repair(tmp_path: Path) -> None:
    destination = tmp_path / "missing" / "report.json"
    async with (
        ScriptedResponses(
            [tool_call("report", {"json": '{"title":"Valid"}'}, call_id="valid")],
        ) as peer,
        timeout(30),
    ):
        agent = Agent(
            instructions="Publish the requested JSON artifact.",
            provider=peer.provider,
            tools=[json_artifact_tool(name="report", path=destination, schema=Report)],
        )
        with pytest.raises(FileNotFoundError):
            await agent.run("Publish the report.")
        assert len(peer.requests) == 1
    assert not destination.exists()
