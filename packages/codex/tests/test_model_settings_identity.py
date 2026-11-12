"""Public model settings keep their identity and detached capture semantics."""

from __future__ import annotations

import json
import pickle
from dataclasses import FrozenInstanceError
from typing import get_type_hints
from unittest import TestCase, main

from agents import Agent, ModelRetrySettings, ModelSettings, ToolRef
from agents.agent import ModelSettings as PublicSettings, _copy_model_settings


class ModelSettingsCaptureTests(TestCase):
    def test_public_identity_and_annotations(self):
        from _agents_codex import _model_settings
        from _agents_codex.agent import (
            ModelSettings as OwnerSettings,
            _copy_model_settings as owner_copy,
        )
        from agents.retry import ModelRetrySettings as RetrySettings

        self.assertIs(ModelSettings, PublicSettings)
        self.assertIs(ModelSettings, OwnerSettings)
        self.assertIs(_copy_model_settings, owner_copy)
        self.assertIs(_model_settings.ModelRetrySettings, RetrySettings)
        self.assertEqual(ModelSettings.__module__, "agents.agent")
        self.assertEqual(_copy_model_settings.__module__, "agents.agent")
        self.assertIs(pickle.loads(pickle.dumps(ModelSettings)), ModelSettings)
        self.assertIs(pickle.loads(pickle.dumps(_copy_model_settings)), _copy_model_settings)
        self.assertIs(type(ModelSettings(reasoning_effort="high")), dict)
        hints = get_type_hints(ModelSettings)
        self.assertIs(hints["reasoning_effort"], str)
        self.assertIn(ToolRef, hints["tool_choice"].__args__)
        self.assertIn(ModelRetrySettings, hints["retry"].__args__)
        self.assertEqual(hints["attempt_timeout_ms"], int | None)
        self.assertIs(get_type_hints(_copy_model_settings)["return"], ModelSettings)
        self.assertIs(
            eval(
                Agent.__annotations__["model_settings"],
                vars(__import__("agents.agent", fromlist=["ModelSettings"])),
            ),
            ModelSettings,
        )
        self.assertNotIn("ModelSettings", vars(_model_settings))
        self.assertNotIn("Agent", vars(_model_settings))
        self.assertNotIn("Session", vars(_model_settings))

    def test_detached_public_capture(self):
        metadata = {"owner": "before"}
        include = ["reasoning.encrypted_content"]
        extra_body = {"custom": {"values": [1]}}
        context = [{"type": "compaction", "compact_threshold": 1000}]
        modalities = ["text", "image"]
        tool = ToolRef("lookup", "catalog")
        agent = Agent(
            instructions="Synthetic.",
            model_settings={
                "metadata": metadata,
                "include": include,
                "extra_body": extra_body,
                "context_management": context,
                "input_modalities": modalities,
                "tool_choice": tool,
                "service_tier": None,
            },
        )
        metadata["owner"] = "after"
        include.clear()
        extra_body["custom"]["values"].append(2)
        context[0]["compact_threshold"] = 2000
        modalities.clear()
        self.assertEqual(agent.model_settings["metadata"], {"owner": "before"})
        self.assertEqual(agent.model_settings["include"], ["reasoning.encrypted_content"])
        self.assertEqual(agent.model_settings["extra_body"], {"custom": {"values": [1]}})
        self.assertEqual(
            agent.model_settings["context_management"],
            [{"type": "compaction", "compact_threshold": 1000}],
        )
        self.assertEqual(agent.model_settings["input_modalities"], ["text", "image"])
        self.assertEqual(agent.model_settings["tool_choice"], tool)
        self.assertIsNot(agent.model_settings["tool_choice"], tool)
        self.assertIsNone(agent.model_settings["service_tier"])
        self.assertNotIn("temperature", agent.model_settings)

    def test_retry_identity_timeout_capture_and_json_omission(self):
        def policy(context):
            return False

        retry = ModelRetrySettings(max_retries=2, policy=policy)
        agent = Agent(
            instructions="Synthetic.",
            model_settings={
                "retry": retry,
                "attempt_timeout_ms": 11,
                "invocation_timeout_ms": 100,
            },
        )
        self.assertIs(agent.model_settings["retry"], retry)
        self.assertIs(retry.policy, policy)
        self.assertEqual(agent.model_settings["attempt_timeout_ms"], 11)
        self.assertEqual(agent.model_settings["invocation_timeout_ms"], 100)
        self.assertEqual(json.loads(json.dumps(retry.to_json_dict())), {"max_retries": 2})
        with self.assertRaises(FrozenInstanceError):
            retry.max_retries = 3
        cleared = Agent(
            instructions="Synthetic.",
            model_settings={
                "retry": None,
                "attempt_timeout_ms": None,
                "invocation_timeout_ms": None,
            },
        )
        self.assertEqual(
            dict(cleared.model_settings),
            {
                "retry": None,
                "attempt_timeout_ms": None,
                "invocation_timeout_ms": None,
            },
        )

    def test_opening_retry_merge_clear_and_hosted_omission(self):
        from _agents_codex._opening import _capture_opening_agent
        from agents import function_tool
        from agents.retry import RetryBackoffSettings

        @function_tool
        def lookup() -> str:
            return "Synthetic."

        def policy(context):
            return False

        backoff = RetryBackoffSettings(initial_delay_ms=17)
        retry = ModelRetrySettings(max_retries=2, backoff=backoff, policy=policy)
        agent = Agent(hosted_prompt={"id": "pmpt_synthetic"}, model_settings={"retry": retry})
        captured = _capture_opening_agent(
            agent,
            provider=None,
            model=None,
            model_settings={"retry": ModelRetrySettings(max_retries=3)},
            tool_selection=None,
        )
        merged = captured.model_settings["retry"]
        self.assertEqual(merged.max_retries, 3)
        self.assertIs(merged.backoff, backoff)
        self.assertIs(merged.policy, policy)
        self.assertIs(agent.model_settings["retry"], retry)
        self.assertFalse(captured._hosted_instructions_provided)
        self.assertFalse(captured._hosted_tools_provided)
        cleared = _capture_opening_agent(
            agent,
            provider=None,
            model=None,
            model_settings={"retry": None},
            tool_selection=[],
        )
        self.assertIsNone(cleared.model_settings["retry"])
        self.assertTrue(cleared._hosted_tools_provided)
        with_tools = Agent(instructions="Synthetic.", tools=[lookup])
        narrowed = _capture_opening_agent(
            with_tools,
            provider=None,
            model=None,
            model_settings=None,
            tool_selection=[],
        )
        self.assertEqual(tuple(narrowed.tools), ())
        self.assertEqual(tuple(with_tools.tools), (lookup,))

    def test_validation_order_at_public_capture(self):
        cases = [
            ({"unknown": 1, "invocation_timeout_ms": 0}, ValueError, "Unsupported model"),
            (
                {"attempt_timeout_ms": 0, "invocation_timeout_ms": 0},
                ValueError,
                "invocation_timeout_ms",
            ),
            ({"retry": {}, "attempt_timeout_ms": 0}, ValueError, "attempt_timeout_ms"),
            ({"retry": {}}, TypeError, "retry must be ModelRetrySettings"),
            ({"attempt_timeout_ms": True}, ValueError, "attempt_timeout_ms"),
            ({"attempt_timeout_ms": 2**32}, ValueError, "attempt_timeout_ms"),
        ]
        for settings, error, message in cases:
            with self.subTest(settings=settings), self.assertRaisesRegex(error, message):
                Agent(instructions="Synthetic.", model_settings=settings)


if __name__ == "__main__":
    main()
