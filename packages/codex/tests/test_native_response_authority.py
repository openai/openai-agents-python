"""Protect completed native items from conflicting terminal response copies."""

from __future__ import annotations

import json
from typing import Any

from agents import Agent, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)


def provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-response-authority-key")


async def test_terminal_reasoning_ciphertext_drift_cannot_replace_completed_items(
    responses: ResponsesFixture,
) -> None:
    async def add_conflicting_terminal_output(event: dict[str, Any]) -> None:
        if event["type"] == "response.completed" and event["response"]["id"] == "resp_1":
            event["response"]["output"] = [
                {
                    "type": "reasoning",
                    "id": "reasoning-authoritative",
                    "summary": [{"type": "summary_text", "text": "visible summary"}],
                    "encrypted_content": "ciphertext-terminal-decoy",
                },
                {
                    "type": "reasoning",
                    "id": "reasoning-terminal-conflict",
                    "summary": [{"type": "summary_text", "text": "terminal summary conflict"}],
                    "encrypted_content": "ciphertext-terminal-conflict",
                },
                {
                    "type": "message",
                    "id": "message-terminal-decoy",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "terminal decoy"}],
                },
            ]

    responses.before_event = add_conflicting_terminal_output
    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "reasoning",
                "id": "reasoning-authoritative",
                "summary": [{"type": "summary_text", "text": "visible summary"}],
                "encrypted_content": "ciphertext-item-done",
            },
        },
        assistant_output("completed answer"),
    )
    responses.respond(assistant_output("follow-up answer"))

    async with (
        timeout(30),
        Agent(instructions="Answer briefly.", provider=provider(responses)).session() as session,
    ):
        first = await session.submit("first")
        completed = await (first).get_result()
        assert completed.final_output == "completed answer"
        assert completed.usage is not None
        observations = [event async for event in first.events()]

        public_items = json.dumps([entry async for entry in session.items()])
        assert "visible summary" in public_items
        assert "ciphertext-item-done" not in public_items
        assert "ciphertext-terminal-decoy" not in public_items
        assert "ciphertext-terminal-conflict" not in public_items
        assert "terminal summary conflict" not in public_items
        assert "terminal decoy" not in public_items
        assert "ciphertext-item-done" not in repr(observations)
        assert "ciphertext-terminal-decoy" not in repr(observations)
        assert "ciphertext-terminal-conflict" not in repr(observations)

        second = await session.submit("second")
        assert (await (second).get_result()).final_output == "follow-up answer"
        replay = json.dumps(responses.requests[1]["input"])
        assert "ciphertext-item-done" in replay
        assert "ciphertext-terminal-decoy" not in replay
        assert "ciphertext-terminal-conflict" not in replay
        assert "terminal summary conflict" not in replay
        assert "terminal decoy" not in replay


async def test_terminal_tool_conflicts_cannot_change_execution_or_replay(
    responses: ResponsesFixture,
) -> None:
    calls: list[str] = []

    @function_tool
    def lookup(key: str) -> str:
        """Read one synthetic record."""
        calls.append(key)
        return f"resolved:{key}"

    async def add_conflicting_terminal_output(event: dict[str, Any]) -> None:
        if event["type"] == "response.completed" and event["response"]["id"] == "resp_1":
            event["response"]["output"] = [
                {
                    "type": "function_call",
                    "id": "call-terminal-conflict",
                    "call_id": "call-authoritative",
                    "name": "lookup",
                    "arguments": json.dumps({"key": "terminal-conflict"}),
                },
                {
                    "type": "function_call",
                    "id": "call-terminal-only",
                    "call_id": "call-terminal-only",
                    "name": "lookup",
                    "arguments": json.dumps({"key": "terminal-only"}),
                },
            ]

    responses.before_event = add_conflicting_terminal_output
    responses.respond(function_call("lookup", {"key": "item-done"}, "call-authoritative"))
    responses.respond(assistant_output("tool completed"))
    async with (
        timeout(30),
        Agent(
            instructions="Use lookup once.",
            tools=[lookup],
            provider=provider(responses),
        ).session() as session,
    ):
        handle = await session.submit("look up the record")
        assert (await (handle).get_result()).final_output == "tool completed"
        assert calls == ["item-done"]
        assert len(responses.requests) == 2
        assert tool_output(responses.requests[1], "call-authoritative") == "resolved:item-done"

        replay = json.dumps(responses.requests[1]["input"])
        assert "terminal-conflict" not in replay
        assert "terminal-only" not in replay
        public_items = json.dumps([entry async for entry in session.items()])
        assert "item-done" in public_items
        assert "terminal-conflict" not in public_items
        assert "terminal-only" not in public_items
