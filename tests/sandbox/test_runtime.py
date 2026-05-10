from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

import pytest
from openai.types.responses.response_output_item import LocalShellCall, LocalShellCallAction
from openai.types.responses.response_reasoning_item import ResponseReasoningItem, Summary

import agents.sandbox.runtime_agent_preparation as runtime_agent_preparation_module
from agents import Agent, AgentHooks, LocalShellTool, RunHooks, Runner, function_tool
from agents.exceptions import InputGuardrailTripwireTriggered, UserError
from agents.guardrail import GuardrailFunctionOutput, InputGuardrail, OutputGuardrail
from agents.items import ModelResponse, ToolCallOutputItem, TResponseInputItem
from agents.model_settings import ModelSettings
from agents.prompts import GenerateDynamicPromptData, Prompt
from agents.run import CallModelData, ModelInputData, RunConfig
from agents.run_context import AgentHookContext, RunContextWrapper
from agents.run_state import RunState, _build_agent_identity_map
from agents.sandbox import (
    FileMode,
    Group,
    Manifest,
    Permissions,
    SandboxAgent,
    SandboxArchiveLimits,
    SandboxConcurrencyLimits,
    SandboxPathGrant,
    SandboxRunConfig,
    User,
)
from agents.sandbox.capabilities import (
    Capability,
    Compaction,
    Filesystem,
    Memory,
    Shell,
    StaticCompactionPolicy,
)
from agents.sandbox.entries import (
    BaseEntry,
    File,
    InContainerMountStrategy,
    MountpointMountPattern,
    S3Mount,
)
from agents.sandbox.errors import (
    ExecNonZeroError,
    ExecTransportError,
    InvalidManifestPathError,
    WorkspaceArchiveWriteError,
)
from agents.sandbox.files import EntryKind, FileEntry
from agents.sandbox.materialization import MaterializedFile
from agents.sandbox.remote_mount_policy import (
    REMOTE_MOUNT_POLICY,
)
from agents.sandbox.runtime import SandboxRuntime
from agents.sandbox.runtime_agent_preparation import get_default_sandbox_instructions
from agents.sandbox.runtime_session_manager import SandboxRuntimeSessionManager
from agents.sandbox.sandboxes import unix_local as unix_local_module
from agents.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxClient,
    UnixLocalSandboxSession,
    UnixLocalSandboxSessionState,
)
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.dependencies import Dependencies
from agents.sandbox.session.runtime_helpers import RuntimeHelperScript
from agents.sandbox.session.sandbox_client import BaseSandboxClient
from agents.sandbox.session.sandbox_session import SandboxSession
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import LocalSnapshotSpec, NoopSnapshot, SnapshotBase
from agents.sandbox.types import ExecResult
from agents.stream_events import RunItemStreamEvent
from agents.tool import Tool
from agents.tracing import trace
from tests.fake_model import FakeModel
from tests.test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
)
from tests.testing_processor import fetch_normalized_spans
from tests.utils.factories import TestSessionState
from tests.utils.simple_session import SimpleListSession


class _FakeSession(BaseSandboxSession):
    def __init__(
        self,
        manifest: Manifest,
        *,
        start_gate: asyncio.Event | None = None,
    ) -> None:
        self.state = TestSessionState(
            manifest=manifest,
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
        )
        self._start_gate = start_gate
        self._running = False
        self.start_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0
        self.close_dependency_calls = 0
        self.archive_limit_values: list[SandboxArchiveLimits | None] = []
        self.concurrency_limit_values: list[SandboxConcurrencyLimits] = []

    def _set_concurrency_limits(self, limits: SandboxConcurrencyLimits) -> None:
        super()._set_concurrency_limits(limits)
        self.concurrency_limit_values.append(limits)

    def _set_archive_limits(self, limits: SandboxArchiveLimits | None) -> None:
        super()._set_archive_limits(limits)
        self.archive_limit_values.append(limits)

    async def start(self) -> None:
        self.start_calls += 1
        if self._start_gate is not None:
            await self._start_gate.wait()
        self._running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self._running = False

    async def shutdown(self) -> None:
        self.shutdown_calls += 1

    async def running(self) -> bool:
        return self._running

    async def read(self, path: Path, *, user: object = None) -> io.BytesIO:
        _ = (path, user)
        raise AssertionError("read() should not be called in these tests")

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = (path, data, user)
        raise AssertionError("write() should not be called in these tests")

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = (command, timeout)
        raise AssertionError("exec() should not be called in these tests")

    async def persist_workspace(self) -> io.IOBase:
        return io.BytesIO()

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data

    async def _aclose_dependencies(self) -> None:
        self.close_dependency_calls += 1
        await super()._aclose_dependencies()


class _FailingStopSession(_FakeSession):
    async def stop(self) -> None:
        await super().stop()
        raise RuntimeError("stop failed")


class _LiveSessionDeltaRecorder(_FakeSession):
    def __init__(self, manifest: Manifest, *, fail_entry_batch_times: int = 0) -> None:
        super().__init__(manifest)
        self.apply_manifest_calls = 0
        self.applied_entry_batches: list[list[tuple[Path, BaseEntry]]] = []
        self._fail_entry_batch_times = fail_entry_batch_times

    async def apply_manifest(self, *, only_ephemeral: bool = False):
        _ = only_ephemeral
        self.apply_manifest_calls += 1
        raise AssertionError("apply_manifest() should not be used for running injected sessions")

    async def _apply_entry_batch(
        self,
        entries: Sequence[tuple[Path, BaseEntry]],
        *,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        _ = base_dir
        self.applied_entry_batches.append(
            [(dest, artifact.model_copy(deep=True)) for dest, artifact in entries]
        )
        if self._fail_entry_batch_times > 0:
            self._fail_entry_batch_times -= 1
            raise RuntimeError("delta apply failed")
        return []


class _PathGuardingSession(_FakeSession):
    def __init__(self, manifest: Manifest) -> None:
        super().__init__(manifest)
        self.normalized_paths: list[Path] = []

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        _ = for_write
        normalized = Path(path)
        self.normalized_paths.append(normalized)
        raise InvalidManifestPathError(rel=normalized, reason="escape_root")


class _LocalShellExecSession(_FakeSession):
    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        process = await asyncio.create_subprocess_exec(
            *(str(part) for part in command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise
        return ExecResult(
            stdout=stdout or b"",
            stderr=stderr or b"",
            exit_code=process.returncode or 0,
        )


class _EmptyRemoteRealpathSession(_FakeSession):
    def __init__(self, manifest: Manifest) -> None:
        super().__init__(manifest)
        self.exec_commands: list[tuple[str, ...]] = []

    async def _ensure_runtime_helper_installed(self, helper: RuntimeHelperScript) -> Path:
        _ = helper
        return Path("/tmp/resolve_workspace_path")

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        self.exec_commands.append(tuple(str(part) for part in command))
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)


class _BlockingStopSession(_FakeSession):
    def __init__(self, manifest: Manifest, stop_gate: asyncio.Event) -> None:
        super().__init__(manifest)
        self._stop_gate = stop_gate

    async def stop(self) -> None:
        await super().stop()
        await self._stop_gate.wait()


class _MarkerSnapshot(SnapshotBase):
    __test__ = False
    type: Literal["marker"] = "marker"
    marker: str = "initial"

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        _ = (data, dependencies)

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        return io.BytesIO()

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return False


class _PersistingStopSession(_BlockingStopSession):
    def __init__(self, manifest: Manifest, stop_gate: asyncio.Event) -> None:
        super().__init__(manifest, stop_gate)
        self.state.snapshot = _MarkerSnapshot(id="marker")

    async def stop(self) -> None:
        self.stop_calls += 1
        self._running = False
        await self._stop_gate.wait()
        snapshot = cast(_MarkerSnapshot, self.state.snapshot)
        self.state.snapshot = snapshot.model_copy(update={"marker": "persisted"})


class _ProvisioningFailureSession(_FakeSession):
    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        cmd = [str(part) for part in command]
        if cmd[:2] == ["mkdir", "-p"]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if cmd and cmd[0] in {"groupadd", "useradd"}:
            return ExecResult(
                stdout=f"attempted {cmd[0]}".encode(),
                stderr=f"missing {cmd[0]}".encode(),
                exit_code=1,
            )
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)


class _RestorableSnapshot(SnapshotBase):
    __test__ = False
    type: Literal["restorable"] = "restorable"

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        _ = (data, dependencies)

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        return io.BytesIO(b"snapshot")

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return True


class _RestorableProvisioningFailureSession(_ProvisioningFailureSession):
    def __init__(self, manifest: Manifest, *, provision_on_resume: bool = True) -> None:
        super().__init__(manifest)
        self.state.snapshot = _RestorableSnapshot(id="resume")
        self.cleared_workspace_root = False
        self.hydrate_calls = 0
        self._set_start_state_preserved(False, system=not provision_on_resume)

    async def start(self) -> None:
        self.start_calls += 1
        self._running = True
        await BaseSandboxSession.start(self)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data
        self.hydrate_calls += 1

    async def _clear_workspace_root_on_resume(self) -> None:
        self.cleared_workspace_root = True


@pytest.mark.asyncio
async def test_sandbox_session_aclose_runs_public_cleanup_lifecycle() -> None:
    inner = _FakeSession(Manifest())
    session = SandboxSession(inner)

    await session.aclose()

    assert inner.stop_calls == 1
    assert inner.shutdown_calls == 1
    assert inner.close_dependency_calls == 1


@pytest.mark.asyncio
async def test_sandbox_session_aclose_closes_dependencies_when_stop_fails() -> None:
    inner = _FailingStopSession(Manifest())
    session = SandboxSession(inner)

    with pytest.raises(RuntimeError, match="stop failed"):
        await session.aclose()

    assert inner.stop_calls == 1
    assert inner.shutdown_calls == 0
    assert inner.close_dependency_calls == 1


@pytest.mark.asyncio
async def test_sandbox_session_routes_helper_path_checks_to_inner_session() -> None:
    inner = _PathGuardingSession(Manifest(root="/workspace"))
    session = SandboxSession(inner)

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.ls("link")
    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.mkdir("link/nested", parents=True)
    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.rm("link/file.txt")
    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.extract(
            "bundle.tar",
            io.BytesIO(b"ignored"),
            compression_scheme="tar",
        )

    assert inner.normalized_paths == [
        Path("link"),
        Path("link/nested"),
        Path("link/file.txt"),
        Path("bundle.tar"),
    ]


@pytest.mark.asyncio
async def test_remote_realpath_guard_fails_closed_on_symlink_cycle(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "loop").symlink_to("loop")

    session = _LocalShellExecSession(Manifest(root=str(workspace_root)))

    with pytest.raises(ExecNonZeroError, match="symlink resolution depth exceeded"):
        await asyncio.wait_for(
            session._validate_remote_path_access("loop"),  # noqa: SLF001
            timeout=1,
        )


@pytest.mark.asyncio
async def test_remote_realpath_empty_success_output_is_transport_error() -> None:
    session = _EmptyRemoteRealpathSession(Manifest(root="/workspace"))

    with pytest.raises(ExecTransportError) as exc_info:
        await session._validate_remote_path_access("file.txt")  # noqa: SLF001

    assert exc_info.value.context == {
        "command": ("resolve_workspace_path", "/workspace", "/workspace/file.txt", "0"),
        "command_str": "resolve_workspace_path /workspace /workspace/file.txt 0",
        "reason": "empty_stdout",
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
    }
    assert session.exec_commands == [
        ("/tmp/resolve_workspace_path", "/workspace", "/workspace/file.txt", "0")
    ]


@pytest.mark.asyncio
async def test_runtime_helper_install_replaces_tampered_executable(tmp_path: Path) -> None:
    install_path = tmp_path / "runtime-helpers" / "helper"
    helper = RuntimeHelperScript(
        name="test-helper",
        content="#!/bin/sh\nprintf 'expected\\n'",
        install_path=install_path,
    )
    session = _LocalShellExecSession(Manifest(root=str(tmp_path / "workspace")))

    command = helper.install_command()
    assert command[:2] == ("sh", "-c")

    initial = await session._exec_internal(*command)  # noqa: SLF001
    assert initial.ok()
    assert install_path.read_text().rstrip("\n") == helper.content

    install_path.chmod(0o755)
    install_path.write_text("#!/bin/sh\nprintf 'tampered\\n'")
    install_path.chmod(0o755)

    repaired = await session._exec_internal(*helper.install_command())  # noqa: SLF001
    assert repaired.ok()
    assert install_path.read_text().rstrip("\n") == helper.content


@pytest.mark.asyncio
async def test_runtime_helper_reinstalls_when_cached_binary_is_missing(tmp_path: Path) -> None:
    install_path = tmp_path / "runtime-helpers" / "helper"
    helper = RuntimeHelperScript(
        name="test-helper",
        content="#!/bin/sh\nprintf 'expected\\n'",
        install_path=install_path,
    )
    session = _LocalShellExecSession(Manifest(root=str(tmp_path / "workspace")))

    installed_path = await session._ensure_runtime_helper_installed(helper)  # noqa: SLF001
    assert installed_path == install_path
    assert install_path.exists()

    install_path.unlink()
    assert not install_path.exists()

    repaired_path = await session._ensure_runtime_helper_installed(helper)  # noqa: SLF001
    assert repaired_path == install_path
    assert install_path.exists()
    assert install_path.read_text().rstrip("\n") == helper.content


def _extract_user_text(item: dict[str, object]) -> str:
    content = item["content"]
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        first = content[0]
        if isinstance(first, dict):
            return str(first.get("text", ""))
    raise AssertionError(f"Unexpected content payload: {content!r}")


def _tripwire_input_guardrail(
    _context: RunContextWrapper[Any],
    _agent: Agent[Any],
    _input: str | list[TResponseInputItem],
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)


def _get_reasoning_item() -> ResponseReasoningItem:
    return ResponseReasoningItem(
        id="rid",
        type="reasoning",
        summary=[Summary(text="thinking", type="summary_text")],
    )


class _CreateKwargs(TypedDict):
    snapshot: object | None
    manifest: Manifest | None
    options: dict[str, str]


class _FakeClient(BaseSandboxClient[dict[str, str]]):
    backend_id = "fake"

    def __init__(self, session: _FakeSession) -> None:
        self.inner_session = session
        self.session = self._wrap_session(session)
        self.create_kwargs: _CreateKwargs | None = None
        self.resume_state: SandboxSessionState | None = None
        self.delete_calls = 0

    async def create(
        self,
        *,
        snapshot: object | None = None,
        manifest: Manifest | None = None,
        options: dict[str, str],
    ) -> SandboxSession:
        base_manifest = manifest if manifest is not None else self.inner_session.state.manifest
        self.create_kwargs = {
            "snapshot": snapshot,
            "manifest": base_manifest,
            "options": options,
        }
        if self.create_kwargs["manifest"] is not None:
            self.inner_session.state.manifest = self.create_kwargs["manifest"]
        return self.session

    async def delete(self, session: SandboxSession) -> SandboxSession:
        self.delete_calls += 1
        return session

    async def resume(
        self,
        state: SandboxSessionState,
    ) -> SandboxSession:
        self.resume_state = state
        self.inner_session.state = self.resume_state
        return self.session

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return SandboxSessionState.model_validate(payload)


class _ManifestSessionClient(BaseSandboxClient[None]):
    backend_id = "manifest"
    supports_default_options = True

    def __init__(self) -> None:
        self.created_manifests: list[Manifest | None] = []

    async def create(
        self,
        *,
        snapshot: object | None = None,
        manifest: Manifest | None = None,
        options: None = None,
    ) -> SandboxSession:
        _ = (snapshot, options)
        self.created_manifests.append(manifest)
        assert manifest is not None
        session = _FakeSession(manifest)
        return self._wrap_session(session)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        return session

    async def resume(
        self,
        state: SandboxSessionState,
    ) -> SandboxSession:
        return self._wrap_session(_FakeSession(state.manifest))

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return SandboxSessionState.model_validate(payload)


class _RecordingCapability(Capability):
    type: str = "recording"
    bound_session: BaseSandboxSession | None = None
    instruction_text: str | None = None
    provided_tools: list[Any]

    def __init__(
        self,
        *,
        instruction_text: str | None = None,
        provided_tools: list[Any] | None = None,
    ) -> None:
        super().__init__(
            type="recording",
            **cast(
                Any,
                {
                    "bound_session": None,
                    "instruction_text": instruction_text,
                    "provided_tools": list(provided_tools or []),
                },
            ),
        )

    def bind(self, session: BaseSandboxSession) -> None:
        self.bound_session = session

    def tools(self) -> list[Tool]:
        return cast(list[Tool], list(self.provided_tools))

    async def instructions(self, manifest: Manifest) -> str | None:
        _ = manifest
        return self.instruction_text


class _NestedStateCapability(Capability):
    type: str = "nested-state"
    state: dict[str, list[str]]

    def __init__(self) -> None:
        super().__init__(type="nested-state", **cast(Any, {"state": {"seen": []}}))


class _NestedObjectState:
    def __init__(self) -> None:
        self.seen: list[str] = []


class _NestedObjectCapability(Capability):
    type: str = "nested-object-state"
    state: _NestedObjectState

    def __init__(self) -> None:
        super().__init__(
            type="nested-object-state",
            **cast(Any, {"state": _NestedObjectState()}),
        )


class _AwaitableSessionCapability(Capability):
    type: str = "awaitable-session"
    bound_session: BaseSandboxSession | None = None
    release_gate: asyncio.Event
    first_instruction_started: asyncio.Event
    second_instruction_started: asyncio.Event

    def __init__(
        self,
        *,
        release_gate: asyncio.Event,
        first_instruction_started: asyncio.Event,
        second_instruction_started: asyncio.Event,
    ) -> None:
        super().__init__(
            type="awaitable-session",
            **cast(
                Any,
                {
                    "bound_session": None,
                    "release_gate": release_gate,
                    "first_instruction_started": first_instruction_started,
                    "second_instruction_started": second_instruction_started,
                },
            ),
        )

    def bind(self, session: BaseSandboxSession) -> None:
        self.bound_session = session

    async def instructions(self, manifest: Manifest) -> str | None:
        _ = manifest
        assert self.bound_session is not None
        readme = self.bound_session.state.manifest.entries["README.md"]
        assert isinstance(readme, File)
        readme_text = readme.content.decode()
        if readme_text == "Session one instructions.":
            self.first_instruction_started.set()
        elif readme_text == "Session two instructions.":
            self.second_instruction_started.set()
        await self.release_gate.wait()
        return readme_text


class _ManifestInstructionsCapability(Capability):
    type: str = "manifest-instructions"
    bound_session: BaseSandboxSession | None = None

    def __init__(self) -> None:
        super().__init__(type="manifest-instructions", **cast(Any, {"bound_session": None}))

    def bind(self, session: BaseSandboxSession) -> None:
        self.bound_session = session

    async def instructions(self, manifest: Manifest) -> str | None:
        _ = manifest
        assert self.bound_session is not None
        readme = self.bound_session.state.manifest.entries["README.md"]
        assert isinstance(readme, File)
        return readme.content.decode()


class _ManifestMutationCapability(Capability):
    type: str = "manifest-mutation"
    rel_path: str
    content: bytes

    def __init__(self, *, rel_path: str = "cap.txt", content: bytes = b"capability") -> None:
        super().__init__(
            type="manifest-mutation",
            **cast(
                Any,
                {
                    "rel_path": rel_path,
                    "content": content,
                },
            ),
        )

    def process_manifest(self, manifest: Manifest) -> Manifest:
        manifest.entries[self.rel_path] = File(content=self.content)
        return manifest


class _ManifestUsersCapability(Capability):
    type: str = "manifest-users"

    def __init__(self) -> None:
        super().__init__(type="manifest-users")

    def process_manifest(self, manifest: Manifest) -> Manifest:
        manifest.users.append(User(name="sandbox-user"))
        return manifest


class _ProcessContextSessionCapability(Capability):
    type: str = "process-context-session"
    bound_session: BaseSandboxSession | None = None
    process_calls: int = 0

    def __init__(self) -> None:
        super().__init__(
            type="process-context-session",
            **cast(
                Any,
                {
                    "bound_session": None,
                    "process_calls": 0,
                },
            ),
        )

    def bind(self, session: BaseSandboxSession) -> None:
        self.bound_session = session

    def process_context(self, context: list[TResponseInputItem]) -> list[TResponseInputItem]:
        assert self.bound_session is not None
        self.process_calls += 1
        return [
            *context,
            cast(
                TResponseInputItem,
                {
                    "role": "user",
                    "content": f"process_calls={self.process_calls}",
                },
            ),
        ]


class _SessionFileCapability(Capability):
    type: str = "session-files"
    bound_session: BaseSandboxSession | None = None

    def __init__(self) -> None:
        super().__init__(type="session-files", **cast(Any, {"bound_session": None}))

    def bind(self, session: BaseSandboxSession) -> None:
        self.bound_session = session

    def tools(self) -> list[Tool]:
        @function_tool(name_override="write_file")
        async def write_file(path: str, content: str) -> str:
            assert self.bound_session is not None
            await self.bound_session.write(Path(path), io.BytesIO(content.encode("utf-8")))
            return "wrote"

        @function_tool(name_override="read_file")
        async def read_file(path: str) -> str:
            assert self.bound_session is not None
            data = await self.bound_session.read(Path(path))
            return cast(bytes, data.read()).decode("utf-8")

        return [write_file, read_file]


class _RecordingRunHooks(RunHooks[None]):
    def __init__(self) -> None:
        self.started_agents: list[Agent[None]] = []
        self.ended_agents: list[Agent[None]] = []
        self.llm_started_agents: list[Agent[None]] = []
        self.llm_ended_agents: list[Agent[None]] = []

    async def on_agent_start(self, context: AgentHookContext[None], agent: Agent[None]) -> None:
        _ = context
        self.started_agents.append(agent)

    async def on_llm_start(
        self,
        context: RunContextWrapper[None],
        agent: Agent[None],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        _ = (context, system_prompt, input_items)
        self.llm_started_agents.append(agent)

    async def on_llm_end(
        self,
        context: RunContextWrapper[None],
        agent: Agent[None],
        response: ModelResponse,
    ) -> None:
        _ = (context, response)
        self.llm_ended_agents.append(agent)

    async def on_agent_end(
        self,
        context: AgentHookContext[None],
        agent: Agent[None],
        output: object,
    ) -> None:
        _ = (context, output)
        self.ended_agents.append(agent)


class _RecordingAgentHooks(AgentHooks[None]):
    def __init__(self) -> None:
        self.started_agents: list[Agent[None]] = []
        self.ended_agents: list[Agent[None]] = []
        self.llm_started_agents: list[Agent[None]] = []
        self.llm_ended_agents: list[Agent[None]] = []

    async def on_start(self, context: AgentHookContext[None], agent: Agent[None]) -> None:
        _ = context
        self.started_agents.append(agent)

    async def on_llm_start(
        self,
        context: RunContextWrapper[None],
        agent: Agent[None],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        _ = (context, system_prompt, input_items)
        self.llm_started_agents.append(agent)

    async def on_llm_end(
        self,
        context: RunContextWrapper[None],
        agent: Agent[None],
        response: ModelResponse,
    ) -> None:
        _ = (context, response)
        self.llm_ended_agents.append(agent)

    async def on_end(
        self,
        context: AgentHookContext[None],
        agent: Agent[None],
        output: object,
    ) -> None:
        _ = (context, output)
        self.ended_agents.append(agent)


def _sandbox_run_config(client: _FakeClient | None = None) -> RunConfig:
    return RunConfig(
        sandbox=SandboxRunConfig(
            client=client,
            options={"image": "sandbox"} if client is not None else None,
        )
    )


def test_sandbox_package_exports_permission_types() -> None:
    assert User(name="sandbox-user").name == "sandbox-user"
    assert Group(name="sandbox-group", users=[]).users == []
    assert Permissions().owner == int(FileMode.ALL)


def _unix_local_manifest(**kwargs: Any) -> Manifest:
    return Manifest(**kwargs)


def _unix_local_run_config(
    *,
    client: UnixLocalSandboxClient | None = None,
    session_state: SandboxSessionState | None = None,
    manifest: Manifest | None = None,
) -> RunConfig:
    sandbox_kwargs: dict[str, Any] = {
        "client": client or UnixLocalSandboxClient(),
    }
    if session_state is not None:
        sandbox_kwargs["session_state"] = session_state
    else:
        sandbox_kwargs["manifest"] = manifest or _unix_local_manifest()
    return RunConfig(sandbox=SandboxRunConfig(**sandbox_kwargs))


@pytest.mark.asyncio
async def test_runner_merges_sandbox_instructions_and_tools() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    capability_tool = get_function_tool("capability_tool", "ok")
    capability = _RecordingCapability(
        instruction_text="Capability instructions.",
        provided_tools=[capability_tool],
    )
    manifest = Manifest(entries={"README.md": File(content=b"Follow the repo contract.")})
    session = _FakeSession(manifest)
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Additional instructions.",
        default_manifest=manifest,
        capabilities=[capability],
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=_sandbox_run_config(client),
    )

    assert result.final_output == "done"
    assert capability.bound_session is None
    assert session.start_calls == 1
    assert session.stop_calls == 1
    assert session.shutdown_calls == 1
    assert session.close_dependency_calls == 1
    assert client.delete_calls == 1

    state = result.to_state()
    assert state._sandbox is not None
    assert state._sandbox["backend_id"] == "fake"
    assert state._sandbox["current_agent_name"] == agent.name
    assert state._sandbox["current_agent_key"] == agent.name
    sessions_by_agent = state._sandbox["sessions_by_agent"]
    assert isinstance(sessions_by_agent, dict)
    assert sessions_by_agent[agent.name] == {
        "agent_name": agent.name,
        "session_state": state._sandbox["session_state"],
    }

    assert client.create_kwargs is not None
    assert client.create_kwargs["manifest"] is not manifest
    assert client.create_kwargs["options"] == {"image": "sandbox"}
    assert isinstance(client.create_kwargs["snapshot"], LocalSnapshotSpec)

    assert model.first_turn_args is not None
    assert model.first_turn_args["system_instructions"] == (
        f"{get_default_sandbox_instructions()}\n\n"
        "# Agent instructions\n\n"
        "Additional instructions.\n\n"
        "# Sandbox capability instructions\n\n"
        "Capability instructions.\n\n"
        f"{runtime_agent_preparation_module._filesystem_instructions(manifest)}"
    )
    assert [tool.name for tool in model.first_turn_args["tools"]] == ["capability_tool"]

    input_items = model.first_turn_args["input"]
    assert isinstance(input_items, list)
    assert _extract_user_text(input_items[0]) == "hello"


def test_filesystem_instructions_omit_extra_path_grants() -> None:
    manifest = Manifest(
        root="/workspace",
        extra_path_grants=(
            SandboxPathGrant(path="/tmp", description="temporary files"),
            SandboxPathGrant(
                path="/opt/toolchain",
                read_only=True,
                description="compiler runtime",
            ),
        ),
    )

    assert runtime_agent_preparation_module._filesystem_instructions(manifest) == (
        "# Filesystem\n"
        "You have access to a container with a filesystem. The filesystem layout is:\n"
        "\n"
        "/workspace"
    )


@pytest.mark.asyncio
async def test_runner_adds_run_as_user_to_created_manifest_without_default_manifest() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    run_as = User(name="sandbox-user")
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        run_as=run_as,
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=_sandbox_run_config(client),
    )

    assert result.final_output == "done"
    assert client.create_kwargs is not None
    created_manifest = client.create_kwargs["manifest"]
    assert created_manifest is not None
    assert created_manifest.users == [run_as]
    assert session.state.manifest.users == [run_as]


@pytest.mark.asyncio
async def test_runner_uses_default_sandbox_prompt_when_instructions_missing() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    capability = _RecordingCapability(instruction_text="Capability instructions.")
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        capabilities=[capability],
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=_sandbox_run_config(client),
    )

    assert result.final_output == "done"
    assert model.first_turn_args is not None
    expected_instructions = (
        f"{get_default_sandbox_instructions()}\n\n"
        "# Sandbox capability instructions\n\n"
        "Capability instructions.\n\n"
        f"{runtime_agent_preparation_module._filesystem_instructions(session.state.manifest)}"
    )
    assert model.first_turn_args["system_instructions"] == (expected_instructions)


