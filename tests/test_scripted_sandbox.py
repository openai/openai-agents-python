from __future__ import annotations

import inspect
import io
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast, get_type_hints

import pytest
from typing_extensions import assert_type

from agents import RunConfig, Runner
from agents.sandbox import ExecResult, Manifest, SandboxAgent
from agents.sandbox.capabilities import Shell
from agents.sandbox.files import FileEntry
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.pty_types import PtyExecUpdate
from agents.testing import (
    InvalidSandboxStep,
    SandboxCall,
    SandboxCallMatcherError,
    ScriptedModel,
    ScriptedSandboxSession,
    UnconsumedSandboxSteps,
    UnexpectedSandboxCall,
    assistant_message,
    function_call,
    scripted_sandbox_session,
)
from agents.testing.sandbox import ScriptedSandboxSession as CanonicalScriptedSandboxSession


class _CallableBytesIO(io.BytesIO):
    def __call__(self) -> None:
        pass


class _CallableBufferedReader(io.BufferedReader):
    def __call__(self) -> None:
        pass


def test_scripted_sandbox_has_public_result_type() -> None:
    session = scripted_sandbox_session()
    assert_type(session, ScriptedSandboxSession)
    base_session: BaseSandboxSession = session

    assert ScriptedSandboxSession is CanonicalScriptedSandboxSession
    assert get_type_hints(scripted_sandbox_session)["return"] is ScriptedSandboxSession
    assert inspect.isabstract(ScriptedSandboxSession)
    assert isinstance(session, ScriptedSandboxSession)
    assert base_session is session


def test_scripted_sandbox_exposes_only_configured_scriptable_methods() -> None:
    session = scripted_sandbox_session(
        [{"method": "exec", "result": ExecResult(stdout=b"", stderr=b"", exit_code=0)}]
    )

    assert isinstance(session, BaseSandboxSession)
    assert hasattr(session, "exec")
    assert not hasattr(session, "read")
    assert not hasattr(session, "apply_patch")
    assert not hasattr(session, "pty_exec_start")
    assert "exec" in dir(session)
    assert "read" not in dir(session)
    assert "apply_patch" not in dir(session)
    assert "pty_exec_start" not in dir(session)


@pytest.mark.asyncio
async def test_scripted_sandbox_cleanup_does_not_advertise_pty_termination() -> None:
    session = scripted_sandbox_session()

    assert not hasattr(session, "pty_terminate_all")
    assert "pty_terminate_all" not in dir(session)

    await session.aclose()

    async with scripted_sandbox_session() as context_session:
        assert await context_session.running() is True

    assert await context_session.running() is False


def test_scripted_sandbox_snapshots_manifest_and_derives_pty_support() -> None:
    manifest = Manifest(root="/configured")
    session = scripted_sandbox_session(
        [{"method": "pty_exec_start", "result": None}],
        manifest=manifest,
    )
    manifest.root = "/mutated"

    assert session.state.manifest.root == "/configured"
    assert session.supports_pty() is True

    pty_session = scripted_sandbox_session(
        [
            {"method": "pty_exec_start", "result": None},
            {"method": "pty_write_stdin", "result": None},
        ]
    )
    assert pty_session.supports_pty() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_method", "missing_method"),
    [
        ("pty_exec_start", "pty_write_stdin"),
        ("pty_write_stdin", "pty_exec_start"),
    ],
)
async def test_scripted_sandbox_exposes_pty_methods_as_one_capability(
    configured_method: str,
    missing_method: str,
) -> None:
    session = scripted_sandbox_session([{"method": configured_method, "result": None}])

    assert session.supports_pty() is True
    assert hasattr(session, "pty_exec_start")
    assert hasattr(session, "pty_write_stdin")
    assert "pty_exec_start" in dir(session)
    assert "pty_write_stdin" in dir(session)

    with pytest.raises(UnexpectedSandboxCall) as exc_info:
        if missing_method == "pty_exec_start":
            await session.pty_exec_start("pwd")
        else:
            await session.pty_write_stdin(session_id=1, chars="")

    assert exc_info.value.actual_method == missing_method
    assert exc_info.value.expected_method == configured_method


