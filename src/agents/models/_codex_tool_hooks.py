"""Connect a model attempt's Python callbacks to the native Codex tool dispatcher."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from contextlib import AsyncExitStack
from copy import copy, deepcopy
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
    def __init__(
        self, bindings: ModelToolHooks, sdk_names: set[str], events: list[str], required: list[str]
    ):
        self.bindings = bindings
        self.settings = copy(bindings.settings)
        self.observers = tuple(
            hook for hook in (bindings.lifecycle, bindings.agent.hooks) if hook is not None
        )
        self.sdk_names = sdk_names
        self.events = events
        self.server_name = f"agents_hooks_{secrets.token_hex(8)}"
        self.key = secrets.token_hex(32)
        self.registered: set[tuple[str, int]] = set()
        self.error: BaseException | None = None
        self.prepared_inputs: dict[str, dict[str, Any] | None] = {}
        self.prepared_outputs: dict[str, str] = {}
        self.required = required
        self._proposal: tuple[str, str, str] | None = None
        self._restoring: tuple[str, str] | None = None
        self._output_seen = False

    @classmethod
    def for_run(
        cls, bindings: ModelToolHooks | None, identities: set[tuple[str | None, str]]
    ) -> CodexToolHooks | None:
        if bindings is None or (bindings.settings and bindings.settings.sdk_only_tool_hooks):
            return None
        settings = bindings.settings
        agent_hooks = bindings.agent.hooks
        events = []
        required = []
        for event, callback, observer in (
            ("PreToolUse", "prepare_tool_input", "on_tool_start"),
            ("PostToolUse", "prepare_tool_output", "on_tool_end"),
        ):
            hooks = []
            if settings and getattr(settings, callback) is not None:
                hooks.append(callback)
            if getattr(type(bindings.lifecycle), observer) is not getattr(RunHooksBase, observer):
                hooks.append("run." + observer)
            if agent_hooks is not None and getattr(type(agent_hooks), observer) is not getattr(
                AgentHooksBase, observer
            ):
                hooks.append("agent." + observer)
            if hooks:
                events.append(event)
                required.extend(hooks)
        if not events:
            return None
        if settings and settings.prepare_tool_input is not None and "PostToolUse" not in events:
            # Observe Core's effective arguments after it preserves environment selectors.
            events.append("PostToolUse")
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
        return cls(bindings, sdk_names, events, required)

    def prepare_pending(self, item: Any, input_hash: str | None) -> None:
        """Bind the displayed call and receipt to the input Core actually accepted."""
        name = "Bash" if item.name == "exec_command" else "apply_patch"
        proposal = self._proposal
        if "PreToolUse" not in self.events:
            command = json.loads(item.arguments)["cmd"] if name == "Bash" else item.input
            proposal = (item.call_id, name, command)
        if (
            self.error is not None
            or proposal is None
            or proposal[:2] != (item.call_id, name)
            or not isinstance(proposal[2], str)
            or hashlib.sha256(proposal[2].encode()).hexdigest() != input_hash
        ):
            raise UserError("Codex could not confirm the prepared input for this approval.")
        if item.call_id in self.prepared_inputs:
            self.prepared_inputs[item.call_id] = {"command": proposal[2]}
            self.prepare_history_item(item)
            del self.prepared_inputs[item.call_id]

    def restore(self, call: dict[str, Any]) -> None:
        # Keep the pre-tool decision and confirm the result on the replacement worker.
        if "PostToolUse" not in self.events:
            self.events.append("PostToolUse")
        self._restoring = (
            call["call_id"],
            "Bash" if call["name"] == "exec_command" else "apply_patch",
        )

    def restored_output(self, output: str) -> str:
        if self.error is not None or not self._output_seen or self._restoring is None:
            raise UserError(
                "Could not prepare the restored Codex result. Check the effect before retrying."
            )
        return self.prepared_outputs.get(self._restoring[0], output)

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
            # Keep the original exception in Python and send a payload-free denial.
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
        if self._restoring is not None and (
            self._restoring != (data["call_id"], name) or self._output_seen
        ):
            raise UserError("Unexpected hook for a restored Codex call.")
        if name in self.sdk_names:
            # SDK-owned calls reach the same callbacks when the runner dispatches them.
            return {}
        tool = NativeTool(name)
        if data["event"] == "PostToolUse" and data["call_id"] in self.prepared_inputs:
            self.prepared_inputs[data["call_id"]] = deepcopy(arguments)
        call = ToolCallHookContext(
            bindings.context, bindings.agent, tool, data["call_id"], None, deepcopy(arguments)
        )
        settings = self.settings
        if data["event"] == "PreToolUse":
            decision: dict[str, Any] = {}
            prepared = arguments
            # The user approved the prepared input. Never transform it a second time.
            if self._restoring is None and settings and settings.prepare_tool_input is not None:
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
                    self.prepared_inputs[data["call_id"]] = None
            await gather_with_cancel(
                *(
                    hook.on_tool_start(bindings.context, bindings.agent, tool)
                    for hook in self.observers
                )
            )
            if bindings.resumable and name in ("Bash", "apply_patch"):
                self._proposal = (data["call_id"], name, prepared["command"])
            return decision
        original = data["result"]
        prepared = original
        if settings and settings.prepare_tool_output is not None:
            prepared = await _await_hook(
                partial(settings.prepare_tool_output, call, deepcopy(original)),
                "prepare_tool_output",
                name,
            )
        if _same_json(prepared, original):
            decision = {}
        elif isinstance(prepared, str):
            self.prepared_outputs[data["call_id"]] = prepared
            decision = {"continue": False, "stopReason": prepared}
        else:
            raise UserError(
                "Codex native output hooks must return the original result or replacement text."
            )
        await gather_with_cancel(
            *(
                hook.on_tool_end(bindings.context, bindings.agent, tool, prepared)
                for hook in self.observers
            )
        )
        if self._restoring is not None:
            # This turn never samples the model. Keep Core's exit evidence intact.
            # The provider returns only restored_output() to the Runner.
            self._output_seen = True
            return {}
        return decision

    def prepare_history_item(self, item: Any) -> None:
        """Keep persisted native history on the payloads approved by this attempt's hooks."""
        call_id = getattr(item, "call_id", None)
        if item.type in ("function_call", "custom_tool_call") and call_id in self.prepared_inputs:
            prepared = self.prepared_inputs[call_id]
            if prepared is None:
                raise UserError(
                    "Codex cannot replay a rewritten native tool call without confirmed effective "
                    "arguments. The native tool did not complete PostToolUse."
                )
            if item.type == "function_call":
                if item.name == "exec_command":
                    arguments = json.loads(item.arguments)
                    arguments["cmd"] = prepared["command"]
                else:
                    arguments = prepared
                item.arguments = json.dumps(arguments, ensure_ascii=False)
            else:
                item.input = (
                    prepared["command"] if item.name == "apply_patch" else prepared["input"]
                )
        elif item.type in ("function_call_output", "custom_tool_call_output"):
            if call_id in self.prepared_outputs:
                item.output = self.prepared_outputs[call_id]
