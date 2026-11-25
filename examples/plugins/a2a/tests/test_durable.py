"""A loopback specialist with a separate database and a fault after commit."""

import asyncio
import hashlib
import sqlite3
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from agents_specialist import DurableCall, Receipt, specialist
from agents_specialist.example import QUESTION, local_client, reviewer
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, function_call

ASK = "ask_specialist"
READ = "refresh_specialist"


async def approved(operation, db_path, endpoint, lookup, *, owner="pat", question=QUESTION):
    plugin = specialist(
        local_client(endpoint, lambda: "synthetic-token"),
        question=question,
        artifact_id="readiness",
        verdicts=("ready", "blocked"),
        durable=DurableCall(
            owner=owner,
            operation_id="release-42",
            db_path=db_path,
            lookup_receipt=lookup,
        ),
    )
    agent = Agent(
        "Manager",
        plugins=[plugin],
        model=ScriptedModel([[function_call(operation, {}, call_id="tool-1")]]),
        tool_use_behavior="stop_on_first_tool",
    )
    config = RunConfig(tracing_disabled=True)
    step = await Runner.run(agent, operation, run_config=config)
    (call,) = step.interruptions
    state = step.to_state()
    state.approve(call)
    return (await Runner.run(agent, state, run_config=config)).final_output


class DurableTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = Path(directory.name) / "manager.db"
        self.peer = self.manager.with_name("peer.db")
        self.granted = True
        self.drop_reply = False
        self.committed = asyncio.Event()
        self.respond = asyncio.Event()
        self.respond.set()
        self.requests = []
        self.lookups = []
        with closing(sqlite3.connect(self.peer)) as db:
            db.execute(
                "CREATE TABLE tasks (owner, endpoint, message_id PRIMARY KEY, digest, "
                "task_id UNIQUE, context_id)"
            )
        app = web.Application()
        app.router.add_post("/rpc", self.rpc)
        app.router.add_post("/rpc/other", self.rpc)
        server = TestServer(app)
        self.addAsyncCleanup(server.close)
        await server.start_server()
        self.url = str(server.make_url("/rpc"))

    async def rpc(self, request):
        body = await request.json()
        self.requests.append(body["method"])
        if not self.granted:
            raise web.HTTPForbidden()
        # The loopback token identifies Pat. The task row doubles as the receipt.
        reply = await reviewer(request)
        if body["method"] == "SendMessage":
            message = body["params"]["message"]
            digest = hashlib.sha256(message["parts"][0]["text"].encode()).hexdigest()
            with closing(sqlite3.connect(self.peer)) as db, db:
                db.execute(
                    "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
                    ("pat", self.url, message["messageId"], digest, "demo-task", "demo-context"),
                )
            self.committed.set()
            await self.respond.wait()
            if self.drop_reply:
                request.transport.close()
        else:
            with closing(sqlite3.connect(self.peer)) as db:
                if not db.execute(
                    "SELECT 1 FROM tasks WHERE owner=? AND task_id=?",
                    ("pat", body["params"]["id"]),
                ).fetchone():
                    raise web.HTTPForbidden()
        return reply

    async def lookup(self, owner, endpoint, message_id):
        self.lookups.append(message_id)
        if not self.granted or (owner, endpoint) != ("pat", self.url):
            raise PermissionError
        with closing(sqlite3.connect(self.peer)) as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE owner=? AND endpoint=? AND message_id=?",
                (owner, endpoint, message_id),
            ).fetchone()
        return Receipt(*row) if row else None

    async def invoke(self, operation, **kwargs):
        return await approved(operation, self.manager, self.url, self.lookup, **kwargs)

    async def test_lost_reply_then_restart(self):
        self.drop_reply = True
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            __file__,
            str(self.manager),
            self.url,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 30)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.assertEqual(process.returncode, 0)
        self.assertEqual(output.strip(), b"unknown")
        self.assertTrue(self.committed.is_set())
        self.assertEqual(await self.invoke(ASK), "unknown")
        self.assertEqual(await self.invoke(READ), "ready")
        self.assertEqual(self.requests, ["SendMessage", "GetTask"])
        for db in (self.manager, self.peer):
            self.assertNotIn(QUESTION.encode(), db.read_bytes())
            self.assertNotIn(b"synthetic-token", db.read_bytes())

    async def test_concurrent_sends(self):
        self.respond.clear()
        attempts = [asyncio.create_task(self.invoke(ASK)) for _ in range(2)]
        try:
            await asyncio.wait_for(self.committed.wait(), 5)
            done, _ = await asyncio.wait(attempts, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            self.assertEqual([t.result() for t in done], ["unknown"])
        finally:
            self.respond.set()
            results = await asyncio.gather(*attempts)
        self.assertCountEqual(results, ["pending", "unknown"])
        self.assertEqual(await self.invoke(READ), "ready")
        self.assertEqual(self.requests, ["SendMessage", "GetTask"])

    async def test_only_original_binding_can_read(self):
        self.assertEqual(await self.invoke(ASK), "pending")
        self.lookups.clear()
        for operation in (ASK, READ):
            self.assertEqual(await self.invoke(operation, owner="lee"), "unknown")
            self.assertEqual(await self.invoke(operation, question="Other question"), "unknown")
            self.assertEqual(
                await approved(operation, self.manager, self.url + "/other", self.lookup),
                "unknown",
            )
        self.assertFalse(self.lookups)
        self.assertEqual(self.requests, ["SendMessage"])
        self.assertEqual(await self.invoke(READ), "ready")

    async def test_missing_or_mismatched_receipt_and_revoked_access(self):
        async def missing(*args):
            return None

        async def wrong_question(*args):
            return replace(await self.lookup(*args), digest="wrong")

        async def revoke_after_lookup(*args):
            receipt = await self.lookup(*args)
            self.granted = False
            return receipt

        self.assertEqual(await approved(ASK, self.manager, self.url, missing), "unknown")
        self.assertEqual(await approved(READ, self.manager, self.url, wrong_question), "unknown")
        self.assertEqual(self.requests, ["SendMessage"])
        self.assertEqual(
            await approved(READ, self.manager, self.url, revoke_after_lookup), "unknown"
        )
        self.assertEqual(await self.invoke(READ), "unknown")
        self.assertEqual(self.requests, ["SendMessage", "GetTask"])


if __name__ == "__main__":
    # The first manager exits after the peer has dropped the response.
    async def unexpected_lookup(*args):
        raise SystemExit("The send unexpectedly got a reply.")

    print(asyncio.run(approved(ASK, Path(sys.argv[1]), sys.argv[2], unexpected_lookup)))
