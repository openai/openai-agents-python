"""Exercise the existing peer's fresh tool turns without importing an SDK."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from argparse import Namespace
from pathlib import Path

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol_peer import Peer, final_text


class WebSocketToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = Peer(
            Namespace(tool_rounds=1, tools_per_round=1, chunks=128, output_chars=16384, delay_ms=0)
        )
        app = web.Application()
        app.router.add_get("/responses", self.peer.websocket)
        self.server = TestServer(app)
        await self.server.start_server()
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()

    async def receive(self, socket):
        events = []
        while True:
            event = await socket.receive_json(timeout=5)
            events.append(event)
            self.assertIn("type", event, event)
            if event["type"] == "error":
                return None, events
            if event["type"] == "response.completed":
                self.assertEqual([e["sequence_number"] for e in events], list(range(len(events))))
                return event["response"], events

    def request(self, operation):
        return {
            "type": "response.create",
            "store": False,
            "model": "benchmark",
            "input": [{"role": "user", "content": f"BENCH_RUN={operation}"}],
            "tools": [
                {
                    "type": "function",
                    "name": "echo",
                    "description": "Echo.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            ],
        }

    async def test_two_fresh_tool_turns_keep_own_history_on_one_socket_each(self):
        for index in range(2):
            operation = f"fresh-{index}"
            body = self.request(operation)
            async with self.client.ws_connect(self.server.make_url("/responses")) as socket:
                if index == 0:
                    await socket.send_json({**body, "generate": False})
                    warmup, _ = await self.receive(socket)
                    first = {**body, "input": [], "previous_response_id": warmup["id"]}
                else:
                    first = body
                await socket.send_json(first)
                response, events = await self.receive(socket)
                call = response["output"][0]
                self.assertEqual(call["type"], "function_call")
                value = json.loads(call["arguments"])["value"]
                output = {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": value,
                }
                second = (
                    {**body, "input": [output], "previous_response_id": response["id"]}
                    if index == 0
                    else {**body, "input": body["input"] + response["output"] + [output]}
                )
                await socket.send_json(second)
                response, events = await self.receive(socket)
                deltas = [e["delta"] for e in events if e["type"] == "response.output_text.delta"]
                self.assertEqual(len(deltas), 128)
                self.assertEqual("".join(deltas), final_text(operation, 16384))
        await asyncio.wait_for(self.peer.websockets_retired.wait(), 5)
        self.assertEqual(self.peer.requests, 4)
        self.assertEqual(self.peer.verified_tool_outputs, 2)
        self.assertEqual(self.peer.errors, 0)
        self.assertEqual(self.peer.websocket_connections, 2)
        self.assertEqual(self.peer.websocket_open_connections, 0)
        for operation in ("fresh-0", "fresh-1"):
            receipts = [r for r in self.peer.receipts if r["op_id"] == operation]
            self.assertEqual(len({r["websocket_connection_id"] for r in receipts}), 1)
            self.assertTrue(all(r["history_user_markers"] == [operation] for r in receipts))
            self.assertEqual(
                receipts[1]["history_tool_call_ids"], receipts[1]["history_tool_output_ids"]
            )

    async def test_foreign_response_and_wrong_tool_output_are_rejected(self):
        body = self.request("origin")
        async with self.client.ws_connect(self.server.make_url("/responses")) as first:
            await first.send_json(body)
            response, _ = await self.receive(first)
            async with self.client.ws_connect(self.server.make_url("/responses")) as other:
                await other.send_json(
                    {**self.request("other"), "previous_response_id": response["id"]}
                )
                failed, events = await self.receive(other)
                self.assertIsNone(failed)
                self.assertIn("another response", events[-1]["error"]["message"])
            call = response["output"][0]
            await first.send_json(
                {
                    **body,
                    "previous_response_id": response["id"],
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": "wrong",
                        }
                    ],
                }
            )
            failed, events = await self.receive(first)
            self.assertIsNone(failed)
            self.assertIn("wrong echo output", events[-1]["error"]["message"])
        await asyncio.wait_for(self.peer.websockets_retired.wait(), 5)
        self.assertEqual(self.peer.errors, 2)
        self.assertEqual(self.peer.websocket_open_connections, 0)
