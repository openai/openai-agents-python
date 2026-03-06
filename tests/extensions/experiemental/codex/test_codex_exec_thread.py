from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

import pytest

from agents.exceptions import UserError
from agents.extensions.experimental.codex import Usage
from agents.extensions.experimental.codex.codex import Codex, _normalize_env
from agents.extensions.experimental.codex.codex_options import CodexOptions, coerce_codex_options
from agents.extensions.experimental.codex.exec import CodexExec
from agents.extensions.experimental.codex.output_schema_file import (
    OutputSchemaFile,
    create_output_schema_file,
)
from agents.extensions.experimental.codex.thread import Thread, _normalize_input
from agents.extensions.experimental.codex.thread_options import ThreadOptions, coerce_thread_options
from agents.extensions.experimental.codex.turn_options import TurnOptions

exec_module = importlib.import_module("agents.extensions.experimental.codex.exec")
thread_module = importlib.import_module("agents.extensions.experimental.codex.thread")
output_schema_module = importlib.import_module(
    "agents.extensions.experimental.codex.output_schema_file"
)


class FakeStdin:
    def __init__(self) -> None:
        self.buffer = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [line.encode("utf-8") for line in lines]

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class FakeStderr:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class FakeProcess:
    def __init__(
        self,
        stdout_lines: list[str],
        stderr_chunks: list[bytes] | None = None,
        *,
        returncode: int | None = 0,
        stdin_present: bool = True,
        stdout_present: bool = True,
        stderr_present: bool = True,
    ) -> None:
        self.stdin = FakeStdin() if stdin_present else None
        self.stdout = FakeStdout(stdout_lines) if stdout_present else None
        self.stderr = FakeStderr(stderr_chunks or []) if stderr_present else None
        self.returncode = returncode
        self.killed = False
        self.terminated = False

    async def wait(self) -> None:
        if self.returncode is None:
            self.returncode = 0

    def kill(self) -> None:
        self.killed = True

    def terminate(self) -> None:
        self.terminated = True


class FakeExec:
    def __init__(self, events: list[Any], delay: float = 0.0) -> None:
        self.events = events
        self.delay = delay
        self.last_args: Any = None

    async def run(self, args: Any):
        self.last_args = args
        for event in self.events:
            if self.delay:
                await asyncio.sleep(self.delay)
            payload = event if isinstance(event, str) else json.dumps(event)
            yield payload


def test_output_schema_file_none_schema() -> None:
    result = create_output_schema_file(None)
    assert result.schema_path is None
    result.cleanup()


def test_output_schema_file_rejects_non_object() -> None:
    with pytest.raises(UserError, match="output_schema must be a plain JSON object"):
        create_output_schema_file(cast(Any, ["not", "an", "object"]))


def test_output_schema_file_creates_and_cleans() -> None:
    schema = {"type": "object", "properties": {"foo": {"type": "string"}}}
    result = create_output_schema_file(schema)
    assert result.schema_path is not None
    with open(result.schema_path, encoding="utf-8") as handle:
        assert json.load(handle) == schema
    result.cleanup()
    assert not os.path.exists(result.schema_path)


def test_output_schema_file_cleanup_swallows_rmtree_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = {"type": "object"}
    called = False

    def bad_rmtree(_path: str, ignore_errors: bool = True) -> None:
        nonlocal called
        called = True
        raise OSError("boom")

    monkeypatch.setattr(output_schema_module.shutil, "rmtree", bad_rmtree)

    result = create_output_schema_file(schema)
    result.cleanup()

    assert called is True