@pytest.mark.asyncio
async def test_runner_handles_missing_default_sandbox_prompt_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    capability = _RecordingCapability(instruction_text="Capability instructions.")
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Additional instructions.",
        capabilities=[capability],
    )

    def _raise_file_not_found(_package: object) -> object:
        raise FileNotFoundError("missing prompt.md")

    runtime_agent_preparation_module.get_default_sandbox_instructions.cache_clear()
    monkeypatch.setattr(runtime_agent_preparation_module, "files", _raise_file_not_found)
    try:
        result = await Runner.run(
            agent,
            "hello",
            run_config=_sandbox_run_config(client),
        )
    finally:
        runtime_agent_preparation_module.get_default_sandbox_instructions.cache_clear()

    assert result.final_output == "done"
    assert model.first_turn_args is not None
    assert model.first_turn_args["system_instructions"] == (
        "# Agent instructions\n\n"
        "Additional instructions.\n\n"
        "# Sandbox capability instructions\n\n"
        "Capability instructions.\n\n"
        f"{runtime_agent_preparation_module._filesystem_instructions(session.state.manifest)}"
    )


@pytest.mark.asyncio
async def test_runner_dynamic_instructions_do_not_override_default_sandbox_prompt() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    capability = _RecordingCapability(instruction_text="Capability instructions.")
    session = _FakeSession(Manifest())
    client = _FakeClient(session)

    def dynamic_instructions(
        _ctx: RunContextWrapper[Any],
        _agent: Agent[Any],
    ) -> str:
        return ""

    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions=dynamic_instructions,
        capabilities=[capability],
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=_sandbox_run_config(client),
    )

    assert result.final_output == "done"
    assert model.first_turn_args is not None
    assert model.first_turn_args["system_instructions"] == (
        f"{get_default_sandbox_instructions()}\n\n"
        "# Sandbox capability instructions\n\n"
        "Capability instructions.\n\n"
        f"{runtime_agent_preparation_module._filesystem_instructions(session.state.manifest)}"
    )


@pytest.mark.asyncio
async def test_runner_base_instructions_override_default_sandbox_prompt() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    capability = _RecordingCapability(instruction_text="Capability instructions.")
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        base_instructions="Custom base instructions.",
        instructions="Additional instructions.",
        capabilities=[capability],
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=_sandbox_run_config(client),
    )

    assert result.final_output == "done"
    assert model.first_turn_args is not None
    assert model.first_turn_args["system_instructions"] == (
        "Custom base instructions.\n\n"
        "# Agent instructions\n\n"
        "Additional instructions.\n\n"
        "# Sandbox capability instructions\n\n"
        "Capability instructions.\n\n"
        f"{runtime_agent_preparation_module._filesystem_instructions(session.state.manifest)}"
    )


@pytest.mark.asyncio
async def test_runner_adds_remote_mount_policy_instructions() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    manifest = Manifest(
        entries={
            "remote": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            )
        }
    )
    session = _FakeSession(manifest)
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
        default_manifest=manifest,
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=_sandbox_run_config(client),
    )

    assert result.final_output == "done"
    assert model.first_turn_args is not None
    system_instructions = model.first_turn_args["system_instructions"]
    assert isinstance(system_instructions, str)
    expected_policy_pattern = re.escape(REMOTE_MOUNT_POLICY)
    expected_policy_pattern = expected_policy_pattern.replace(
        re.escape("{path_lines}"),
        re.escape("- /workspace/remote (mounted in read-only mode)"),
    )
    expected_policy_pattern = expected_policy_pattern.replace(
        re.escape("{REMOTE_MOUNT_COMMAND_ALLOWLIST_TEXT}"),
        re.escape(", ".join(f"`{command}`" for command in manifest.remote_mount_command_allowlist)),
    )
    expected_policy_pattern = expected_policy_pattern.replace(
        re.escape("{edit_instructions}"),
        re.escape(
            "Use `apply_patch` directly for text edits. "
            "For shell-based edits, first `cp` the mounted file to a normal local workspace "
            "path, edit the local copy there, then `cp` it back. "
        ),
    )
    assert isinstance(re.search(expected_policy_pattern, system_instructions), re.Match)
    agent_index = system_instructions.index("# Agent instructions")
    capability_index = system_instructions.index("# Sandbox capability instructions")
    remote_policy_index = system_instructions.index("# Sandbox remote mount policy")
    filesystem_index = system_instructions.index("# Filesystem")
    assert agent_index < capability_index < remote_policy_index < filesystem_index


@pytest.mark.asyncio
async def test_runner_adds_remote_mount_policy_for_non_ephemeral_mounts() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    manifest = Manifest(
        entries={
            "remote": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
                ephemeral=False,
            )
        }
    )
    session = _FakeSession(manifest)
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
        default_manifest=manifest,
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=_sandbox_run_config(client),
    )

    assert result.final_output == "done"
    assert model.first_turn_args is not None
    system_instructions = model.first_turn_args["system_instructions"]
    assert isinstance(system_instructions, str)
    assert "- /workspace/remote (mounted in read-only mode)" in system_instructions


@pytest.mark.asyncio
async def test_runner_applies_compaction_capability_to_input_and_model_settings() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        default_manifest=Manifest(),
        capabilities=[Compaction(policy=StaticCompactionPolicy(threshold=123))],
    )
    input_items: list[TResponseInputItem] = [
        {"type": "message", "role": "user", "content": "old-user"},
        cast(TResponseInputItem, {"type": "compaction", "summary": "compacted-up-to-here"}),
        {"type": "message", "role": "assistant", "content": "recent-assistant"},
        {"type": "message", "role": "user", "content": "new-user"},
    ]

    result = await Runner.run(
        agent,
        input_items,
        run_config=_sandbox_run_config(client),
    )

    assert result.final_output == "done"
    assert model.first_turn_args is not None
    assert model.first_turn_args["input"] == input_items[1:]
    model_settings = model.first_turn_args["model_settings"]
    assert isinstance(model_settings, ModelSettings)
    assert model_settings.extra_args == {
        "context_management": [
            {
                "type": "compaction",
                "compact_threshold": 123,
            }
        ]
    }


