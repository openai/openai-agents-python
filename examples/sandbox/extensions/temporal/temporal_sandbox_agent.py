"""Temporal Sandbox agent example.

Runs a SandboxAgent as a durable Temporal workflow.  The workflow is long-lived
and conversational: after processing each turn it idles waiting for the next
user message.  Workflows persist indefinitely in Temporal.  A separate session
manager workflow (``temporal_session_manager.py``) orchestrates session
creation, destruction, and discovery.

Usage
-----
Install the Temporal extra first::

    uv sync --extra temporal --extra daytona

Start a local Temporal server (requires the Temporal CLI)::

    temporal server start-dev

In one terminal, start the worker::

    python examples/sandbox/extensions/temporal_sandbox_agent.py worker

In another terminal, start the TUI::

    python examples/sandbox/extensions/temporal_sandbox_agent.py run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os as _os
import sys
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, SerializeAsAny, field_validator, model_serializer
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.openai_agents.workflow import (  # type: ignore[attr-defined]
    temporal_sandbox_client,
)
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from agents import ModelSettings, Runner
from agents.agent import Agent
from agents.extensions.sandbox import (
    DaytonaSandboxClientOptions,
    DaytonaSandboxSessionState,
    E2BSandboxClientOptions,
    E2BSandboxSessionState,
)
from agents.items import (
    MessageOutputItem,
    RunItem,
    ToolApprovalItem,
    ToolCallItem,
    TResponseInputItem,
)
from agents.lifecycle import RunHooksBase
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.sandboxes import (
    DockerSandboxClientOptions,
    DockerSandboxSessionState,
    UnixLocalSandboxClientOptions,
    UnixLocalSandboxSessionState,
)
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import SnapshotBase

# Allow sibling and repo-root imports.
_THIS_DIR = _os.path.dirname(_os.path.abspath(__file__))
_REPO_ROOT = _os.path.abspath(_os.path.join(_THIS_DIR, "..", "..", "..", ".."))
for _p in (_THIS_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability  # noqa: E402


class SandboxBackend(str, Enum):
    DAYTONA = "daytona"
    DOCKER = "docker"
    E2B = "e2b"
    LOCAL = "local"


DEFAULT_BACKEND = SandboxBackend.DAYTONA
TASK_QUEUE = "sandbox-agent-queue"


class _AlwaysSerializeType(BaseModel):
    """Base that ensures the ``type`` discriminator survives ``exclude_unset`` round-trips."""

    @model_serializer(mode="wrap")
    def _serialize_always_include_type(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        data["type"] = self.type  # type: ignore[attr-defined]
        return data


class SwitchToLocalBackend(_AlwaysSerializeType):
    """Switch target for the local unix sandbox backend."""

    type: Literal["local"] = "local"
    workspace_root: str = "/workspace"


class SwitchBackendSignal(BaseModel):
    """Payload for the ``switch_backend`` signal."""

    target: Literal["daytona", "docker", "e2b"] | SwitchToLocalBackend


# ---------------------------------------------------------------------------
# Workflow input / output types
# ---------------------------------------------------------------------------


class _HasSnapshot(BaseModel):
    @field_validator("snapshot", mode="before", check_fields=False)
    @classmethod
    def _parse_snapshot(cls, v: object) -> SnapshotBase | None:
        if v is None or isinstance(v, SnapshotBase):
            return v
        return SnapshotBase.parse(v)


class WorkflowSnapshot(_HasSnapshot):
    """Atomic snapshot of an agent workflow's forkable state."""

    sandbox_session_state: (
        DaytonaSandboxSessionState
        | DockerSandboxSessionState
        | E2BSandboxSessionState
        | UnixLocalSandboxSessionState
        | None
    ) = None
    snapshot: SerializeAsAny[SnapshotBase] | None = (
        None  # serialized SnapshotBase for cross-backend creation
    )
    previous_response_id: str | None = None
    history: list[dict[str, Any]] = []


class AgentRequest(_HasSnapshot):
    messages: list[dict[str, Any]]
    cwd: str = ""
    backend: str = "daytona"  # SandboxBackend value — determines client options
    sandbox_session_state: (
        DaytonaSandboxSessionState
        | DockerSandboxSessionState
        | E2BSandboxSessionState
        | UnixLocalSandboxSessionState
        | None
    ) = None
    snapshot: SerializeAsAny[SnapshotBase] | None = (
        None  # serialized SnapshotBase for cross-backend creation
    )
    previous_response_id: str | None = None
    history: list[dict[str, Any]] = []  # conversation history to seed (e.g. when forking)
    manifest: Manifest | None = None  # per-session manifest override


