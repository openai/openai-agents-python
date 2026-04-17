from __future__ import annotations

import asyncio
import builtins
import importlib
import io
import json
import shlex
import sys
import tarfile
import types
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

import pytest
from pydantic import BaseModel, Field, PrivateAttr

from agents import Agent
from agents.run_context import RunContextWrapper
from agents.run_state import RunState
from agents.sandbox import Manifest
from agents.sandbox.capabilities import Shell
from agents.sandbox.capabilities.tools.shell_tool import ExecCommandArgs, ExecCommandTool
from agents.sandbox.entries import File, InContainerMountStrategy, Mount, MountpointMountPattern
from agents.sandbox.entries.mounts.base import InContainerMountAdapter
from agents.sandbox.manifest import Environment
from agents.sandbox.materialization import MaterializedFile
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.dependencies import Dependencies
from agents.sandbox.session.sandbox_client import BaseSandboxClientOptions
from agents.sandbox.snapshot import NoopSnapshot, SnapshotBase
from agents.sandbox.types import ExposedPortEndpoint
from tests.utils.factories import make_run_state


class _RestorableSnapshot(SnapshotBase):
    type: Literal["test-restorable-runloop"] = "test-restorable-runloop"
    payload: bytes = b"restored"

    async def persist(
        self,
        data: io.IOBase,
        *,
        dependencies: Dependencies | None = None,
    ) -> None:
        _ = (data, dependencies)

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        return io.BytesIO(self.payload)

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return True


class _DependencyAwareSnapshot(SnapshotBase):
    type: Literal["test-restorable-runloop-deps"] = "test-restorable-runloop-deps"
    payload: bytes = b"restored"
    _restorable_dependencies: list[Dependencies | None] = PrivateAttr(default_factory=list)
    _restore_dependencies: list[Dependencies | None] = PrivateAttr(default_factory=list)

    @property
    def restorable_dependencies(self) -> list[Dependencies | None]:
        return self._restorable_dependencies

    @property
    def restore_dependencies(self) -> list[Dependencies | None]:
        return self._restore_dependencies

    async def persist(
        self,
        data: io.IOBase,
        *,
        dependencies: Dependencies | None = None,
    ) -> None:
        _ = (data, dependencies)

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        self._restore_dependencies.append(dependencies)
        return io.BytesIO(self.payload)

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        self._restorable_dependencies.append(dependencies)
        return True


class _FakeRunloopError(Exception):
    pass