@pytest.mark.asyncio
async def test_runner_marks_writable_remote_mounts_in_policy() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    manifest = Manifest(
        entries={
            "remote": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
                read_only=False,
            )
        }
    )
    session = _FakeSession(manifest)
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
        default_manifest=manifest,
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=_sandbox_run_config(client),
    )

    assert result.final_output == "done"
    assert model.first_turn_args is not None
    system_instructions = model.first_turn_args["system_instructions"]
    assert isinstance(system_instructions, str)
    assert "- /workspace/remote (mounted in read+write mode)" in system_instructions
    assert "Use `apply_patch` directly for text edits." in system_instructions
    assert (
        "For shell-based edits, first `cp` the mounted file to a normal local workspace path, "
        "edit the local copy there, then `cp` it back." in system_instructions
    )


@pytest.mark.asyncio
async def test_runner_uses_manifest_remote_mount_command_allowlist_override() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    manifest = Manifest(
        entries={
            "remote": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            )
        },
        remote_mount_command_allowlist=["ls", "cp"],
    )
    session = _FakeSession(manifest)
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
        default_manifest=manifest,
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=_sandbox_run_config(client),
    )

    assert result.final_output == "done"
    assert model.first_turn_args is not None
    system_instructions = model.first_turn_args["system_instructions"]
    assert isinstance(system_instructions, str)
    assert "Only use these commands on remote mounts:" in system_instructions
    assert "`ls`, `cp`" in system_instructions


@pytest.mark.asyncio
async def test_runner_requires_sandbox_config_for_sandbox_agent() -> None:
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
    )

    with pytest.raises(UserError, match="RunConfig\\(sandbox=.*\\)"):
        await Runner.run(agent, "hello")


@pytest.mark.asyncio
async def test_runner_streamed_cleans_runner_owned_session() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
    )

    result = Runner.run_streamed(
        agent,
        "hello",
        run_config=_sandbox_run_config(client),
    )
    events = [event async for event in result.stream_events()]

    assert events
    assert result.final_output == "done"
    assert session.start_calls == 1
    assert session.stop_calls == 1
    assert session.shutdown_calls == 1
    assert session.close_dependency_calls == 1
    assert client.delete_calls == 1

    state = result.to_state()
    assert state._sandbox is not None
    assert state._sandbox["backend_id"] == "fake"
    assert state._sandbox["current_agent_name"] == agent.name
    assert state._sandbox["current_agent_key"] == agent.name
    sessions_by_agent = state._sandbox["sessions_by_agent"]
    assert isinstance(sessions_by_agent, dict)
    assert sessions_by_agent[agent.name] == {
        "agent_name": agent.name,
        "session_state": state._sandbox["session_state"],
    }


@pytest.mark.asyncio
async def test_runner_streamed_guardrail_trip_blocks_runner_owned_sandbox_creation() -> None:
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
        input_guardrails=[
            InputGuardrail(
                guardrail_function=_tripwire_input_guardrail,
                run_in_parallel=False,
            )
        ],
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, "hello", run_config=_sandbox_run_config(client))
        async for _ in result.stream_events():
            pass

    assert client.create_kwargs is None
    assert session.start_calls == 0
    assert session.stop_calls == 0
    assert session.shutdown_calls == 0
    assert session.close_dependency_calls == 0


@pytest.mark.asyncio
async def test_runner_does_not_close_injected_sandbox_session() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    default_manifest = Manifest(entries={"default.txt": File(content=b"default")})
    session_manifest = Manifest(entries={"session.txt": File(content=b"session")})
    injected_session = _FakeSession(session_manifest)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
        default_manifest=default_manifest,
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=RunConfig(
            sandbox=SandboxRunConfig(
                session=injected_session,
                manifest=Manifest(entries={"override.txt": File(content=b"override")}),
            )
        ),
    )

    assert result.final_output == "done"
    assert injected_session.start_calls == 1
    assert injected_session.stop_calls == 0
    assert injected_session.shutdown_calls == 0
    assert injected_session.close_dependency_calls == 0

    assert model.first_turn_args is not None
    input_items = model.first_turn_args["input"]
    assert isinstance(input_items, str) or isinstance(input_items, list)
    assert injected_session.state.manifest.entries == session_manifest.entries


@pytest.mark.asyncio
async def test_runner_does_not_restart_running_injected_sandbox_session() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    injected_session = _FakeSession(Manifest(entries={"session.txt": File(content=b"session")}))
    injected_session._running = True
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=RunConfig(sandbox=SandboxRunConfig(session=injected_session)),
    )

    assert result.final_output == "done"
    assert injected_session.start_calls == 0
    assert injected_session.stop_calls == 0
    assert injected_session.shutdown_calls == 0


@pytest.mark.asyncio
async def test_runner_guardrail_trip_blocks_runner_owned_sandbox_creation() -> None:
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
        input_guardrails=[
            InputGuardrail(
                guardrail_function=_tripwire_input_guardrail,
                run_in_parallel=False,
            )
        ],
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, "hello", run_config=_sandbox_run_config(client))

    assert client.create_kwargs is None
    assert session.start_calls == 0
    assert session.stop_calls == 0
    assert session.shutdown_calls == 0
    assert session.close_dependency_calls == 0


@pytest.mark.asyncio
async def test_runner_guardrail_trip_blocks_running_injected_session_mutation() -> None:
    live_session = _LiveSessionDeltaRecorder(Manifest())
    live_session._running = True
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
        capabilities=[_ManifestMutationCapability()],
        input_guardrails=[
            InputGuardrail(
                guardrail_function=_tripwire_input_guardrail,
                run_in_parallel=False,
            )
        ],
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(
            agent,
            "hello",
            run_config=RunConfig(sandbox=SandboxRunConfig(session=live_session)),
        )

    assert "cap.txt" not in live_session.state.manifest.entries
    assert live_session.start_calls == 0
    assert live_session.applied_entry_batches == []
    assert live_session.stop_calls == 0
    assert live_session.shutdown_calls == 0


@pytest.mark.asyncio
async def test_runner_streamed_guardrail_trip_blocks_running_injected_session_mutation() -> None:
    live_session = _LiveSessionDeltaRecorder(Manifest())
    live_session._running = True
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
        capabilities=[_ManifestMutationCapability()],
        input_guardrails=[
            InputGuardrail(
                guardrail_function=_tripwire_input_guardrail,
                run_in_parallel=False,
            )
        ],
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(
            agent,
            "hello",
            run_config=RunConfig(sandbox=SandboxRunConfig(session=live_session)),
        )
        async for _ in result.stream_events():
            pass

    assert "cap.txt" not in live_session.state.manifest.entries
    assert live_session.start_calls == 0
    assert live_session.applied_entry_batches == []
    assert live_session.stop_calls == 0
    assert live_session.shutdown_calls == 0


@pytest.mark.asyncio
async def test_runner_uses_public_sandbox_agent_for_dynamic_instructions() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    seen_agents: list[Agent[Any]] = []

    def dynamic_instructions(_ctx: RunContextWrapper[Any], current_agent: Agent[Any]) -> str:
        seen_agents.append(current_agent)
        return "Saw public agent." if current_agent is agent else "Saw execution clone."

    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions=dynamic_instructions,
        capabilities=[
            _RecordingCapability(
                instruction_text="Capability instructions.",
                provided_tools=[get_function_tool("capability_tool", "ok")],
            )
        ],
    )

    result = await Runner.run(agent, "hello", run_config=_sandbox_run_config(client))

    assert result.final_output == "done"
    assert seen_agents == [agent]
    assert model.first_turn_args is not None
    assert model.first_turn_args["system_instructions"] == (
        f"{get_default_sandbox_instructions()}\n\n"
        "# Agent instructions\n\n"
        "Saw public agent.\n\n"
        "# Sandbox capability instructions\n\n"
        "Capability instructions.\n\n"
        f"{runtime_agent_preparation_module._filesystem_instructions(Manifest())}"
    )


@pytest.mark.asyncio
async def test_runner_uses_public_sandbox_agent_for_dynamic_prompts() -> None:
    seen_agents: list[Agent[Any]] = []

    def dynamic_prompt(data: GenerateDynamicPromptData) -> Prompt:
        seen_agents.append(data.agent)
        return {"id": "prompt_test", "variables": {"agent_name": data.agent.name}}

    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
        prompt=dynamic_prompt,
        capabilities=[_RecordingCapability(instruction_text="Capability instructions.")],
    )

    result = await Runner.run(
        agent, "hello", run_config=_sandbox_run_config(_FakeClient(_FakeSession(Manifest())))
    )

    assert result.final_output == "done"
    assert seen_agents == [agent]

    streamed_agent = SandboxAgent(
        name="streamed-sandbox",
        model=FakeModel(initial_output=[get_final_output_message("streamed done")]),
        instructions="Base instructions.",
        prompt=dynamic_prompt,
        capabilities=[_RecordingCapability(instruction_text="Capability instructions.")],
    )
    streamed = Runner.run_streamed(
        streamed_agent,
        "hello",
        run_config=_sandbox_run_config(_FakeClient(_FakeSession(Manifest()))),
    )
    async for _ in streamed.stream_events():
        pass

    assert streamed.final_output == "streamed done"
    assert seen_agents == [agent, streamed_agent]


@pytest.mark.asyncio
async def test_runner_uses_public_agent_for_call_model_input_filter() -> None:
    seen_agents: list[Agent[Any]] = []

    def capture_model_input(data: CallModelData[Any]) -> ModelInputData:
        seen_agents.append(data.agent)
        return data.model_data

    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
        capabilities=[_RecordingCapability(instruction_text="Capability instructions.")],
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=RunConfig(
            sandbox=SandboxRunConfig(
                client=_FakeClient(_FakeSession(Manifest())),
                options={"image": "sandbox"},
            ),
            call_model_input_filter=capture_model_input,
        ),
    )

    assert result.final_output == "done"
    assert seen_agents == [agent]


@pytest.mark.asyncio
async def test_runner_streamed_uses_public_agent_for_call_model_input_filter() -> None:
    seen_agents: list[Agent[Any]] = []

    def capture_model_input(data: CallModelData[Any]) -> ModelInputData:
        seen_agents.append(data.agent)
        return data.model_data

    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
        capabilities=[_RecordingCapability(instruction_text="Capability instructions.")],
    )

    result = Runner.run_streamed(
        agent,
        "hello",
        run_config=RunConfig(
            sandbox=SandboxRunConfig(
                client=_FakeClient(_FakeSession(Manifest())),
                options={"image": "sandbox"},
            ),
            call_model_input_filter=capture_model_input,
        ),
    )
    events = [event async for event in result.stream_events()]

    assert events
    assert result.final_output == "done"
    assert seen_agents == [agent]


@pytest.mark.asyncio
async def test_runner_reuses_prepared_sandbox_agent_across_turns_for_tool_choice_reset() -> None:
    model = FakeModel()
    tool = get_function_tool("capability_tool", "ok")
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("capability_tool", json.dumps({}))],
            [get_final_output_message("done")],
        ]
    )
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
        tools=[tool],
        model_settings=ModelSettings(tool_choice="required"),
    )

    result = await Runner.run(agent, "hello", run_config=_sandbox_run_config(client))

    assert result.final_output == "done"
    assert model.first_turn_args is not None
    assert model.first_turn_args["model_settings"].tool_choice == "required"
    assert model.last_turn_args["model_settings"].tool_choice is None


@pytest.mark.asyncio
async def test_runner_rebuilds_sandbox_resources_for_handoff_target_agent() -> None:
    triage_model = FakeModel()
    worker_model = FakeModel(initial_output=[get_final_output_message("done")])
    client = _ManifestSessionClient()
    triage_manifest = Manifest(entries={"README.md": File(content=b"Triage workspace")})
    worker_manifest = Manifest(entries={"README.md": File(content=b"Worker workspace")})
    worker = SandboxAgent(
        name="worker",
        model=worker_model,
        instructions="Worker instructions.",
        default_manifest=worker_manifest,
        capabilities=[_ManifestInstructionsCapability()],
    )
    triage = SandboxAgent(
        name="triage",
        model=triage_model,
        instructions="Triage instructions.",
        default_manifest=triage_manifest,
        capabilities=[_ManifestInstructionsCapability()],
        handoffs=[worker],
    )
    triage_model.turn_outputs = [[get_handoff_tool_call(worker)]]

    result = await Runner.run(
        triage,
        "route this",
        run_config=RunConfig(sandbox=SandboxRunConfig(client=client)),
    )

    assert result.final_output == "done"
    assert len(client.created_manifests) == 2
    assert client.created_manifests[0] is not None
    assert client.created_manifests[1] is not None
    assert (
        client.created_manifests[0].entries["README.md"]
        != client.created_manifests[1].entries["README.md"]
    )
    assert worker_model.first_turn_args is not None
    assert worker_model.first_turn_args["system_instructions"] == (
        f"{get_default_sandbox_instructions()}\n\n"
        "# Agent instructions\n\n"
        "Worker instructions.\n\n"
        "# Sandbox capability instructions\n\n"
        "Worker workspace\n\n"
        f"{runtime_agent_preparation_module._filesystem_instructions(worker_manifest)}"
    )


@pytest.mark.asyncio
async def test_runner_resumed_handoff_materializes_manifest_for_new_sandbox_agent() -> None:
    triage_model = FakeModel()
    worker_model = FakeModel(initial_output=[get_final_output_message("done")])
    client = _ManifestSessionClient()

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    triage_manifest = Manifest(entries={"README.md": File(content=b"Triage workspace")})
    worker_manifest = Manifest(entries={"README.md": File(content=b"Worker workspace")})
    worker = SandboxAgent(
        name="worker",
        model=worker_model,
        instructions="Worker instructions.",
        default_manifest=worker_manifest,
        capabilities=[_ManifestInstructionsCapability()],
    )
    triage = SandboxAgent(
        name="triage",
        model=triage_model,
        instructions="Triage instructions.",
        default_manifest=triage_manifest,
        tools=[approval_tool],
        capabilities=[_ManifestInstructionsCapability()],
        handoffs=[worker],
    )
    triage_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", json.dumps({}), call_id="call_resume")],
            [get_handoff_tool_call(worker)],
        ]
    )

    first_run = await Runner.run(
        triage,
        "route this",
        run_config=RunConfig(sandbox=SandboxRunConfig(client=client)),
    )

    assert len(first_run.interruptions) == 1
    state = first_run.to_state()
    state.approve(first_run.interruptions[0])

    resumed = await Runner.run(
        triage,
        state,
        run_config=RunConfig(sandbox=SandboxRunConfig(client=client)),
    )

    assert resumed.final_output == "done"
    assert len(client.created_manifests) == 2
    assert client.created_manifests[1] is not None
    assert worker_model.first_turn_args is not None
    assert worker_model.first_turn_args["system_instructions"] == (
        f"{get_default_sandbox_instructions()}\n\n"
        "# Agent instructions\n\n"
        "Worker instructions.\n\n"
        "# Sandbox capability instructions\n\n"
        "Worker workspace\n\n"
        f"{runtime_agent_preparation_module._filesystem_instructions(worker_manifest)}"
    )


@pytest.mark.asyncio
async def test_unix_local_client_rewrites_default_manifest_root_to_temp_workspace() -> None:
    client = UnixLocalSandboxClient()
    manifest = _unix_local_manifest(entries={"default.txt": File(content=b"default")})

    session = await client.create(manifest=manifest, options=None)
    workspace_root = Path(session.state.manifest.root)
    try:
        session_manifest = session.state.manifest
        session_state = cast(UnixLocalSandboxSessionState, session.state)

        assert session_manifest is not manifest
        assert session_manifest.entries == manifest.entries
        assert session_manifest.root != manifest.root
        assert workspace_root.is_absolute()
        assert workspace_root.name.startswith("sandbox-local-")
        assert session_state.workspace_root_owned is True
        assert manifest.root == "/workspace"
    finally:
        await client.delete(session)
    assert not workspace_root.exists()


