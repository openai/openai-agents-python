"""Example-local routing for Live's managed Responses function calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from agents import Agent

from .agent import OrderRequest, ask_order_agent

Send = Callable[[dict[str, Any]], Awaitable[None]]


class DelegationHandler:
    def __init__(self, agent: Agent, send: Send, notify: Send) -> None:
        self.agent = agent
        self.send = send
        self.notify = notify
        self.responses: dict[str, str] = {}
        self.calls: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self.completed: set[tuple[str, str]] = set()
        self.seen_calls: set[str] = set()
        self.queue: asyncio.Queue[tuple[str, str, list[dict[str, Any]]]] = asyncio.Queue()

    def receive(self, envelope: dict[str, Any]) -> None:
        """Collect wire events without awaiting Agent work."""
        if envelope.get("type") != "response.event":
            return
        delegation_id = envelope["delegation_id"]
        event = envelope["event"]
        kind = event["type"]
        if kind == "response.created":
            self.responses[delegation_id] = event["response"]["id"]
            return
        if delegation_id not in self.responses:
            raise ValueError("Received a Responses event before response.created.")
        response_id = self.responses[delegation_id]
        key = (delegation_id, response_id)
        if kind == "response.output_item.done" and event["item"]["type"] == "function_call":
            if key not in self.completed:
                call = event["item"]
                self.calls.setdefault(key, {})[call["call_id"]] = call
        elif kind == "response.completed":
            key = (delegation_id, event["response"]["id"])
            if key in self.completed:
                return
            self.completed.add(key)
            calls = self.calls.pop(key, {})
            pending = [call for call in calls.values() if call["call_id"] not in self.seen_calls]
            self.seen_calls.update(call["call_id"] for call in pending)
            if pending:
                self.queue.put_nowait((*key, pending))
        elif kind in {"response.failed", "response.incomplete"}:
            self.calls.pop((delegation_id, event["response"]["id"]), None)
            raise RuntimeError("The managed Responses backend did not complete.")

    async def work(self) -> None:
        """Process one response batch at a time; the caller owns and cancels this task."""
        while True:
            delegation_id, response_id, calls = await self.queue.get()
            try:
                for call in calls:
                    await self.notify(
                        {"type": "backend", "status": "working", "call_id": call["call_id"]}
                    )
                    try:
                        if call["name"] != "ask_order_agent":
                            raise ValueError("Unknown function.")
                        request = OrderRequest.model_validate_json(call["arguments"])
                    except (ValueError, ValidationError):
                        output = "Invalid specialist request. Supply a self-contained request."
                    else:
                        try:
                            output = await ask_order_agent(self.agent, request)
                        except Exception:
                            # Do not expose exception payloads to the browser or the models.
                            output = "The order specialist failed. No order was changed."
                    await self.send(
                        {
                            "type": "response.item.create",
                            "event_id": str(uuid4()),
                            "item": {
                                "type": "function_call_output",
                                "call_id": call["call_id"],
                                "output": output,
                            },
                        }
                    )
                    await self.notify(
                        {
                            "type": "backend",
                            "status": "result_submitted",
                            "call_id": call["call_id"],
                            "result": output,
                        }
                    )
                await self.send({"type": "response.create", "event_id": str(uuid4())})
                await self.notify(
                    {
                        "type": "backend",
                        "status": "continued",
                        "delegation_id": delegation_id,
                        "response_id": response_id,
                    }
                )
            finally:
                self.queue.task_done()