def test_output_schema_file_cleanup_on_write_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = {"type": "object"}
    cleanup_called = False

    def bad_dump(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    def fake_rmtree(_path: str, ignore_errors: bool = True) -> None:
        nonlocal cleanup_called
        cleanup_called = True

    monkeypatch.setattr(output_schema_module.json, "dump", bad_dump)
    monkeypatch.setattr(output_schema_module.shutil, "rmtree", fake_rmtree)

    with pytest.raises(RuntimeError, match="boom"):
        create_output_schema_file(schema)

    assert cleanup_called is True


def test_normalize_input_merges_text_and_images() -> None:
    prompt, images = _normalize_input(
        [
            {"type": "text", "text": "first"},
            {"type": "local_image", "path": "/tmp/a.png"},
            {"type": "text", "text": "second"},
            {"type": "local_image", "path": ""},
        ]
    )
    assert prompt == "first\n\nsecond"
    assert images == ["/tmp/a.png"]


def test_normalize_env_stringifies_values() -> None:
    env = _normalize_env(CodexOptions(env=cast(dict[str, str], {"FOO": 1, 2: "bar"})))
    assert env == {"FOO": "1", "2": "bar"}


def test_coerce_codex_options_rejects_unknown_fields() -> None:
    with pytest.raises(UserError, match="Unknown CodexOptions field"):
        coerce_codex_options({"unknown": "value"})


def test_coerce_thread_options_rejects_unknown_fields() -> None:
    with pytest.raises(UserError, match="Unknown ThreadOptions field"):
        coerce_thread_options({"unknown": "value"})


def test_codex_start_and_resume_thread() -> None:
    codex = Codex(CodexOptions(codex_path_override="/bin/codex"))
    thread = codex.start_thread({"model": "gpt"})
    assert thread.id is None
    resumed = codex.resume_thread("thread-1", {"model": "gpt"})
    assert resumed.id == "thread-1"


def test_codex_init_accepts_mapping_options() -> None:
    codex = Codex({"codex_path_override": "/bin/codex"})
    assert codex._exec._executable_path == "/bin/codex"


def test_codex_init_accepts_kwargs() -> None:
    codex = Codex(codex_path_override="/bin/codex", base_url="https://example.com")
    assert codex._exec._executable_path == "/bin/codex"
    assert codex._options.base_url == "https://example.com"


def test_codex_init_accepts_stream_limit_kwarg() -> None:
    codex = Codex(codex_path_override="/bin/codex", codex_subprocess_stream_limit_bytes=123456)
    assert codex._exec._subprocess_stream_limit_bytes == 123456


def test_codex_init_rejects_options_and_kwargs() -> None:
    with pytest.raises(UserError, match="Codex options must be provided"):
        Codex(  # type: ignore[call-overload]
            cast(Any, CodexOptions()), codex_path_override="/bin/codex"
        )


def test_codex_init_kw_matches_codex_options() -> None:
    signature = inspect.signature(Codex.__init__)
    kw_only = [
        param.name
        for param in signature.parameters.values()
        if param.kind == inspect.Parameter.KEYWORD_ONLY
    ]
    option_fields = [field.name for field in fields(CodexOptions)]
    assert kw_only == option_fields


def test_codex_exec_stream_limit_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exec_module._SUBPROCESS_STREAM_LIMIT_ENV_VAR, "131072")
    exec_client = exec_module.CodexExec(executable_path="/bin/codex")
    assert exec_client._subprocess_stream_limit_bytes == 131072


def test_codex_exec_stream_limit_explicit_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exec_module._SUBPROCESS_STREAM_LIMIT_ENV_VAR, "262144")
    exec_client = exec_module.CodexExec(
        executable_path="/bin/codex",
        subprocess_stream_limit_bytes=524288,
    )
    assert exec_client._subprocess_stream_limit_bytes == 524288


def test_codex_exec_stream_limit_rejects_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exec_module._SUBPROCESS_STREAM_LIMIT_ENV_VAR, "not-a-number")
    with pytest.raises(UserError, match=exec_module._SUBPROCESS_STREAM_LIMIT_ENV_VAR):
        _ = exec_module.CodexExec(executable_path="/bin/codex")


def test_codex_exec_stream_limit_rejects_out_of_range_value() -> None:
    with pytest.raises(UserError, match="must be between"):
        _ = exec_module.CodexExec(
            executable_path="/bin/codex",
            subprocess_stream_limit_bytes=1024,
        )