@pytest.mark.asyncio
async def test_unix_local_client_delete_unmounts_workspace_mounts_before_rmtree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = UnixLocalSandboxClient()
    manifest = _unix_local_manifest(
        entries={
            "remote": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
        }
    )
    session = await client.create(manifest=manifest, options=None)
    workspace_root = Path(session.state.manifest.root)
    calls: list[str] = []
    real_rmtree = shutil.rmtree

    async def _fake_unmount(
        self: S3Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        _ = (self, session, dest, base_dir)
        calls.append("unmount")

    def _fake_rmtree(path: Path, ignore_errors: bool = False) -> None:
        _ = ignore_errors
        calls.append("rmtree")
        real_rmtree(path, ignore_errors=False)

    monkeypatch.setattr(S3Mount, "unmount", _fake_unmount)
    monkeypatch.setattr(shutil, "rmtree", _fake_rmtree)

    await client.delete(session)

    assert calls == ["unmount", "rmtree"]
    assert not workspace_root.exists()


@pytest.mark.asyncio
async def test_unix_local_client_delete_unmounts_nested_mounts_deepest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = UnixLocalSandboxClient()
    manifest = _unix_local_manifest(
        entries={
            "outer": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
            "outer/child": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
        }
    )
    session = await client.create(manifest=manifest, options=None)
    order: list[Path] = []

    async def _fake_unmount(
        self: S3Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        _ = (self, session, base_dir)
        order.append(dest)

    monkeypatch.setattr(S3Mount, "unmount", _fake_unmount)

    await client.delete(session)

    root = Path(session.state.manifest.root)
    assert order == [root / "outer" / "child", root / "outer"]


@pytest.mark.asyncio
async def test_unix_local_client_delete_skips_rmtree_when_unmount_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = UnixLocalSandboxClient()
    manifest = _unix_local_manifest(
        entries={
            "remote": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
        }
    )
    session = await client.create(manifest=manifest, options=None)
    workspace_root = Path(session.state.manifest.root)
    rmtree_called = False

    async def _failing_unmount(
        self: S3Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        _ = (self, session, dest, base_dir)
        raise RuntimeError("busy")

    def _fake_rmtree(path: Path, ignore_errors: bool = False) -> None:
        _ = (path, ignore_errors)
        nonlocal rmtree_called
        rmtree_called = True

    monkeypatch.setattr(S3Mount, "unmount", _failing_unmount)
    monkeypatch.setattr(shutil, "rmtree", _fake_rmtree)

    await client.delete(session)

    assert rmtree_called is False
    assert workspace_root.exists()

    shutil.rmtree(workspace_root, ignore_errors=True)


@pytest.mark.asyncio
async def test_unix_local_persist_workspace_excludes_mounted_directory_contents() -> None:
    workspace_root = Path(tempfile.mkdtemp(prefix="workspace-root-"))
    (workspace_root / "logical").mkdir(parents=True)
    (workspace_root / "logical" / "marker.txt").write_text("logical", encoding="utf-8")
    (workspace_root / "actual").mkdir(parents=True)
    (workspace_root / "actual" / "mounted.txt").write_text("mounted", encoding="utf-8")
    session = UnixLocalSandboxSession.from_state(
        UnixLocalSandboxSessionState(
            session_id=uuid.uuid4(),
            manifest=_unix_local_manifest(
                root=str(workspace_root),
                entries={
                    "logical": S3Mount(
                        bucket="bucket",
                        mount_path=Path("actual"),
                        mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
                    ),
                },
            ),
            snapshot=NoopSnapshot(id="snapshot"),
            workspace_root_owned=False,
        )
    )

    try:
        archive = await session.persist_workspace()
        payload = archive.read()
        if not isinstance(payload, bytes):
            raise AssertionError(f"Expected bytes archive payload, got {type(payload)!r}")
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tar:
            names = tar.getnames()
    finally:
        shutil.rmtree(workspace_root)

    assert names == ["."]


@pytest.mark.asyncio
async def test_runner_allows_fresh_unix_local_sessions_without_options() -> None:
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=_unix_local_run_config(),
    )

    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_unix_local_client_delete_preserves_caller_owned_workspace_root() -> None:
    client = UnixLocalSandboxClient()
    workspace_root = Path(tempfile.mkdtemp(prefix="caller-owned-"))
    manifest = _unix_local_manifest(root=str(workspace_root))

    session = await client.create(manifest=manifest, options=None)
    assert cast(UnixLocalSandboxSessionState, session.state).workspace_root_owned is False

    await client.delete(session)

    assert workspace_root.exists()
    shutil.rmtree(workspace_root)


@pytest.mark.asyncio
async def test_unix_local_runner_cleanup_preserves_resumed_caller_owned_workspace_root() -> None:
    workspace_root = Path(tempfile.mkdtemp(prefix="resumed-owned-"))
    state = UnixLocalSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=_unix_local_manifest(root=str(workspace_root)),
        snapshot=NoopSnapshot(id=str(uuid.uuid4())),
    )
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
    )

    try:
        result = await Runner.run(
            agent,
            "hello",
            run_config=_unix_local_run_config(session_state=state),
        )
    finally:
        assert workspace_root.exists()
        shutil.rmtree(workspace_root)

    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_unix_local_read_and_write_reject_paths_outside_workspace_root() -> None:
    client = UnixLocalSandboxClient()
    workspace_root = Path(tempfile.mkdtemp(prefix="workspace-root-"))
    session = await client.create(
        manifest=_unix_local_manifest(root=str(workspace_root)),
        options=None,
    )

    try:
        with pytest.raises(InvalidManifestPathError, match="must not escape root"):
            await session.write(Path("../secret.txt"), io.BytesIO(b"nope"))
        with pytest.raises(InvalidManifestPathError, match="must not escape root"):
            await session.read(Path("../secret.txt"))
    finally:
        await client.delete(session)
        shutil.rmtree(workspace_root)


@pytest.mark.asyncio
async def test_unix_local_rm_recursive_ignores_missing_paths() -> None:
    client = UnixLocalSandboxClient()
    workspace_root = Path(tempfile.mkdtemp(prefix="workspace-root-"))
    session = await client.create(
        manifest=_unix_local_manifest(root=str(workspace_root)),
        options=None,
    )

    try:
        await session.rm("missing-dir", recursive=True)
    finally:
        await client.delete(session)
        shutil.rmtree(workspace_root)


@pytest.mark.asyncio
async def test_unix_local_rm_non_recursive_still_errors_for_missing_paths() -> None:
    client = UnixLocalSandboxClient()
    workspace_root = Path(tempfile.mkdtemp(prefix="workspace-root-"))
    session = await client.create(
        manifest=_unix_local_manifest(root=str(workspace_root)),
        options=None,
    )

    try:
        with pytest.raises(ExecNonZeroError):
            await session.rm("missing-dir")
    finally:
        await client.delete(session)
        shutil.rmtree(workspace_root)


@pytest.mark.asyncio
async def test_wrapped_unix_local_helpers_reject_symlink_escape_paths(tmp_path: Path) -> None:
    client = UnixLocalSandboxClient()
    workspace_root = tmp_path / "workspace"
    session = await client.create(
        manifest=_unix_local_manifest(root=str(workspace_root)),
        options=None,
    )

    try:
        workspace_root.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, workspace_root / "link", target_is_directory=True)

        with pytest.raises(InvalidManifestPathError, match="must not escape root"):
            await session.mkdir("link/nested", parents=True)
        with pytest.raises(InvalidManifestPathError, match="must not escape root"):
            await session.ls("link")
        with pytest.raises(InvalidManifestPathError, match="must not escape root"):
            await session.rm("link/file.txt")
    finally:
        await client.delete(session)


@pytest.mark.asyncio
async def test_runner_streamed_ignores_sandbox_cleanup_failures_after_success() -> None:
    session = _FailingStopSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
    )

    result = Runner.run_streamed(agent, "hello", run_config=_sandbox_run_config(client))
    events = [event async for event in result.stream_events()]

    assert events
    assert result.final_output == "done"
    assert result._sandbox_session is None


@pytest.mark.asyncio
async def test_runner_omits_sandbox_resume_state_when_cleanup_fails() -> None:
    session = _FailingStopSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
    )

    result = await Runner.run(agent, "hello", run_config=_sandbox_run_config(client))
    state = result.to_state()

    assert result.final_output == "done"
    assert result._sandbox_resume_state is None
    assert result._sandbox_session is None
    assert state._sandbox is None


@pytest.mark.asyncio
async def test_runner_clears_sandbox_session_from_non_streamed_results_after_cleanup() -> None:
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
    )

    result = await Runner.run(agent, "hello", run_config=_sandbox_run_config(client))

    assert result.final_output == "done"
    assert result._sandbox_session is None


@pytest.mark.asyncio
async def test_runner_streamed_cleans_sandbox_once_after_stream_completion() -> None:
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
    )

    result = Runner.run_streamed(agent, "hello", run_config=_sandbox_run_config(client))
    events = [event async for event in result.stream_events()]
    await asyncio.sleep(0)

    assert events
    assert result.final_output == "done"
    assert result._sandbox_session is None
    assert session.stop_calls == 1
    assert session.shutdown_calls == 1
    assert session.close_dependency_calls == 1
    assert client.delete_calls == 1


@pytest.mark.asyncio
async def test_runner_uses_public_agent_for_non_streaming_output_guardrails() -> None:
    seen_agents: list[Agent[None]] = []

    async def output_guardrail(
        _context: RunContextWrapper[None],
        guardrail_agent: Agent[None],
        _output: object,
    ) -> GuardrailFunctionOutput:
        seen_agents.append(guardrail_agent)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
        capabilities=[_RecordingCapability(instruction_text="Capability instructions.")],
        output_guardrails=[OutputGuardrail(guardrail_function=output_guardrail)],
    )

    result = await Runner.run(
        agent, "hello", run_config=_sandbox_run_config(_FakeClient(_FakeSession(Manifest())))
    )

    assert result.final_output == "done"
    assert seen_agents == [agent]


@pytest.mark.asyncio
async def test_runner_streamed_immediate_cancel_skips_waiting_for_sandbox_cleanup() -> None:
    stop_gate = asyncio.Event()
    session = _BlockingStopSession(Manifest(), stop_gate)
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
    )

    result = Runner.run_streamed(agent, "hello", run_config=_sandbox_run_config(client))

    async def consume_with_cancel() -> None:
        async for _event in result.stream_events():
            result.cancel(mode="immediate")
            break

    try:
        await asyncio.wait_for(consume_with_cancel(), timeout=0.2)
    finally:
        stop_gate.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_runner_streamed_run_loop_task_waits_for_sandbox_cleanup_and_persisted_state() -> (
    None
):
    stop_gate = asyncio.Event()
    session = _PersistingStopSession(Manifest(), stop_gate)
    client = _FakeClient(session)
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_final_output_message("done")],
            [get_final_output_message("again")],
        ]
    )
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
    )
    run_config = _sandbox_run_config(client)

    result = Runner.run_streamed(agent, "hello", run_config=run_config)
    assert result.run_loop_task is not None

    while session.stop_calls == 0:
        await asyncio.sleep(0)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(result.run_loop_task), timeout=0.05)

    stop_gate.set()
    await result.run_loop_task

    state = result.to_state()
    assert state._sandbox is not None
    session_state = state._sandbox["session_state"]
    assert isinstance(session_state, dict)
    snapshot = session_state["snapshot"]
    assert isinstance(snapshot, dict)
    assert snapshot["marker"] == "persisted"

    second = await Runner.run(agent, "again", run_config=run_config)

    assert second.final_output == "again"


@pytest.mark.asyncio
async def test_runner_rejects_unix_local_manifest_user_and_group_provisioning() -> None:
    workspace_root = Path(tempfile.mkdtemp(prefix="unix-local-users-"))
    session = await UnixLocalSandboxClient().create(
        manifest=_unix_local_manifest(
            root=str(workspace_root),
            users=[User(name="sandbox-user")],
        ),
        options=None,
    )

    try:
        with pytest.raises(ValueError, match="does not support manifest users or groups"):
            await session.start()
    finally:
        shutil.rmtree(workspace_root)


@pytest.mark.asyncio
async def test_runner_persists_workspace_and_tool_choice_state_across_sandbox_resume() -> None:
    client = UnixLocalSandboxClient()
    file_capability = _SessionFileCapability()

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "write_file",
                    json.dumps({"path": "note.txt", "content": "persist me"}),
                    call_id="call_write",
                )
            ],
            [
                get_function_tool_call(
                    "approval_tool",
                    json.dumps({}),
                    call_id="call_approval",
                )
            ],
        ]
    )
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
        tools=[approval_tool],
        capabilities=[file_capability],
        model_settings=ModelSettings(tool_choice="required"),
    )

    first_run = await Runner.run(
        agent,
        "hello",
        run_config=_unix_local_run_config(client=client),
    )

    assert len(first_run.interruptions) == 1
    state = first_run.to_state()
    assert state._sandbox is not None
    assert state._sandbox["backend_id"] == "unix_local"
    session_state = state._sandbox["session_state"]
    assert isinstance(session_state, dict)
    snapshot_payload = session_state.get("snapshot")
    assert isinstance(snapshot_payload, dict)
    assert snapshot_payload.get("type") == "local"
    sessions_by_agent = state._sandbox["sessions_by_agent"]
    assert isinstance(sessions_by_agent, dict)
    assert sessions_by_agent[agent.name] == {
        "agent_name": agent.name,
        "session_state": session_state,
    }

    state_json = state.to_json()
    resumed_model = FakeModel()
    resumed_model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "read_file",
                    json.dumps({"path": "note.txt"}),
                    call_id="call_read",
                )
            ],
            [get_final_output_message("done")],
        ]
    )
    resumed_agent = SandboxAgent(
        name="sandbox",
        model=resumed_model,
        instructions="Base instructions.",
        tools=[approval_tool],
        capabilities=[_SessionFileCapability()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    restored_state = await RunState.from_json(resumed_agent, state_json)
    restored_state.approve(restored_state.get_interruptions()[0])
    resumed = await Runner.run(
        resumed_agent,
        restored_state,
        run_config=_unix_local_run_config(client=client),
    )

    assert resumed.final_output == "done"
    assert resumed_model.last_turn_args["model_settings"].tool_choice is None
    assert any(
        isinstance(item, ToolCallOutputItem)
        and item.output == "persist me"
        and item.agent is resumed_agent
        for item in resumed.new_items
    )


@pytest.mark.asyncio
async def test_runner_restores_all_sandbox_agents_from_run_state_across_handoffs() -> None:
    client = UnixLocalSandboxClient()
    file_capability = _SessionFileCapability()

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    triage_model = FakeModel()
    worker_model = FakeModel()
    worker = SandboxAgent(
        name="worker",
        model=worker_model,
        instructions="Worker instructions.",
        tools=[approval_tool],
    )
    triage = SandboxAgent(
        name="triage",
        model=triage_model,
        instructions="Triage instructions.",
        capabilities=[file_capability],
        handoffs=[worker],
    )
    worker.handoffs = [triage]
    triage_model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "write_file",
                    json.dumps({"path": "note.txt", "content": "persist triage"}),
                    call_id="call_write",
                )
            ],
            [get_handoff_tool_call(worker)],
        ]
    )
    worker_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", json.dumps({}), call_id="call_approval")],
        ]
    )

    first_run = await Runner.run(
        triage,
        "hello",
        run_config=_unix_local_run_config(client=client),
    )

    assert len(first_run.interruptions) == 1
    state = first_run.to_state()
    assert state._sandbox is not None
    assert state._sandbox["backend_id"] == "unix_local"
    assert state._sandbox["current_agent_name"] == worker.name
    sessions_by_agent = state._sandbox["sessions_by_agent"]
    assert isinstance(sessions_by_agent, dict)
    assert set(sessions_by_agent) == {triage.name, worker.name}

    state_json = state.to_json()
    resumed_triage_model = FakeModel()
    resumed_worker_model = FakeModel()
    resumed_worker = SandboxAgent(
        name="worker",
        model=resumed_worker_model,
        instructions="Worker instructions.",
        tools=[approval_tool],
    )
    resumed_triage = SandboxAgent(
        name="triage",
        model=resumed_triage_model,
        instructions="Triage instructions.",
        capabilities=[_SessionFileCapability()],
        handoffs=[resumed_worker],
    )
    resumed_worker.handoffs = [resumed_triage]
    resumed_worker_model.add_multiple_turn_outputs([[get_handoff_tool_call(resumed_triage)]])
    resumed_triage_model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "read_file",
                    json.dumps({"path": "note.txt"}),
                    call_id="call_read",
                )
            ],
            [get_final_output_message("done")],
        ]
    )

    restored_state = await RunState.from_json(resumed_triage, state_json)
    restored_state.approve(restored_state.get_interruptions()[0])
    resumed = await Runner.run(
        resumed_triage,
        restored_state,
        run_config=_unix_local_run_config(client=client),
    )

    assert resumed.final_output == "done"
    assert any(
        isinstance(item, ToolCallOutputItem)
        and item.output == "persist triage"
        and item.agent is resumed_triage
        for item in resumed.new_items
    )


