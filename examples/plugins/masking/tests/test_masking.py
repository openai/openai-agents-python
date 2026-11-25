import json
import unittest

from agents_masking import masking

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.plugins import Plugin
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call


def echo_result(call):
    item = next(item for item in call.input if item.get("type") == "function_call_output")
    return [assistant_message(item["output"])]


async def read(payload, plugin, *, stream=False):
    @tool
    def lookup():
        return payload

    model = ScriptedModel(
        [[function_call("lookup", {}, call_id="lookup-1")], ModelStep.respond(echo_result)]
    )
    agent = Agent("Desk", model=model, plugins=[Plugin(name="desk", tools=[lookup]), plugin])
    config = RunConfig(tracing_disabled=True)
    if stream:
        result = Runner.run_streamed(agent, "Read the record", run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "Read the record", run_config=config)
    model.assert_complete()
    return result.final_output


class MaskingTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_turn_receives_masked_text_in_both_run_styles(self):
        policy = {"lookup": ["demo", "demo.42"]}
        plugin = masking(policy)
        policy["lookup"].clear()
        for stream in (False, True):
            with self.subTest(stream=stream):
                answer = await read("demo.42 DEMO demo", plugin, stream=stream)
                self.assertEqual(answer, "[REDACTED] DEMO [REDACTED]")

    async def test_other_tool_untouched(self):
        self.assertEqual(await read("demo", masking({"other": ["demo"]})), "demo")

    async def test_json_text(self):
        plugin = masking({"lookup": ["demo.42", "1234"]})
        answer = await read('{"value":"demo.42","visible":"demoX42"}', plugin)
        self.assertEqual(json.loads(answer), {"value": "[REDACTED]", "visible": "demoX42"})
        answer = await read('{"value":1234}', plugin)
        self.assertEqual(json.loads(answer), "[REDACTED]")

    async def test_new_match_at_marker_edge_withholds_result(self):
        answer = await read("prefixDemo", masking({"lookup": ["prefix[", "Demo"]}))
        self.assertEqual(json.loads(answer), "[REDACTED]")

    async def test_structured_results_bypass_the_filter(self):
        plugin = masking({"lookup": ["demo.42"]})
        for payload in ({"value": "demo.42"}, ["demo.42"]):
            with self.subTest(payload=payload):
                self.assertIn("demo.42", await read(payload, plugin))

    def test_invalid_policy_rejected_at_construction(self):
        for policy in (
            {},
            {"": ["demo"]},
            {"lookup": "demo"},
            {"lookup": []},
            {"lookup": [""]},
            {"lookup": ["REDACTED"]},
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                masking(policy)
