"""Optional native-SDK correctness check; run explicitly, never as a benchmark."""

from __future__ import annotations

import asyncio
import sys
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
from aiohttp.test_utils import TestServer
from protocol_peer import Peer, final_text
from v1_adapter import Adapter, Conversation


class CompletedBeforeObservation:
    """Delay the observer until the real public turn has completed."""

    def __init__(self, session):
        self.session = session

    async def submit(self, prompt):
        turn = await self.session.submit(prompt)
        await turn.get_result()
        return turn


class NativeStreamCursorTests(unittest.IsolatedAsyncioTestCase):
    async def test_retained_stream_and_genuine_sdk_gap(self):
        peer = Peer(
            Namespace(tool_rounds=0, tools_per_round=0, chunks=32, output_chars=64, delay_ms=0)
        )
        app = web.Application()
        app.router.add_post("/v1/responses", peer.handle)
        async with TestServer(app) as server:
            async with Adapter(str(server.make_url("/v1")), stream=True) as adapter:
                async with adapter.conversation() as conversation:
                    for index in range(20):
                        identity = f"retained-{index}"
                        result = await asyncio.wait_for(
                            conversation.run(f"BENCH_RUN={identity}"), 10
                        )
                        self.assertEqual(result["text"], final_text(identity, 64))
                        self.assertEqual("".join(result["deltas"]), result["text"])
                        self.assertEqual(len(result["deltas"]), 32)
                    self.assertGreater(conversation.after, 256)

                    # A late public observer proves that the SDK actually retired
                    # the old prefix, rather than merely accumulating many turns.
                    events = conversation.session.events()
                    try:
                        gap = await asyncio.wait_for(anext(events), 2)
                        self.assertEqual(gap.type, "gap")
                        self.assertEqual(gap.first_sequence, 1)
                        self.assertGreater(gap.sequence, 0)
                    finally:
                        await events.aclose()

                # A fresh Session's single response exceeds retention before
                # observation begins. Reject its actual current-turn SDK gap.
                peer.args.chunks = 320
                peer.args.output_chars = 1024
                async with adapter.conversation() as conversation:
                    stale = Conversation(CompletedBeforeObservation(conversation.session), True)
                    with self.assertRaisesRegex(AssertionError, "dropped observations"):
                        await asyncio.wait_for(stale.run("BENCH_RUN=retained-control"), 10)
        self.assertEqual(peer.requests, 21)
        self.assertEqual(peer.errors, 0)
        self.assertEqual(peer.emitted_text_chunks, 20 * 32 + 320)
        self.assertTrue(all(op.status == "completed" for op in peer.operations.values()))


if __name__ == "__main__":
    unittest.main()
