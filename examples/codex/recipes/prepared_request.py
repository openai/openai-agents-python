"""Edit a detached sampling copy after a real tool result, without live credentials.

ScriptedResponses supplies an explicitly HTTP loopback provider with a synthetic
key. Use NativeProvider(supports_websockets=False) for the same transport choice
with your own provider.

Select a standard Responses (non-Lite) model, such as gpt-5.5.
This profile supports direct roots with eager ordinary function/MCP tools and
no Environment, children or code mode. Edits leave canonical history, original
handlers, validators and approval policies authoritative. Active editor state
cannot be exported. Operations requiring automatic compaction are rejected
before compaction dispatch. For structured output, use NativeOutput or
PromptedOutput(max_retries=0): separate output correction requests cannot carry
this callback.
"""

import asyncio
from typing import Any, Literal

from agents import (
    Agent,
    PreparedRequest,
    ReplaceArguments,
    ToolContext,
    ToolOutput,
    Turn,
    function_tool,
)
from agents.testing import ScriptedResponses, text, tool_call


def prepare(
    turn: Turn[Any],
    request: PreparedRequest,
    *,
    preparation_index: int,
    recovery_attempt: int,
) -> PreparedRequest | None:
    # Recovery can advance preparation_index without a tool result. Inspect
    # the native input and retain call IDs instead of treating the index as proof.
    if not any(
        item.get("type") == "function_call_output" and item.get("call_id") == "lookup"
        for item in request["input"]
    ):
        return None
    request["instructions"] = "Explain the lookup result in one sentence."
    request["input"].append(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "SAMPLING_ONLY_NOTE"}],
        }
    )
    request["tools"][0]["description"] = "Sampled record description."
    request["tools"][0]["parameters"]["properties"]["key"] = {
        "type": "string",
        "enum": ["allowed", "sampled-only"],
    }
    return request


async def run_example() -> str:
    effects = []

    def correct(context: ToolContext[None]) -> ReplaceArguments:
        assert context.call_id == "lookup"
        assert context.original_arguments == {"key": "allowed", "label": "original"}
        return ReplaceArguments({"key": "allowed", "label": "corrected"})

    def shape(context: ToolContext[None], output: ToolOutput) -> ToolOutput:
        assert context.call_id == "lookup"
        assert context.effective_arguments["label"] == "corrected"
        return ToolOutput.text("SHAPED_RESULT")

    @function_tool(before=correct, after=shape)
    async def lookup(key: Literal["allowed"], label: str) -> str:
        """Read a record after original validation and the captured before policy."""
        effects.append(label)
        return f"Raw record {key}"

    async with ScriptedResponses(
        [tool_call("lookup", {"key": "allowed", "label": "original"}, call_id="lookup")],
        [text("The record is allowed.")],
    ) as peer:
        result = await Agent(
            model="gpt-5.5",
            instructions="Call lookup, then explain its result.",
            tools=[lookup],
            provider=peer.provider,
        ).run("Look up the record.", prepare_request=prepare)
        assert effects == ["corrected"]
        assert peer.tool_output("lookup") == "SHAPED_RESULT"
        assert len(peer.requests) == 2
        assert "SAMPLING_ONLY_NOTE" in str(peer.requests[1]["input"])
        assert "Sampled record description." in str(peer.requests[1]["tools"])
        wire_request: Any = peer.requests[1]
        assert any(
            part.get("text") == "Explain the lookup result in one sentence."
            for item in wire_request["input"]
            if isinstance(item, dict) and item.get("role") == "developer"
            for part in item.get("content", [])
            if isinstance(part, dict)
        )
        return result.final_output


if __name__ == "__main__":
    print(asyncio.run(run_example()))
