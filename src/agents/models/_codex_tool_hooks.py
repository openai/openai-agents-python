"""Connect a model attempt's Python callbacks to the native Codex tool dispatcher."""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import AsyncExitStack
from copy import deepcopy
from functools import partial
from typing import Any

from ..exceptions import UserError
from ..lifecycle import AgentHooksBase, RunHooksBase
from ..run_config import ToolCallHookContext
from ..run_internal.tool_hooks import _await_hook
from ..tool import NativeTool
from ..util._asyncio_tasks import gather_with_cancel
from ._codex_hook_relay import serve_hook
from ._run_context import ModelToolHooks

# Give the SDK time to send an explicit blocking decision before the native transport expires.
_NATIVE_HOOK_TIMEOUT = 600

# Fixed hook names from the pinned Codex revision. The binding checks the configured namespace.
_RESERVED_NATIVE_HOOK_NAMES = {
    "Bash",
    "apply_patch",
    "clockcurr_time",
    "clocksleep",
    "get_context_remaining",
    "image_genimagegen",
    "list_available_plugins_to_install",
    "list_mcp_resource_templates",
    "list_mcp_resources",
    "multi_agent_v1close_agent",
    "multi_agent_v1resume_agent",
    "multi_agent_v1send_input",
    "multi_agent_v1wait_agent",
    "new_context",
    "read_mcp_resource",
    "request_permissions",
    "request_plugin_install",
    "request_user_input",
    "request_user_input_async",
    "send_message_to_user_async",
    "skillsinstallations",
    "skillslist",
    "skillsread",
    "spawn_agent",
    "test_sync_tool",
    "tool_search",
    "update_plan",
    "view_image",
    "wait_for_environment",
    "webrun",
}


def _same_json(left: Any, right: Any) -> bool:
    # Python equates True with 1. Native MCP arguments and results preserve JSON types.
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False
    )