@pytest.mark.asyncio
async def test_codex_exec_run_builds_command_args_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    process = FakeProcess(stdout_lines=["line-1\n", "line-2\n"])

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(exec_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    exec_client = exec_module.CodexExec(executable_path="/bin/codex", env={"FOO": "bar"})
    args = exec_module.CodexExecArgs(
        input="hello",
        base_url="https://example.com",
        api_key="api-key",
        thread_id="thread-123",
        images=["/tmp/img.png"],
        model="gpt-4.1-mini",
        sandbox_mode="read-only",
        working_directory="/work",
        additional_directories=["/extra-a", "/extra-b"],
        skip_git_repo_check=True,
        output_schema_file="/tmp/schema.json",
        model_reasoning_effort="high",
        network_access_enabled=True,
        web_search_mode="live",
        approval_policy="on-request",
    )

    output = [line async for line in exec_client.run(args)]

    assert output == ["line-1", "line-2"]
    assert process.stdin is not None
    assert process.stdin.buffer == b"hello"
    assert process.stdin.closed is True

    assert captured["args"][0] == "/bin/codex"
    assert list(captured["args"][1:]) == [
        "exec",
        "--experimental-json",
        "--model",
        "gpt-4.1-mini",
        "--sandbox",
        "read-only",
        "--cd",
        "/work",
        "--add-dir",
        "/extra-a",
        "--add-dir",
        "/extra-b",
        "--skip-git-repo-check",
        "--output-schema",
        "/tmp/schema.json",
        "--config",
        'model_reasoning_effort="high"',
        "--config",
        "sandbox_workspace_write.network_access=true",
        "--config",
        'web_search="live"',
        "--config",
        'approval_policy="on-request"',
        "resume",
        "thread-123",
        "--image",
        "/tmp/img.png",
        "-",
    ]

    env = captured["kwargs"]["env"]
    assert env["FOO"] == "bar"
    assert env[exec_module._INTERNAL_ORIGINATOR_ENV] == exec_module._TYPESCRIPT_SDK_ORIGINATOR
    assert env["OPENAI_BASE_URL"] == "https://example.com"
    assert env["CODEX_API_KEY"] == "api-key"


@pytest.mark.asyncio
async def test_codex_exec_run_handles_large_single_line_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    large_payload = "x" * (2**16 + 1)

    class StreamReaderProcess:
        def __init__(self, *, line: str, limit: int) -> None:
            self.stdin = FakeStdin()
            self.stdout = asyncio.StreamReader(limit=limit)
            self.stdout.feed_data(f"{line}\n".encode())
            self.stdout.feed_eof()
            self.stderr = FakeStderr([])
            self.returncode: int | None = 0
            self.killed = False
            self.terminated = False

        async def wait(self) -> None:
            if self.returncode is None:
                self.returncode = 0

        def kill(self) -> None:
            self.killed = True

        def terminate(self) -> None:
            self.terminated = True

    async def fake_create_subprocess_exec(*_args: Any, **kwargs: Any) -> StreamReaderProcess:
        captured["kwargs"] = kwargs
        return StreamReaderProcess(line=large_payload, limit=kwargs["limit"])

    monkeypatch.setattr(exec_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    exec_client = exec_module.CodexExec(executable_path="/bin/codex")
    output = [line async for line in exec_client.run(exec_module.CodexExecArgs(input="hello"))]

    assert output == [large_payload]
    assert captured["kwargs"]["limit"] == exec_module._DEFAULT_SUBPROCESS_STREAM_LIMIT_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "expected_config"),
    [
        (True, 'web_search="live"'),
        (False, 'web_search="disabled"'),
    ],
)
async def test_codex_exec_run_web_search_enabled_flags(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, expected_config: str
) -> None:
    captured: dict[str, Any] = {}
    process = FakeProcess(stdout_lines=[])

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        captured["args"] = args
        return process

    monkeypatch.setattr(exec_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    exec_client = exec_module.CodexExec(executable_path="/bin/codex")
    args = exec_module.CodexExecArgs(input="hello", web_search_enabled=enabled)

    _ = [line async for line in exec_client.run(args)]
    command_args = list(captured["args"][1:])
    assert "--config" in command_args
    assert expected_config in command_args


@pytest.mark.asyncio
async def test_codex_exec_run_raises_on_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(stdout_lines=[], stderr_chunks=[b"bad"], returncode=2)

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr(exec_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    exec_client = exec_module.CodexExec(executable_path="/bin/codex")
    args = exec_module.CodexExecArgs(input="hello")

    with pytest.raises(RuntimeError, match="exited with code 2"):
        async for _ in exec_client.run(args):
            pass


@pytest.mark.asyncio
async def test_codex_exec_run_raises_without_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(stdout_lines=[], stdin_present=False)

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr(exec_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    exec_client = exec_module.CodexExec(executable_path="/bin/codex")
    args = exec_module.CodexExecArgs(input="hello")

    with pytest.raises(RuntimeError, match="no stdin"):
        async for _ in exec_client.run(args):
            pass
    assert process.killed is True


@pytest.mark.asyncio
async def test_codex_exec_run_raises_without_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(stdout_lines=[], stdout_present=False)

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr(exec_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    exec_client = exec_module.CodexExec(executable_path="/bin/codex")
    args = exec_module.CodexExecArgs(input="hello")

    with pytest.raises(RuntimeError, match="no stdout"):
        async for _ in exec_client.run(args):
            pass
    assert process.killed is True


@pytest.mark.asyncio
async def test_watch_signal_terminates_process() -> None:
    signal = asyncio.Event()
    process = FakeProcess(stdout_lines=[], returncode=None)

    task = asyncio.create_task(exec_module._watch_signal(signal, process))
    signal.set()
    await task

    assert process.terminated is True


@pytest.mark.parametrize(
    ("system", "arch", "expected"),
    [
        ("linux", "x86_64", "x86_64-unknown-linux-musl"),
        ("linux", "aarch64", "aarch64-unknown-linux-musl"),
        ("darwin", "x86_64", "x86_64-apple-darwin"),
        ("darwin", "arm64", "aarch64-apple-darwin"),
        ("win32", "x86_64", "x86_64-pc-windows-msvc"),
        ("win32", "arm64", "aarch64-pc-windows-msvc"),
    ],
)
def test_platform_target_triple_mapping(
    monkeypatch: pytest.MonkeyPatch, system: str, arch: str, expected: str
) -> None:
    monkeypatch.setattr(exec_module.sys, "platform", system)
    monkeypatch.setattr(exec_module.platform, "machine", lambda: arch)
    assert exec_module._platform_target_triple() == expected


def test_platform_target_triple_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exec_module.sys, "platform", "solaris")
    monkeypatch.setattr(exec_module.platform, "machine", lambda: "sparc")
    with pytest.raises(RuntimeError, match="Unsupported platform"):
        exec_module._platform_target_triple()


def test_find_codex_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_PATH", "/custom/codex")
    assert exec_module.find_codex_path() == "/custom/codex"


def test_find_codex_path_uses_shutil_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_PATH", raising=False)
    monkeypatch.setattr(exec_module.shutil, "which", lambda _name: "/usr/local/bin/codex")
    assert exec_module.find_codex_path() == "/usr/local/bin/codex"


def test_find_codex_path_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_PATH", raising=False)
    monkeypatch.setattr(exec_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(exec_module, "_platform_target_triple", lambda: "dummy-triple")
    monkeypatch.setattr(exec_module.sys, "platform", "linux")
    result = exec_module.find_codex_path()
    expected_root = (
        Path(cast(str, exec_module.__file__)).resolve().parent.parent.parent
        / "vendor"
        / "dummy-triple"
        / "codex"
        / "codex"
    )
    assert result == str(expected_root)


@pytest.mark.asyncio
async def test_thread_run_streamed_passes_options_and_updates_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {"type": "thread.started", "thread_id": "thread-42"},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
    ]
    fake_exec = FakeExec(events)
    options = CodexOptions(base_url="https://example.com", api_key="api-key")
    thread_options = ThreadOptions(
        model="gpt-4.1-mini",
        sandbox_mode="read-only",
        working_directory="/work",
        skip_git_repo_check=True,
        model_reasoning_effort="low",
        network_access_enabled=False,
        web_search_mode="cached",
        approval_policy="on-request",
        additional_directories=["/extra"],
    )
    thread = Thread(
        exec_client=cast(CodexExec, fake_exec),
        options=options,
        thread_options=thread_options,
    )
    cleanup_called = False

    def fake_create_output_schema_file(schema: dict[str, Any] | None) -> OutputSchemaFile:
        nonlocal cleanup_called

        def cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True

        return OutputSchemaFile(schema_path="/tmp/schema.json", cleanup=cleanup)

    monkeypatch.setattr(thread_module, "create_output_schema_file", fake_create_output_schema_file)

    streamed = await thread.run_streamed(
        [
            {"type": "text", "text": "hello"},
            {"type": "local_image", "path": "/tmp/a.png"},
        ],
        TurnOptions(output_schema={"type": "object"}),
    )
    collected = [event async for event in streamed.events]

    assert collected[0].type == "thread.started"
    assert thread.id == "thread-42"
    assert cleanup_called is True

    assert fake_exec.last_args is not None
    assert fake_exec.last_args.output_schema_file == "/tmp/schema.json"
    assert fake_exec.last_args.model == "gpt-4.1-mini"
    assert fake_exec.last_args.sandbox_mode == "read-only"
    assert fake_exec.last_args.working_directory == "/work"
    assert fake_exec.last_args.skip_git_repo_check is True
    assert fake_exec.last_args.model_reasoning_effort == "low"
    assert fake_exec.last_args.network_access_enabled is False
    assert fake_exec.last_args.web_search_mode == "cached"
    assert fake_exec.last_args.approval_policy == "on-request"
    assert fake_exec.last_args.additional_directories == ["/extra"]
    assert fake_exec.last_args.images == ["/tmp/a.png"]


@pytest.mark.asyncio
async def test_thread_run_aggregates_items_and_usage() -> None:
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "agent-1", "type": "agent_message", "text": "done"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 2, "cached_input_tokens": 1, "output_tokens": 3},
        },
    ]
    thread = Thread(
        exec_client=cast(CodexExec, FakeExec(events)),
        options=CodexOptions(),
        thread_options=ThreadOptions(),
    )
    result = await thread.run("hello")

    assert result.final_response == "done"
    assert result.usage == Usage(
        input_tokens=2,
        cached_input_tokens=1,
        output_tokens=3,
    )
    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_thread_run_raises_on_failure() -> None:
    events = [
        {"type": "turn.failed", "error": {"message": "boom"}},
    ]
    thread = Thread(
        exec_client=cast(CodexExec, FakeExec(events)),
        options=CodexOptions(),
        thread_options=ThreadOptions(),
    )
    with pytest.raises(RuntimeError, match="boom"):
        await thread.run("hello")