@pytest.mark.asyncio
async def test_runner_serializes_unique_sandbox_resume_keys_for_duplicate_agent_names() -> None:
    client = UnixLocalSandboxClient()
    file_capability = _SessionFileCapability()

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    first_model = FakeModel()
    second_model = FakeModel()
    first = SandboxAgent(
        name="sandbox",
        model=first_model,
        instructions="First instructions.",
        capabilities=[file_capability],
    )
    second = SandboxAgent(
        name="sandbox",
        model=second_model,
        instructions="Second instructions.",
        tools=[approval_tool],
    )
    first.handoffs = [second]
    second.handoffs = [first]
    first_model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "write_file",
                    json.dumps({"path": "note.txt", "content": "first"}),
                    call_id="call_write",
                )
            ],
            [get_handoff_tool_call(second)],
            [
                get_function_tool_call(
                    "read_file",
                    json.dumps({"path": "note.txt"}),
                    call_id="call_read",
                )
            ],
            [get_final_output_message("done")],
        ]
    )
    second_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", json.dumps({}), call_id="call_approval")],
            [get_handoff_tool_call(first)],
        ]
    )

    first_run = await Runner.run(
        first,
        "hello",
        run_config=_unix_local_run_config(client=client),
    )

    state = first_run.to_state()
    assert state._sandbox is not None
    sessions_by_agent = cast(dict[str, dict[str, object]], state._sandbox["sessions_by_agent"])
    assert len(sessions_by_agent) == 2
    assert state._sandbox["current_agent_key"] in sessions_by_agent

    state.approve(first_run.interruptions[0])
    resumed = await Runner.run(
        first,
        state,
        run_config=_unix_local_run_config(client=client),
    )

    assert resumed.final_output == "done"
    assert any(
        isinstance(item, ToolCallOutputItem) and item.output == "first" and item.agent is first
        for item in resumed.new_items
    )


def test_duplicate_name_sandbox_identity_map_uses_capability_and_manifest_config() -> None:
    """Duplicate-name sandbox identities should stay stable when only sandbox config differs."""

    def _make_agent(readme: bytes, capability_text: str) -> SandboxAgent[None]:
        return SandboxAgent(
            name="sandbox",
            model=FakeModel(),
            instructions="Base instructions.",
            default_manifest=Manifest(entries={"README.md": File(content=readme)}),
            capabilities=[_RecordingCapability(instruction_text=capability_text)],
        )

    def _identity_for(identity_map: dict[str, Agent[Any]], target: Agent[Any]) -> str:
        return next(identity for identity, agent in identity_map.items() if agent is target)

    first_alpha = _make_agent(b"alpha", "Alpha capability.")
    first_beta = _make_agent(b"beta", "Beta capability.")
    first_root = Agent(name="triage", handoffs=[first_beta, first_alpha])
    first_alpha.handoffs = [first_root]
    first_beta.handoffs = [first_root]

    second_alpha = _make_agent(b"alpha", "Alpha capability.")
    second_beta = _make_agent(b"beta", "Beta capability.")
    second_root = Agent(name="triage", handoffs=[second_alpha, second_beta])
    second_alpha.handoffs = [second_root]
    second_beta.handoffs = [second_root]

    first_identity_map = _build_agent_identity_map(first_root)
    second_identity_map = _build_agent_identity_map(second_root)

    assert _identity_for(first_identity_map, first_alpha) == _identity_for(
        second_identity_map, second_alpha
    )
    assert _identity_for(first_identity_map, first_beta) == _identity_for(
        second_identity_map, second_beta
    )


@pytest.mark.asyncio
async def test_session_manager_reserves_current_duplicate_resume_key_for_current_agent() -> None:
    manifest = Manifest(entries={"README.md": File(content=b"duplicate resume")})
    client = _FakeClient(_FakeSession(manifest))
    first = SandboxAgent(name="sandbox", model=FakeModel(), instructions="First.")
    second = SandboxAgent(name="sandbox", model=FakeModel(), instructions="Second.")
    first.handoffs = [second]
    second.handoffs = [first]
    first_session_state = client.serialize_session_state(
        TestSessionState(manifest=Manifest(), snapshot=NoopSnapshot(id="first"))
    )
    second_session_state = client.serialize_session_state(
        TestSessionState(manifest=Manifest(), snapshot=NoopSnapshot(id="second"))
    )
    run_state: RunState[Any, Agent[Any]] = cast(
        RunState[Any, Agent[Any]],
        RunState(
            context=RunContextWrapper(context={}),
            original_input="hello",
            starting_agent=first,
        ),
    )
    run_state._current_agent = second
    run_state._sandbox = {
        "backend_id": "fake",
        "current_agent_key": "sandbox#2",
        "current_agent_name": second.name,
        "session_state": second_session_state,
        "sessions_by_agent": {
            "sandbox": {"agent_name": first.name, "session_state": first_session_state},
            "sandbox#2": {"agent_name": second.name, "session_state": second_session_state},
        },
    }
    manager = SandboxRuntimeSessionManager(
        starting_agent=first,
        sandbox_config=SandboxRunConfig(client=client, options={"image": "sandbox"}),
        run_state=run_state,
    )

    assert (
        manager._resume_state_payload_for_agent(client=client, agent=first, agent_id=id(first))
        == first_session_state
    )
    assert (
        manager._resume_state_payload_for_agent(client=client, agent=second, agent_id=id(second))
        == second_session_state
    )


def test_session_manager_generates_collision_free_resume_keys_for_literal_suffix_names() -> None:
    client = _FakeClient(_FakeSession(Manifest()))
    first = SandboxAgent(name="sandbox", model=FakeModel(), instructions="First.")
    literal_suffix = SandboxAgent(name="sandbox#2", model=FakeModel(), instructions="Literal.")
    second = SandboxAgent(name="sandbox", model=FakeModel(), instructions="Second.")
    first.handoffs = [literal_suffix, second]
    literal_suffix.handoffs = [first, second]
    second.handoffs = [first, literal_suffix]
    manager = SandboxRuntimeSessionManager(
        starting_agent=first,
        sandbox_config=SandboxRunConfig(client=client, options={"image": "sandbox"}),
        run_state=None,
    )

    manager.acquire_agent(first)
    manager.acquire_agent(literal_suffix)
    manager.acquire_agent(second)

    assert manager._ensure_resume_key(first) == "sandbox"
    assert manager._ensure_resume_key(literal_suffix) == "sandbox#2"
    assert manager._ensure_resume_key(second) == "sandbox#3"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["create", "resume", "live_session"])
async def test_session_manager_passes_concurrency_limits_from_run_config(
    source: str,
) -> None:
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    live_session = _FakeSession(Manifest())
    client = _FakeClient(live_session)

    if source == "live_session":
        sandbox_config = SandboxRunConfig(
            session=live_session,
            concurrency_limits=SandboxConcurrencyLimits(
                manifest_entries=2,
                local_dir_files=3,
            ),
        )
    elif source == "resume":
        sandbox_config = SandboxRunConfig(
            client=client,
            session_state=TestSessionState(
                manifest=Manifest(),
                snapshot=NoopSnapshot(id="resume"),
            ),
            options={"image": "sandbox"},
            concurrency_limits=SandboxConcurrencyLimits(
                manifest_entries=2,
                local_dir_files=3,
            ),
        )
    else:
        sandbox_config = SandboxRunConfig(
            client=client,
            options={"image": "sandbox"},
            concurrency_limits=SandboxConcurrencyLimits(
                manifest_entries=2,
                local_dir_files=3,
            ),
        )

    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=sandbox_config,
        run_state=None,
    )

    manager.acquire_agent(agent)
    await manager.ensure_session(agent=agent, capabilities=[], is_resumed_state=source == "resume")

    assert live_session.concurrency_limit_values == [
        SandboxConcurrencyLimits(manifest_entries=2, local_dir_files=3)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["create", "resume", "live_session"])
async def test_session_manager_passes_archive_limits_from_run_config(
    source: str,
) -> None:
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    live_session = _FakeSession(Manifest())
    client = _FakeClient(live_session)
    archive_limits = SandboxArchiveLimits(
        max_input_bytes=10,
        max_extracted_bytes=20,
        max_members=30,
    )

    if source == "live_session":
        sandbox_config = SandboxRunConfig(
            session=live_session,
            archive_limits=archive_limits,
        )
    elif source == "resume":
        sandbox_config = SandboxRunConfig(
            client=client,
            session_state=TestSessionState(
                manifest=Manifest(),
                snapshot=NoopSnapshot(id="resume"),
            ),
            options={"image": "sandbox"},
            archive_limits=archive_limits,
        )
    else:
        sandbox_config = SandboxRunConfig(
            client=client,
            options={"image": "sandbox"},
            archive_limits=archive_limits,
        )

    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=sandbox_config,
        run_state=None,
    )

    manager.acquire_agent(agent)
    await manager.ensure_session(agent=agent, capabilities=[], is_resumed_state=source == "resume")

    assert live_session.archive_limit_values == [archive_limits]


@pytest.mark.asyncio
async def test_session_manager_default_archive_limits_preserves_no_resource_limits() -> None:
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    live_session = _FakeSession(Manifest())
    client = _FakeClient(live_session)
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(client=client, options={"image": "sandbox"}),
        run_state=None,
    )

    manager.acquire_agent(agent)
    await manager.ensure_session(agent=agent, capabilities=[], is_resumed_state=False)

    assert live_session.archive_limit_values == [None]


@pytest.mark.asyncio
async def test_session_manager_rejects_invalid_archive_limits() -> None:
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    client = _FakeClient(_FakeSession(Manifest()))
    limits = SandboxArchiveLimits(max_input_bytes=1)
    limits.max_input_bytes = 0
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(
            client=client,
            options={"image": "sandbox"},
            archive_limits=limits,
        ),
        run_state=None,
    )

    manager.acquire_agent(agent)
    with pytest.raises(ValueError) as exc_info:
        await manager.ensure_session(agent=agent, capabilities=[], is_resumed_state=False)

    assert str(exc_info.value) == "archive_limits.max_input_bytes must be at least 1"
    assert client.create_kwargs is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (
            SandboxConcurrencyLimits(manifest_entries=0, local_dir_files=1),
            "concurrency_limits.manifest_entries must be at least 1",
        ),
        (
            SandboxConcurrencyLimits(manifest_entries=1, local_dir_files=0),
            "concurrency_limits.local_dir_files must be at least 1",
        ),
    ],
)
async def test_session_manager_rejects_invalid_concurrency_limits(
    limits: SandboxConcurrencyLimits,
    message: str,
) -> None:
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    client = _FakeClient(_FakeSession(Manifest()))
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(
            client=client,
            options={"image": "sandbox"},
            concurrency_limits=limits,
        ),
        run_state=None,
    )

    manager.acquire_agent(agent)
    with pytest.raises(ValueError) as exc_info:
        await manager.ensure_session(agent=agent, capabilities=[], is_resumed_state=False)

    assert str(exc_info.value) == message
    assert client.create_kwargs is None


@pytest.mark.asyncio
async def test_session_manager_preserves_untouched_run_state_sessions_on_cleanup() -> None:
    manifest = Manifest(entries={"README.md": File(content=b"duplicate resume")})
    client = _FakeClient(_FakeSession(manifest))
    triage = SandboxAgent(name="triage", model=FakeModel(), instructions="Triage.")
    worker = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    triage.handoffs = [worker]
    worker.handoffs = [triage]
    triage_session_state = client.serialize_session_state(
        TestSessionState(manifest=Manifest(), snapshot=NoopSnapshot(id="triage"))
    )
    worker_session_state = client.serialize_session_state(
        TestSessionState(manifest=Manifest(), snapshot=NoopSnapshot(id="worker"))
    )
    run_state: RunState[Any, Agent[Any]] = cast(
        RunState[Any, Agent[Any]],
        RunState(
            context=RunContextWrapper(context={}),
            original_input="hello",
            starting_agent=triage,
        ),
    )
    run_state._current_agent = worker
    run_state._sandbox = {
        "backend_id": "fake",
        "current_agent_key": worker.name,
        "current_agent_name": worker.name,
        "session_state": worker_session_state,
        "sessions_by_agent": {
            triage.name: {"agent_name": triage.name, "session_state": triage_session_state},
            worker.name: {"agent_name": worker.name, "session_state": worker_session_state},
        },
    }
    manager = SandboxRuntimeSessionManager(
        starting_agent=triage,
        sandbox_config=SandboxRunConfig(client=client, options={"image": "sandbox"}),
        run_state=run_state,
    )

    manager.acquire_agent(worker)
    await manager.ensure_session(agent=worker, capabilities=[], is_resumed_state=True)
    payload = await manager.cleanup()

    assert payload is not None
    sessions_by_agent = cast(dict[str, dict[str, object]], payload["sessions_by_agent"])
    assert set(sessions_by_agent) == {triage.name, worker.name}
    assert sessions_by_agent[triage.name] == {
        "agent_name": triage.name,
        "session_state": triage_session_state,
    }
    assert sessions_by_agent[worker.name] == {
        "agent_name": worker.name,
        "session_state": worker_session_state,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_source", ["run_state", "session_state"])
async def test_session_manager_reapplies_capability_manifest_mutations_on_resume(
    resume_source: str,
) -> None:
    client = _FakeClient(_FakeSession(Manifest()))
    capability = _ManifestMutationCapability()
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    session_state = TestSessionState(
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="resume"),
    )

    run_state: RunState[Any, Agent[Any]] | None = None
    if resume_source == "run_state":
        run_state = cast(
            RunState[Any, Agent[Any]],
            RunState(
                context=RunContextWrapper(context={}),
                original_input="hello",
                starting_agent=agent,
            ),
        )
        run_state._current_agent = agent
        serialized_state = client.serialize_session_state(session_state)
        run_state._sandbox = {
            "backend_id": client.backend_id,
            "current_agent_key": agent.name,
            "current_agent_name": agent.name,
            "session_state": serialized_state,
            "sessions_by_agent": {
                agent.name: {
                    "agent_name": agent.name,
                    "session_state": serialized_state,
                }
            },
        }
        sandbox_config = SandboxRunConfig(client=client, options={"image": "sandbox"})
    else:
        sandbox_config = SandboxRunConfig(
            client=client,
            session_state=session_state,
            options={"image": "sandbox"},
        )

    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=sandbox_config,
        run_state=run_state,
    )

    manager.acquire_agent(agent)
    session = await manager.ensure_session(
        agent=agent,
        capabilities=[capability],
        is_resumed_state=True,
    )

    assert session.state.manifest.entries["cap.txt"] == File(content=b"capability")
    assert client.resume_state is not None
    assert client.resume_state.manifest.entries["cap.txt"] == File(content=b"capability")


@pytest.mark.asyncio
async def test_session_manager_adds_run_as_user_on_resume() -> None:
    client = _FakeClient(_FakeSession(Manifest()))
    run_as = User(name="sandbox-user")
    agent = SandboxAgent(
        name="worker",
        model=FakeModel(),
        instructions="Worker.",
        run_as=run_as,
    )
    session_state = TestSessionState(
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="resume"),
    )
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(
            client=client,
            session_state=session_state,
            options={"image": "sandbox"},
        ),
        run_state=None,
    )

    manager.acquire_agent(agent)
    session = await manager.ensure_session(
        agent=agent,
        capabilities=[],
        is_resumed_state=True,
    )

    assert session.state.manifest.users == [run_as]
    assert client.resume_state is not None
    assert client.resume_state.manifest.users == [run_as]


def test_session_manager_does_not_duplicate_run_as_user_from_group() -> None:
    run_as = User(name="sandbox-user")
    manifest = Manifest(groups=[Group(name="sandbox-group", users=[run_as])])

    processed = SandboxRuntimeSessionManager._manifest_with_run_as_user(manifest, run_as)

    assert processed is manifest
    assert processed.users == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["live_session", "session_state", "create"])
async def test_session_manager_applies_capability_manifest_mutations_with_session_parity(
    source: str,
) -> None:
    capability = _ManifestMutationCapability()
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    run_state: RunState[Any, Agent[Any]] | None = None

    if source == "live_session":
        live_session = _FakeSession(Manifest())
        sandbox_config = SandboxRunConfig(session=live_session)
    else:
        client = _FakeClient(_FakeSession(Manifest()))
        if source == "session_state":
            sandbox_config = SandboxRunConfig(
                client=client,
                session_state=TestSessionState(
                    manifest=Manifest(),
                    snapshot=NoopSnapshot(id="resume"),
                ),
                options={"image": "sandbox"},
            )
        else:
            sandbox_config = SandboxRunConfig(
                client=client,
                manifest=Manifest(),
                options={"image": "sandbox"},
            )

    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=sandbox_config,
        run_state=run_state,
    )

    manager.acquire_agent(agent)
    session = await manager.ensure_session(
        agent=agent,
        capabilities=[capability],
        is_resumed_state=False,
    )

    assert session.state.manifest.entries["cap.txt"] == File(content=b"capability")
    if source == "session_state":
        assert client.resume_state is not None
        assert client.resume_state.manifest.entries["cap.txt"] == File(content=b"capability")
    if source == "create":
        assert client.create_kwargs is not None
        manifest = client.create_kwargs["manifest"]
        assert manifest is not None
        assert manifest.entries["cap.txt"] == File(content=b"capability")


@pytest.mark.asyncio
async def test_session_manager_starts_stopped_injected_session_with_manifest_mutation() -> None:
    live_session = _LiveSessionDeltaRecorder(Manifest())
    capability = _ManifestMutationCapability()
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(session=live_session),
        run_state=None,
    )

    manager.acquire_agent(agent)
    session = await manager.ensure_session(
        agent=agent,
        capabilities=[capability],
        is_resumed_state=False,
    )
    payload = await manager.cleanup()

    assert session is live_session
    assert live_session.start_calls == 1
    assert live_session.apply_manifest_calls == 0
    assert live_session.stop_calls == 0
    assert live_session.shutdown_calls == 0
    assert session.state.manifest.entries["cap.txt"] == File(content=b"capability")
    assert payload is None


