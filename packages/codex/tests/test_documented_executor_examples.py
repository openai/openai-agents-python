"""Run documented executor flows against real native commands and local model fixtures."""

from __future__ import annotations

import ast
import shlex
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from agents import Agent, Environment
from examples.codex import offline_unix_sockets, remote_executor
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider

pytestmark = pytest.mark.requires_native_linux_sandbox

# These fixtures test documented SDK flows, not job-private offline network provisioning.
_COMMAND = "printf 'workspace inspected\\n' > inspected.txt; cat inspected.txt"


def _configure_executor(
    peer: ExecutorPeer, responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXECUTOR_URL", peer.url)
    monkeypatch.setenv("EXECUTOR_CWD", str(peer.root))
    monkeypatch.setenv("EXECUTOR_TOKEN", peer.token)
    monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
    monkeypatch.setenv("OPENAI_BASE_URL", responses.url)
    monkeypatch.setenv("RESPONSES_FIXTURE_URL", responses.url)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-documentation-key")
    monkeypatch.delenv("EXECUTOR_CAPABILITIES", raising=False)


def _documented_code(heading: str) -> str:
    guide = Path(__file__).resolve().parents[3] / "docs/sdk/execution.md"
    section = guide.read_text().split(heading + "\n", 1)[1]
    return section.split("```python\n", 1)[1].split("```", 1)[0]


async def test_documented_local_environment_lists_its_workspace(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "assigned-file.txt").write_text("assigned workspace\n")
    code = _documented_code("## Local execution")
    assert code.count('Path("/jobs/worker/work")') == 1
    code = code.replace('Path("/jobs/worker/work")', f"Path({str(workspace)!r})")
    declaration = 'Agent(instructions="Work in the assigned workspace.")'
    assert code.count(declaration) == 1
    code = code.replace(
        declaration,
        'Agent(instructions="Work in the assigned workspace.", provider=fixture_provider)',
    )
    responses.respond(function_call("exec_command", {"cmd": "pwd; ls"}, "list-workspace"))
    responses.respond(assistant_output("workspace listed"))
    compiled = compile(code, "docs/sdk/execution.md", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    async with timeout(30):
        await eval(compiled, {"fixture_provider": provider(responses)})
    output = tool_output(responses.requests[1], "list-workspace")
    assert "Process exited with code 0" in output, output
    assert str(workspace) in output, output
    assert "assigned-file.txt" in output, output
    assert not responses.replies


async def test_documented_write_root_replacement_writes_report_after_settled_turn(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    workspace, output_directory = tmp_path / "workspace", tmp_path / "output"
    workspace.mkdir()
    output_directory.mkdir()
    report = output_directory / "report.txt"
    code = _documented_code("### Replace write roots between turns")
    assert code.count('Path("/jobs/worker/output")') == 1
    code = code.replace('Path("/jobs/worker/output")', f"Path({str(output_directory)!r})")
    responses.respond(function_call("exec_command", {"cmd": "pwd"}, "inspect-workspace"))
    responses.respond(assistant_output("inspection complete"))
    responses.respond(
        function_call(
            "exec_command",
            {"cmd": f"printf 'report complete\\n' > {shlex.quote(str(report))}"},
            "write-report",
        )
    )
    responses.respond(assistant_output("report written"))
    environment = Environment.local(cwd=workspace, tools=("exec_command",))
    async with (
        timeout(30),
        Agent(instructions="Work in the assigned workspace.", provider=provider(responses)).session(
            environment=environment
        ) as session,
    ):
        await (await session.submit("Inspect the workspace.")).get_result()
        assert str(workspace) in tool_output(responses.requests[1], "inspect-workspace")
        compiled = compile(
            code, "docs/sdk/execution.md", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        )
        await eval(compiled, {"session": session, "Path": Path})
    output = tool_output(responses.requests[3], "write-report")
    assert "Process exited with code 0" in output, output
    assert report.read_text() == "report complete\n"
    assert not responses.replies


@pytest.mark.parametrize("offline", [False, True], ids=["remote-execution", "offline-unix-policy"])
async def test_documented_environment_executes_in_assigned_workspace(
    offline: bool, responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as peer:
        _configure_executor(peer, responses, tmp_path, monkeypatch)
        code = _documented_code(
            "### Unix sockets in an offline job" if offline else "## Remote execution"
        )
        assert code.count('cwd="/workspace"') == 1
        code = code.replace('cwd="/workspace"', f"cwd={str(peer.root)!r}")
        if not offline:
            declaration = 'Agent(instructions="Inspect the assigned workspace.")'
            assert code.count(declaration) == 1
            code = code.replace(
                declaration,
                'Agent(instructions="Inspect the assigned workspace.", provider=fixture_provider)',
            )
        namespace: dict[str, Any] = {"fixture_provider": provider(responses)}
        responses.respond(
            function_call("exec_command", {"cmd": _COMMAND, "max_output_tokens": 200}, "inspect")
        )
        responses.respond(assistant_output("inspection complete"))
        compiled = compile(
            code, "docs/sdk/execution.md", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        )
        if offline:
            exec(compiled, namespace)
            async with Agent(instructions="Inspect.", provider=provider(responses)).session(
                environment=namespace["environment"]
            ) as session:
                await (await session.submit("Inspect the workspace.")).get_result()
        else:
            await eval(compiled, namespace)
        output = tool_output(responses.requests[1], "inspect")
        assert "Process exited with code 0" in output, output
        assert "workspace inspected" in output, output
        assert (peer.root / "inspected.txt").read_text() == "workspace inspected\n"
        assert any(request.get("method") == "process/start" for request in peer.requests)
        assert not responses.replies


async def test_remote_executor_main_executes_and_reads_native_checklist(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as peer:
        _configure_executor(peer, responses, tmp_path, monkeypatch)
        responses.respond(
            function_call("exec_command", {"cmd": _COMMAND, "max_output_tokens": 200}, "inspect")
        )
        responses.respond(
            function_call(
                "update_plan",
                {"plan": [{"step": "Inspect workspace", "status": "completed"}]},
                "checklist",
            )
        )
        responses.respond(assistant_output("inspection complete"))
        await remote_executor.main("Inspect the assigned workspace and update the checklist.")
        output = tool_output(responses.requests[1], "inspect")
        assert "Process exited with code 0" in output, output
        assert "workspace inspected" in output, output
        assert (peer.root / "inspected.txt").read_text() == "workspace inspected\n"
        assert "Plan updated" in tool_output(responses.requests[2], "checklist")
        assert capsys.readouterr().out == "inspection complete\ncompleted: Inspect workspace\n"
        assert not responses.replies


async def test_offline_unix_sockets_main_exchanges_reply_between_processes(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = textwrap.dedent(
        """\
        import socket
        import subprocess
        import sys
        from pathlib import Path

        client = '''import socket
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect("reply.sock")
            connection.sendall(b"ping")
            assert connection.recv(4) == b"pong"
            print("pong")
        '''
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind("reply.sock")
            listener.listen(1)
            with subprocess.Popen(
                [sys.executable, "-c", client], stdout=subprocess.PIPE, text=True
            ) as child:
                connection, _ = listener.accept()
                with connection:
                    assert connection.recv(4) == b"ping"
                    connection.sendall(b"pong")
                reply, _ = child.communicate(timeout=10)
                assert child.returncode == 0
        assert reply == "pong\\n"
        Path("socket-reply.txt").write_text(reply)
        print(reply, end="")
        """
    )
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as peer:
        _configure_executor(peer, responses, tmp_path, monkeypatch)
        command = shlex.join([sys.executable, "-c", script])
        responses.respond(
            function_call("exec_command", {"cmd": command, "max_output_tokens": 200}, "exchange")
        )
        responses.respond(assistant_output("pong"))
        await offline_unix_sockets.main()
        output = tool_output(responses.requests[1], "exchange")
        assert "Process exited with code 0" in output, output
        assert "pong" in output, output
        assert (peer.root / "socket-reply.txt").read_text() == "pong\n"
        assert capsys.readouterr().out == "pong\n"
        assert any(request.get("method") == "process/start" for request in peer.requests)
        assert not responses.replies
