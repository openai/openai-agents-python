# mypy: ignore-errors
# standalone example with sys.path sibling imports that mypy cannot follow
"""Temporal session manager workflow.

A long-lived singleton workflow that acts as the sole orchestrator for agent
session lifecycles.  It starts and stops agent workflows, and maintains a
registry of active sessions so that TUI clients can list, resume, rename,
and destroy sessions without any filesystem persistence.

The manager is started once (well-known workflow ID ``session-manager``) and
lives forever.  All lifecycle operations — create, destroy, rename, fork — go
through the manager so the registry is always consistent.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, field_validator, model_serializer
    from temporal_sandbox_agent import (  # type: ignore[import-not-found]
        TASK_QUEUE,
        AgentRequest,
        AgentWorkflow,
        SwitchBackendSignal,
        SwitchToLocalBackend,
        WorkflowSnapshot,
    )
    from temporalio.client import Client
    from temporalio.contrib.openai_agents import OpenAIAgentsPlugin
    from temporalio.contrib.pydantic import pydantic_data_converter

    from agents import trace
    from agents.sandbox import Manifest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MANAGER_WORKFLOW_ID = "session-manager"

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class DaytonaBackendConfig(BaseModel):
    type: Literal["daytona"] = "daytona"

    @model_serializer(mode="wrap")
    def _serialize_always_include_type(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        data["type"] = self.type
        return data


class DockerBackendConfig(BaseModel):
    type: Literal["docker"] = "docker"

    @model_serializer(mode="wrap")
    def _serialize_always_include_type(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        data["type"] = self.type
        return data


class E2BBackendConfig(BaseModel):
    type: Literal["e2b"] = "e2b"

    @model_serializer(mode="wrap")
    def _serialize_always_include_type(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        data["type"] = self.type
        return data


class LocalBackendConfig(BaseModel):
    type: Literal["local"] = "local"
    workspace_root: Path | None = None

    @model_serializer(mode="wrap")
    def _serialize_always_include_type(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        data["type"] = self.type
        return data

    @field_validator("workspace_root")
    @classmethod
    def _must_be_absolute(cls, v: Path | None) -> Path | None:
        if v is not None and not v.is_absolute():
            raise ValueError("workspace_root must be an absolute path")
        return v


BackendConfig = DaytonaBackendConfig | DockerBackendConfig | E2BBackendConfig | LocalBackendConfig


class SessionInfo(BaseModel):
    workflow_id: str
    title: str
    created_at: datetime
    cwd: str = ""
    backend: BackendConfig = DaytonaBackendConfig()
    parent_workflow_id: str | None = None
    fork_count: int = 0
    snapshot_id: str | None = None


class CreateSessionRequest(BaseModel):
    cwd: str
    manifest: Manifest | None = None
    backend: BackendConfig = DaytonaBackendConfig()


class RenameRequest(BaseModel):
    workflow_id: str
    title: str


class ForkSessionRequest(BaseModel):
    source_workflow_id: str
    title: str | None = None  # defaults to "{original title} (fork #N)"
    target_backend: BackendConfig | None = None


class SwitchBackendRequest(BaseModel):
    source_workflow_id: str
    target_backend: BackendConfig


class _SwitchWorkflowBackendArgs(BaseModel):
    """Activity args for switch_workflow_backend."""

    workflow_id: str
    signal: SwitchBackendSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_manifest(
    backend: BackendConfig,
) -> Manifest:
    """Return the default workspace manifest for the given backend config."""
    if isinstance(backend, DaytonaBackendConfig):
        return Manifest(root="/home/daytona/workspace")
    if isinstance(backend, DockerBackendConfig):
        return Manifest(root="/workspace")
    if isinstance(backend, E2BBackendConfig):
        return Manifest()  # E2B resolves workspace root relative to the sandbox home
    root = str(backend.workspace_root) if backend.workspace_root else "/workspace"
    return Manifest(root=root)


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@activity.defn
async def pause_workflow(workflow_id: str) -> None:
    """Pause the agent workflow and wait for its session to fully stop."""
    client = await Client.connect("localhost:7233", data_converter=pydantic_data_converter)
    handle = client.get_workflow_handle(workflow_id)
    await handle.execute_update(AgentWorkflow.pause)


@activity.defn
async def switch_workflow_backend(args: _SwitchWorkflowBackendArgs) -> None:
    """Switch the agent workflow's backend and wait for it to take effect."""
    client = await Client.connect("localhost:7233", data_converter=pydantic_data_converter)
    handle = client.get_workflow_handle(args.workflow_id)
    await handle.execute_update(AgentWorkflow.switch_backend, args.signal)