class AgentResponse(BaseModel):
    """Returned when the workflow is destroyed."""

    pass


class ToolCallRecord(BaseModel):
    """A single tool call with its input and output for TUI display."""

    tool_name: str
    description: str
    arguments_json: str
    output: str | None = None
    requires_approval: bool = False
    approved: bool | None = None


class ChatResponse(BaseModel):
    """Structured response from chat() replacing the plain string."""

    text: str | None = None
    tool_calls: list[ToolCallRecord] = []
    approval_request: ToolCallRecord | None = None


class LiveToolCall(BaseModel):
    """A tool call visible to the TUI during an active turn."""

    call_id: str
    tool_name: str
    arguments: str
    status: str = "pending"  # pending | running | completed
    output: str | None = None


class TurnState(BaseModel):
    """Everything the TUI needs — returned by a single query during polling."""

    # idle | thinking | awaiting_approval | complete
    status: str = "idle"
    tool_calls: list[LiveToolCall] = []
    response_text: str | None = None
    approval_request: ToolCallRecord | None = None
    turn_id: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_approval_item(item: ToolApprovalItem) -> str:
    """Return a human-readable summary of a tool approval request."""
    raw = item.raw_item
    name = getattr(raw, "name", None) or item.tool_name or "unknown"

    # Try to extract arguments for shell commands
    args_str = getattr(raw, "arguments", None)
    if args_str and isinstance(args_str, str):
        try:
            parsed = json.loads(args_str)
            if name == "shell" and "commands" in parsed:
                cmds = parsed["commands"]
                return f"shell: {'; '.join(cmds)}"
        except (json.JSONDecodeError, TypeError):
            pass

    return f"{name}: {args_str or '(no args)'}"


def _extract_text_from_items(items: list[RunItem]) -> str | None:
    """Pull the last assistant text from generated run items."""
    for item in reversed(items):
        if isinstance(item, MessageOutputItem):
            raw = item.raw_item
            content = getattr(raw, "content", [])
            if isinstance(content, list):
                for block in content:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        return text
    return None


def _tool_call_records_from_items(items: list[RunItem]) -> list[ToolCallRecord]:
    """Build ToolCallRecord list from generated RunItems."""
    records: list[ToolCallRecord] = []
    for item in items:
        if isinstance(item, ToolCallItem):
            raw = item.raw_item
            name = getattr(raw, "name", None) or "unknown"
            args = getattr(raw, "arguments", "{}")
            records.append(
                ToolCallRecord(
                    tool_name=name,
                    description=f"{name}: {args}",
                    arguments_json=args if isinstance(args, str) else json.dumps(args),
                )
            )
    return records


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------


class _LiveStateHooks(RunHooksBase[Any, Agent[Any]]):
    """RunHooks that update workflow-queryable state for live TUI polling."""

    def __init__(self, wf: AgentWorkflow) -> None:
        self._wf = wf

    async def on_llm_end(self, context, agent, response):
        """Extract tool calls from the model response and register them."""
        for item in response.output:
            call_id = getattr(item, "call_id", None)
            if not call_id:
                continue
            # Standard function calls have name + arguments
            name = getattr(item, "name", None)
            if name:
                self._wf._live_tool_calls.append(
                    LiveToolCall(
                        call_id=call_id,
                        tool_name=name,
                        arguments=getattr(item, "arguments", None) or "{}",
                        status="pending",
                    )
                )
                continue
            # Shell tool calls have action.commands / action.command
            action = getattr(item, "action", None)
            if action:
                cmds = getattr(action, "commands", None) or getattr(action, "command", None)
                if isinstance(cmds, list):
                    args = json.dumps({"commands": cmds})
                elif isinstance(cmds, str):
                    args = json.dumps({"command": cmds})
                else:
                    args = "{}"
                tool_name = getattr(item, "type", None) or "shell"
                self._wf._live_tool_calls.append(
                    LiveToolCall(
                        call_id=call_id,
                        tool_name=tool_name,
                        arguments=args,
                        status="pending",
                    )
                )

    async def on_tool_start(self, context, agent, tool):
        # Match first pending tool call (tools execute in order)
        for tc in self._wf._live_tool_calls:
            if tc.status == "pending":
                tc.status = "running"
                break

    async def on_tool_end(self, context, agent, tool, result):
        # Match first running tool call
        for tc in self._wf._live_tool_calls:
            if tc.status == "running":
                tc.status = "completed"
                tc.output = result[:4000] if result else None
                break