class _FakeAPIError(_FakeRunloopError):
    def __init__(
        self,
        message: str,
        *,
        url: str = "https://api.runloop.ai/v1/test",
        method: str = "POST",
        body: object | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.request = types.SimpleNamespace(url=url, method=method)
        self.body = body


class _FakeAPIConnectionError(_FakeAPIError):
    def __init__(
        self,
        message: str = "Connection error.",
        *,
        url: str = "https://api.runloop.ai/v1/test",
        method: str = "POST",
    ) -> None:
        super().__init__(message, url=url, method=method, body=None)


class _FakeAPITimeoutError(_FakeAPIConnectionError):
    def __init__(
        self,
        *,
        url: str = "https://api.runloop.ai/v1/test",
        method: str = "POST",
    ) -> None:
        super().__init__("Request timed out.", url=url, method=method)


class _FakeAPIStatusError(_FakeAPIError):
    def __init__(
        self,
        status_code: int,
        *,
        body: object | None = None,
        url: str = "https://api.runloop.ai/v1/test",
        method: str = "POST",
        message: str | None = None,
    ) -> None:
        super().__init__(message or f"HTTP {status_code}", url=url, method=method, body=body)
        self.status_code = status_code
        self.response = types.SimpleNamespace(
            status_code=status_code,
            request=types.SimpleNamespace(url=url, method=method),
        )


class _FakeAPIResponseValidationError(_FakeAPIError):
    def __init__(
        self,
        *,
        status_code: int = 500,
        body: object | None = None,
        url: str = "https://api.runloop.ai/v1/test",
        method: str = "POST",
        message: str = "Data returned by API invalid for expected schema.",
    ) -> None:
        super().__init__(message, url=url, method=method, body=body)
        self.status_code = status_code
        self.response = types.SimpleNamespace(
            status_code=status_code,
            request=types.SimpleNamespace(url=url, method=method),
        )


class _FakeNotFoundError(_FakeAPIStatusError):
    def __init__(
        self,
        message: str = "not found",
        *,
        body: object | None = None,
        url: str = "https://api.runloop.ai/v1/test",
        method: str = "GET",
    ) -> None:
        super().__init__(404, body=body, url=url, method=method, message=message)


class _FakeExecutionResult:
    def __init__(self, *, stdout: str = "", stderr: str = "", exit_code: int | None = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.exit_code = exit_code

    async def stdout(self, num_lines: int | None = None) -> str:
        _ = num_lines
        return self._stdout

    async def stderr(self, num_lines: int | None = None) -> str:
        _ = num_lines
        return self._stderr


class _FakeExecution:
    _counter = 0

    def __init__(
        self,
        *,
        devbox: _FakeDevbox,
        devbox_id: str,
        command: str,
        stdout_cb: object | None,
        stderr_cb: object | None,
        shell_name: str | None,
        attach_stdin: bool,
        home_dir: str,
    ) -> None:
        type(self)._counter += 1
        self._devbox = devbox
        self.execution_id = f"exec-{type(self)._counter}"
        self.devbox_id = devbox_id
        self.command = command
        self.shell_name = shell_name
        self.attach_stdin = attach_stdin
        self._stdout_cb = stdout_cb
        self._stderr_cb = stderr_cb
        self._done = asyncio.Event()
        self._stdout = ""
        self._stderr = ""
        self._exit_code: int | None = None
        self._killed = False
        self._home_dir = home_dir
        self._interactive = attach_stdin and (
            "python3 -i" in command or "python3" == command.strip()
        )
        self._sleep_forever = "sleep-forever" in command
        if self._interactive:
            self._emit(stdout_cb, ">>> ")
        elif "emit-after-result" in command:
            asyncio.get_running_loop().call_soon(self._emit, stdout_cb, "final chunk\n")
            self._exit_code = 0
            self._done.set()
        elif "echo hello" in command:
            self._stdout = "hello\n"
            self._emit(stdout_cb, self._stdout)
            self._exit_code = 0
            self._done.set()
        elif " tar -C " in command or command.startswith("tar -C "):
            self._apply_tar_extract()
            self._exit_code = 0
            self._done.set()
        elif " cat -- " in command or command.startswith("cat -- "):
            self._stdout = self._read_file_text(command)
            self._emit(stdout_cb, self._stdout)
            self._exit_code = 0
            self._done.set()
        elif " rm -f -- " in command or command.startswith("rm -f -- "):
            self._remove_file(command)
            self._exit_code = 0
            self._done.set()
        elif "pwd" in command:
            self._stdout = f"{self._home_dir}\n"
            self._emit(stdout_cb, self._stdout)
            self._exit_code = 0
            self._done.set()
        elif self._sleep_forever:
            return
        else:
            self._exit_code = 0
            self._done.set()

    def _emit(self, callback: object | None, text: str) -> None:
        if callback is None:
            return
        cast(Any, callback)(text)

    def _command_tokens(self) -> list[str]:
        return shlex.split(self.command)

    def _path_relative_to_home(self, raw_path: str) -> str:
        normalized = PurePosixPath(raw_path)
        home = PurePosixPath(self._home_dir)
        try:
            relative = normalized.relative_to(home)
        except ValueError:
            return normalized.as_posix().lstrip("/")
        rel_str = relative.as_posix()
        return rel_str if rel_str else "."

    def _apply_tar_extract(self) -> None:
        tokens = self._command_tokens()
        tar_index = tokens.index("tar")
        root = tokens[tar_index + 2]
        archive_path = tokens[tar_index + 4]
        archive_rel = self._path_relative_to_home(archive_path)
        root_rel = self._path_relative_to_home(root)
        payload = self._devbox.files[archive_rel]
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            for member in archive.getmembers():
                if member.isdir():
                    continue
                fileobj = archive.extractfile(member)
                if fileobj is None:
                    continue
                target = PurePosixPath(member.name)
                if root_rel != ".":
                    target = PurePosixPath(root_rel) / target
                self._devbox.files[target.as_posix()] = fileobj.read()

    def _read_file_text(self, command: str) -> str:
        tokens = shlex.split(command)
        path = tokens[-1]
        rel_path = self._path_relative_to_home(path)
        return self._devbox.files.get(rel_path, b"").decode("utf-8", errors="replace")

    def _remove_file(self, command: str) -> None:
        tokens = shlex.split(command)
        path = tokens[-1]
        rel_path = self._path_relative_to_home(path)
        self._devbox.files.pop(rel_path, None)

    async def result(self, timeout: float | None = None) -> _FakeExecutionResult:
        _ = timeout
        await self._done.wait()
        return _FakeExecutionResult(
            stdout=self._stdout,
            stderr=self._stderr,
            exit_code=self._exit_code,
        )

    async def kill(self, timeout: float | None = None) -> None:
        _ = timeout
        self._killed = True
        self._exit_code = -9
        self._done.set()

    async def send_input(self, text: str) -> None:
        if not self._interactive:
            return
        if text == "5 + 5\n":
            self._stdout += "10\n>>> "
            self._emit(self._stdout_cb, "10\n>>> ")
            return
        if text in {"exit()\n", "exit\n"}:
            self._exit_code = 0
            self._done.set()
            return


class _FakeExecutionsAPI:
    send_std_in_calls: list[tuple[str, str, str]]

    def __init__(self, owner: _FakeAsyncRunloopSDK) -> None:
        self._owner = owner
        self.send_std_in_calls = []

    async def send_std_in(
        self,
        execution_id: str,
        *,
        devbox_id: str,
        text: str | None = None,
        timeout: float | None = None,
        **_: object,
    ) -> object:
        del timeout
        self.send_std_in_calls.append((execution_id, devbox_id, text or ""))
        execution = self._owner.executions[execution_id]
        await execution.send_input(text or "")
        return types.SimpleNamespace(success=True)


class _FakeFileInterface:
    def __init__(self, devbox: _FakeDevbox) -> None:
        self._devbox = devbox

    async def download(self, *, path: str, timeout: float | None = None, **_: object) -> bytes:
        del timeout
        if path not in self._devbox.files:
            raise _FakeNotFoundError(path)
        return self._devbox.files[path]

    async def upload(
        self,
        *,
        path: str,
        file: bytes,
        timeout: float | None = None,
        **_: object,
    ) -> object:
        del timeout
        self._devbox.files[path] = bytes(file)
        return {}


class _FakeNetworkInterface:
    def __init__(self, devbox: _FakeDevbox) -> None:
        self._devbox = devbox

    async def enable_tunnel(self, **params: object) -> object:
        self._devbox.enable_tunnel_calls.append(dict(params))
        self._devbox.tunnel_key = "test-key"
        return types.SimpleNamespace(tunnel_key="test-key")


class _FakeCommandInterface:
    def __init__(self, devbox: _FakeDevbox) -> None:
        self._devbox = devbox

    async def exec(self, command: str, **params: object) -> _FakeExecutionResult:
        execution = _FakeExecution(
            devbox=self._devbox,
            devbox_id=self._devbox.id,
            command=command,
            stdout_cb=params.get("stdout"),
            stderr_cb=params.get("stderr"),
            shell_name=cast(str | None, params.get("shell_name")),
            attach_stdin=bool(params.get("attach_stdin", False)),
            home_dir=self._devbox.home_dir,
        )
        self._devbox.owner.executions[execution.execution_id] = execution
        self._devbox.exec_calls.append((command, dict(params)))
        return await execution.result()

    async def exec_async(self, command: str, **params: object) -> _FakeExecution:
        execution = _FakeExecution(
            devbox=self._devbox,
            devbox_id=self._devbox.id,
            command=command,
            stdout_cb=params.get("stdout"),
            stderr_cb=params.get("stderr"),
            shell_name=cast(str | None, params.get("shell_name")),
            attach_stdin=bool(params.get("attach_stdin", False)),
            home_dir=self._devbox.home_dir,
        )
        self._devbox.owner.executions[execution.execution_id] = execution
        self._devbox.exec_async_calls.append((command, dict(params)))
        return execution


class _FakeDevbox:
    def __init__(
        self,
        owner: _FakeAsyncRunloopSDK,
        *,
        devbox_id: str,
        status: str = "running",
        snapshot_source_id: str | None = None,
        environment_variables: dict[str, str] | None = None,
        launch_parameters: dict[str, object] | None = None,
    ) -> None:
        self.owner = owner
        self.id = devbox_id
        self.status = status
        self.snapshot_source_id = snapshot_source_id
        self.environment_variables = dict(environment_variables or {})
        self.launch_parameters = dict(launch_parameters or {})
        user_parameters = self.launch_parameters.get("user_parameters")
        if isinstance(user_parameters, dict):
            username = user_parameters.get("username")
            uid = user_parameters.get("uid")
            if username == "root" and uid == 0:
                self.home_dir = "/root"
            elif isinstance(username, str) and username:
                self.home_dir = f"/home/{username}"
            else:
                self.home_dir = "/home/user"
        else:
            self.home_dir = "/home/user"
        self.files: dict[str, bytes] = {}
        self.tunnel_key: str | None = None
        self.enable_tunnel_calls: list[dict[str, object]] = []
        self.exec_calls: list[tuple[str, dict[str, object]]] = []
        self.exec_async_calls: list[tuple[str, dict[str, object]]] = []
        self.snapshot_calls: list[dict[str, object]] = []
        self.shutdown_calls = 0
        self.suspend_calls = 0
        self.resume_calls = 0
        self.await_running_calls = 0
        self.resume_returns_before_running = False
        self.cmd = _FakeCommandInterface(self)
        self.file = _FakeFileInterface(self)
        self.net = _FakeNetworkInterface(self)

    async def get_info(self, timeout: float | None = None, **_: object) -> object:
        del timeout
        tunnel = (
            types.SimpleNamespace(tunnel_key=self.tunnel_key)
            if self.tunnel_key is not None
            else None
        )
        return types.SimpleNamespace(status=self.status, tunnel=tunnel)

    async def get_tunnel_url(
        self,
        port: int,
        timeout: float | None = None,
        **_: object,
    ) -> str | None:
        del timeout
        if self.tunnel_key is None:
            return None
        return f"https://{port}-{self.tunnel_key}.tunnel.runloop.ai"

    async def snapshot_disk(self, **params: object) -> object:
        self.snapshot_calls.append(dict(params))
        snapshot_id = f"snap-{len(self.snapshot_calls)}"
        return types.SimpleNamespace(id=snapshot_id)

    async def shutdown(self, timeout: float | None = None, **_: object) -> object:
        del timeout
        self.shutdown_calls += 1
        self.status = "shutdown"
        return types.SimpleNamespace(status=self.status)

    async def suspend(self, timeout: float | None = None, **_: object) -> object:
        del timeout
        self.suspend_calls += 1
        self.status = "suspended"
        return types.SimpleNamespace(status=self.status)

    async def await_suspended(self) -> object:
        return types.SimpleNamespace(status="suspended")

    async def await_running(self, **_: object) -> object:
        self.await_running_calls += 1
        self.status = "running"
        return types.SimpleNamespace(status=self.status)

    async def resume(self, timeout: float | None = None, **_: object) -> object:
        del timeout
        self.resume_calls += 1
        if self.resume_returns_before_running:
            self.status = "resuming"
            return types.SimpleNamespace(status=self.status)
        self.status = "running"
        return types.SimpleNamespace(status=self.status)


class _FakeDevboxOps:
    def __init__(self, owner: _FakeAsyncRunloopSDK) -> None:
        self._owner = owner
        self.create_calls: list[dict[str, object]] = []
        self.create_from_snapshot_calls: list[tuple[str, dict[str, object]]] = []
        self.from_id_calls: list[str] = []
        self.devboxes: dict[str, _FakeDevbox] = {}
        self._counter = 0

    def _new_devbox(
        self,
        *,
        snapshot_source_id: str | None = None,
        environment_variables: dict[str, str] | None = None,
        launch_parameters: dict[str, object] | None = None,
    ) -> _FakeDevbox:
        self._counter += 1
        devbox = _FakeDevbox(
            self._owner,
            devbox_id=f"devbox-{self._counter}",
            snapshot_source_id=snapshot_source_id,
            environment_variables=environment_variables,
            launch_parameters=launch_parameters,
        )
        self.devboxes[devbox.id] = devbox
        return devbox

    async def create(self, **params: object) -> _FakeDevbox:
        self.create_calls.append(dict(params))
        return self._new_devbox(
            environment_variables=cast(dict[str, str] | None, params.get("environment_variables")),
            launch_parameters=cast(dict[str, object] | None, params.get("launch_parameters")),
        )

    async def create_from_snapshot(self, snapshot_id: str, **params: object) -> _FakeDevbox:
        self.create_from_snapshot_calls.append((snapshot_id, dict(params)))
        return self._new_devbox(
            snapshot_source_id=snapshot_id,
            environment_variables=cast(dict[str, str] | None, params.get("environment_variables")),
            launch_parameters=cast(dict[str, object] | None, params.get("launch_parameters")),
        )

    def from_id(self, devbox_id: str) -> _FakeDevbox:
        self.from_id_calls.append(devbox_id)
        if devbox_id not in self.devboxes:
            raise _FakeNotFoundError(devbox_id)
        return self.devboxes[devbox_id]


class _FakeBlueprint:
    def __init__(
        self, owner: _FakeAsyncRunloopSDK, *, blueprint_id: str, name: str | None = None
    ) -> None:
        self.owner = owner
        self.id = blueprint_id
        self.name = name or blueprint_id
        self.logs_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    async def get_info(self, **_: object) -> object:
        return types.SimpleNamespace(id=self.id, name=self.name, status="build_complete")

    async def logs(self, **params: object) -> object:
        self.logs_calls.append(dict(params))
        return types.SimpleNamespace(items=[f"log:{self.id}"])

    async def delete(self, **params: object) -> object:
        self.delete_calls.append(dict(params))
        return types.SimpleNamespace(id=self.id, deleted=True)


class _FakeBlueprintOps:
    def __init__(self, owner: _FakeAsyncRunloopSDK) -> None:
        self._owner = owner
        self.create_calls: list[dict[str, object]] = []
        self.list_calls: list[dict[str, object]] = []
        self.from_id_calls: list[str] = []
        self.blueprints: dict[str, _FakeBlueprint] = {}
        self._counter = 0

    def _new_blueprint(self, *, name: str | None = None) -> _FakeBlueprint:
        self._counter += 1
        blueprint = _FakeBlueprint(
            self._owner,
            blueprint_id=f"blueprint-{self._counter}",
            name=name,
        )
        self.blueprints[blueprint.id] = blueprint
        return blueprint

    async def create(self, **params: object) -> _FakeBlueprint:
        self.create_calls.append(dict(params))
        return self._new_blueprint(name=cast(str | None, params.get("name")))

    async def list(self, **params: object) -> list[_FakeBlueprint]:
        self.list_calls.append(dict(params))
        return list(self.blueprints.values())

    def from_id(self, blueprint_id: str) -> _FakeBlueprint:
        self.from_id_calls.append(blueprint_id)
        return self.blueprints.setdefault(
            blueprint_id,
            _FakeBlueprint(self._owner, blueprint_id=blueprint_id, name=blueprint_id),
        )


class _FakeBlueprintsAPI:
    def __init__(self, owner: _FakeAsyncRunloopSDK) -> None:
        self._owner = owner
        self.list_public_calls: list[dict[str, object]] = []
        self.logs_calls: list[tuple[str, dict[str, object]]] = []
        self.await_build_complete_calls: list[tuple[str, dict[str, object]]] = []

    async def list_public(self, **params: object) -> object:
        self.list_public_calls.append(dict(params))
        return types.SimpleNamespace(data=list(self._owner.blueprint.blueprints.values()))

    async def logs(self, blueprint_id: str, **params: object) -> object:
        self.logs_calls.append((blueprint_id, dict(params)))
        return types.SimpleNamespace(items=[f"log:{blueprint_id}"])

    async def await_build_complete(self, blueprint_id: str, **params: object) -> object:
        self.await_build_complete_calls.append((blueprint_id, dict(params)))
        blueprint = self._owner.blueprint.from_id(blueprint_id)
        return types.SimpleNamespace(id=blueprint.id, status="build_complete")


class _FakeBenchmarkRun:
    def __init__(self, *, run_id: str, benchmark_id: str) -> None:
        self.id = run_id
        self.benchmark_id = benchmark_id

    async def get_info(self, **_: object) -> object:
        return types.SimpleNamespace(id=self.id, benchmark_id=self.benchmark_id)


class _FakeBenchmark:
    def __init__(
        self, owner: _FakeAsyncRunloopSDK, *, benchmark_id: str, name: str | None = None
    ) -> None:
        self.owner = owner
        self.id = benchmark_id
        self.name = name or benchmark_id
        self.update_calls: list[dict[str, object]] = []
        self.start_run_calls: list[dict[str, object]] = []

    async def get_info(self, **_: object) -> object:
        return types.SimpleNamespace(id=self.id, name=self.name)

    async def update(self, **params: object) -> object:
        self.update_calls.append(dict(params))
        return types.SimpleNamespace(id=self.id, name=params.get("name", self.name))

    async def start_run(self, **params: object) -> _FakeBenchmarkRun:
        self.start_run_calls.append(dict(params))
        return _FakeBenchmarkRun(run_id=f"run-{self.id}", benchmark_id=self.id)

    async def list_runs(self, **_: object) -> list[_FakeBenchmarkRun]:
        return [_FakeBenchmarkRun(run_id=f"run-{self.id}", benchmark_id=self.id)]


class _FakeBenchmarkOps:
    def __init__(self, owner: _FakeAsyncRunloopSDK) -> None:
        self._owner = owner
        self.create_calls: list[dict[str, object]] = []
        self.list_calls: list[dict[str, object]] = []
        self.from_id_calls: list[str] = []
        self.benchmarks: dict[str, _FakeBenchmark] = {}
        self._counter = 0

    def _new_benchmark(self, *, name: str | None = None) -> _FakeBenchmark:
        self._counter += 1
        benchmark = _FakeBenchmark(
            self._owner, benchmark_id=f"benchmark-{self._counter}", name=name
        )
        self.benchmarks[benchmark.id] = benchmark
        return benchmark

    async def create(self, **params: object) -> _FakeBenchmark:
        self.create_calls.append(dict(params))
        return self._new_benchmark(name=cast(str | None, params.get("name")))

    async def list(self, **params: object) -> list[_FakeBenchmark]:
        self.list_calls.append(dict(params))
        return list(self.benchmarks.values())

    def from_id(self, benchmark_id: str) -> _FakeBenchmark:
        self.from_id_calls.append(benchmark_id)
        return self.benchmarks.setdefault(
            benchmark_id,
            _FakeBenchmark(self._owner, benchmark_id=benchmark_id, name=benchmark_id),
        )


class _FakeBenchmarksAPI:
    def __init__(self, owner: _FakeAsyncRunloopSDK) -> None:
        self._owner = owner
        self.list_public_calls: list[dict[str, object]] = []
        self.definitions_calls: list[tuple[str, dict[str, object]]] = []
        self.update_scenarios_calls: list[tuple[str, dict[str, object]]] = []

    async def list_public(self, **params: object) -> object:
        self.list_public_calls.append(dict(params))
        return types.SimpleNamespace(data=list(self._owner.benchmark.benchmarks.values()))

    async def definitions(self, benchmark_id: str, **params: object) -> object:
        self.definitions_calls.append((benchmark_id, dict(params)))
        return types.SimpleNamespace(definitions=[types.SimpleNamespace(id=f"def-{benchmark_id}")])

    async def update_scenarios(self, benchmark_id: str, **params: object) -> object:
        self.update_scenarios_calls.append((benchmark_id, dict(params)))
        return types.SimpleNamespace(id=benchmark_id, **dict(params))


class _FakeSecret:
    def __init__(
        self, owner: _FakeAsyncRunloopSDK, *, name: str, value: str, secret_id: str
    ) -> None:
        self.owner = owner
        self.name = name
        self.value = value
        self.id = secret_id
        self.update_calls: list[tuple[str, dict[str, object]]] = []
        self.delete_calls: list[dict[str, object]] = []

    async def get_info(self, **_: object) -> object:
        return types.SimpleNamespace(id=self.id, name=self.name)

    async def update(self, value: str, **params: object) -> _FakeSecret:
        self.update_calls.append((value, dict(params)))
        self.value = value
        return self

    async def delete(self, **params: object) -> object:
        self.delete_calls.append(dict(params))
        self.owner.secret.secrets.pop(self.name, None)
        return types.SimpleNamespace(id=self.id, name=self.name, deleted=True)


class _FakeSecretOps:
    def __init__(self, owner: _FakeAsyncRunloopSDK) -> None:
        self._owner = owner
        self.create_calls: list[tuple[str, str, dict[str, object]]] = []
        self.update_calls: list[tuple[str, str, dict[str, object]]] = []
        self.delete_calls: list[tuple[str, dict[str, object]]] = []
        self.list_calls: list[dict[str, object]] = []
        self.secrets: dict[str, _FakeSecret] = {}
        self._counter = 0
        self.conflict_status_code = 409
        self.conflict_body: object | None = {"error": "secret exists"}
        self.conflict_message: str | None = None

    def _new_secret(self, *, name: str, value: str) -> _FakeSecret:
        self._counter += 1
        secret = _FakeSecret(
            self._owner, name=name, value=value, secret_id=f"secret-{self._counter}"
        )
        self.secrets[name] = secret
        return secret

    async def create(self, name: str, value: str, **params: object) -> _FakeSecret:
        self.create_calls.append((name, value, dict(params)))
        if name in self.secrets:
            raise _FakeAPIStatusError(
                self.conflict_status_code,
                body=self.conflict_body,
                message=self.conflict_message,
            )
        return self._new_secret(name=name, value=value)

    async def list(self, **params: object) -> list[_FakeSecret]:
        self.list_calls.append(dict(params))
        return list(self.secrets.values())

    async def update(self, secret: _FakeSecret | str, value: str, **params: object) -> _FakeSecret:
        name = secret.name if isinstance(secret, _FakeSecret) else secret
        self.update_calls.append((name, value, dict(params)))
        secret_obj = self.secrets[name]
        secret_obj.value = value
        return secret_obj

    async def delete(self, secret: _FakeSecret | str, **params: object) -> object:
        name = secret.name if isinstance(secret, _FakeSecret) else secret
        self.delete_calls.append((name, dict(params)))
        secret_obj = self.secrets.pop(name)
        return types.SimpleNamespace(id=secret_obj.id, name=name, deleted=True)


class _FakeSecretsAPI:
    def __init__(self, owner: _FakeAsyncRunloopSDK) -> None:
        self._owner = owner
        self.retrieve_calls: list[tuple[str, dict[str, object]]] = []

    async def retrieve(self, name: str, **params: object) -> object:
        self.retrieve_calls.append((name, dict(params)))
        secret = self._owner.secret.secrets[name]
        return types.SimpleNamespace(id=secret.id, name=secret.name)


class _FakeNetworkPolicy:
    def __init__(
        self, owner: _FakeAsyncRunloopSDK, *, policy_id: str, name: str | None = None
    ) -> None:
        self.owner = owner
        self.id = policy_id
        self.name = name or policy_id
        self.update_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    async def get_info(self, **_: object) -> object:
        return types.SimpleNamespace(id=self.id, name=self.name)

    async def update(self, **params: object) -> object:
        self.update_calls.append(dict(params))
        return types.SimpleNamespace(id=self.id, name=params.get("name", self.name))

    async def delete(self, **params: object) -> object:
        self.delete_calls.append(dict(params))
        self.owner.network_policy.policies.pop(self.id, None)
        return types.SimpleNamespace(id=self.id, deleted=True)


class _FakeNetworkPolicyOps:
    def __init__(self, owner: _FakeAsyncRunloopSDK) -> None:
        self._owner = owner
        self.create_calls: list[dict[str, object]] = []
        self.list_calls: list[dict[str, object]] = []
        self.from_id_calls: list[str] = []
        self.policies: dict[str, _FakeNetworkPolicy] = {}
        self._counter = 0

    def _new_policy(self, *, name: str | None = None) -> _FakeNetworkPolicy:
        self._counter += 1
        policy = _FakeNetworkPolicy(self._owner, policy_id=f"policy-{self._counter}", name=name)
        self.policies[policy.id] = policy
        return policy

    async def create(self, **params: object) -> _FakeNetworkPolicy:
        self.create_calls.append(dict(params))
        return self._new_policy(name=cast(str | None, params.get("name")))

    async def list(self, **params: object) -> list[_FakeNetworkPolicy]:
        self.list_calls.append(dict(params))
        return list(self.policies.values())

    def from_id(self, network_policy_id: str) -> _FakeNetworkPolicy:
        self.from_id_calls.append(network_policy_id)
        return self.policies.setdefault(
            network_policy_id,
            _FakeNetworkPolicy(self._owner, policy_id=network_policy_id, name=network_policy_id),
        )


class _FakeNetworkPoliciesAPI:
    def __init__(self, owner: _FakeAsyncRunloopSDK) -> None:
        self._owner = owner
        self.retrieve_calls: list[tuple[str, dict[str, object]]] = []

    async def retrieve(self, network_policy_id: str, **params: object) -> object:
        self.retrieve_calls.append((network_policy_id, dict(params)))
        policy = self._owner.network_policy.from_id(network_policy_id)
        return types.SimpleNamespace(id=policy.id, name=policy.name)


class _FakeAxonSql:
    def __init__(self) -> None:
        self.query_calls: list[dict[str, object]] = []
        self.batch_calls: list[dict[str, object]] = []

    async def query(self, **params: object) -> object:
        self.query_calls.append(dict(params))
        return types.SimpleNamespace(rows=[["ok"]])

    async def batch(self, **params: object) -> object:
        self.batch_calls.append(dict(params))
        return types.SimpleNamespace(results=[types.SimpleNamespace(success=True)])


class _FakeAxon:
    def __init__(
        self, owner: _FakeAsyncRunloopSDK, *, axon_id: str, name: str | None = None
    ) -> None:
        self.owner = owner
        self.id = axon_id
        self.name = name or axon_id
        self.publish_calls: list[dict[str, object]] = []
        self.sql = _FakeAxonSql()

    async def get_info(self, **_: object) -> object:
        return types.SimpleNamespace(id=self.id, name=self.name)

    async def publish(self, **params: object) -> object:
        self.publish_calls.append(dict(params))
        return types.SimpleNamespace(published=True)


class _FakeAxonOps:
    def __init__(self, owner: _FakeAsyncRunloopSDK) -> None:
        self._owner = owner
        self.create_calls: list[dict[str, object]] = []
        self.list_calls: list[dict[str, object]] = []
        self.from_id_calls: list[str] = []
        self.axons: dict[str, _FakeAxon] = {}
        self._counter = 0

    def _new_axon(self, *, name: str | None = None) -> _FakeAxon:
        self._counter += 1
        axon = _FakeAxon(self._owner, axon_id=f"axon-{self._counter}", name=name)
        self.axons[axon.id] = axon
        return axon

    async def create(self, **params: object) -> _FakeAxon:
        self.create_calls.append(dict(params))
        return self._new_axon(name=cast(str | None, params.get("name")))

    async def list(self, **params: object) -> list[_FakeAxon]:
        self.list_calls.append(dict(params))
        return list(self.axons.values())

    def from_id(self, axon_id: str) -> _FakeAxon:
        self.from_id_calls.append(axon_id)
        return self.axons.setdefault(
            axon_id,
            _FakeAxon(self._owner, axon_id=axon_id, name=axon_id),
        )


class _FakeLaunchAfterIdle(BaseModel):
    idle_time_seconds: int
    on_idle: Literal["shutdown", "suspend"]

    def to_dict(
        self,
        *,
        mode: str = "python",
        exclude_none: bool = False,
        exclude_defaults: bool = False,
    ) -> dict[str, object]:
        return cast(
            dict[str, object],
            self.model_dump(
                mode=cast(Literal["json", "python"], mode),
                exclude_none=exclude_none,
                exclude_defaults=exclude_defaults,
            ),
        )


class _FakeUserParameters(BaseModel):
    username: str
    uid: int

    def to_dict(
        self,
        *,
        mode: str = "python",
        exclude_none: bool = False,
        exclude_defaults: bool = False,
    ) -> dict[str, object]:
        return cast(
            dict[str, object],
            self.model_dump(
                mode=cast(Literal["json", "python"], mode),
                exclude_none=exclude_none,
                exclude_defaults=exclude_defaults,
            ),
        )


class _FakeLaunchParameters(BaseModel):
    network_policy_id: str | None = None
    resource_size_request: (
        Literal["X_SMALL", "SMALL", "MEDIUM", "LARGE", "X_LARGE", "XX_LARGE", "CUSTOM_SIZE"] | None
    ) = None
    custom_cpu_cores: float | None = None
    custom_gb_memory: int | None = None
    custom_disk_size: int | None = None
    architecture: Literal["x86_64", "arm64"] | None = None
    keep_alive_time_seconds: int | None = None
    after_idle: _FakeLaunchAfterIdle | dict[str, object] | None = None
    launch_commands: list[str] | tuple[str, ...] | None = None
    required_services: list[str] | tuple[str, ...] | None = None
    user_parameters: dict[str, object] | None = None

    def to_dict(
        self,
        *,
        mode: str = "python",
        exclude_none: bool = False,
        exclude_defaults: bool = False,
    ) -> dict[str, object]:
        return cast(
            dict[str, object],
            self.model_dump(
                mode=cast(Literal["json", "python"], mode),
                exclude_none=exclude_none,
                exclude_defaults=exclude_defaults,
            ),
        )


class _FakeAsyncRunloopSDK:
    created_instances: list[_FakeAsyncRunloopSDK] = []

    def __init__(
        self,
        *,
        bearer_token: str | None = None,
        base_url: str | None = None,
        **_: object,
    ) -> None:
        self.bearer_token = bearer_token
        self.base_url = base_url or "https://api.runloop.ai"
        self.executions: dict[str, _FakeExecution] = {}
        self.devbox = _FakeDevboxOps(self)
        self.blueprint = _FakeBlueprintOps(self)
        self.benchmark = _FakeBenchmarkOps(self)
        self.secret = _FakeSecretOps(self)
        self.network_policy = _FakeNetworkPolicyOps(self)
        self.axon = _FakeAxonOps(self)
        self.api = types.SimpleNamespace(
            devboxes=types.SimpleNamespace(executions=_FakeExecutionsAPI(self)),
            blueprints=_FakeBlueprintsAPI(self),
            benchmarks=_FakeBenchmarksAPI(self),
            secrets=_FakeSecretsAPI(self),
            network_policies=_FakeNetworkPoliciesAPI(self),
        )
        type(self).created_instances.append(self)

    async def aclose(self) -> None:
        return None


def _load_runloop_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    _FakeAsyncRunloopSDK.created_instances.clear()
    _FakeExecution._counter = 0
    fake_runloop: Any = types.ModuleType("runloop_api_client")
    fake_runloop.APIConnectionError = _FakeAPIConnectionError
    fake_runloop.APIResponseValidationError = _FakeAPIResponseValidationError
    fake_runloop.APITimeoutError = _FakeAPITimeoutError
    fake_runloop.APIStatusError = _FakeAPIStatusError
    fake_runloop.NotFoundError = _FakeNotFoundError
    fake_runloop.RunloopError = _FakeRunloopError

    fake_sdk: Any = types.ModuleType("runloop_api_client.sdk")
    fake_sdk.AsyncRunloopSDK = _FakeAsyncRunloopSDK

    fake_types: Any = types.ModuleType("runloop_api_client.types")
    fake_types.AfterIdle = _FakeLaunchAfterIdle
    fake_types.LaunchParameters = _FakeLaunchParameters
    fake_shared: Any = types.ModuleType("runloop_api_client.types.shared")
    fake_launch_parameters_module: Any = types.ModuleType(
        "runloop_api_client.types.shared.launch_parameters"
    )
    fake_launch_parameters_module.UserParameters = _FakeUserParameters
    fake_shared.launch_parameters = fake_launch_parameters_module
    fake_types.shared = fake_shared

    monkeypatch.setitem(sys.modules, "runloop_api_client", fake_runloop)
    monkeypatch.setitem(sys.modules, "runloop_api_client.sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "runloop_api_client.types", fake_types)
    monkeypatch.setitem(sys.modules, "runloop_api_client.types.shared", fake_shared)
    monkeypatch.setitem(
        sys.modules,
        "runloop_api_client.types.shared.launch_parameters",
        fake_launch_parameters_module,
    )
    sys.modules.pop("agents.extensions.sandbox.runloop.sandbox", None)
    sys.modules.pop("agents.extensions.sandbox.runloop", None)
    return importlib.import_module("agents.extensions.sandbox.runloop.sandbox")


def _build_tar_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def test_runloop_package_re_exports_backend_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    runloop_module = _load_runloop_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.runloop")

    assert package_module.RunloopSandboxClient is runloop_module.RunloopSandboxClient
    assert package_module.RunloopPlatformClient is runloop_module.RunloopPlatformClient
    assert package_module.RunloopLaunchParameters is runloop_module.RunloopLaunchParameters
    assert package_module.RunloopAfterIdle is runloop_module.RunloopAfterIdle
    assert package_module.RunloopUserParameters is runloop_module.RunloopUserParameters


class _RecordingMount(Mount):
    type: str = "runloop_recording_mount"
    mount_strategy: InContainerMountStrategy = Field(
        default_factory=lambda: InContainerMountStrategy(pattern=MountpointMountPattern())
    )
    _mounted_paths: list[Path] = PrivateAttr(default_factory=list)
    _unmounted_paths: list[Path] = PrivateAttr(default_factory=list)

    def supported_in_container_patterns(
        self,
    ) -> tuple[builtins.type[MountpointMountPattern], ...]:
        return (MountpointMountPattern,)

    def in_container_adapter(self) -> InContainerMountAdapter:
        mount = self

        class _Adapter(InContainerMountAdapter):
            def validate(self, strategy: InContainerMountStrategy) -> None:
                _ = strategy

            async def activate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> list[MaterializedFile]:
                _ = (strategy, session, base_dir)
                path = mount._resolve_mount_path(session, dest)
                mount._mounted_paths.append(path)
                return []

            async def deactivate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> None:
                _ = (strategy, session, base_dir)
                path = mount._resolve_mount_path(session, dest)
                mount._unmounted_paths.append(path)

            async def teardown_for_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                _ = (strategy, session)
                mount._unmounted_paths.append(path)

            async def restore_after_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                _ = (strategy, session)
                mount._mounted_paths.append(path)

        return _Adapter(self)


class TestRunloopSandbox:
    @pytest.mark.asyncio
    async def test_runloop_does_not_advertise_pty_support(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())

        assert session.supports_pty() is False

    @pytest.mark.asyncio
    async def test_create_uses_runloop_default_workspace_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())

        assert session.state.manifest.root == runloop_module.DEFAULT_RUNLOOP_WORKSPACE_ROOT

    @pytest.mark.asyncio
    async def test_create_uses_root_workspace_root_when_root_launch_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(
                    user_parameters=runloop_module.RunloopUserParameters(
                        username="root",
                        uid=0,
                    ),
                )
            )
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]

        assert session.state.manifest.root == runloop_module.DEFAULT_RUNLOOP_ROOT_WORKSPACE_ROOT
        assert sdk.devbox.create_calls[0]["launch_parameters"] == {
            "user_parameters": {"username": "root", "uid": 0}
        }

    def test_runloop_sdk_backed_user_parameters_construct_from_extension_exports(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        user_parameters = runloop_module.RunloopUserParameters(username="user", uid=1000)

        assert user_parameters.username == "user"
        assert user_parameters.uid == 1000
        assert user_parameters.to_dict(mode="json", exclude_none=True) == {
            "username": "user",
            "uid": 1000,
        }

    @pytest.mark.asyncio
    async def test_create_normalizes_dict_user_parameters(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(
                    user_parameters={"username": "root", "uid": 0},
                )
            )
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]

        assert sdk.devbox.create_calls[0]["launch_parameters"] == {
            "user_parameters": {"username": "root", "uid": 0}
        }
        assert session.state.user_parameters is not None
        assert session.state.user_parameters.username == "root"
        assert session.state.user_parameters.uid == 0

    @pytest.mark.asyncio
    async def test_empty_manifest_exec_succeeds_immediately_after_start_non_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                manifest=Manifest(root=f"{runloop_module.DEFAULT_RUNLOOP_WORKSPACE_ROOT}/project"),
                options=runloop_module.RunloopSandboxClientOptions(),
            )
            await session.start()
            result = await session.exec("pwd", shell=False)
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]
            command, _ = devbox.exec_calls[-1]

        assert result.ok()
        assert "cd /home/user/project &&" in command

    @pytest.mark.asyncio
    async def test_empty_manifest_exec_succeeds_immediately_after_start_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                manifest=Manifest(root="/root/project"),
                options=runloop_module.RunloopSandboxClientOptions(
                    user_parameters=runloop_module.RunloopUserParameters(
                        username="root",
                        uid=0,
                    )
                ),
            )
            await session.start()
            result = await session.exec("pwd", shell=False)
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]
            command, _ = devbox.exec_calls[-1]

        assert result.ok()
        assert "cd /root/project &&" in command

    @pytest.mark.asyncio
    async def test_create_merges_env_vars_with_manifest_precedence(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            await client.create(
                manifest=Manifest(
                    root=runloop_module.DEFAULT_RUNLOOP_WORKSPACE_ROOT,
                    environment=Environment(value={"SHARED": "manifest", "ONLY_MANIFEST": "1"}),
                ),
                options=runloop_module.RunloopSandboxClientOptions(
                    env_vars={"SHARED": "option", "ONLY_OPTION": "1"},
                ),
            )
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]

        assert sdk.devbox.create_calls
        create_params = sdk.devbox.create_calls[0]
        assert create_params["environment_variables"] == {
            "SHARED": "manifest",
            "ONLY_MANIFEST": "1",
            "ONLY_OPTION": "1",
        }

    def test_runloop_client_options_preserve_positional_exposed_ports(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        options = runloop_module.RunloopSandboxClientOptions(
            None,
            None,
            None,
            False,
            None,
            None,
            (8765,),
        )

        assert options.exposed_ports == (8765,)

    def test_runloop_client_options_append_new_fields_after_existing_positionals(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        options = runloop_module.RunloopSandboxClientOptions(
            None,
            None,
            None,
            False,
            None,
            None,
            (8765,),
            None,
            launch_parameters=runloop_module.RunloopLaunchParameters(
                network_policy_id="np-123",
            ),
            managed_secrets={"API_KEY": "secret"},
        )

        assert options.exposed_ports == (8765,)
        assert options.launch_parameters is not None
        assert options.launch_parameters.network_policy_id == "np-123"
        assert options.managed_secrets == {"API_KEY": "secret"}

    def test_runloop_sdk_backed_launch_models_construct_from_extension_exports(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        after_idle = runloop_module.RunloopAfterIdle(idle_time_seconds=300, on_idle="suspend")
        launch_parameters = runloop_module.RunloopLaunchParameters(
            network_policy_id="np-123",
            after_idle=after_idle,
            launch_commands=["echo hi"],
        )

        assert after_idle.idle_time_seconds == 300
        assert launch_parameters.after_idle is not None
        assert launch_parameters.after_idle.on_idle == "suspend"
        assert launch_parameters.to_dict(mode="json", exclude_none=True)["launch_commands"] == [
            "echo hi"
        ]

    def test_runloop_tunnel_config_remains_extension_model(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        tunnel = runloop_module.RunloopTunnelConfig(auth_mode="authenticated")

        assert isinstance(tunnel, BaseModel)
        assert tunnel.model_dump(mode="json", exclude_none=True) == {"auth_mode": "authenticated"}

    @pytest.mark.asyncio
    async def test_create_passes_runloop_native_launch_options_and_persists_secret_refs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(
                    name="native-runloop",
                    user_parameters=runloop_module.RunloopUserParameters(username="user", uid=1000),
                    launch_parameters=runloop_module.RunloopLaunchParameters(
                        network_policy_id="np-123",
                        resource_size_request="MEDIUM",
                        custom_cpu_cores=2,
                        custom_gb_memory=8,
                        custom_disk_size=16,
                        architecture="arm64",
                        keep_alive_time_seconds=600,
                        after_idle=runloop_module.RunloopAfterIdle(
                            idle_time_seconds=300,
                            on_idle="suspend",
                        ),
                        launch_commands=("echo hi",),
                        required_services=("postgres",),
                    ),
                    tunnel=runloop_module.RunloopTunnelConfig(
                        auth_mode="authenticated",
                        http_keep_alive=True,
                        wake_on_http=True,
                    ),
                    gateways={
                        "GWS_OPENAI": runloop_module.RunloopGatewaySpec(
                            gateway="openai-gateway",
                            secret="OPENAI_GATEWAY_SECRET",
                        )
                    },
                    mcp={
                        "MCP_TOKEN": runloop_module.RunloopMcpSpec(
                            mcp_config="github-readonly",
                            secret="MCP_SECRET",
                        )
                    },
                    metadata={"team": "agents"},
                    managed_secrets={"API_KEY": "super-secret"},
                ),
            )
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]

        assert sdk.secret.create_calls == [("API_KEY", "super-secret", {"timeout": 30.0})]
        assert sdk.devbox.create_calls
        create_params = sdk.devbox.create_calls[0]
        assert create_params["launch_parameters"] == {
            "network_policy_id": "np-123",
            "resource_size_request": "MEDIUM",
            "custom_cpu_cores": 2.0,
            "custom_gb_memory": 8,
            "custom_disk_size": 16,
            "architecture": "arm64",
            "keep_alive_time_seconds": 600,
            "after_idle": {"idle_time_seconds": 300, "on_idle": "suspend"},
            "launch_commands": ["echo hi"],
            "required_services": ["postgres"],
            "user_parameters": {"username": "user", "uid": 1000},
        }
        assert create_params["tunnel"] == {
            "auth_mode": "authenticated",
            "http_keep_alive": True,
            "wake_on_http": True,
        }
        assert create_params["gateways"] == {
            "GWS_OPENAI": {"gateway": "openai-gateway", "secret": "OPENAI_GATEWAY_SECRET"}
        }
        assert create_params["mcp"] == {
            "MCP_TOKEN": {"mcp_config": "github-readonly", "secret": "MCP_SECRET"}
        }
        assert create_params["metadata"] == {"team": "agents"}
        assert create_params["secrets"] == {"API_KEY": "API_KEY"}
        assert session.state.secret_refs == {"API_KEY": "API_KEY"}
        assert session.state.metadata == {"team": "agents"}
        assert "super-secret" not in json.dumps(session.state.model_dump(mode="json"))

    @pytest.mark.asyncio
    async def test_create_normalizes_dict_launch_parameters_and_tunnel_options(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(
                    launch_parameters={
                        "network_policy_id": "np-123",
                        "launch_commands": ["echo hi"],
                    },
                    tunnel={
                        "auth_mode": "authenticated",
                        "wake_on_http": True,
                    },
                )
            )
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]

        assert sdk.devbox.create_calls[0]["launch_parameters"] == {
            "network_policy_id": "np-123",
            "launch_commands": ["echo hi"],
        }
        assert sdk.devbox.create_calls[0]["tunnel"] == {
            "auth_mode": "authenticated",
            "wake_on_http": True,
        }
        assert session.state.launch_parameters is not None
        assert session.state.launch_parameters.network_policy_id == "np-123"
        assert session.state.tunnel is not None
        assert session.state.tunnel.auth_mode == "authenticated"

    @pytest.mark.asyncio
    async def test_create_normalizes_dict_launch_parameters_and_tunnel_from_parsed_options(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)
        options = cast(
            Any,
            BaseSandboxClientOptions.parse(
                {
                    "type": "runloop",
                    "launch_parameters": {
                        "network_policy_id": "np-456",
                        "required_services": ["postgres"],
                    },
                    "tunnel": {
                        "auth_mode": "open",
                        "http_keep_alive": True,
                    },
                }
            ),
        )

        assert options.type == "runloop"
        assert options.launch_parameters is not None
        assert options.tunnel is not None

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=options)
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]

        assert sdk.devbox.create_calls[0]["launch_parameters"] == {
            "network_policy_id": "np-456",
            "required_services": ["postgres"],
        }
        assert sdk.devbox.create_calls[0]["tunnel"] == {
            "auth_mode": "open",
            "http_keep_alive": True,
        }
        assert session.state.launch_parameters is not None
        assert session.state.launch_parameters.network_policy_id == "np-456"
        assert session.state.tunnel is not None
        assert session.state.tunnel.auth_mode == "open"

    @pytest.mark.asyncio
    async def test_run_state_round_trip_preserves_runloop_session_state_without_secret_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)
        agent = Agent(name="TestAgent")
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        state: RunState[dict[str, str], Agent[Any]] = make_run_state(
            agent,
            context=context,
            original_input="test",
        )
        client = runloop_module.RunloopSandboxClient(bearer_token="test-token")
        session_state = runloop_module.RunloopSandboxSessionState(
            manifest=Manifest(),
            snapshot=NoopSnapshot(id="runloop-state"),
            devbox_id="devbox-123",
            launch_parameters=runloop_module.RunloopLaunchParameters(network_policy_id="np-123"),
            secret_refs={"API_KEY": "API_KEY"},
        )
        serialized_session_state = client.serialize_session_state(session_state)
        state._sandbox = {
            "backend_id": "runloop",
            "current_agent_key": agent.name,
            "current_agent_name": agent.name,
            "session_state": serialized_session_state,
            "sessions_by_agent": {
                agent.name: {
                    "agent_name": agent.name,
                    "session_state": serialized_session_state,
                }
            },
        }

        restored = await RunState.from_json(agent, state.to_json())

        assert restored._sandbox is not None
        restored_session_payload = cast(dict[str, object], restored._sandbox["session_state"])
        assert restored_session_payload["secret_refs"] == {"API_KEY": "API_KEY"}
        assert "managed_secrets" not in restored_session_payload
        assert "secret-value" not in json.dumps(restored_session_payload)

        restored_session_state = client.deserialize_session_state(restored_session_payload)
        assert isinstance(restored_session_state, runloop_module.RunloopSandboxSessionState)
        assert restored_session_state.secret_refs == {"API_KEY": "API_KEY"}
        assert restored_session_state.launch_parameters is not None
        assert restored_session_state.launch_parameters.network_policy_id == "np-123"

        await client.close()

    @pytest.mark.asyncio
    async def test_create_upserts_managed_secret_when_secret_exists(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            sdk.secret._new_secret(name="API_KEY", value="old-value")

            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(
                    managed_secrets={"API_KEY": "new-value"},
                )
            )

        assert sdk.secret.create_calls == [("API_KEY", "new-value", {"timeout": 30.0})]
        assert sdk.secret.update_calls == [("API_KEY", "new-value", {"timeout": 30.0})]
        assert session.state.secret_refs == {"API_KEY": "API_KEY"}

    @pytest.mark.asyncio
    async def test_create_upserts_managed_secret_when_runloop_returns_bad_request_exists(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            sdk.secret._new_secret(name="API_KEY", value="old-value")
            sdk.secret.conflict_status_code = 400
            sdk.secret.conflict_body = {
                "message": "Secret with name 'API_KEY' already exists",
            }
            sdk.secret.conflict_message = "Secret with name 'API_KEY' already exists"

            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(
                    managed_secrets={"API_KEY": "new-value"},
                )
            )

        assert sdk.secret.create_calls == [("API_KEY", "new-value", {"timeout": 30.0})]
        assert sdk.secret.update_calls == [("API_KEY", "new-value", {"timeout": 30.0})]
        assert session.state.secret_refs == {"API_KEY": "API_KEY"}

    @pytest.mark.asyncio
    async def test_resume_and_snapshot_restore_reuse_runloop_native_options(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(
                    name="native-runloop",
                    launch_parameters=runloop_module.RunloopLaunchParameters(
                        network_policy_id="np-123",
                        launch_commands=("echo hi",),
                    ),
                    tunnel=runloop_module.RunloopTunnelConfig(auth_mode="open"),
                    gateways={
                        "GWS_OPENAI": runloop_module.RunloopGatewaySpec(
                            gateway="openai-gateway",
                            secret="OPENAI_GATEWAY_SECRET",
                        )
                    },
                    mcp={
                        "MCP_TOKEN": runloop_module.RunloopMcpSpec(
                            mcp_config="github-readonly",
                            secret="MCP_SECRET",
                        )
                    },
                    metadata={"team": "agents"},
                    managed_secrets={"API_KEY": "super-secret"},
                ),
            )
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            sdk.devbox.devboxes[session.state.devbox_id].status = "shutdown"
            sdk.devbox.create_calls.clear()

            resumed = await client.resume(session.state)
            await resumed._inner.hydrate_workspace(  # noqa: SLF001
                io.BytesIO(runloop_module._encode_runloop_snapshot_ref(snapshot_id="snap-123"))  # noqa: SLF001
            )

        assert sdk.devbox.create_calls == [
            {
                "timeout": session.state.timeouts.create_s,
                "name": "native-runloop",
                "launch_parameters": {
                    "network_policy_id": "np-123",
                    "launch_commands": ["echo hi"],
                },
                "tunnel": {"auth_mode": "open"},
                "gateways": {
                    "GWS_OPENAI": {
                        "gateway": "openai-gateway",
                        "secret": "OPENAI_GATEWAY_SECRET",
                    }
                },
                "mcp": {
                    "MCP_TOKEN": {
                        "mcp_config": "github-readonly",
                        "secret": "MCP_SECRET",
                    }
                },
                "metadata": {"team": "agents"},
                "secrets": {"API_KEY": "API_KEY"},
            }
        ]
        assert sdk.devbox.create_from_snapshot_calls == [
            (
                "snap-123",
                {
                    "timeout": session.state.timeouts.resume_s,
                    "name": "native-runloop",
                    "launch_parameters": {
                        "network_policy_id": "np-123",
                        "launch_commands": ["echo hi"],
                    },
                    "tunnel": {"auth_mode": "open"},
                    "gateways": {
                        "GWS_OPENAI": {
                            "gateway": "openai-gateway",
                            "secret": "OPENAI_GATEWAY_SECRET",
                        }
                    },
                    "mcp": {
                        "MCP_TOKEN": {
                            "mcp_config": "github-readonly",
                            "secret": "MCP_SECRET",
                        }
                    },
                    "metadata": {"team": "agents"},
                    "secrets": {"API_KEY": "API_KEY"},
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_platform_blueprints_and_benchmarks_clients(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            blueprint = await client.platform.blueprints.create(name="bp1")
            listed_blueprints = await client.platform.blueprints.list(limit=5)
            public_blueprints = await client.platform.blueprints.list_public(limit=10)
            await client.platform.blueprints.logs(blueprint.id)
            build_info = await client.platform.blueprints.await_build_complete(blueprint.id)
            await client.platform.blueprints.delete(blueprint.id)

            benchmark = await client.platform.benchmarks.create(
                name="bm1",
                required_secret_names=["API_KEY"],
            )
            listed_benchmarks = await client.platform.benchmarks.list(limit=5)
            public_benchmarks = await client.platform.benchmarks.list_public(limit=10)
            await client.platform.benchmarks.update(benchmark.id, description="desc")
            definitions = await client.platform.benchmarks.definitions(benchmark.id)
            run = await client.platform.benchmarks.start_run(benchmark.id, run_name="eval")
            scenario_update = await client.platform.benchmarks.update_scenarios(
                benchmark.id,
                scenarios_to_add=["scenario-1"],
            )
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]

        assert blueprint in listed_blueprints
        assert public_blueprints.data
        assert build_info.status == "build_complete"
        assert sdk.api.blueprints.logs_calls == [(blueprint.id, {})]
        assert sdk.api.blueprints.await_build_complete_calls == [(blueprint.id, {})]
        assert benchmark in listed_benchmarks
        assert public_benchmarks.data
        assert definitions.definitions[0].id == f"def-{benchmark.id}"
        assert run.benchmark_id == benchmark.id
        assert scenario_update.scenarios_to_add == ["scenario-1"]

    @pytest.mark.asyncio
    async def test_platform_secrets_network_policies_and_axons_clients(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            assert not hasattr(client.platform.axons, "subscribe_sse")
            secret = await client.platform.secrets.create(name="SECRET_A", value="secret-value")
            listed_secrets = await client.platform.secrets.list()
            secret_info = await client.platform.secrets.get("SECRET_A")
            updated_secret = await client.platform.secrets.update(
                name="SECRET_A",
                value="secret-value-2",
            )
            deleted_secret = await client.platform.secrets.delete("SECRET_A")

            policy = await client.platform.network_policies.create(name="policy-a", allow_all=True)
            listed_policies = await client.platform.network_policies.list()
            await client.platform.network_policies.update(policy.id, description="limited")
            deleted_policy = await client.platform.network_policies.delete(policy.id)

            axon = await client.platform.axons.create(name="axon-a")
            listed_axons = await client.platform.axons.list()
            publish_result = await client.platform.axons.publish(
                axon.id,
                event_type="task_done",
                origin="AGENT_EVENT",
                payload="{}",
                source="agent",
            )
            query_result = await client.platform.axons.query_sql(axon.id, sql="select 1")
            batch_result = await client.platform.axons.batch_sql(
                axon.id,
                statements=[{"sql": "select 1"}],
            )

        assert secret in listed_secrets
        assert secret_info.name == "SECRET_A"
        assert updated_secret.name == "SECRET_A"
        assert deleted_secret.name == "SECRET_A"
        assert policy in listed_policies
        assert deleted_policy.id == policy.id
        assert axon in listed_axons
        assert publish_result.published is True
        assert query_result.rows == [["ok"]]
        assert batch_result.results[0].success is True

    @pytest.mark.asyncio
    async def test_resume_reconnects_suspended_devbox_and_skips_start(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(pause_on_exit=True),
            )
            state = session.state
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            sdk.devbox.create_calls.clear()
            sdk.devbox.devboxes[state.devbox_id].status = "suspended"

            resumed = await client.resume(state)

        assert sdk.devbox.from_id_calls == [state.devbox_id]
        assert sdk.devbox.create_calls == []
        assert resumed._inner._skip_start is True  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_resume_reconnects_running_devbox_without_pause(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            await session.start()
            state = session.state
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[state.devbox_id]
            devbox.files["existing.txt"] = b"keep"
            sdk.devbox.create_calls.clear()

            resumed = await client.resume(state)
            await resumed.start()

        assert sdk.devbox.from_id_calls == [state.devbox_id]
        assert sdk.devbox.create_calls == []
        assert resumed.state.devbox_id == state.devbox_id
        assert resumed._inner._skip_start is False  # noqa: SLF001
        assert devbox.files["existing.txt"] == b"keep"

    @pytest.mark.asyncio
    async def test_resume_reconnected_devbox_without_pause_does_not_reprovision_accounts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            session.state.snapshot = _RestorableSnapshot(id="snapshot-mismatch")
            state = session.state
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            sdk.devbox.create_calls.clear()

            resumed = await client.resume(state)
            inner = resumed._inner
            provision_called = False

            async def _cannot_skip(self: object, *, is_running: bool) -> bool:
                return False

            async def _restore(self: object) -> None:
                return None

            async def _provision_accounts() -> None:
                nonlocal provision_called
                provision_called = True

            async def _reapply(self: object) -> None:
                return None

            monkeypatch.setattr(
                inner,
                "_can_skip_snapshot_restore_on_resume",
                types.MethodType(_cannot_skip, inner),
            )
            monkeypatch.setattr(
                inner,
                "_restore_snapshot_into_workspace_on_resume",
                types.MethodType(_restore, inner),
            )
            monkeypatch.setattr(inner, "provision_manifest_accounts", _provision_accounts)
            monkeypatch.setattr(
                inner,
                "_reapply_ephemeral_manifest_on_resume",
                types.MethodType(_reapply, inner),
            )

            await resumed.start()

        assert sdk.devbox.from_id_calls == [state.devbox_id]
        assert sdk.devbox.create_calls == []
        assert provision_called is False

    @pytest.mark.asyncio
    async def test_resume_recreates_terminal_devbox_without_pause(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            state = session.state
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            sdk.devbox.devboxes[state.devbox_id].status = "shutdown"
            sdk.devbox.create_calls.clear()
            original_devbox_id = state.devbox_id

            resumed = await client.resume(state)

        assert sdk.devbox.from_id_calls == [original_devbox_id]
        assert len(sdk.devbox.create_calls) == 1
        assert resumed.state.devbox_id != original_devbox_id
        assert resumed._inner._skip_start is False  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_resume_waits_for_devbox_running_before_skip_start(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(pause_on_exit=True),
            )
            session.state.snapshot = _RestorableSnapshot(id="resume-race")
            state = session.state
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            sdk.devbox.create_calls.clear()
            devbox = sdk.devbox.devboxes[state.devbox_id]
            devbox.status = "suspended"
            devbox.resume_returns_before_running = True

            resumed = await client.resume(state)
            inner = resumed._inner

            async def _can_skip(self: object, *, is_running: bool) -> bool:
                return is_running

            async def _reapply(self: object) -> None:
                return None

            async def _restore(self: object) -> None:
                raise AssertionError("resume should wait for running instead of restoring snapshot")

            monkeypatch.setattr(
                inner,
                "_can_skip_snapshot_restore_on_resume",
                types.MethodType(_can_skip, inner),
            )
            monkeypatch.setattr(
                inner,
                "_reapply_ephemeral_manifest_on_resume",
                types.MethodType(_reapply, inner),
            )
            monkeypatch.setattr(
                inner,
                "_restore_snapshot_into_workspace_on_resume",
                types.MethodType(_restore, inner),
            )

            await resumed.start()

        assert devbox.resume_calls == 1
        assert devbox.await_running_calls == 1
        assert devbox.status == "running"
        assert sdk.devbox.create_calls == []
        assert resumed._inner._skip_start is True  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_skip_start_resume_passes_dependencies_to_snapshot_restorable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)
        dependencies = Dependencies().bind_value("test.dep", object())

        async with runloop_module.RunloopSandboxClient(dependencies=dependencies) as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(pause_on_exit=True),
            )
            snapshot = _DependencyAwareSnapshot(id="dep-aware")
            session.state.snapshot = snapshot
            state = session.state
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            sdk.devbox.devboxes[state.devbox_id].status = "suspended"

            resumed = await client.resume(state)
            inner = resumed._inner

            async def _can_skip(self: object, *, is_running: bool) -> bool:
                return is_running

            async def _reapply(self: object) -> None:
                return None

            monkeypatch.setattr(
                inner,
                "_can_skip_snapshot_restore_on_resume",
                types.MethodType(_can_skip, inner),
            )
            monkeypatch.setattr(
                inner,
                "_reapply_ephemeral_manifest_on_resume",
                types.MethodType(_reapply, inner),
            )

            await resumed.start()

        assert snapshot.restorable_dependencies
        assert snapshot.restorable_dependencies[-1] is not None

    @pytest.mark.asyncio
    async def test_root_launch_exec_and_io_use_root_home(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                manifest=Manifest(root="/root/project"),
                options=runloop_module.RunloopSandboxClientOptions(
                    user_parameters=runloop_module.RunloopUserParameters(
                        username="root",
                        uid=0,
                    )
                ),
            )
            await session.start()
            await session.exec("pwd && echo hello", shell=True)
            exec_sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            exec_devbox = exec_sdk.devbox.devboxes[session.state.devbox_id]
            command, _ = exec_devbox.exec_calls[-1]
            await session.write("/root/project/output.txt", io.BytesIO(b"hello"))
            payload = await session.read("/root/project/output.txt")
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]

        assert payload.read() == b"hello"
        assert "cd /root/project &&" in command
        assert devbox.files["project/output.txt"] == b"hello"

    @pytest.mark.asyncio
    async def test_delete_shuts_down_runloop_devbox(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(),
            )
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]

            await client.delete(session)

        assert devbox.shutdown_calls == 1
        assert devbox.status == "shutdown"

    @pytest.mark.asyncio
    async def test_resolve_exposed_port_enables_tunnel_and_formats_endpoint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(exposed_ports=(4500,)),
            )
            await session.start()
            endpoint = await session.resolve_exposed_port(4500)
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]

        assert endpoint == ExposedPortEndpoint(
            host="4500-test-key.tunnel.runloop.ai",
            port=443,
            tls=True,
        )
        assert devbox.enable_tunnel_calls

    @pytest.mark.asyncio
    async def test_exec_timeout_raises_for_runloop_one_shot_exec(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            await session.start()
            with pytest.raises(runloop_module.ExecTimeoutError):
                await session.exec("sleep-forever", shell=False, timeout=0.01)
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            executions = list(sdk.executions.values())

        assert executions
        assert any("sleep-forever" in execution.command for execution in executions)

    @pytest.mark.asyncio
    async def test_exec_maps_runloop_http_408_to_timeout_with_provider_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            await session.start()
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]

            async def _raise_timeout(*args: object, **kwargs: object) -> object:
                _ = (args, kwargs)
                raise _FakeAPIStatusError(
                    408,
                    body={"error": "execution timed out"},
                    url=f"https://api.runloop.ai/v1/devboxes/{devbox.id}/execute",
                    method="POST",
                )

            monkeypatch.setattr(devbox.cmd, "exec", _raise_timeout)

            with pytest.raises(runloop_module.ExecTimeoutError) as exc_info:
                await session.exec("pwd", shell=False, timeout=3.0)

        assert exc_info.value.context["http_status"] == 408
        assert exc_info.value.context["cause_type"] == "_FakeAPIStatusError"
        assert exc_info.value.context["request_method"] == "POST"
        assert exc_info.value.context["request_url"] == (
            f"https://api.runloop.ai/v1/devboxes/{devbox.id}/execute"
        )
        assert exc_info.value.context["provider_body"] == {"error": "execution timed out"}

    @pytest.mark.asyncio
    async def test_exec_maps_runloop_http_error_to_transport_with_provider_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            await session.start()
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]

            async def _raise_rate_limit(*args: object, **kwargs: object) -> object:
                _ = (args, kwargs)
                raise _FakeAPIStatusError(
                    429,
                    body={"error": "rate limited"},
                    url=f"https://api.runloop.ai/v1/devboxes/{devbox.id}/execute",
                    method="POST",
                )

            monkeypatch.setattr(devbox.cmd, "exec", _raise_rate_limit)

            with pytest.raises(runloop_module.ExecTransportError) as exc_info:
                await session.exec("pwd", shell=False)

        assert exc_info.value.context["http_status"] == 429
        assert exc_info.value.context["cause_type"] == "_FakeAPIStatusError"
        assert exc_info.value.context["provider_body"] == {"error": "rate limited"}
        assert exc_info.value.context["detail"] == "exec_failed"

    @pytest.mark.asyncio
    async def test_exec_wraps_command_with_workspace_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                manifest=Manifest(
                    root=f"{runloop_module.DEFAULT_RUNLOOP_WORKSPACE_ROOT}/project",
                    environment=Environment(value={"ONLY_MANIFEST": "1"}),
                ),
                options=runloop_module.RunloopSandboxClientOptions(env_vars={"ONLY_OPTION": "2"}),
            )
            await session.start()
            await session.exec("pwd && echo hello", shell=True)
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]

        assert devbox.exec_calls
        command, params = devbox.exec_calls[-1]
        assert "cd /home/user/project &&" in command
        assert "env --" in command
        assert "ONLY_MANIFEST=1" in command
        assert "ONLY_OPTION=2" in command
        assert "attach_stdin" not in params
        assert "polling_config" in params

    @pytest.mark.asyncio
    async def test_read_and_write_use_home_relative_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            await session.start()
            await session.write(
                "/home/user/project/output.txt",
                io.BytesIO(b"hello"),
            )
            payload = await session.read("/home/user/project/output.txt")
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]

        assert payload.read() == b"hello"
        assert devbox.files["project/output.txt"] == b"hello"

    @pytest.mark.asyncio
    async def test_read_wraps_runloop_http_error_with_provider_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            await session.start()
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]

            async def _raise_download_error(**kwargs: object) -> bytes:
                _ = kwargs
                raise _FakeAPIStatusError(
                    500,
                    body={"error": "download failed"},
                    url=f"https://api.runloop.ai/v1/devboxes/{devbox.id}/files/project/output.txt",
                    method="GET",
                )

            monkeypatch.setattr(devbox.file, "download", _raise_download_error)

            with pytest.raises(runloop_module.WorkspaceArchiveReadError) as exc_info:
                await session.read("/home/user/project/output.txt")

        assert exc_info.value.context["http_status"] == 500
        assert exc_info.value.context["cause_type"] == "_FakeAPIStatusError"
        assert exc_info.value.context["provider_body"] == {"error": "download failed"}
        assert exc_info.value.context["detail"] == "file_download_failed"

    @pytest.mark.asyncio
    async def test_write_wraps_runloop_http_error_with_provider_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            await session.start()
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]

            async def _raise_upload_error(**kwargs: object) -> object:
                _ = kwargs
                raise _FakeAPIStatusError(
                    429,
                    body={"error": "upload rate limited"},
                    url=f"https://api.runloop.ai/v1/devboxes/{devbox.id}/files/project/output.txt",
                    method="PUT",
                )

            monkeypatch.setattr(devbox.file, "upload", _raise_upload_error)

            with pytest.raises(runloop_module.WorkspaceArchiveWriteError) as exc_info:
                await session.write("/home/user/project/output.txt", io.BytesIO(b"hello"))

        assert exc_info.value.context["http_status"] == 429
        assert exc_info.value.context["cause_type"] == "_FakeAPIStatusError"
        assert exc_info.value.context["provider_body"] == {"error": "upload rate limited"}
        assert exc_info.value.context["detail"] == "file_upload_failed"

    @pytest.mark.asyncio
    async def test_manifest_apply_preserves_existing_files_in_non_empty_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                manifest=Manifest(
                    root=f"{runloop_module.DEFAULT_RUNLOOP_WORKSPACE_ROOT}/project",
                    entries={"new.txt": File(content=b"new")},
                ),
                options=runloop_module.RunloopSandboxClientOptions(),
            )
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]
            devbox.files["project/existing.txt"] = b"keep"

            await session.start()

        assert devbox.files["project/existing.txt"] == b"keep"
        assert devbox.files["project/new.txt"] == b"new"

    @pytest.mark.asyncio
    async def test_persist_workspace_returns_native_snapshot_ref_and_hydrate_recreates_devbox(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            old_devbox_id = session.state.devbox_id
            archive = await session.persist_workspace()
            snapshot_id = runloop_module._decode_runloop_snapshot_ref(archive.read())  # noqa: SLF001
            await session.hydrate_workspace(
                io.BytesIO(runloop_module._encode_runloop_snapshot_ref(snapshot_id="snap-1"))  # noqa: SLF001
            )
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]

        assert snapshot_id == "snap-1"
        assert sdk.devbox.create_from_snapshot_calls == [
            ("snap-1", {"timeout": session.state.timeouts.resume_s})
        ]
        assert session.state.devbox_id != old_devbox_id

    @pytest.mark.asyncio
    async def test_restore_snapshot_on_resume_bypasses_workspace_clear(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(),
            )
            session.state.snapshot = _RestorableSnapshot(
                id="runloop-snapshot",
                payload=runloop_module._encode_runloop_snapshot_ref(snapshot_id="snap-9"),  # noqa: SLF001
            )
            state = session.state
            resumed = await client.resume(state)
            inner = resumed._inner

            async def _unexpected_clear() -> None:
                raise AssertionError("workspace clear should be bypassed for Runloop restore")

            inner._clear_workspace_root_on_resume = _unexpected_clear  # noqa: SLF001
            await inner._restore_snapshot_into_workspace_on_resume()  # noqa: SLF001
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]

        assert sdk.devbox.create_from_snapshot_calls == [
            ("snap-9", {"timeout": state.timeouts.resume_s})
        ]

    @pytest.mark.asyncio
    async def test_restore_tar_snapshot_on_resume_clears_workspace_before_hydrate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                manifest=Manifest(root=f"{runloop_module.DEFAULT_RUNLOOP_WORKSPACE_ROOT}/project"),
                options=runloop_module.RunloopSandboxClientOptions(),
            )
            session.state.snapshot = _RestorableSnapshot(
                id="tar-snapshot",
                payload=_build_tar_bytes({"new.txt": b"new"}),
            )
            resumed = await client.resume(session.state)
            inner = resumed._inner
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[resumed.state.devbox_id]
            devbox.files["project/existing.txt"] = b"stale"
            cleared = False

            async def _clear_workspace_root_on_resume() -> None:
                nonlocal cleared
                cleared = True
                devbox.files.pop("project/existing.txt", None)

            inner._clear_workspace_root_on_resume = (  # noqa: SLF001
                _clear_workspace_root_on_resume
            )
            await inner._restore_snapshot_into_workspace_on_resume()  # noqa: SLF001

        assert cleared is True
        assert devbox.files["project/new.txt"] == b"new"
        assert "project/existing.txt" not in devbox.files

    @pytest.mark.asyncio
    async def test_restore_snapshot_on_resume_passes_dependencies_to_snapshot_restore(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)
        dependencies = Dependencies().bind_value("test.dep", object())

        async with runloop_module.RunloopSandboxClient(dependencies=dependencies) as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            snapshot = _DependencyAwareSnapshot(
                id="dep-aware-restore",
                payload=runloop_module._encode_runloop_snapshot_ref(snapshot_id="snap-dep"),  # noqa: SLF001
            )
            session.state.snapshot = snapshot
            resumed = await client.resume(session.state)

            await resumed._inner._restore_snapshot_into_workspace_on_resume()  # noqa: SLF001

        assert snapshot.restore_dependencies
        assert snapshot.restore_dependencies[-1] is not None

    @pytest.mark.asyncio
    async def test_hydrate_workspace_wraps_provider_error_with_snapshot_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]

            async def _raise_restore_error(snapshot_id: str, **kwargs: object) -> object:
                _ = (snapshot_id, kwargs)
                raise _FakeAPIStatusError(
                    500,
                    body={"error": "restore failed"},
                    url="https://api.runloop.ai/v1/devboxes/from_snapshot",
                    method="POST",
                )

            monkeypatch.setattr(sdk.devbox, "create_from_snapshot", _raise_restore_error)

            with pytest.raises(runloop_module.WorkspaceArchiveWriteError) as exc_info:
                await session.hydrate_workspace(
                    io.BytesIO(runloop_module._encode_runloop_snapshot_ref(snapshot_id="snap-7"))  # noqa: SLF001
                )

        assert exc_info.value.context["reason"] == "snapshot_restore_failed"
        assert exc_info.value.context["snapshot_id"] == "snap-7"
        assert exc_info.value.context["http_status"] == 500
        assert exc_info.value.context["cause_type"] == "_FakeAPIStatusError"
        assert exc_info.value.context["provider_body"] == {"error": "restore failed"}

    @pytest.mark.asyncio
    async def test_hydrate_workspace_accepts_tar_fallback_payload(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)
        archive = _build_tar_bytes({"notes/output.txt": b"from tar"})

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            await session.hydrate_workspace(io.BytesIO(archive))
            payload = await session.read("/home/user/notes/output.txt")
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]

        assert payload.read() == b"from tar"
        assert f".sandbox-runloop-hydrate-{session.state.session_id.hex}.tar" not in devbox.files

    @pytest.mark.asyncio
    async def test_hydrate_workspace_rejects_invalid_non_snapshot_non_tar_payload(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())

            with pytest.raises(runloop_module.WorkspaceArchiveWriteError) as exc_info:
                await session.hydrate_workspace(io.BytesIO(b"not-a-valid-tar"))

        assert exc_info.value.context["reason"] == "unsafe_or_invalid_tar"

    @pytest.mark.asyncio
    async def test_persist_workspace_remounts_mounts_after_snapshot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)
        mount = _RecordingMount()

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                manifest=Manifest(
                    root=runloop_module.DEFAULT_RUNLOOP_WORKSPACE_ROOT,
                    entries={"mount": mount},
                ),
                options=runloop_module.RunloopSandboxClientOptions(),
            )
            archive = await session.persist_workspace()

        assert runloop_module._decode_runloop_snapshot_ref(archive.read()) == "snap-1"  # noqa: SLF001
        mount_path = Path(f"{runloop_module.DEFAULT_RUNLOOP_WORKSPACE_ROOT}/mount")
        assert mount._unmounted_paths == [mount_path]
        assert mount._mounted_paths == [mount_path]

    @pytest.mark.asyncio
    async def test_resolve_exposed_port_wraps_provider_error_with_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(exposed_ports=(4500,))
            )
            await session.start()
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]

            async def _raise_tunnel_error(*args: object, **kwargs: object) -> str | None:
                _ = (args, kwargs)
                raise _FakeAPIStatusError(
                    429,
                    body={"error": "tunnel rate limited"},
                    url=f"https://api.runloop.ai/v1/devboxes/{devbox.id}",
                    method="GET",
                )

            monkeypatch.setattr(devbox, "get_tunnel_url", _raise_tunnel_error)

            with pytest.raises(runloop_module.ExposedPortUnavailableError) as exc_info:
                await session.resolve_exposed_port(4500)

        assert exc_info.value.context["http_status"] == 429
        assert exc_info.value.context["cause_type"] == "_FakeAPIStatusError"
        assert exc_info.value.context["provider_body"] == {"error": "tunnel rate limited"}
        assert exc_info.value.context["detail"] == "get_tunnel_url_failed"

    @pytest.mark.asyncio
    async def test_resolve_exposed_port_keeps_invalid_url_detail_for_parse_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(
                options=runloop_module.RunloopSandboxClientOptions(exposed_ports=(4500,))
            )
            await session.start()
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]

            async def _invalid_tunnel_url(*args: object, **kwargs: object) -> str | None:
                _ = (args, kwargs)
                return "https://"

            monkeypatch.setattr(devbox, "get_tunnel_url", _invalid_tunnel_url)

            with pytest.raises(runloop_module.ExposedPortUnavailableError) as exc_info:
                await session.resolve_exposed_port(4500)

        assert exc_info.value.context["detail"] == "invalid_tunnel_url"

    @pytest.mark.asyncio
    async def test_runloop_shell_capability_does_not_expose_write_stdin(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            capability = Shell()
            capability.bind(session)
            tools = capability.tools()

        assert [tool.name for tool in tools] == ["exec_command"]

    @pytest.mark.asyncio
    async def test_exec_command_tool_uses_one_shot_exec_for_tty_requests(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runloop_module = _load_runloop_module(monkeypatch)

        async with runloop_module.RunloopSandboxClient() as client:
            session = await client.create(options=runloop_module.RunloopSandboxClientOptions())
            await session.start()
            sdk = _FakeAsyncRunloopSDK.created_instances[-1]
            devbox = sdk.devbox.devboxes[session.state.devbox_id]
            exec_calls_before = len(devbox.exec_calls)
            exec_async_calls_before = len(devbox.exec_async_calls)

            output = await ExecCommandTool(session=session).run(
                ExecCommandArgs(cmd="echo hello", tty=True, yield_time_ms=50)
            )

        assert "Process exited with code 0" in output
        assert "Process running with session ID" not in output
        assert "hello" in output
        assert len(devbox.exec_calls) == exec_calls_before + 1
        assert len(devbox.exec_async_calls) == exec_async_calls_before