@pytest.mark.asyncio
async def test_scripted_sandbox_supports_capability_method_inventory() -> None:
    read_result = io.BytesIO(b"contents")
    list_result: list[FileEntry] = []
    pty_start_result = PtyExecUpdate(
        process_id=123,
        output=b"started",
        exit_code=None,
        original_token_count=None,
    )
    pty_write_result = PtyExecUpdate(
        process_id=None,
        output=b"done",
        exit_code=0,
        original_token_count=None,
    )
    session = scripted_sandbox_session(
        [
            {"method": "read", "result": read_result},
            {"method": "write", "result": None},
            {"method": "ls", "result": list_result},
            {"method": "mkdir", "result": None},
            {"method": "rm", "result": None},
            {"method": "apply_patch", "result": "Done!"},
            {"method": "pty_exec_start", "result": pty_start_result},
            {"method": "pty_write_stdin", "result": pty_write_result},
        ]
    )
    stream = io.BytesIO(b"payload")

    returned_read_result = cast(io.BytesIO, await session.read(Path("in.txt")))
    assert returned_read_result is not read_result
    assert returned_read_result.getvalue() == b"contents"
    await session.write(Path("out.txt"), stream)
    assert await session.ls(".") == []
    await session.mkdir("new", parents=True)
    await session.rm("old", recursive=True)
    assert await session.apply_patch({"type": "delete_file", "path": "old.txt"}) == "Done!"
    assert (await session.pty_exec_start("sh", tty=True)).process_id == 123
    assert (await session.pty_write_stdin(session_id=123, chars="exit")).exit_code == 0

    assert [call.method for call in session.calls] == [
        "read",
        "write",
        "ls",
        "mkdir",
        "rm",
        "apply_patch",
        "pty_exec_start",
        "pty_write_stdin",
    ]
    recorded_stream = cast(io.BytesIO, session.calls[1].args[1])
    assert recorded_stream is not stream
    assert recorded_stream.getvalue() == b"payload"
    assert recorded_stream.tell() == 0
    session.assert_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "original_content"),
    [(io.BytesIO(b"before"), b"before"), (io.StringIO("before"), "before")],
)
async def test_scripted_sandbox_snapshots_supported_stream_results(
    configured: io.BytesIO | io.StringIO,
    original_content: bytes | str,
) -> None:
    configured.seek(2)
    session = scripted_sandbox_session([{"method": "read", "result": configured}])
    configured.seek(0)
    configured.write(b"after" if isinstance(configured, io.BytesIO) else "after")
    configured.close()

    result = cast(io.BytesIO | io.StringIO, await session.read(Path("input.txt")))

    assert result is not configured
    assert result.tell() == 2
    assert result.getvalue() == original_content
    session.assert_complete()


@pytest.mark.asyncio
async def test_scripted_sandbox_snapshots_supported_stream_call_arguments() -> None:
    session = scripted_sandbox_session([{"method": "write", "result": None}])
    source = io.BytesIO(b"before")
    source.seek(3)

    await session.write(Path("output.bin"), source)
    source.seek(0)
    source.write(b"after")
    source.close()

    recorded = cast(io.BytesIO, session.calls[0].args[1])
    assert recorded is not source
    assert recorded.tell() == 3
    assert recorded.getvalue() == b"before"
    recorded.write(b"changed")
    retained = cast(io.BytesIO, session.calls[0].args[1])
    assert retained.tell() == 3
    assert retained.getvalue() == b"before"
    session.assert_complete()