@workflow.defn
class AgentWorkflow:
    """A long-lived conversational agent workflow.

    The workflow persists indefinitely in Temporal, idling between TUI
    sessions.  It only terminates when explicitly destroyed via the
    ``destroy`` signal (sent by the session manager).
    """

    def __init__(self) -> None:
        self._pending_messages: list[str] = []
        self._done = False
        self._conversation_history: list[dict[str, Any]] = []
        self._sandbox_session_state: (
            DaytonaSandboxSessionState
            | DockerSandboxSessionState
            | E2BSandboxSessionState
            | UnixLocalSandboxSessionState
            | None
        ) = None
        self._previous_response_id: str | None = None
        self._paused: bool = False
        self._pause_requested = False
        self._turn_tool_calls: list[ToolCallRecord] = []
        self._manifest_override: Manifest | None = None
        self._backend: SandboxBackend = DEFAULT_BACKEND
        self._snapshot: SnapshotBase | None = None
        self._live_tool_calls: list[LiveToolCall] = []
        # Turn state — queried by the TUI polling loop
        self._turn_status: str = "idle"
        self._turn_id: int = 0
        self._last_response_text: str | None = None
        self._pending_approval: ToolCallRecord | None = None

    @workflow.query
    def is_paused(self) -> bool:
        return self._paused

    @workflow.signal
    async def send_message(self, msg: str) -> None:
        """Enqueue a user message.  The TUI drives everything via get_turn_state polling."""
        self._pending_messages.append(msg)
        self._conversation_history.append({"role": "user", "content": msg})

    @workflow.query
    def get_history(self) -> list[dict[str, Any]]:
        """Return conversation history for TUI replay on reconnect."""
        return self._conversation_history

    @workflow.query
    def get_snapshot_id(self) -> str | None:
        """Return just the current snapshot ID (lightweight)."""
        if self._sandbox_session_state:
            return self._sandbox_session_state.snapshot.id
        return None

    @workflow.query
    def get_snapshot(self) -> WorkflowSnapshot:
        """Return an atomic snapshot of run state and conversation history."""
        # Prefer the live session snapshot, but fall back to self._snapshot
        # so workspace state survives a backend switch (which clears
        # _sandbox_session_state) until the next turn recreates a session.
        snapshot = self._snapshot
        if self._sandbox_session_state:
            snapshot = self._sandbox_session_state.snapshot
        return WorkflowSnapshot(
            sandbox_session_state=self._sandbox_session_state,
            snapshot=snapshot,
            previous_response_id=self._previous_response_id,
            history=self._conversation_history,
        )

    @workflow.query
    def get_turn_state(self) -> TurnState:
        """Single query that returns everything the TUI needs."""
        return TurnState(
            status=self._turn_status,
            tool_calls=list(self._live_tool_calls),
            response_text=self._last_response_text,
            approval_request=self._pending_approval,
            turn_id=self._turn_id,
        )

    @workflow.update
    async def pause(self) -> None:
        """Request the workflow to pause."""
        if self._paused:
            return
        self._pause_requested = True
        await workflow.wait_condition(lambda: self._paused)

    @workflow.update
    async def switch_backend(self, args: SwitchBackendSignal) -> None:
        """Switch to a different sandbox backend for subsequent turns.

        Clears the backend-specific session state so the next turn creates a
        fresh session on the new backend.  The portable snapshot is preserved
        so the workspace filesystem can be carried over.
        """
        match args.target:
            case "daytona":
                self._backend = SandboxBackend.DAYTONA
                self._manifest_override = Manifest(root="/home/daytona/workspace")
            case "docker":
                self._backend = SandboxBackend.DOCKER
                self._manifest_override = Manifest(root="/workspace")
            case "e2b":
                self._backend = SandboxBackend.E2B
                self._manifest_override = Manifest()  # E2B resolves relative to sandbox home
            case SwitchToLocalBackend(workspace_root=root):
                self._backend = SandboxBackend.LOCAL
                self._manifest_override = Manifest(root=root)
        self._sandbox_session_state = None

    @workflow.signal
    async def destroy(self) -> None:
        """Terminate the workflow permanently."""
        self._done = True

    def _resolve_sandbox_options(
        self,
    ) -> (
        DaytonaSandboxClientOptions
        | DockerSandboxClientOptions
        | E2BSandboxClientOptions
        | UnixLocalSandboxClientOptions
    ):
        match self._backend:
            case SandboxBackend.DAYTONA:
                return DaytonaSandboxClientOptions(pause_on_exit=False)
            case SandboxBackend.DOCKER:
                return DockerSandboxClientOptions(image="python:3.14")
            case SandboxBackend.E2B:
                return E2BSandboxClientOptions(sandbox_type="e2b")
            case SandboxBackend.LOCAL:
                return UnixLocalSandboxClientOptions()

    def _resolve_manifest(self) -> Manifest:
        match self._backend:
            case SandboxBackend.DAYTONA:
                return Manifest(root="/home/daytona/workspace")
            case SandboxBackend.DOCKER:
                return Manifest(root="/workspace")
            case SandboxBackend.E2B:
                return Manifest()  # E2B resolves workspace root relative to the sandbox home
            case SandboxBackend.LOCAL:
                return Manifest(root="/workspace")

    @workflow.run
    async def run(self, request: AgentRequest) -> AgentResponse:
        self._backend = SandboxBackend(request.backend)
        self._snapshot = request.snapshot
        if request.history:
            self._conversation_history = list(request.history)
        if request.sandbox_session_state:
            self._sandbox_session_state = request.sandbox_session_state
        if request.previous_response_id:
            self._previous_response_id = request.previous_response_id

        self._manifest_override = request.manifest

        while not self._done:
            await workflow.wait_condition(
                lambda: (len(self._pending_messages) > 0 or self._pause_requested or self._done),
            )

            if self._pause_requested:
                # Let the caller (e.g. SessionManagerWorkflow.fork_session) know
                # no turn is in progress so it can safely snapshot state.
                self._paused = True
                self._pause_requested = False
                await workflow.wait_condition(lambda: len(self._pending_messages) > 0 or self._done)
                self._paused = False

            if self._done:
                break

            user_messages = list(self._pending_messages)
            self._pending_messages.clear()

            self._turn_id += 1
            self._turn_status = "thinking"
            self._live_tool_calls = []
            self._pending_approval = None
            self._last_response_text = None

            try:
                manifest = self._manifest_override or self._resolve_manifest()
                agent = self._build_agent(manifest)
                await self._run_turn(agent, user_messages)
                self._last_response_text = self._last_text
                if self._last_text:
                    self._conversation_history.append(
                        {"role": "assistant", "content": self._last_text}
                    )
            except Exception as e:
                self._last_response_text = f"Error: {e}"
            finally:
                self._turn_status = "complete"

        return AgentResponse()

    def _build_agent(self, manifest: Manifest, model: str = "gpt-5.4") -> SandboxAgent:
        """Construct the SandboxAgent used by the workflow."""
        return SandboxAgent(
            name="Temporal Sandbox Agent",
            model=model,
            instructions=(
                "You are a helpful coding assistant. Inspect the workspace and answer "
                "questions. Use the shell tool to run commands. "
                "Do not invent files or statuses that are not present in the workspace. "
                "Cite the file names you inspected."
            ),
            default_manifest=manifest,
            capabilities=[WorkspaceShellCapability()],
            model_settings=ModelSettings(tool_choice="auto"),
        )

    async def _run_turn(
        self,
        agent: SandboxAgent,
        user_messages: list[str],
    ) -> None:
        self._turn_tool_calls = []
        self._last_text: str | None = None

        hooks = _LiveStateHooks(self)

        # Always pass fresh input — previous_response_id gives the API
        # conversation context. Sandbox session state is carried via
        # run_config.sandbox.session_state to preserve the sandbox across turns.
        if len(user_messages) == 1:
            input_arg: str | list[TResponseInputItem] = user_messages[0]
        else:
            input_arg = [{"role": "user", "content": m} for m in user_messages]

        run_config = RunConfig(
            sandbox=SandboxRunConfig(
                client=temporal_sandbox_client(self._backend.value),
                options=self._resolve_sandbox_options(),
                # Restore sandbox session state from the previous turn if available.
                session_state=self._sandbox_session_state,
                snapshot=self._snapshot,
            ),
            workflow_name="Temporal Sandbox workflow",
        )

        # Run the agent -- loops internally handling tool calls
        result = await Runner.run(
            agent,
            input_arg,
            run_config=run_config,
            hooks=hooks,
            previous_response_id=self._previous_response_id,
        )

        # Extract results
        self._turn_tool_calls.extend(_tool_call_records_from_items(result.new_items))
        self._last_text = _extract_text_from_items(result.new_items)

        # Track response ID for conversation continuity and save state
        # to preserve sandbox session across turns.
        self._previous_response_id = result.last_response_id

        # Persist sandbox session state for the next turn.
        try:
            state = result.to_state()
            sandbox_data = state.to_json().get("sandbox", {})
            session_state_data = sandbox_data.get("session_state")
            if session_state_data:
                self._sandbox_session_state = cast(
                    DaytonaSandboxSessionState | UnixLocalSandboxSessionState,
                    SandboxSessionState.parse(session_state_data),
                )
                # Keep the portable snapshot up to date so it can seed a
                # fresh session after a backend switch.
                self._snapshot = self._sandbox_session_state.snapshot
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Worker entrypoint
# ---------------------------------------------------------------------------


