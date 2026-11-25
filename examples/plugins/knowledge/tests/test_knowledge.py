import asyncio
import json
import unittest

from agents_knowledge import Hit, knowledge

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message, function_call


class Client:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    async def search(self, query, *, limit):
        self.calls.append((query, limit))
        await asyncio.sleep(0)
        if self.hits is None:
            raise PermissionError("private credential")
        return self.hits


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = Agent("Reader", plugins=[knowledge(lambda context: context["client"])])

    async def search(self, client, **args):
        model = ScriptedModel(
            [[function_call("knowledge_search", args, call_id="search")], [assistant_message("ok")]]
        )
        await Runner.run(
            self.agent,
            "Find hours",
            context={"client": client},
            run_config=RunConfig(model=model, tracing_disabled=True),
        )
        model.assert_complete()
        self.assertIn("Cite each supporting source", model.first_call.system_instructions)
        (tool,) = model.first_call.tools
        self.assertEqual(list(tool.params_json_schema["properties"]), ["query"])
        (result,) = [i for i in model.last_call.input if i.get("type") == "function_call_output"]
        return json.loads(result["output"])

    async def test_same_agent_concurrent_users(self):
        users = [Client([Hit("desk", hours)]) for hours in ("9–5", "10–6")]
        answers = await asyncio.gather(*(self.search(c, query=" hours ") for c in users))
        for client, answer in zip(users, answers, strict=True):
            self.assertEqual(client.calls, [("hours", 3)])
            self.assertEqual(
                answer, {"results": [{"source": "desk", "excerpt": client.hits[0].excerpt}]}
            )

    async def test_bounds_and_empty_results(self):
        hits = [Hit(str(i), "x" * 600) for i in range(4)]
        answer = await self.search(Client(hits), query="hours")
        self.assertEqual(
            answer["results"],
            [{"source": str(i), "excerpt": "x" * 512} for i in range(3)],
        )
        self.assertEqual(await self.search(Client([]), query="hours"), {"results": []})

    async def test_bad_queries_skip_client(self):
        client = Client([])
        for args in ({}, {"query": "  "}, {"query": "x" * 257}):
            answer = await self.search(client, **args)
            self.assertIn("search query", answer["error"])
        self.assertEqual(client.calls, [])

    async def test_service_failure_and_unsafe_citation(self):
        for client in (None, Client(None), Client([Hit("private]link", "private")])):
            answer = await self.search(client, query="hours")
            self.assertEqual(set(answer), {"error"})
            self.assertNotIn("private", answer["error"])
