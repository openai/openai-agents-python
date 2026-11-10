"""Check and repair a JSON artifact with the packaged tool and native Session."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, Field

from agents import Agent
from agents.artifacts import json_artifact_tool
from agents.testing import ScriptedResponses, text, tool_call


class Report(BaseModel):
    title: str = Field(min_length=1)


async def main() -> None:
    with TemporaryDirectory() as directory:
        destination = Path(directory) / "report.json"
        async with ScriptedResponses(
            [tool_call("publish_report", {"json": "{"}, call_id="syntax")],
            [tool_call("publish_report", {"json": '{"title":""}'}, call_id="schema")],
            [
                tool_call(
                    "publish_report",
                    {"json": '{"title":"Quarterly results"}'},
                    call_id="corrected",
                )
            ],
            [text("Published checked report.")],
        ) as peer:
            agent = Agent(
                instructions="Publish the report. Repair JSON rejected by publish_report.",
                provider=peer.provider,
                tools=[json_artifact_tool(name="publish_report", path=destination, schema=Report)],
            )
            async with agent.session() as session:
                result = await (await session.submit("Publish the report.")).get_result()
            assert "JSON syntax check failed" in (peer.tool_output("syntax") or "")
            assert "schema check failed" in (peer.tool_output("schema") or "")
            assert json.loads(destination.read_text(encoding="utf-8")) == {
                "title": "Quarterly results"
            }
            print(result.final_output)
            print(destination.read_text(encoding="utf-8"), end="")


if __name__ == "__main__":
    asyncio.run(main())