@activity.defn
async def query_workflow_snapshot(workflow_id: str) -> WorkflowSnapshot:
    """Query the target workflow for its run state and conversation history."""
    client = await Client.connect("localhost:7233", data_converter=pydantic_data_converter)
    handle = client.get_workflow_handle(workflow_id)
    return await handle.query(AgentWorkflow.get_snapshot)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow.defn
class SessionManagerWorkflow:
    """Registry and orchestrator for agent sessions.

    * ``create_session`` — starts a new agent child workflow and registers it.
    * ``destroy_session`` — signals the agent workflow to terminate and
      removes it from the registry.
    * ``list_sessions`` — query returning all active sessions.
    * ``rename_session`` — signal to update a session title.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._shutdown = False

    # -- Main loop (lives forever) -----------------------------------------

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._shutdown)

    # -- Lifecycle: create & destroy (updates for request-response) ---------

    @workflow.update
    async def create_session(self, request: CreateSessionRequest) -> str:
        """Start a new agent workflow and register it.  Returns the workflow ID."""
        workflow_id = f"sandbox-agent-{workflow.uuid4()}"

        manifest = request.manifest
        if manifest is None:
            manifest = _default_manifest(request.backend)

        with OpenAIAgentsPlugin().tracing_context():
            with trace("Temporal Sandbox Sandbox Agent"):
                await workflow.start_child_workflow(
                    AgentWorkflow.run,
                    AgentRequest(
                        messages=[],
                        cwd=request.cwd,
                        backend=request.backend.type,
                        history=[],
                        manifest=manifest,
                    ),
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                    parent_close_policy=ParentClosePolicy.ABANDON,
                )
        self._sessions[workflow_id] = SessionInfo(
            workflow_id=workflow_id,
            title=f"Session {workflow_id[-8:]}",
            created_at=workflow.now(),
            cwd=request.cwd,
            backend=request.backend,
        )
        return workflow_id

    @workflow.update
    async def fork_session(self, request: ForkSessionRequest) -> str:
        """Fork an existing session into a new workflow with identical state.

        Pauses the source workflow, queries its RunState and conversation
        history, then starts a new child workflow seeded with that state.
        When ``target_backend`` differs from the source, the sandbox session
        state is not carried over (it is backend-specific), but the portable
        snapshot is extracted so the new backend can create a fresh session
        from the same workspace filesystem state.
        """
        source = self._sessions.get(request.source_workflow_id)
        if source is None:
            raise ApplicationError(f"Source session {request.source_workflow_id} not found")

        # Pause the source workflow so its session stops naturally
        await workflow.execute_activity(
            pause_workflow,
            request.source_workflow_id,
            start_to_close_timeout=timedelta(minutes=11),
        )

        # Fetch the source workflow's state via activity
        workflow_snapshot: WorkflowSnapshot = await workflow.execute_activity(
            query_workflow_snapshot,
            request.source_workflow_id,
            start_to_close_timeout=timedelta(seconds=30),
        )

        target_config = (
            request.target_backend if request.target_backend is not None else source.backend
        )
        cross_backend = target_config.type != source.backend.type

        # Determine fork title
        source.fork_count += 1
        if cross_backend:
            title = request.title or f"{source.title} [{target_config.type}]"
        else:
            title = request.title or f"{source.title} (fork #{source.fork_count})"

        # Always pass the portable snapshot so the forked session can seed
        # its workspace.  Never carry session_state — a fork creates an
        # independent session seeded from the snapshot, not a resume of the
        # source session.
        snapshot = workflow_snapshot.snapshot

        manifest = _default_manifest(target_config)

        # Start the forked workflow with the source's run state and history
        workflow_id = f"sandbox-agent-{workflow.uuid4()}"
        await workflow.start_child_workflow(
            AgentWorkflow.run,
            AgentRequest(
                messages=[],
                cwd=source.cwd,
                backend=target_config.type,
                sandbox_session_state=None,
                snapshot=snapshot,
                previous_response_id=workflow_snapshot.previous_response_id,
                history=workflow_snapshot.history,
                manifest=manifest,
            ),
            id=workflow_id,
            task_queue=TASK_QUEUE,
            parent_close_policy=ParentClosePolicy.ABANDON,
        )

        self._sessions[workflow_id] = SessionInfo(
            workflow_id=workflow_id,
            title=title,
            created_at=workflow.now(),
            cwd=source.cwd,
            backend=target_config,
            parent_workflow_id=request.source_workflow_id,
            snapshot_id=workflow_snapshot.sandbox_session_state.snapshot.id
            if workflow_snapshot.sandbox_session_state
            else None,
        )
        return workflow_id

    @workflow.update
    async def switch_backend(self, request: SwitchBackendRequest) -> str:
        """Switch a session to a different sandbox backend in-place.

        Signals the agent workflow to change its backend for subsequent turns.
        The workflow stays the same — no fork, no new child workflow.  The
        portable snapshot is preserved so the workspace can be carried over;
        the backend-specific session state is cleared by the agent workflow.
        """
        source = self._sessions.get(request.source_workflow_id)
        if source is None:
            raise ApplicationError(f"Session {request.source_workflow_id} not found")

        if isinstance(request.target_backend, LocalBackendConfig):
            target: Literal["daytona", "docker", "e2b"] | SwitchToLocalBackend = (
                SwitchToLocalBackend(
                    workspace_root=str(request.target_backend.workspace_root)
                    if request.target_backend.workspace_root
                    else "/workspace",
                )
            )
        else:
            target = request.target_backend.type
        await workflow.execute_activity(
            switch_workflow_backend,
            _SwitchWorkflowBackendArgs(
                workflow_id=request.source_workflow_id,
                signal=SwitchBackendSignal(target=target),
            ),
            start_to_close_timeout=timedelta(seconds=30),
        )

        source.backend = request.target_backend
        return request.source_workflow_id

    @workflow.update
    async def destroy_session(self, workflow_id: str) -> None:
        """Signal the agent workflow to destroy and remove it from the registry."""
        handle = workflow.get_external_workflow_handle(workflow_id)
        await handle.signal(AgentWorkflow.destroy)
        self._sessions.pop(workflow_id, None)

    # -- Metadata: queries and signals --------------------------------------

    @workflow.query
    def list_sessions(self) -> list[SessionInfo]:
        """Return all active sessions, newest first."""
        return sorted(
            self._sessions.values(),
            key=lambda s: s.created_at,
            reverse=True,
        )

    @workflow.signal
    async def rename_session(self, request: RenameRequest) -> None:
        """Update the title of an existing session."""
        if request.workflow_id in self._sessions:
            self._sessions[request.workflow_id].title = request.title

    @workflow.signal
    async def update_snapshot_id(self, request: RenameRequest) -> None:
        """Update the cached snapshot_id for a session.

        Reuses RenameRequest where ``title`` carries the snapshot ID.
        """
        if request.workflow_id in self._sessions:
            self._sessions[request.workflow_id].snapshot_id = request.title

    @workflow.signal
    async def shutdown(self) -> None:
        """Terminate the manager workflow (rarely needed)."""
        self._shutdown = True