async def run_worker() -> None:
    # Imported here to avoid unnecessary passthroughs in the workflow sandbox.
    import docker  # type: ignore[import-untyped]
    from _worker_setup import print_backend_warnings  # type: ignore[import-not-found]
    from temporal_session_manager import (  # type: ignore[import-not-found]
        SessionManagerWorkflow,
        pause_workflow,
        query_workflow_snapshot,
        switch_workflow_backend,
    )
    from temporalio.contrib.openai_agents import (  # type: ignore[attr-defined]
        ModelActivityParameters,
        OpenAIAgentsPlugin,
        SandboxClientProvider,
    )

    from agents.extensions.sandbox import DaytonaSandboxClient, E2BSandboxClient
    from agents.sandbox.sandboxes import DockerSandboxClient, UnixLocalSandboxClient

    sandbox_clients: list[SandboxClientProvider] = [
        SandboxClientProvider("local", UnixLocalSandboxClient()),
    ]
    if _os.environ.get("DAYTONA_API_KEY"):
        sandbox_clients.append(SandboxClientProvider("daytona", DaytonaSandboxClient()))
    if _os.environ.get("E2B_API_KEY"):
        sandbox_clients.append(SandboxClientProvider("e2b", E2BSandboxClient()))
    try:
        sandbox_clients.append(
            SandboxClientProvider("docker", DockerSandboxClient(docker.from_env()))
        )
    except docker.errors.DockerException:
        pass

    plugin = OpenAIAgentsPlugin(  # type: ignore[call-arg]
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=120),
        ),
        sandbox_clients=sandbox_clients,
    )

    temporal_client = await Client.connect("localhost:7233", plugins=[plugin])

    worker = Worker(
        temporal_client,
        task_queue=TASK_QUEUE,
        workflows=[AgentWorkflow, SessionManagerWorkflow],
        activities=[pause_workflow, query_workflow_snapshot, switch_workflow_backend],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "pydantic_core",
            ),
        ),
    )

    print_backend_warnings({p.name for p in sandbox_clients})
    print(f"Worker started on task queue '{TASK_QUEUE}'. Press Ctrl-C to stop.")
    await worker.run()


# ---------------------------------------------------------------------------
# CLI entrypoints
# ---------------------------------------------------------------------------


async def run_conversation() -> None:
    """Start the TUI -- sessions are managed entirely via Temporal."""
    from temporal_sandbox_tui import ConversationApp  # type: ignore[import-not-found]

    app = ConversationApp(
        workflow_cls=AgentWorkflow,
        task_queue=TASK_QUEUE,
        cwd=str(Path.cwd()),
    )
    await app.run_async()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Sandbox agent as a multi-turn Temporal workflow."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("worker", help="Start the Temporal worker process.")
    sub.add_parser("run", help="Start an interactive agent conversation.")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.command == "worker":
        asyncio.run(run_worker())
    else:
        asyncio.run(run_conversation())
