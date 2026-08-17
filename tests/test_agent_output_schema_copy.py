from __future__ import annotations

from pydantic import BaseModel

from agents.agent_output import AgentOutputSchema


class _Output(BaseModel):
    value: str


def test_agent_output_json_schema_returns_isolated_copy() -> None:
    output_schema = AgentOutputSchema(_Output)

    first = output_schema.json_schema()
    first["properties"]["value"]["type"] = "integer"

    second = output_schema.json_schema()
    assert second["properties"]["value"]["type"] == "string"