@pytest.mark.asyncio
async def test_scripted_sandbox_snapshots_callable_supported_streams() -> None:
    result_source = _CallableBytesIO(b"result")
    call_source = _CallableBytesIO(b"call")
    session = scripted_sandbox_session(
        [
            {"method": "read", "result": result_source},
            {"method": "write", "result": None},
        ]
    )

    result = cast(io.BytesIO, await session.read(Path("input.bin")))
    await session.write(Path("output.bin"), call_source)

    assert result is not result_source
    assert result.getvalue() == b"result"
    recorded = cast(io.BytesIO, session.calls[1].args[1])
    assert recorded is not call_source
    assert recorded.getvalue() == b"call"
    session.assert_complete()


def test_scripted_sandbox_rejects_unsupported_or_closed_stream_results() -> None:
    with io.BufferedReader(io.BytesIO(b"payload")) as unsupported:
        with pytest.raises(InvalidSandboxStep) as unsupported_info:
            scripted_sandbox_session([{"method": "read", "result": unsupported}])
    assert unsupported_info.value.reason == "invalid_outcome"

    closed = io.BytesIO(b"payload")
    closed.close()
    with pytest.raises(InvalidSandboxStep) as closed_info:
        scripted_sandbox_session([{"method": "read", "result": closed}])
    assert closed_info.value.reason == "invalid_outcome"


@pytest.mark.asyncio
async def test_scripted_sandbox_rejects_unsupported_stream_call_before_commit() -> None:
    session = scripted_sandbox_session([{"method": "write", "result": None}])

    with io.BufferedReader(io.BytesIO(b"payload")) as unsupported:
        with pytest.raises(TypeError, match="support only io.BytesIO and io.StringIO"):
            await session.write(Path("output.bin"), unsupported)

    assert session.calls == ()
    assert session.remaining_steps == 1


def test_scripted_sandbox_rejects_callable_unsupported_stream_result() -> None:
    with _CallableBufferedReader(io.BytesIO(b"payload")) as unsupported:
        with pytest.raises(InvalidSandboxStep) as exc_info:
            scripted_sandbox_session([{"method": "read", "result": unsupported}])

    assert exc_info.value.reason == "invalid_outcome"


@pytest.mark.parametrize(
    "stream_factory",
    [
        lambda: _CallableBytesIO(b"payload"),
        lambda: _CallableBufferedReader(io.BytesIO(b"payload")),
    ],
)
@pytest.mark.parametrize(
    ("field", "reason"),
    [("match", "invalid_matcher"), ("responder", "invalid_outcome")],
)
def test_scripted_sandbox_rejects_callable_stream_matchers_and_responders(
    stream_factory: Any,
    field: str,
    reason: str,
) -> None:
    stream = cast(io.IOBase, stream_factory())
    step: dict[str, Any] = {"method": "exec", field: stream}
    if field == "match":
        step["result"] = ExecResult(stdout=b"", stderr=b"", exit_code=0)

    try:
        with pytest.raises(InvalidSandboxStep) as exc_info:
            scripted_sandbox_session([step])
    finally:
        stream.close()

    assert exc_info.value.reason == reason
    assert exc_info.value.input_index == 0


@pytest.mark.asyncio
async def test_scripted_sandbox_rejects_callable_unsupported_stream_call_before_commit() -> None:
    session = scripted_sandbox_session([{"method": "write", "result": None}])

    with _CallableBufferedReader(io.BytesIO(b"payload")) as unsupported:
        with pytest.raises(TypeError, match="support only io.BytesIO and io.StringIO"):
            await session.write(Path("output.bin"), unsupported)

    assert session.calls == ()
    assert session.remaining_steps == 1


@pytest.mark.asyncio
async def test_scripted_sandbox_snapshots_static_results_when_queued() -> None:
    configured = ExecResult(stdout=b"before", stderr=b"", exit_code=0)
    session = scripted_sandbox_session([{"method": "exec", "result": configured}])
    configured.stdout = b"after"

    result = await session.exec("pwd")

    assert result.stdout == b"before"
    session.assert_complete()