@pytest.mark.asyncio
async def test_thread_run_raises_on_stream_error() -> None:
    events = [
        {"type": "error", "message": "boom"},
    ]
    thread = Thread(
        exec_client=cast(CodexExec, FakeExec(events)),
        options=CodexOptions(),
        thread_options=ThreadOptions(),
    )
    with pytest.raises(RuntimeError, match="Codex stream error: boom"):
        await thread.run("hello")


@pytest.mark.asyncio
async def test_thread_run_streamed_raises_on_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = ["not-json"]
    fake_exec = FakeExec(events)
    thread = Thread(
        exec_client=cast(CodexExec, fake_exec),
        options=CodexOptions(),
        thread_options=ThreadOptions(),
    )

    def fake_create_output_schema_file(schema: dict[str, Any] | None) -> OutputSchemaFile:
        return OutputSchemaFile(schema_path=None, cleanup=lambda: None)

    monkeypatch.setattr(thread_module, "create_output_schema_file", fake_create_output_schema_file)

    streamed = await thread.run_streamed("hello")
    with pytest.raises(RuntimeError, match="Failed to parse event"):
        async for _ in streamed.events:
            pass


@pytest.mark.asyncio
async def test_thread_run_streamed_idle_timeout_sets_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        }
    ]
    fake_exec = FakeExec(events, delay=0.2)
    thread = Thread(
        exec_client=cast(CodexExec, fake_exec),
        options=CodexOptions(),
        thread_options=ThreadOptions(),
    )
    signal = asyncio.Event()

    def fake_create_output_schema_file(schema: dict[str, Any] | None) -> OutputSchemaFile:
        return OutputSchemaFile(schema_path=None, cleanup=lambda: None)

    monkeypatch.setattr(thread_module, "create_output_schema_file", fake_create_output_schema_file)

    with pytest.raises(RuntimeError, match="Codex stream idle for"):
        async for _ in thread._run_streamed_internal(
            "hello", TurnOptions(signal=signal, idle_timeout_seconds=0.01)
        ):
            pass

    assert signal.is_set() is True