@pytest.mark.asyncio
async def test_session_manager_materializes_running_injected_session_manifest_mutation() -> None:
    live_session = _LiveSessionDeltaRecorder(Manifest())
    live_session._running = True
    capability = _ManifestMutationCapability()
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(session=live_session),
        run_state=None,
    )

    manager.acquire_agent(agent)
    session = await manager.ensure_session(
        agent=agent,
        capabilities=[capability],
        is_resumed_state=False,
    )
    payload = await manager.cleanup()

    assert session is live_session
    assert live_session.start_calls == 0
    assert live_session.apply_manifest_calls == 0
    assert live_session.applied_entry_batches == [
        [(Path("/workspace/cap.txt"), File(content=b"capability"))]
    ]
    assert session.state.manifest.entries["cap.txt"] == File(content=b"capability")
    assert live_session.stop_calls == 0
    assert live_session.shutdown_calls == 0
    assert payload is None


@pytest.mark.asyncio
async def test_session_manager_retries_running_injected_session_delta_apply_after_failure() -> None:
    live_session = _LiveSessionDeltaRecorder(Manifest(), fail_entry_batch_times=1)
    live_session._running = True
    capability = _ManifestMutationCapability()
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(session=live_session),
        run_state=None,
    )

    manager.acquire_agent(agent)
    with pytest.raises(RuntimeError, match="delta apply failed"):
        await manager.ensure_session(
            agent=agent,
            capabilities=[capability],
            is_resumed_state=False,
        )

    assert live_session.state.manifest.entries == {}
    assert live_session.applied_entry_batches == [
        [(Path("/workspace/cap.txt"), File(content=b"capability"))]
    ]

    session = await manager.ensure_session(
        agent=agent,
        capabilities=[capability],
        is_resumed_state=False,
    )
    payload = await manager.cleanup()

    assert session is live_session
    assert live_session.state.manifest.entries["cap.txt"] == File(content=b"capability")
    assert live_session.applied_entry_batches == [
        [(Path("/workspace/cap.txt"), File(content=b"capability"))],
        [(Path("/workspace/cap.txt"), File(content=b"capability"))],
    ]
    assert payload is None


@pytest.mark.asyncio
async def test_session_manager_skips_rematerialization_for_unchanged_running_session() -> None:
    live_session = _LiveSessionDeltaRecorder(Manifest())
    live_session._running = True
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(session=live_session),
        run_state=None,
    )

    manager.acquire_agent(agent)
    session = await manager.ensure_session(
        agent=agent,
        capabilities=[Capability(type="noop")],
        is_resumed_state=False,
    )
    payload = await manager.cleanup()

    assert session is live_session
    assert live_session.start_calls == 0
    assert live_session.apply_manifest_calls == 0
    assert live_session.applied_entry_batches == []
    assert session.state.manifest.entries == {}
    assert live_session.stop_calls == 0
    assert live_session.shutdown_calls == 0
    assert payload is None


@pytest.mark.asyncio
async def test_session_manager_rejects_running_injected_session_account_mutation() -> None:
    live_session = _LiveSessionDeltaRecorder(Manifest())
    live_session._running = True
    agent = SandboxAgent(name="worker", model=FakeModel(), instructions="Worker.")
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(session=live_session),
        run_state=None,
    )

    manager.acquire_agent(agent)
    with pytest.raises(ValueError, match="manifest.users` or `manifest.groups"):
        await manager.ensure_session(
            agent=agent,
            capabilities=[_ManifestUsersCapability()],
            is_resumed_state=False,
        )

    assert live_session.apply_manifest_calls == 0
    assert live_session.applied_entry_batches == []
    assert live_session.state.manifest.users == []


@pytest.mark.asyncio
async def test_session_manager_preserves_existing_payload_when_no_sandbox_session_is_used() -> None:
    client = _FakeClient(_FakeSession(Manifest()))
    agent = SandboxAgent(name="sandbox", model=FakeModel(), instructions="Base instructions.")
    run_state: RunState[Any, Agent[Any]] = cast(
        RunState[Any, Agent[Any]],
        RunState(
            context=RunContextWrapper(context={}),
            original_input="hello",
            starting_agent=agent,
        ),
    )
    existing_payload = {
        "backend_id": "fake",
        "current_agent_key": agent.name,
        "current_agent_name": agent.name,
        "session_state": {"snapshot": {"id": "persisted"}},
        "sessions_by_agent": {
            agent.name: {
                "agent_name": agent.name,
                "session_state": {"snapshot": {"id": "persisted"}},
            }
        },
    }
    run_state._sandbox = existing_payload
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(client=client, options={"image": "sandbox"}),
        run_state=run_state,
    )

    payload = await manager.cleanup()

    assert payload == existing_payload
    assert payload is not existing_payload


@pytest.mark.asyncio
async def test_session_manager_omits_existing_payload_for_injected_live_session() -> None:
    agent = SandboxAgent(name="sandbox", model=FakeModel(), instructions="Base instructions.")
    live_session = _FakeSession(Manifest())
    run_state: RunState[Any, Agent[Any]] = cast(
        RunState[Any, Agent[Any]],
        RunState(
            context=RunContextWrapper(context={}),
            original_input="hello",
            starting_agent=agent,
        ),
    )
    run_state._sandbox = {
        "backend_id": "fake",
        "current_agent_key": agent.name,
        "current_agent_name": agent.name,
        "session_state": {"snapshot": {"id": "persisted"}},
        "sessions_by_agent": {
            agent.name: {
                "agent_name": agent.name,
                "session_state": {"snapshot": {"id": "persisted"}},
            }
        },
    }
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent,
        sandbox_config=SandboxRunConfig(session=live_session),
        run_state=run_state,
    )

    manager.acquire_agent(agent)
    await manager.ensure_session(agent=agent, capabilities=[], is_resumed_state=True)
    payload = await manager.cleanup()

    assert payload is None
    assert live_session.stop_calls == 0
    assert live_session.shutdown_calls == 0


@pytest.mark.asyncio
async def test_session_manager_uses_run_state_starting_agent_for_duplicate_resume_keys() -> None:
    manifest = Manifest(entries={"README.md": File(content=b"duplicate resume")})
    client = _FakeClient(_FakeSession(manifest))
    first = SandboxAgent(name="sandbox", model=FakeModel(), instructions="First.")
    second = SandboxAgent(name="sandbox", model=FakeModel(), instructions="Second.")
    approver = Agent(name="approver", model=FakeModel(), instructions="Approve.", handoffs=[])
    approver.handoffs = [second, first]
    first.handoffs = [second]
    second.handoffs = [approver]
    first_session_state = client.serialize_session_state(
        TestSessionState(manifest=Manifest(), snapshot=NoopSnapshot(id="first"))
    )
    second_session_state = client.serialize_session_state(
        TestSessionState(manifest=Manifest(), snapshot=NoopSnapshot(id="second"))
    )
    run_state: RunState[Any, Agent[Any]] = cast(
        RunState[Any, Agent[Any]],
        RunState(
            context=RunContextWrapper(context={}),
            original_input="hello",
            starting_agent=first,
        ),
    )
    run_state._current_agent = approver
    run_state._starting_agent = first
    run_state._sandbox = {
        "backend_id": "fake",
        "current_agent_key": "sandbox#2",
        "current_agent_name": second.name,
        "session_state": second_session_state,
        "sessions_by_agent": {
            "sandbox": {"agent_name": first.name, "session_state": first_session_state},
            "sandbox#2": {"agent_name": second.name, "session_state": second_session_state},
        },
    }
    manager = SandboxRuntimeSessionManager(
        starting_agent=approver,
        sandbox_config=SandboxRunConfig(client=client, options={"image": "sandbox"}),
        run_state=run_state,
    )

    assert (
        manager._resume_state_payload_for_agent(client=client, agent=first, agent_id=id(first))
        == first_session_state
    )
    assert (
        manager._resume_state_payload_for_agent(client=client, agent=second, agent_id=id(second))
        == second_session_state
    )


@pytest.mark.asyncio
async def test_session_manager_restores_duplicate_name_sessions_when_only_sandbox_config_differs():
    client = _FakeClient(_FakeSession(Manifest()))

    def _make_agent(readme: bytes, capability_text: str) -> SandboxAgent[None]:
        return SandboxAgent(
            name="sandbox",
            model=FakeModel(),
            instructions="Base instructions.",
            default_manifest=Manifest(entries={"README.md": File(content=readme)}),
            capabilities=[_RecordingCapability(instruction_text=capability_text)],
        )

    first = _make_agent(b"first", "First capability.")
    second = _make_agent(b"second", "Second capability.")
    root = Agent(name="triage", handoffs=[second, first])
    first.handoffs = [root]
    second.handoffs = [root]

    first_session_state = client.serialize_session_state(
        TestSessionState(manifest=Manifest(), snapshot=NoopSnapshot(id="first"))
    )
    second_session_state = client.serialize_session_state(
        TestSessionState(manifest=Manifest(), snapshot=NoopSnapshot(id="second"))
    )

    state: RunState[Any, Agent[Any]] = cast(
        RunState[Any, Agent[Any]],
        RunState(
            context=RunContextWrapper(context={}),
            original_input="hello",
            starting_agent=root,
        ),
    )
    state._current_agent = second
    state._sandbox = {
        "backend_id": "fake",
        "current_agent_key": "sandbox#2",
        "current_agent_name": second.name,
        "session_state": second_session_state,
        "sessions_by_agent": {
            "sandbox": {"agent_name": first.name, "session_state": first_session_state},
            "sandbox#2": {"agent_name": second.name, "session_state": second_session_state},
        },
    }

    restored_first = _make_agent(b"first", "First capability.")
    restored_second = _make_agent(b"second", "Second capability.")
    restored_root = Agent(name="triage", handoffs=[restored_first, restored_second])
    restored_first.handoffs = [restored_root]
    restored_second.handoffs = [restored_root]

    restored_state = await RunState.from_json(restored_root, state.to_json())
    assert restored_state._current_agent is restored_second

    manager = SandboxRuntimeSessionManager(
        starting_agent=restored_root,
        sandbox_config=SandboxRunConfig(client=client, options={"image": "sandbox"}),
        run_state=restored_state,
    )

    assert (
        manager._resume_state_payload_for_agent(
            client=client,
            agent=restored_first,
            agent_id=id(restored_first),
        )
        == first_session_state
    )
    assert (
        manager._resume_state_payload_for_agent(
            client=client,
            agent=restored_second,
            agent_id=id(restored_second),
        )
        == second_session_state
    )


@pytest.mark.asyncio
async def test_runner_restores_duplicate_name_sandbox_sessions_after_json_roundtrip() -> None:
    client = UnixLocalSandboxClient()
    file_capability = _SessionFileCapability()

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    first_model = FakeModel()
    second_model = FakeModel()
    first = SandboxAgent(
        name="sandbox",
        model=first_model,
        instructions="First instructions.",
        capabilities=[file_capability],
    )
    second = SandboxAgent(
        name="sandbox",
        model=second_model,
        instructions="Second instructions.",
        tools=[approval_tool],
    )
    first.handoffs = [second]
    second.handoffs = [first]
    first_model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "write_file",
                    json.dumps({"path": "note.txt", "content": "first"}),
                    call_id="call_write",
                )
            ],
            [get_handoff_tool_call(second)],
        ]
    )
    second_model.add_multiple_turn_outputs(
        [[get_function_tool_call("approval_tool", json.dumps({}), call_id="call_approval")]]
    )

    first_run = await Runner.run(
        first,
        "hello",
        run_config=_unix_local_run_config(client=client),
    )

    state = first_run.to_state()
    state_json = state.to_json()

    resumed_first_model = FakeModel()
    resumed_second_model = FakeModel()
    resumed_first = SandboxAgent(
        name="sandbox",
        model=resumed_first_model,
        instructions="First instructions.",
        capabilities=[_SessionFileCapability()],
    )
    resumed_second = SandboxAgent(
        name="sandbox",
        model=resumed_second_model,
        instructions="Second instructions.",
        tools=[approval_tool],
    )
    resumed_first.handoffs = [resumed_second]
    resumed_second.handoffs = [resumed_first]
    resumed_second_model.add_multiple_turn_outputs([[get_handoff_tool_call(resumed_first)]])
    resumed_first_model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "read_file",
                    json.dumps({"path": "note.txt"}),
                    call_id="call_read",
                )
            ],
            [get_final_output_message("done")],
        ]
    )

    restored_state = await RunState.from_json(resumed_first, state_json)
    restored_state.approve(restored_state.get_interruptions()[0])
    resumed = await Runner.run(
        resumed_first,
        restored_state,
        run_config=_unix_local_run_config(client=client),
    )

    assert resumed.final_output == "done"
    assert any(
        isinstance(item, ToolCallOutputItem)
        and item.output == "first"
        and item.agent is resumed_first
        for item in resumed.new_items
    )


@pytest.mark.asyncio
async def test_runner_restores_legacy_current_sandbox_payload_after_json_roundtrip() -> None:
    client = UnixLocalSandboxClient()

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    initial_model = FakeModel()
    initial_model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "write_file", json.dumps({"path": "note.txt", "content": "legacy"})
                )
            ],
            [get_function_tool_call("approval_tool", json.dumps({}), call_id="call_approval")],
        ]
    )
    agent = SandboxAgent(
        name="sandbox",
        model=initial_model,
        instructions="Base instructions.",
        tools=[approval_tool],
        capabilities=[_SessionFileCapability()],
    )

    first_run = await Runner.run(
        agent,
        "hello",
        run_config=_unix_local_run_config(client=client),
    )
    state = first_run.to_state()
    assert state._sandbox is not None
    session_state = cast(dict[str, object], state._sandbox["session_state"])
    state._sandbox = {
        "backend_id": "unix_local",
        "current_agent_id": id(agent),
        "session_state": session_state,
        "sessions_by_agent": {str(id(agent)): session_state},
    }

    resumed_model = FakeModel()
    resumed_model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "read_file", json.dumps({"path": "note.txt"}), call_id="call_read"
                )
            ],
            [get_final_output_message("done")],
        ]
    )
    resumed_agent = SandboxAgent(
        name="sandbox",
        model=resumed_model,
        instructions="Base instructions.",
        tools=[approval_tool],
        capabilities=[_SessionFileCapability()],
    )

    restored_state = await RunState.from_json(resumed_agent, state.to_json())
    restored_state.approve(restored_state.get_interruptions()[0])
    resumed = await Runner.run(
        resumed_agent,
        restored_state,
        run_config=_unix_local_run_config(client=client),
    )

    assert resumed.final_output == "done"
    assert any(
        isinstance(item, ToolCallOutputItem)
        and item.output == "legacy"
        and item.agent is resumed_agent
        for item in resumed.new_items
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="sandbox-exec is only available on macOS when installed",
)
async def test_unix_local_exec_confines_commands_to_workspace_root() -> None:
    workspace_root = Path(tempfile.mkdtemp(prefix="unix-local-exec-"))
    session = await UnixLocalSandboxClient().create(
        manifest=_unix_local_manifest(root=str(workspace_root)),
        options=None,
    )

    try:
        async with session:
            result = await session.exec("echo hi > note.txt && cat note.txt")
            assert result.ok()
            assert result.stdout.decode("utf-8", errors="replace").strip().endswith("hi")

            forbidden = await session.exec("cat /etc/passwd >/dev/null")
            assert not forbidden.ok()

            outside_write = await session.exec("echo nope > /usr/local/test-sandbox")
            assert not outside_write.ok()

            sibling = workspace_root.parent / "escape.txt"
            sibling.unlink(missing_ok=True)
            escaped = await session.exec("echo nope > ../escape.txt")
            assert not escaped.ok()
            assert not sibling.exists()
    finally:
        shutil.rmtree(workspace_root, ignore_errors=True)


@pytest.mark.asyncio
async def test_unix_local_exec_rejects_when_confinement_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = Path(tempfile.mkdtemp(prefix="unix-local-exec-"))
    session = await UnixLocalSandboxClient().create(
        manifest=_unix_local_manifest(root=str(workspace_root)),
        options=None,
    )
    unix_local = cast(Any, unix_local_module)
    monkeypatch.setattr(unix_local.sys, "platform", "darwin")
    monkeypatch.setattr(unix_local.shutil, "which", lambda _name: None)

    try:
        with pytest.raises(ExecTransportError) as exc_info:
            await session.exec("pwd")
    finally:
        shutil.rmtree(workspace_root, ignore_errors=True)

    assert exc_info.value.context["reason"] == "unix_local_confinement_unavailable"