@pytest.mark.asyncio
async def test_scripted_sandbox_records_detached_fifo_calls() -> None:
    source_operations: list[dict[str, object]] = [{"type": "delete_file", "path": "before.txt"}]
    session = scripted_sandbox_session(
        [
            {"method": "apply_patch", "result": "Done!"},
            {"method": "exec", "result": ExecResult(stdout=b"ok", stderr=b"", exit_code=0)},
        ]
    )

    assert await session.apply_patch(cast(Any, source_operations)) == "Done!"
    source_operations[0]["path"] = "after.txt"
    result = await session.exec("pwd", shell=False)

    assert result.stdout == b"ok"
    assert session.remaining_steps == 0
    session.assert_complete()
    assert session.calls[0].method == "apply_patch"
    assert session.calls[0].args[0] == [{"type": "delete_file", "path": "before.txt"}]
    assert session.calls[1] == SandboxCall(
        call_index=1,
        method="exec",
        args=("pwd",),
        kwargs=MappingProxyType({"timeout": None, "shell": False, "user": None}),
    )

    returned_operations = cast(list[dict[str, object]], session.calls[0].args[0])
    returned_operations[0]["path"] = "mutated.txt"
    assert session.calls[0].args[0] == [{"type": "delete_file", "path": "before.txt"}]


@pytest.mark.asyncio
async def test_scripted_sandbox_snapshot_failure_does_not_commit_call() -> None:
    class Uncopyable:
        def __deepcopy__(self, memo: dict[int, object]) -> object:
            _ = memo
            raise RuntimeError("cannot snapshot")

    session = scripted_sandbox_session(
        [{"method": "exec", "result": ExecResult(stdout=b"", stderr=b"", exit_code=0)}]
    )

    with pytest.raises(RuntimeError, match="cannot snapshot"):
        await session.exec(cast(Any, Uncopyable()))

    assert session.calls == ()
    assert session.remaining_steps == 1


@pytest.mark.asyncio
async def test_scripted_sandbox_supports_matchers_responders_and_errors() -> None:
    injected_error = RuntimeError("sandbox unavailable")
    session = scripted_sandbox_session(
        [
            {
                "method": "exec",
                "match": lambda call: call.args == ("pwd",),
                "responder": lambda call: ExecResult(
                    stdout=f"call {len(call.args)}".encode(), stderr=b"", exit_code=0
                ),
            },
            {"method": "exec", "error": injected_error},
        ]
    )

    assert (await session.exec("pwd")).stdout == b"call 1"
    with pytest.raises(RuntimeError) as exc_info:
        await session.exec("next")
    assert exc_info.value is injected_error
    session.assert_complete()


@pytest.mark.asyncio
async def test_scripted_sandbox_reports_structured_payload_free_failures() -> None:
    mismatch = scripted_sandbox_session(
        [
            {"method": "exec", "result": ExecResult(stdout=b"", stderr=b"", exit_code=0)},
            {"method": "read", "result": None},
        ]
    )

    with pytest.raises(UnexpectedSandboxCall) as mismatch_info:
        await mismatch.read(Path("/secret/payload.txt"))
    mismatch_error = mismatch_info.value
    assert mismatch_error.call_index == 0
    assert "call #1" in str(mismatch_error)
    assert mismatch_error.actual_method == "read"
    assert mismatch_error.expected_method == "exec"
    assert mismatch_error.remaining_steps == 2
    assert mismatch.remaining_steps == 2
    assert "/secret/payload.txt" not in str(mismatch_error)
    await mismatch.exec("retry")
    assert mismatch.remaining_steps == 1

    rejected = scripted_sandbox_session(
        [
            {
                "method": "exec",
                "match": lambda call: call.args == ("expected",),
                "result": ExecResult(stdout=b"", stderr=b"", exit_code=0),
            }
        ]
    )
    with pytest.raises(SandboxCallMatcherError) as rejected_info:
        await rejected.exec("secret command")
    assert rejected_info.value.call_index == 0
    assert "call #1" in str(rejected_info.value)
    assert rejected_info.value.method == "exec"
    assert "secret command" not in str(rejected_info.value)
    assert rejected.remaining_steps == 1
    await rejected.exec("expected")
    rejected.assert_complete()

    extra = scripted_sandbox_session(
        [{"method": "exec", "result": ExecResult(stdout=b"", stderr=b"", exit_code=0)}]
    )
    await extra.exec("first")
    assert hasattr(extra, "exec")
    with pytest.raises(UnexpectedSandboxCall) as extra_info:
        await extra.exec("second secret")
    assert extra_info.value.call_index == 1
    assert "call #2" in str(extra_info.value)
    assert extra_info.value.expected_method is None
    assert extra_info.value.remaining_steps == 0
    assert "second secret" not in str(extra_info.value)


