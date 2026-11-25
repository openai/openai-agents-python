"""Run against the installed preview and partner wheels."""

import asyncio
import json
import os
import sys
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch
from uuid import UUID

from agents_langsmith import langsmith

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_export_waits_for_client(self) -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()

        class Client:
            def create_run(self, **record: Any) -> None:
                loop.call_soon_threadsafe(started.set)
                release.wait(5)
                finished.set()
                raise OSError("late failure")

        agent = Agent(
            name="Demo",
            model=ScriptedModel([[assistant_message("Ready")]]),
            plugins=[langsmith(Client())],
        )
        task = asyncio.create_task(
            Runner.run(agent, "Status?", run_config=RunConfig(tracing_disabled=True))
        )
        try:
            await asyncio.wait_for(started.wait(), 5)
            task.cancel("first")
            await asyncio.sleep(0)
            task.cancel("second")
            await asyncio.sleep(0)
            self.assertFalse(task.done(), "Application cleanup can race with create_run")
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError) as cancelled:
            await task
        # Python 3.11+ passes the cancellation message to the task's awaiter.
        if sys.version_info >= (3, 11):
            self.assertEqual(str(cancelled.exception), "first")
        self.assertTrue(finished.is_set())


class ClientTests(unittest.TestCase):
    @unittest.skipIf(sys.version_info < (3, 11), "LangSmith 0.15.0 requires Python 3.11+")
    def test_minimal_trace_over_http(self) -> None:
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, self.headers["x-api-key"], body))
                self.send_response(200)
                self.end_headers()

        with (
            TemporaryDirectory() as home,
            patch.dict(os.environ, {"HOME": home, "LANGCHAIN_TRIAL": "private"}, clear=True),
            HTTPServer(("127.0.0.1", 0), Handler) as server,
        ):
            from langsmith import Client

            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(
                    api_url=f"http://127.0.0.1:{server.server_port}",
                    api_key="local-test",
                    auto_batch_tracing=False,
                    omit_traced_runtime_info=True,
                    timeout_ms=3000,
                )
                with closing(client):
                    agent = Agent(
                        name="private agent",
                        model=ScriptedModel([[assistant_message("private answer")]]),
                        plugins=[langsmith(client, project_name="Desk")],
                    )
                    Runner.run_sync(
                        agent, "private question", run_config=RunConfig(tracing_disabled=True)
                    )
            finally:
                server.shutdown()
                thread.join()

        ((path, key, record),) = received
        self.assertEqual((path, key), ("/runs", "local-test"))
        self.assertEqual(record["session_name"], "Desk")
        self.assertEqual(UUID(record["id"]).version, 4)
        self.assertEqual(record["inputs"], {})
        self.assertEqual(record["outputs"], {"status": "completed"})
        self.assertNotIn("extra", record)
        self.assertNotIn("private", repr(record))