@pytest.mark.asyncio
async def test_unix_local_exec_runs_without_wrapper_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = Path(tempfile.mkdtemp(prefix="unix-local-exec-"))
    session = await UnixLocalSandboxClient().create(
        manifest=_unix_local_manifest(root=str(workspace_root)),
        options=None,
    )
    unix_local = cast(Any, unix_local_module)
    monkeypatch.setattr(unix_local.sys, "platform", "linux")

    try:
        async with session:
            result = await session.exec("pwd")
    finally:
        shutil.rmtree(workspace_root, ignore_errors=True)

    assert result.ok()
    assert result.stdout.decode("utf-8", errors="replace").strip() == str(workspace_root.resolve())


@pytest.mark.asyncio
async def test_unix_local_file_io_allows_extra_path_grant(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    allowed_root = tmp_path / "allowed"
    workspace_root.mkdir()
    allowed_root.mkdir()
    session = UnixLocalSandboxSession.from_state(
        UnixLocalSandboxSessionState(
            session_id=uuid.uuid4(),
            manifest=Manifest(
                root=str(workspace_root),
                extra_path_grants=(SandboxPathGrant(path=str(allowed_root)),),
            ),
            snapshot=NoopSnapshot(id="extra-path-grant"),
            workspace_root_owned=False,
        )
    )

    await session.write(allowed_root / "result.txt", io.BytesIO(b"scratch output"))
    payload = await session.read(allowed_root / "result.txt")

    assert payload.read() == b"scratch output"


@pytest.mark.asyncio
async def test_unix_local_file_io_rejects_write_under_read_only_extra_path_grant(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    allowed_root = tmp_path / "allowed"
    workspace_root.mkdir()
    allowed_root.mkdir()
    (allowed_root / "existing.txt").write_text("readable", encoding="utf-8")
    session = UnixLocalSandboxSession.from_state(
        UnixLocalSandboxSessionState(
            session_id=uuid.uuid4(),
            manifest=Manifest(
                root=str(workspace_root),
                extra_path_grants=(SandboxPathGrant(path=str(allowed_root), read_only=True),),
            ),
            snapshot=NoopSnapshot(id="read-only-extra-path-grant"),
            workspace_root_owned=False,
        )
    )

    payload = await session.read(allowed_root / "existing.txt")
    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        await session.write(allowed_root / "result.txt", io.BytesIO(b"scratch output"))

    assert payload.read() == b"readable"
    assert str(exc_info.value) == f"failed to write archive for path: {allowed_root / 'result.txt'}"
    assert exc_info.value.context == {
        "path": str(allowed_root / "result.txt"),
        "reason": "read_only_extra_path_grant",
        "grant_path": str(allowed_root),
    }


def test_unix_local_confined_exec_command_allows_common_darwin_interpreter_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = Path(tempfile.mkdtemp(prefix="unix-local-exec-"))
    session = UnixLocalSandboxSession.from_state(
        UnixLocalSandboxSessionState(
            session_id=uuid.uuid4(),
            manifest=_unix_local_manifest(root=str(workspace_root)),
            snapshot=NoopSnapshot(id="darwin"),
            workspace_root_owned=False,
        )
    )
    unix_local = cast(Any, unix_local_module)
    host_home = Path.home()
    path_env = os.pathsep.join(
        [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            str(host_home / ".local" / "bin"),
        ]
    )

    def _fake_which(name: str, path: str | None = None) -> str | None:
        if name == "sandbox-exec":
            return "/usr/bin/sandbox-exec"
        if name == "python3":
            assert path == path_env
            return "/opt/homebrew/bin/python3"
        return None

    monkeypatch.setattr(unix_local.sys, "platform", "darwin")
    monkeypatch.setattr(unix_local.shutil, "which", _fake_which)

    command = session._confined_exec_command(
        command_parts=["python3", "-V"],
        workspace_root=workspace_root,
        env={"PATH": path_env},
    )
    profile = command[2]

    assert command[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert '(allow file-read-data file-read-metadata (subpath "/opt/homebrew"))' in profile
    assert '(allow file-read-data file-read-metadata (subpath "/usr/local"))' in profile
    assert (
        f'(allow file-read-data file-read-metadata (subpath "{host_home / ".local"}"))' in profile
    )
    assert '(deny file-write* (subpath "/opt"))' in profile
    assert '(allow file-write* (subpath "/opt/homebrew"))' not in profile


def test_unix_local_darwin_exec_profile_allows_extra_path_grants(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    read_write_root = tmp_path / "read-write"
    read_only_root = tmp_path / "read-only"
    workspace_root.mkdir()
    read_write_root.mkdir()
    read_only_root.mkdir()
    session = UnixLocalSandboxSession.from_state(
        UnixLocalSandboxSessionState(
            session_id=uuid.uuid4(),
            manifest=Manifest(
                root=str(workspace_root),
                extra_path_grants=(
                    SandboxPathGrant(path=str(read_write_root)),
                    SandboxPathGrant(path=str(read_only_root), read_only=True),
                ),
            ),
            snapshot=NoopSnapshot(id="darwin-extra-path-grant"),
            workspace_root_owned=False,
        )
    )

    profile = session._darwin_exec_profile(
        workspace_root,
        extra_path_grants=session._darwin_extra_path_grant_roots(),
    )
    profile_lines = set(profile.splitlines())

    assert (
        f'(allow file-read-data file-read-metadata (subpath "{read_write_root}"))' in profile_lines
    )
    assert f'(allow file-write* (subpath "{read_write_root}"))' in profile_lines
    assert (
        f'(allow file-read-data file-read-metadata (subpath "{read_only_root}"))' in profile_lines
    )
    assert f'(allow file-write* (subpath "{read_only_root}"))' not in profile_lines


def test_unix_local_darwin_exec_profile_denies_nested_read_only_extra_path_grant(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    read_write_root = tmp_path / "read-write"
    read_only_root = read_write_root / "protected"
    workspace_root.mkdir()
    read_only_root.mkdir(parents=True)
    session = UnixLocalSandboxSession.from_state(
        UnixLocalSandboxSessionState(
            session_id=uuid.uuid4(),
            manifest=Manifest(
                root=str(workspace_root),
                extra_path_grants=(
                    SandboxPathGrant(path=str(read_write_root)),
                    SandboxPathGrant(path=str(read_only_root), read_only=True),
                ),
            ),
            snapshot=NoopSnapshot(id="darwin-nested-extra-path-grant"),
            workspace_root_owned=False,
        )
    )

    profile = session._darwin_exec_profile(
        workspace_root,
        extra_path_grants=session._darwin_extra_path_grant_roots(),
    )
    profile_lines = profile.splitlines()
    parent_write_allow = f'(allow file-write* (subpath "{read_write_root}"))'
    child_write_deny = f'(deny file-write* (subpath "{read_only_root}"))'

    assert parent_write_allow in profile_lines
    assert child_write_deny in profile_lines
    assert profile_lines.index(parent_write_allow) < profile_lines.index(child_write_deny)
    assert f'(allow file-write* (subpath "{read_only_root}"))' not in profile_lines


def test_unix_local_darwin_exec_profile_rejects_extra_path_grant_symlink_to_root(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    root_alias = tmp_path / "root-alias"
    workspace_root.mkdir()
    root_alias.symlink_to(Path("/"), target_is_directory=True)
    session = UnixLocalSandboxSession.from_state(
        UnixLocalSandboxSessionState(
            session_id=uuid.uuid4(),
            manifest=Manifest(
                root=str(workspace_root),
                extra_path_grants=(SandboxPathGrant(path=str(root_alias)),),
            ),
            snapshot=NoopSnapshot(id="darwin-extra-path-grant-root-alias"),
            workspace_root_owned=False,
        )
    )

    with pytest.raises(ValueError) as exc_info:
        session._darwin_extra_path_grant_roots()

    assert str(exc_info.value) == "sandbox path grant path must not resolve to filesystem root"


@pytest.mark.asyncio
async def test_sandbox_run_persists_only_new_session_input_items() -> None:
    session = SimpleListSession(
        history=[
            {
                "role": "user",
                "content": "old",
            }
        ]
    )
    model = FakeModel(initial_output=[get_final_output_message("done")])
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
    )

    result = await Runner.run(
        agent,
        "new",
        session=session,
        run_config=_sandbox_run_config(_FakeClient(_FakeSession(Manifest()))),
    )

    assert result.final_output == "done"
    saved_user_items = [
        item
        for item in await session.get_items()
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    assert saved_user_items == [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "new"},
    ]


@pytest.mark.asyncio
async def test_runner_streamed_emits_public_agent_for_tool_and_reasoning_events() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                _get_reasoning_item(),
                get_function_tool_call("tool1", json.dumps({}), call_id="call_tool"),
            ],
            [get_final_output_message("done")],
        ]
    )
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
        tools=[get_function_tool("tool1", "tool result")],
    )

    result = Runner.run_streamed(
        agent,
        "hello",
        run_config=_sandbox_run_config(_FakeClient(_FakeSession(Manifest()))),
    )
    events = [event async for event in result.stream_events()]
    relevant_events = [
        event
        for event in events
        if isinstance(event, RunItemStreamEvent)
        and event.name in {"reasoning_item_created", "tool_called", "tool_output"}
    ]

    assert relevant_events
    assert all(event.item.agent is agent for event in relevant_events)


def test_capability_clone_deep_copies_nested_mutable_state() -> None:
    capability = _NestedStateCapability()

    cloned = cast(_NestedStateCapability, capability.clone())
    cloned.state["seen"].append("turn-1")

    assert capability.state == {"seen": []}
    assert cloned.state == {"seen": ["turn-1"]}


def test_capability_clone_deep_copies_nested_object_state() -> None:
    capability = _NestedObjectCapability()

    cloned = cast(_NestedObjectCapability, capability.clone())
    cloned.state.seen.append("turn-1")

    assert capability.state.seen == []
    assert cloned.state.seen == ["turn-1"]


def test_capability_clone_preserves_session_field_identity() -> None:
    capability = Shell()
    session = _FakeSession(Manifest())
    capability.bind(session)

    cloned = capability.clone()

    assert capability.session is session
    assert cloned.session is session
    assert capability.model_dump() == {"type": "shell"}
    assert cloned.model_dump() == {"type": "shell"}


@pytest.mark.asyncio
async def test_apply_manifest_raises_on_account_provisioning_failures() -> None:
    session = _ProvisioningFailureSession(
        Manifest(users=[User(name="sandbox-user")]),
    )

    with pytest.raises(ExecNonZeroError) as exc_info:
        await session.apply_manifest()

    assert exc_info.value.context["command_str"] == (
        "useradd -U -M -s /usr/sbin/nologin sandbox-user"
    )
    assert exc_info.value.context["stdout"] == "attempted useradd"
    assert exc_info.value.context["stderr"] == "missing useradd"
    assert exc_info.value.message == "stdout: attempted useradd\nstderr: missing useradd"


@pytest.mark.asyncio
async def test_apply_manifest_only_ephemeral_skips_account_provisioning_failures() -> None:
    session = _ProvisioningFailureSession(
        Manifest(users=[User(name="sandbox-user")]),
    )

    result = await session.apply_manifest(only_ephemeral=True)

    assert result.files == []


@pytest.mark.asyncio
async def test_resume_reprovisions_manifest_accounts_before_reapplying_ephemeral_entries() -> None:
    session = _RestorableProvisioningFailureSession(
        Manifest(users=[User(name="sandbox-user")]),
    )

    with pytest.raises(ExecNonZeroError):
        await session.start()

    assert session.cleared_workspace_root is True
    assert session.hydrate_calls == 1


@pytest.mark.asyncio
async def test_resume_can_skip_manifest_account_reprovisioning_when_os_state_is_preserved() -> None:
    session = _RestorableProvisioningFailureSession(
        Manifest(users=[User(name="sandbox-user")]),
        provision_on_resume=False,
    )

    await session.start()

    assert session.cleared_workspace_root is True
    assert session.hydrate_calls == 1


@pytest.mark.asyncio
async def test_clear_workspace_root_on_resume_preserves_nested_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _ls_entry(path: str, *, kind: EntryKind) -> FileEntry:
        return FileEntry(
            path=path,
            permissions=Permissions.from_str(
                "drwxr-xr-x" if kind == EntryKind.DIRECTORY else "-rw-r--r--"
            ),
            owner="root",
            group="root",
            size=0,
            kind=kind,
        )

    session = _FakeSession(
        Manifest(
            entries={
                "a/b": S3Mount(
                    bucket="bucket",
                    mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
                ),
            }
        )
    )
    ls_calls: list[Path] = []
    rm_calls: list[tuple[Path, bool]] = []

    async def _fake_ls(path: Path | str) -> list[FileEntry]:
        rendered = Path(path)
        ls_calls.append(rendered)
        if rendered == Path("/workspace"):
            return [
                _ls_entry("/workspace/a", kind=EntryKind.DIRECTORY),
                _ls_entry("/workspace/root.txt", kind=EntryKind.FILE),
            ]
        if rendered == Path("/workspace/a"):
            return [
                _ls_entry("/workspace/a/b", kind=EntryKind.DIRECTORY),
                _ls_entry("/workspace/a/local.txt", kind=EntryKind.FILE),
            ]
        raise AssertionError(f"unexpected ls path: {rendered}")

    async def _fake_rm(path: Path | str, *, recursive: bool = False) -> None:
        rm_calls.append((Path(path), recursive))

    monkeypatch.setattr(session, "ls", _fake_ls)
    monkeypatch.setattr(session, "rm", _fake_rm)

    await session._clear_workspace_root_on_resume()  # noqa: SLF001

    assert ls_calls == [Path("/workspace"), Path("/workspace/a")]
    assert rm_calls == [
        (Path("/workspace/a/local.txt"), True),
        (Path("/workspace/root.txt"), True),
    ]


@pytest.mark.asyncio
async def test_clear_workspace_root_on_resume_deletes_file_ancestor_of_skipped_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _ls_entry(path: str, *, kind: EntryKind) -> FileEntry:
        return FileEntry(
            path=path,
            permissions=Permissions.from_str(
                "drwxr-xr-x" if kind == EntryKind.DIRECTORY else "-rw-r--r--"
            ),
            owner="root",
            group="root",
            size=0,
            kind=kind,
        )

    session = _FakeSession(
        Manifest(
            entries={
                "a/b": S3Mount(
                    bucket="bucket",
                    mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
                ),
            }
        )
    )
    ls_calls: list[Path] = []
    rm_calls: list[tuple[Path, bool]] = []

    async def _fake_ls(path: Path | str) -> list[FileEntry]:
        rendered = Path(path)
        ls_calls.append(rendered)
        if rendered == Path("/workspace"):
            return [
                _ls_entry("/workspace/a", kind=EntryKind.FILE),
                _ls_entry("/workspace/root.txt", kind=EntryKind.FILE),
            ]
        raise AssertionError(f"unexpected ls path: {rendered}")

    async def _fake_rm(path: Path | str, *, recursive: bool = False) -> None:
        rm_calls.append((Path(path), recursive))

    monkeypatch.setattr(session, "ls", _fake_ls)
    monkeypatch.setattr(session, "rm", _fake_rm)

    await session._clear_workspace_root_on_resume()  # noqa: SLF001

    assert ls_calls == [Path("/workspace")]
    assert rm_calls == [
        (Path("/workspace/a"), True),
        (Path("/workspace/root.txt"), True),
    ]


@pytest.mark.asyncio
async def test_clear_workspace_root_on_resume_preserves_workspace_root_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        Manifest(
            entries={
                ".": S3Mount(
                    bucket="bucket",
                    mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
                ),
            }
        )
    )
    ls_calls: list[Path] = []
    rm_calls: list[tuple[Path, bool]] = []

    async def _fake_ls(path: Path | str) -> list[object]:
        ls_calls.append(Path(path))
        return []

    async def _fake_rm(path: Path | str, *, recursive: bool = False) -> None:
        rm_calls.append((Path(path), recursive))

    monkeypatch.setattr(session, "ls", _fake_ls)
    monkeypatch.setattr(session, "rm", _fake_rm)

    await session._clear_workspace_root_on_resume()  # noqa: SLF001

    assert ls_calls == []
    assert rm_calls == []


@pytest.mark.asyncio
async def test_prepare_agent_rechecks_session_liveness_before_reusing_cached_agent() -> None:
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
    )
    runtime = SandboxRuntime(
        starting_agent=agent,
        run_config=_sandbox_run_config(client),
        run_state=None,
    )
    context_wrapper = RunContextWrapper(context=None)

    first_prepared = await runtime.prepare_agent(
        current_agent=agent,
        current_input="hello",
        context_wrapper=context_wrapper,
        is_resumed_state=False,
    )
    assert session.start_calls == 1

    session._running = False

    second_prepared = await runtime.prepare_agent(
        current_agent=agent,
        current_input="hello again",
        context_wrapper=context_wrapper,
        is_resumed_state=False,
    )

    assert second_prepared.bindings.execution_agent is first_prepared.bindings.execution_agent
    assert session.start_calls == 2