@pytest.mark.asyncio
async def test_scripted_sandbox_retains_step_after_matcher_exception() -> None:
    matcher_error = RuntimeError("matcher failed")
    matcher_calls = 0

    def match(_call: SandboxCall) -> bool:
        nonlocal matcher_calls
        matcher_calls += 1
        if matcher_calls == 1:
            raise matcher_error
        return True

    session = scripted_sandbox_session(
        [
            {
                "method": "exec",
                "match": match,
                "result": ExecResult(stdout=b"ok", stderr=b"", exit_code=0),
            }
        ]
    )

    with pytest.raises(RuntimeError) as exc_info:
        await session.exec("first")

    assert exc_info.value is matcher_error
    assert session.remaining_steps == 1
    assert (await session.exec("retry")).stdout == b"ok"
    session.assert_complete()


def test_scripted_sandbox_reports_unconsumed_steps() -> None:
    session = scripted_sandbox_session(
        [
            {"method": "exec", "result": ExecResult(stdout=b"", stderr=b"", exit_code=0)},
            {"method": "read", "result": None},
        ]
    )

    with pytest.raises(UnconsumedSandboxSteps) as exc_info:
        session.assert_complete()
    assert exc_info.value.remaining_steps == 2
    assert exc_info.value.pending_methods == ("exec", "read")


@pytest.mark.parametrize(
    ("step", "reason"),
    [
        ("not a mapping", "invalid_input"),
        ({"method": "exec", "matc": lambda _call: True, "result": None}, "invalid_input"),
        ({"method": "unknown", "result": None}, "unknown_method"),
        ({"method": "exec", "match": "no", "result": None}, "invalid_matcher"),
        ({"method": "exec"}, "invalid_outcome"),
        ({"method": "exec", "result": None, "error": RuntimeError()}, "invalid_outcome"),
        ({"method": "exec", "responder": "no"}, "invalid_outcome"),
        ({"method": "exec", "error": "no"}, "invalid_outcome"),
    ],
)
def test_scripted_sandbox_validates_steps_before_use(step: object, reason: str) -> None:
    with pytest.raises(InvalidSandboxStep) as exc_info:
        scripted_sandbox_session(cast(Any, [step]))
    assert exc_info.value.reason == reason
    assert exc_info.value.input_index == 0
    assert "step #1" in str(exc_info.value)


@pytest.mark.asyncio
async def test_scripted_sandbox_drives_black_box_sandbox_agent_workflow() -> None:
    session = scripted_sandbox_session(
        [
            {
                "method": "exec",
                "match": lambda call: call.args == ("pwd",),
                "result": ExecResult(stdout=b"/workspace\n", stderr=b"", exit_code=0),
            }
        ]
    )
    model = ScriptedModel(
        [
            [function_call("exec_command", {"cmd": "pwd"}, call_id="call_1")],
            [assistant_message("The workspace is /workspace.")],
        ]
    )
    agent = SandboxAgent(
        name="Test agent",
        model=model,
        capabilities=[Shell()],
    )

    result = await Runner.run(
        agent,
        "Where am I?",
        run_config=RunConfig(sandbox={"session": session}),
    )

    assert result.final_output == "The workspace is /workspace."
    assert len(session.calls) == 1
    assert len(model.calls) == 2
    session.assert_complete()
    model.assert_complete()
