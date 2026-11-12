"""Explicit hosted choices keep captured function references separate."""

from __future__ import annotations

import json
from unittest import TestCase

from _agents_codex.session import _native_model_settings
from agents import HostedToolChoice, ToolRef
from agents.agent import _copy_model_settings


class HostedToolChoiceTests(TestCase):
    def test_projection_and_named_function_compatibility(self):
        choice: HostedToolChoice = {"type": "web_search"}
        self.assertEqual(
            _native_model_settings(None, {"tool_choice": choice}),
            {
                "tool_choice": "required",
                "request_controls": {
                    "named_tool_choice": None,
                    "hosted_tool_choice": {"type": "web_search"},
                },
            },
        )
        self.assertEqual(
            _native_model_settings(None, {"tool_choice": ToolRef("web_search", "application")}),
            {
                "tool_choice": "required",
                "request_controls": {
                    "named_tool_choice": {"name": "web_search", "namespace": "application"},
                },
            },
        )

    def test_detached_capture_and_portable_control_json(self):
        source = {"type": "mcp", "server_label": "captured", "name": "lookup"}
        captured = _copy_model_settings({"tool_choice": source})
        source["server_label"] = "changed"
        controls = _native_model_settings(None, captured)["request_controls"]
        self.assertEqual(
            controls["hosted_tool_choice"],
            {"type": "mcp", "server_label": "captured", "name": "lookup"},
        )
        self.assertEqual(json.loads(json.dumps(controls)), controls)

    def test_selector_rejects_function_shape_and_unknown_fields(self):
        for choice in [
            {"type": "function", "name": "web_search"},
            {"type": "web_search", "name": "hidden"},
            {"type": "mcp", "server_label": " "},
            {"type": "unsupported"},
        ]:
            with self.subTest(choice=choice), self.assertRaisesRegex(ValueError, "hosted"):
                _copy_model_settings({"tool_choice": choice})