class CodexToolHooks:
    def __init__(self, bindings: ModelToolHooks, sdk_names: set[str], events: list[str]):
        self.bindings = bindings
        self.sdk_names = sdk_names
        self.events = events
        self.server_name = f"agents_hooks_{secrets.token_hex(8)}"
        self.key = secrets.token_hex(32)
        self.registered: set[tuple[str, int]] = set()
        self.error: BaseException | None = None

    @classmethod
    def for_run(
        cls, bindings: ModelToolHooks | None, identities: set[tuple[str | None, str]]
    ) -> CodexToolHooks | None:
        if bindings is None or (bindings.settings and bindings.settings.sdk_only_tool_hooks):
            return None
        settings = bindings.settings
        agent_hooks = bindings.agent.hooks
        events = []
        for event, callback, observer in (
            ("PreToolUse", settings and settings.prepare_tool_input, "on_tool_start"),
            ("PostToolUse", settings and settings.prepare_tool_output, "on_tool_end"),
        ):
            if (
                callback is not None
                or getattr(type(bindings.lifecycle), observer)
                is not getattr(RunHooksBase, observer)
                or (
                    agent_hooks is not None
                    and getattr(type(agent_hooks), observer)
                    is not getattr(AgentHooksBase, observer)
                )
            ):
                events.append(event)
        if not events:
            return None
        # Match Core's hook names, including the legacy name for multi_agent_v1.spawn_agent.
        sdk_names = {
            name
            if namespace is None
            or namespace == "functions"
            or (namespace == "multi_agent_v1" and name == "spawn_agent")
            else namespace + name
            for namespace, name in identities
        }
        reserved = sorted(
            name
            for name in sdk_names
            if name in _RESERVED_NATIVE_HOOK_NAMES or name.startswith("mcp__")
        )
        if reserved:
            raise UserError(
                f"Codex native hooks reserve {', '.join(reserved)}. Rename or namespace "
                "the SDK tool, or enable sdk_only_tool_hooks."
            )
        return cls(bindings, sdk_names, events)

    async def configure(self, resources: AsyncExitStack) -> dict[str, Any]:
        """Give the embedded binding this attempt's private callback configuration."""
        budget = _NATIVE_HOOK_TIMEOUT + 30
        handlers = {}
        for event in self.events:
            inputs = {
                "key": self.key,
                "event": event,
                "name": "${tool_name}",
                "call_id": "${tool_use_id}",
                "arguments": "${tool_input}",
            }
            if event == "PostToolUse":
                inputs["result"] = "${tool_response}"
            handlers[event] = {
                "hooks": [
                    {
                        "type": "mcp_tool",
                        "server": self.server_name,
                        "tool": "dispatch",
                        "input": inputs,
                        "timeout": budget,
                    }
                ]
            }
        port = await resources.enter_async_context(serve_hook(self.key, self.decision))
        return {
            "server": self.server_name,
            "handlers": handlers,
            "port": port,
            "key": self.key,
            "timeout": budget,
            "sdk_names": sorted(self.sdk_names),
        }

    def verify(self, thread: Any) -> None:
        self.registered = set(thread.tool_hook_orders(self.server_name))
        if {event for event, _ in self.registered} != {
            "pre_tool_use" if event == "PreToolUse" else "post_tool_use" for event in self.events
        }:
            raise UserError("Codex could not register the requested native tool hooks.")

    def check_completion(self, run: dict[str, Any]) -> None:
        if (
            run.get("source") != "session_flags"
            or (run.get("event_name"), run.get("display_order")) not in self.registered
        ):
            return
        if self.error is not None:
            raise self.error
        if run.get("status") == "failed":
            raise UserError("Codex could not apply the registered native tool hook.")

    async def decision(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            if self.error is not None or data["event"] not in self.events:
                raise UserError("The native tool hook could not run.")
            decision = await asyncio.wait_for(self._dispatch(data), _NATIVE_HOOK_TIMEOUT)
            json.dumps(decision, allow_nan=False)
            return decision
        except (Exception, asyncio.CancelledError) as exc:
            self.error = self.error or exc
            # A transport-level hook error lets Codex run the original tool. Block explicitly.
            message = "The registered tool hook did not complete."
            if data.get("event") == "PreToolUse":
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": message,
                    }
                }
            return {"continue": False, "stopReason": message}

    async def _dispatch(self, data: dict[str, Any]) -> dict[str, Any]:
        bindings = self.bindings
        name, arguments = data["name"], data["arguments"]
        if name in self.sdk_names:
            # SDK-owned calls reach the same callbacks when the runner dispatches them.
            return {}
        tool = NativeTool(name)
        call = ToolCallHookContext(
            bindings.context, bindings.agent, tool, data["call_id"], None, deepcopy(arguments)
        )
        settings = bindings.settings
        observers = tuple(
            hook for hook in (bindings.lifecycle, bindings.agent.hooks) if hook is not None
        )
        if data["event"] == "PreToolUse":
            decision = {}
            if settings and settings.prepare_tool_input:
                prepared = await _await_hook(
                    partial(settings.prepare_tool_input, call), "prepare_tool_input", name
                )
                if not isinstance(prepared, dict):
                    raise UserError("prepare_tool_input must return a dictionary of arguments.")
                if not _same_json(prepared, arguments):
                    decision = {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                            "updatedInput": prepared,
                        }
                    }
            await gather_with_cancel(
                *(hook.on_tool_start(bindings.context, bindings.agent, tool) for hook in observers)
            )
            return decision
        original = data["result"]
        prepared = original
        if settings and settings.prepare_tool_output:
            prepared = await _await_hook(
                partial(settings.prepare_tool_output, call, deepcopy(original)),
                "prepare_tool_output",
                name,
            )
        if _same_json(prepared, original):
            decision = {}
        elif isinstance(prepared, str):
            decision = {"continue": False, "stopReason": prepared}
        else:
            raise UserError(
                "Codex native output hooks must return the original result or replacement text."
            )
        await gather_with_cancel(
            *(
                hook.on_tool_end(bindings.context, bindings.agent, tool, prepared)
                for hook in observers
            )
        )
        return decision