@pytest.mark.asyncio
async def test_prepare_agent_binds_run_as_to_cloned_capabilities() -> None:
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    capability = _RecordingCapability()
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        capabilities=[capability],
        run_as="sandbox-user",
    )
    runtime = SandboxRuntime(
        starting_agent=agent,
        run_config=_sandbox_run_config(client),
        run_state=None,
    )

    prepared = await runtime.prepare_agent(
        current_agent=agent,
        current_input="hello",
        context_wrapper=RunContextWrapper(context=None),
        is_resumed_state=False,
    )

    execution_agent = cast(SandboxAgent[Any], prepared.bindings.execution_agent)
    prepared_capability = cast(_RecordingCapability, execution_agent.capabilities[0])
    assert capability.bound_session is None
    assert prepared_capability.bound_session is client.session
    assert prepared_capability.run_as == User(name="sandbox-user")


@pytest.mark.asyncio
async def test_prepare_agent_processes_context_with_bound_cached_capabilities() -> None:
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        capabilities=[_ProcessContextSessionCapability()],
    )
    runtime = SandboxRuntime(
        starting_agent=agent,
        run_config=_sandbox_run_config(client),
        run_state=None,
    )
    context_wrapper = RunContextWrapper(context=None)

    first_prepared = await runtime.prepare_agent(
        current_agent=agent,
        current_input=[{"role": "user", "content": "hello"}],
        context_wrapper=context_wrapper,
        is_resumed_state=False,
    )

    assert first_prepared.input == [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": "process_calls=1"},
    ]

    second_prepared = await runtime.prepare_agent(
        current_agent=agent,
        current_input=[{"role": "user", "content": "hello again"}],
        context_wrapper=context_wrapper,
        is_resumed_state=False,
    )

    assert second_prepared.bindings.execution_agent is first_prepared.bindings.execution_agent
    assert second_prepared.input == [
        {"role": "user", "content": "hello again"},
        {"role": "user", "content": "process_calls=2"},
    ]


@pytest.mark.asyncio
async def test_prepare_agent_starts_new_live_session_even_when_backend_reports_running() -> None:
    session = _FakeSession(Manifest())
    session._running = True
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
    )
    runtime = SandboxRuntime(
        starting_agent=agent,
        run_config=_sandbox_run_config(client),
        run_state=None,
    )

    await runtime.prepare_agent(
        current_agent=agent,
        current_input="hello",
        context_wrapper=RunContextWrapper(context=None),
        is_resumed_state=False,
    )

    assert session.start_calls == 1


@pytest.mark.asyncio
async def test_sandbox_runtime_emits_high_level_sdk_spans() -> None:
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Base instructions.",
    )
    runtime = SandboxRuntime(
        starting_agent=agent,
        run_config=_sandbox_run_config(client),
        run_state=None,
    )

    with trace("sandbox_runtime_test"):
        await runtime.prepare_agent(
            current_agent=agent,
            current_input="hello",
            context_wrapper=RunContextWrapper(context=None),
            is_resumed_state=False,
        )
        await runtime.cleanup()

    def _custom_span_names(node: dict[str, object]) -> list[str]:
        names: list[str] = []
        children = node.get("children", [])
        if not isinstance(children, list):
            return names
        for child in children:
            assert isinstance(child, dict)
            if child.get("type") == "custom":
                data = child.get("data", {})
                if isinstance(data, dict):
                    name = data.get("name")
                    if isinstance(name, str):
                        names.append(name)
            names.extend(_custom_span_names(child))
        return names

    normalized = fetch_normalized_spans()
    assert len(normalized) == 1
    names = _custom_span_names(normalized[0])
    assert {
        "sandbox.prepare_agent",
        "sandbox.create_session",
        "sandbox.start",
        "sandbox.cleanup",
        "sandbox.cleanup_sessions",
        "sandbox.stop",
        "sandbox.shutdown",
    }.issubset(set(names))


@pytest.mark.asyncio
async def test_runner_uses_public_agent_for_non_function_tool_outputs() -> None:
    tool = LocalShellTool(executor=lambda _request: "shell result")
    action = LocalShellCallAction(
        command=["bash", "-lc", "echo sandbox"],
        env={},
        type="exec",
        timeout_ms=1000,
        working_directory="/workspace",
    )
    local_shell_call = LocalShellCall(
        id="lsh_sandbox",
        action=action,
        call_id="call_local_shell",
        status="completed",
        type="local_shell_call",
    )

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [local_shell_call],
            [get_final_output_message("done")],
        ]
    )

    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
        tools=[tool],
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=_sandbox_run_config(_FakeClient(_FakeSession(Manifest()))),
    )

    output_items = [
        item
        for item in result.new_items
        if isinstance(item, ToolCallOutputItem)
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "local_shell_call_output"
    ]

    assert output_items
    assert all(item.agent is agent for item in output_items)


@pytest.mark.asyncio
async def test_sandbox_agent_as_tool_uses_runner_sandbox_prep() -> None:
    child_model = FakeModel(initial_output=[get_final_output_message("child done")])
    parent_model = FakeModel(
        initial_output=[
            get_function_tool_call("delegate_to_child", json.dumps({"input": "check sandbox"}))
        ]
    )
    parent_model.set_next_output([get_final_output_message("parent done")])

    capability = _RecordingCapability(instruction_text="Use the sandbox carefully.")
    manifest = Manifest(entries={"README.md": File(content=b"Use repo-safe commands only.")})
    session = _FakeSession(manifest)
    client = _FakeClient(session)

    child = SandboxAgent(
        name="child",
        model=child_model,
        instructions="Child base instructions.",
        default_manifest=manifest,
        capabilities=[capability],
    )
    parent = Agent(
        name="parent",
        model=parent_model,
        instructions="Parent instructions.",
        tools=[child.as_tool("delegate_to_child", "Delegate to the sandbox child.")],
    )

    result = await Runner.run(
        parent,
        "hello",
        run_config=_sandbox_run_config(client),
    )

    assert result.final_output == "parent done"
    assert capability.bound_session is None
    assert child_model.first_turn_args is not None
    child_input = child_model.first_turn_args["input"]
    assert isinstance(child_input, list)
    assert _extract_user_text(child_input[0]) == "check sandbox"


@pytest.mark.asyncio
async def test_runner_reapplies_sandbox_prep_on_handoff() -> None:
    triage_model = FakeModel()
    worker_model = FakeModel(initial_output=[get_final_output_message("done")])
    manifest = Manifest(entries={"README.md": File(content=b"Shared repo instructions.")})
    session = _FakeSession(manifest)
    client = _FakeClient(session)

    capability_one = _RecordingCapability(instruction_text="Triage capability.")
    capability_two = _RecordingCapability(instruction_text="Worker capability.")
    worker = SandboxAgent(
        name="worker",
        model=worker_model,
        instructions="Worker instructions.",
        default_manifest=manifest,
        capabilities=[capability_two],
    )
    triage = SandboxAgent(
        name="triage",
        model=triage_model,
        instructions="Triage instructions.",
        default_manifest=manifest,
        capabilities=[capability_one],
        handoffs=[worker],
    )
    triage_model.turn_outputs = [[get_handoff_tool_call(worker)]]

    result = await Runner.run(
        triage,
        "route this",
        run_config=_sandbox_run_config(client),
    )

    assert result.final_output == "done"
    assert capability_one.bound_session is None
    assert capability_two.bound_session is None
    assert worker_model.first_turn_args is not None
    assert worker_model.first_turn_args["system_instructions"] == (
        f"{get_default_sandbox_instructions()}\n\n"
        "# Agent instructions\n\n"
        "Worker instructions.\n\n"
        "# Sandbox capability instructions\n\n"
        "Worker capability.\n\n"
        f"{runtime_agent_preparation_module._filesystem_instructions(session.state.manifest)}"
    )


@pytest.mark.asyncio
async def test_prepare_agent_uses_active_sandbox_agent_memory_capability_for_handoffs() -> None:
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    triage = SandboxAgent(
        name="triage",
        model=FakeModel(),
        capabilities=[Memory(), Filesystem(), Shell()],
    )
    reviewer = SandboxAgent(
        name="reviewer",
        model=FakeModel(),
        capabilities=[Memory(generate=None), Filesystem(), Shell()],
    )
    runtime = SandboxRuntime(
        starting_agent=triage,
        run_config=_sandbox_run_config(client),
        run_state=None,
    )
    context_wrapper = RunContextWrapper(context=None)

    await runtime.prepare_agent(
        current_agent=triage,
        current_input="hello",
        context_wrapper=context_wrapper,
        is_resumed_state=False,
    )
    assert runtime._memory_generation_manager() is not None  # noqa: SLF001

    await runtime.prepare_agent(
        current_agent=reviewer,
        current_input="review this",
        context_wrapper=context_wrapper,
        is_resumed_state=False,
    )
    assert runtime._memory_generation_manager() is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_prepare_agent_enables_memory_when_handoff_target_adds_capability() -> None:
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    triage = SandboxAgent(
        name="triage",
        model=FakeModel(),
    )
    worker = SandboxAgent(
        name="worker",
        model=FakeModel(),
        capabilities=[Memory(), Filesystem(), Shell()],
    )
    runtime = SandboxRuntime(
        starting_agent=triage,
        run_config=_sandbox_run_config(client),
        run_state=None,
    )
    context_wrapper = RunContextWrapper(context=None)

    await runtime.prepare_agent(
        current_agent=triage,
        current_input="hello",
        context_wrapper=context_wrapper,
        is_resumed_state=False,
    )
    assert runtime._memory_generation_manager() is None  # noqa: SLF001

    await runtime.prepare_agent(
        current_agent=worker,
        current_input="do the work",
        context_wrapper=context_wrapper,
        is_resumed_state=False,
    )
    assert runtime._memory_generation_manager() is not None  # noqa: SLF001


@pytest.mark.asyncio
async def test_runner_restores_sandbox_from_run_state() -> None:
    model = FakeModel()

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    manifest = Manifest(entries={"README.md": File(content=b"Resume with sandbox state.")})
    session = _FakeSession(manifest)
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
        tools=[approval_tool],
        default_manifest=manifest,
    )
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", json.dumps({}), call_id="call_resume")],
            [get_final_output_message("done")],
        ]
    )

    first_run = await Runner.run(
        agent,
        "hello",
        run_config=_sandbox_run_config(client),
    )

    assert len(first_run.interruptions) == 1
    state = first_run.to_state()
    assert state._sandbox is not None
    state.approve(first_run.interruptions[0])

    resumed = await Runner.run(
        agent,
        state,
        run_config=_sandbox_run_config(client),
    )

    assert resumed.final_output == "done"
    assert client.resume_state is not None


@pytest.mark.asyncio
async def test_runner_rejects_concurrent_reuse_of_same_sandbox_agent() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    start_gate = asyncio.Event()
    session = _FakeSession(Manifest(), start_gate=start_gate)
    client = _FakeClient(session)
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
    )
    run_config = _sandbox_run_config(client)

    first_run = asyncio.create_task(Runner.run(agent, "hello", run_config=run_config))
    while session.start_calls == 0:
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="cannot be reused concurrently"):
        await Runner.run(agent, "again", run_config=run_config)

    start_gate.set()
    result = await first_run
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_runner_isolates_shared_capabilities_per_run() -> None:
    release_gate = asyncio.Event()
    first_instruction_started = asyncio.Event()
    second_instruction_started = asyncio.Event()
    shared_capability = _AwaitableSessionCapability(
        release_gate=release_gate,
        first_instruction_started=first_instruction_started,
        second_instruction_started=second_instruction_started,
    )

    session_one = _FakeSession(
        Manifest(entries={"README.md": File(content=b"Session one instructions.")})
    )
    session_two = _FakeSession(
        Manifest(entries={"README.md": File(content=b"Session two instructions.")})
    )
    client_one = _FakeClient(session_one)
    client_two = _FakeClient(session_two)
    model_one = FakeModel(initial_output=[get_final_output_message("done one")])
    model_two = FakeModel(initial_output=[get_final_output_message("done two")])
    agent_one = SandboxAgent(
        name="sandbox-one",
        model=model_one,
        instructions="Base instructions.",
        capabilities=[shared_capability],
    )
    agent_two = SandboxAgent(
        name="sandbox-two",
        model=model_two,
        instructions="Base instructions.",
        capabilities=[shared_capability],
    )

    first_run = asyncio.create_task(
        Runner.run(agent_one, "hello one", run_config=_sandbox_run_config(client_one))
    )
    await first_instruction_started.wait()

    second_run = asyncio.create_task(
        Runner.run(agent_two, "hello two", run_config=_sandbox_run_config(client_two))
    )
    await second_instruction_started.wait()

    release_gate.set()
    first_result, second_result = await asyncio.gather(first_run, second_run)

    assert first_result.final_output == "done one"
    assert second_result.final_output == "done two"
    assert model_one.first_turn_args is not None
    assert model_two.first_turn_args is not None
    assert model_one.first_turn_args["system_instructions"] == (
        f"{get_default_sandbox_instructions()}\n\n"
        "# Agent instructions\n\n"
        "Base instructions.\n\n"
        "# Sandbox capability instructions\n\n"
        "Session one instructions.\n\n"
        f"{runtime_agent_preparation_module._filesystem_instructions(session_one.state.manifest)}"
    )
    assert model_two.first_turn_args["system_instructions"] == (
        f"{get_default_sandbox_instructions()}\n\n"
        "# Agent instructions\n\n"
        "Base instructions.\n\n"
        "# Sandbox capability instructions\n\n"
        "Session two instructions.\n\n"
        f"{runtime_agent_preparation_module._filesystem_instructions(session_two.state.manifest)}"
    )
    assert shared_capability.bound_session is None


@pytest.mark.asyncio
async def test_runner_deep_clones_capability_runtime_state() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    session = _FakeSession(Manifest(entries={"README.md": File(content=b"hello")}))
    client = _FakeClient(session)

    class _MutableCapability(Capability):
        bound_labels: list[str]

        def __init__(self) -> None:
            super().__init__(type="mutable", **cast(Any, {"bound_labels": []}))

        def bind(self, session: BaseSandboxSession) -> None:
            readme = session.state.manifest.entries["README.md"]
            assert isinstance(readme, File)
            self.bound_labels.append(readme.content.decode())

    capability = _MutableCapability()
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
        capabilities=[capability],
    )

    result = await Runner.run(agent, "hello", run_config=_sandbox_run_config(client))

    assert result.final_output == "done"
    assert capability.bound_labels == []


@pytest.mark.asyncio
async def test_runner_keeps_public_agent_identity_for_hooks_and_streaming() -> None:
    model = FakeModel(initial_output=[get_final_output_message("done")])
    session = _FakeSession(Manifest())
    client = _FakeClient(session)
    run_hooks = _RecordingRunHooks()
    agent_hooks = _RecordingAgentHooks()
    agent = SandboxAgent(
        name="sandbox",
        model=model,
        instructions="Base instructions.",
        hooks=agent_hooks,
        capabilities=[_RecordingCapability(instruction_text="Capability instructions.")],
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=_sandbox_run_config(client),
        hooks=run_hooks,
    )

    assert result.last_agent is agent
    assert run_hooks.started_agents == [agent]
    assert run_hooks.ended_agents == [agent]
    assert run_hooks.llm_started_agents == [agent]
    assert run_hooks.llm_ended_agents == [agent]
    assert agent_hooks.started_agents == [agent]
    assert agent_hooks.ended_agents == [agent]
    assert agent_hooks.llm_started_agents == [agent]
    assert agent_hooks.llm_ended_agents == [agent]
    assert all(item.agent is agent for item in result.new_items)

    streamed_model = FakeModel(initial_output=[get_final_output_message("streamed done")])
    streamed_session = _FakeSession(Manifest())
    streamed_client = _FakeClient(streamed_session)
    streamed_run_hooks = _RecordingRunHooks()
    streamed_agent_hooks = _RecordingAgentHooks()
    streamed_agent = SandboxAgent(
        name="streamed-sandbox",
        model=streamed_model,
        instructions="Base instructions.",
        hooks=streamed_agent_hooks,
        capabilities=[_RecordingCapability(instruction_text="Capability instructions.")],
    )

    streamed_result = Runner.run_streamed(
        streamed_agent,
        "hello",
        run_config=_sandbox_run_config(streamed_client),
        hooks=streamed_run_hooks,
    )
    streamed_events = [event async for event in streamed_result.stream_events()]
    run_item_events = [event for event in streamed_events if isinstance(event, RunItemStreamEvent)]

    assert streamed_result.current_agent is streamed_agent
    assert streamed_run_hooks.started_agents == [streamed_agent]
    assert streamed_run_hooks.ended_agents == [streamed_agent]
    assert streamed_run_hooks.llm_started_agents == [streamed_agent]
    assert streamed_run_hooks.llm_ended_agents == [streamed_agent]
    assert streamed_agent_hooks.started_agents == [streamed_agent]
    assert streamed_agent_hooks.ended_agents == [streamed_agent]
    assert streamed_agent_hooks.llm_started_agents == [streamed_agent]
    assert streamed_agent_hooks.llm_ended_agents == [streamed_agent]
    assert all(item.agent is streamed_agent for item in streamed_result.new_items)
    assert run_item_events
    assert all(event.item.agent is streamed_agent for event in run_item_events)
