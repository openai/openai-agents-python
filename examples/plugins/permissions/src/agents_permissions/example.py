"""Offline record access with a simulated user, review and revocation."""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from agents import Agent, RunConfig, RunContextWrapper, Runner, RunResult, RunState
from agents.decorators import tool
from agents.plugins import Plugin, PluginHooks, ReplaceArguments, ToolCall
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext

from . import permissions


@dataclass(frozen=True)
class Records:
    tenant: str
    subject: str
    grants: dict[tuple[str, str, str], str]
    reads: list[str] = field(default_factory=list)

    def decision(self, record_id: str) -> str:
        if not self.tenant or not self.subject or not record_id:
            return "deny"
        return self.grants.get((self.tenant, self.subject, record_id), "deny")

    def read(self, record_id: str) -> str:
        if self.decision(record_id) not in ("allow", "review"):
            raise PermissionError("No access to this record.")
        self.reads.append(record_id)
        return f"Record {record_id}"


@tool(failure_error_function=None)
async def read_record(ctx: ToolContext[Records], record_id: str) -> str:
    """Read a record by ID."""
    return ctx.context.read(record_id)


def _is_read(call: ToolCall) -> bool:
    return (call.owner, call.namespace, call.name) == ("records", None, "read_record")


def _prepare(call: ToolCall) -> ReplaceArguments | None:
    if _is_read(call) and call.arguments == {"record_id": "latest"}:
        return ReplaceArguments({"record_id": "R42"})
    return None


records: Plugin[Records] = Plugin(
    name="records", tools=[read_record], hooks=PluginHooks(prepare_tool=_prepare)
)


def resolve(_agent: Agent[Any], policy: str, call: ToolCall, app: Records) -> dict[str, str]:
    if policy != "permissions" or not _is_read(call):
        return {}
    record_id = call.arguments.get("record_id")
    if not isinstance(record_id, str):
        return {}
    return {"decision": app.decision(record_id)}


async def resume_once(
    agent: Agent[Records],
    app: Records,
    saved: dict[tuple[str, str, str], str],
    request_id: str,
    config: RunConfig,
) -> RunResult:
    """Single-use lookup in a simulated, server-owned store."""
    snapshot = saved.pop((app.tenant, app.subject, request_id), None)
    if snapshot is None:
        raise PermissionError("No saved approval for this request.")
    restored = await RunState.from_string(
        agent,
        snapshot,
        context_override=RunContextWrapper(app),
        plugin_revision=config.plugin_revision,
    )
    return await Runner.run(agent, restored, run_config=config)


async def main() -> None:
    key = ("acme", "pat", "R42")
    app = Records(tenant="acme", subject="pat", grants={key: "review"})
    model = ScriptedModel(
        [
            [function_call("read_record", {"record_id": "latest"}, call_id="one")],
            [assistant_message("Done")],
        ]
    )
    agent = Agent[Records](name="Desk", model=model, plugins=[permissions(), records])
    config = RunConfig(policy_context=resolve, tracing_disabled=True)
    paused = await Runner.run(agent, "Read the latest record.", context=app, run_config=config)
    (item,) = paused.interruptions
    assert item.arguments and json.loads(item.arguments) == {"record_id": "R42"}
    state = paused.to_state()
    state.approve(item)
    del app.grants[key]
    finished = await Runner.run(agent, state, run_config=config)
    assert not finished.interruptions and not app.reads
    print("0 reads of R42 after approval and revocation.")


if __name__ == "__main__":
    asyncio.run(main())
