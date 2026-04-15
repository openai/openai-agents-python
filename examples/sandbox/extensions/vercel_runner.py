"""
Vercel-backed sandbox example for manual validation.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal, cast

from openai.types.responses import ResponseCompletedEvent, ResponseTextDeltaEvent

from agents import ModelSettings, MultiProvider, Runner
from agents.run import RunConfig
from agents.sandbox import LocalSnapshotSpec, Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File
from agents.sandbox.session import BaseSandboxSession

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest, tool_call_name
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox import VercelSandboxClient, VercelSandboxClientOptions
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Vercel sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra vercel"
    ) from exc


DEFAULT_MODEL = "gpt-5.4"
DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."
DEFAULT_PTY_QUESTION = (
    "Start an interactive Python session with `tty=true`. In that same session, compute "
    "`5 + 5`, then add 5 more to the previous result. Briefly report the outputs and "
    "confirm that you stayed in one Python process."
)
SNAPSHOT_CHECK_PATH = Path("snapshot-check.txt")
SNAPSHOT_CHECK_CONTENT = "vercel snapshot round-trip ok\n"
LIVE_RESUME_CHECK_PATH = Path("live-resume-check.txt")
LIVE_RESUME_CHECK_CONTENT = "vercel live resume ok\n"
PTY_CHECK_VALUE = "vercel pty round-trip ok"
EXPOSED_PORT = 3000
PORT_CHECK_CONTENT = "<h1>vercel exposed port ok</h1>\n"
PORT_CHECK_NODE_SERVER_PATH = Path(".port-check-server.js")
PORT_CHECK_NODE_SERVER_CONTENT = f"""\
const http = require("node:http");

http
  .createServer((_request, response) => {{
    response.writeHead(200, {{"Content-Type": "text/html; charset=utf-8"}});
    response.end({json.dumps(PORT_CHECK_CONTENT)});
  }})
  .listen({EXPOSED_PORT}, "0.0.0.0");
"""
PORT_CHECK_PYTHON_SERVER_PATH = Path(".port-check-server.py")
PORT_CHECK_PYTHON_SERVER_CONTENT = f"""\
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = {PORT_CHECK_CONTENT!r}.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


ThreadingHTTPServer(("0.0.0.0", {EXPOSED_PORT}), Handler).serve_forever()
"""


def _build_manifest() -> Manifest:
    return text_manifest(
        {
            "README.md": (
                "# Vercel Demo Workspace\n\n"
                "This workspace exists to validate the Vercel sandbox backend manually.\n"
            ),
            "handoff.md": (
                "# Handoff\n\n"
                "- Customer: Northwind Traders.\n"
                "- Goal: validate Vercel sandbox exec, PTY, and persistence flows.\n"
                "- Current status: backend slice is wired and under test.\n"
            ),
            "todo.md": (
                "# Todo\n\n"
                "1. Inspect the workspace files.\n"
                "2. Summarize the current status in two sentences.\n"
            ),
        }
    )


def _build_pty_manifest() -> Manifest:
    return Manifest(
        entries={
            "README.md": File(
                content=(
                    b"# Vercel PTY Agent Example\n\n"
                    b"This workspace is used by the Vercel PTY demo.\n"
                )
            ),
        }
    )


def _stream_event_banner(event_name: str, raw_item: object) -> str | None:
    _ = raw_item
    if event_name == "tool_called":
        return "[tool call]"
    if event_name == "tool_output":
        return "[tool output]"
    return None


def _raw_item_call_id(raw_item: object) -> str | None:
    if isinstance(raw_item, dict):
        call_id = raw_item.get("call_id") or raw_item.get("id")
    else:
        call_id = getattr(raw_item, "call_id", None) or getattr(raw_item, "id", None)
    return call_id if isinstance(call_id, str) and call_id else None


async def _read_text(session: BaseSandboxSession, path: Path) -> str:
    data = await session.read(path)
    text = cast(str | bytes, data.read())
    if isinstance(text, bytes):
        return text.decode("utf-8")
    return text


def _require_env(name: str) -> None:
    if os.environ.get(name):
        return
    raise SystemExit(f"{name} must be set before running this example.")


def _require_vercel_credentials() -> None:
    if os.environ.get("VERCEL_OIDC_TOKEN"):
        return
    if (
        os.environ.get("VERCEL_TOKEN")
        and os.environ.get("VERCEL_PROJECT_ID")
        and os.environ.get("VERCEL_TEAM_ID")
    ):
        return
    raise SystemExit(
        "Vercel credentials are required. Set VERCEL_OIDC_TOKEN, or set "
        "VERCEL_TOKEN together with VERCEL_PROJECT_ID and VERCEL_TEAM_ID."
    )


def _build_options(
    *,
    runtime: str | None,
    timeout_ms: int | None,
    workspace_persistence: Literal["tar", "snapshot"],
    interactive: bool = False,
    exposed_ports: tuple[int, ...] = (),
) -> VercelSandboxClientOptions:
    return VercelSandboxClientOptions(
        runtime=runtime,
        timeout_ms=timeout_ms,
        workspace_persistence=workspace_persistence,
        interactive=interactive,
        exposed_ports=exposed_ports,
    )


def _build_run_config(*, sandbox: BaseSandboxSession, workflow_name: str) -> RunConfig:
    return RunConfig(
        sandbox=SandboxRunConfig(session=sandbox),
        workflow_name=workflow_name,
        model_provider=MultiProvider(openai_prefix_mode="model_id"),
    )


async def _verify_stop_resume(
    *,
    manifest: Manifest,
    runtime: str | None,
    timeout_ms: int | None,
    workspace_persistence: Literal["tar", "snapshot"],
) -> None:
    client = VercelSandboxClient()
    options = _build_options(
        runtime=runtime,
        timeout_ms=timeout_ms,
        workspace_persistence=workspace_persistence,
    )
    with tempfile.TemporaryDirectory(prefix="vercel-snapshot-example-") as snapshot_dir:
        sandbox = await client.create(
            manifest=manifest,
            snapshot=LocalSnapshotSpec(base_path=Path(snapshot_dir)),
            options=options,
        )

        try:
            await sandbox.start()
            await sandbox.write(
                SNAPSHOT_CHECK_PATH,
                io.BytesIO(SNAPSHOT_CHECK_CONTENT.encode("utf-8")),
            )
            await sandbox.stop()
        finally:
            await sandbox.shutdown()

        resumed_sandbox = await client.resume(sandbox.state)
        try:
            await resumed_sandbox.start()
            restored_text = await _read_text(resumed_sandbox, SNAPSHOT_CHECK_PATH)
            if restored_text != SNAPSHOT_CHECK_CONTENT:
                raise RuntimeError(
                    f"Snapshot resume verification failed for {workspace_persistence!r}: "
                    f"expected {SNAPSHOT_CHECK_CONTENT!r}, got {restored_text!r}"
                )
        finally:
            await resumed_sandbox.aclose()

    print(f"snapshot round-trip ok ({workspace_persistence})")


async def _verify_resume_running_sandbox(
    *,
    manifest: Manifest,
    runtime: str | None,
    timeout_ms: int | None,
    workspace_persistence: Literal["tar", "snapshot"],
) -> None:
    client = VercelSandboxClient()
    sandbox = await client.create(
        manifest=manifest,
        options=_build_options(
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
        ),
    )

    try:
        await sandbox.start()
        await sandbox.write(
            LIVE_RESUME_CHECK_PATH,
            io.BytesIO(LIVE_RESUME_CHECK_CONTENT.encode("utf-8")),
        )
        serialized = client.serialize_session_state(sandbox.state)
        resumed_sandbox = await client.resume(client.deserialize_session_state(serialized))
        try:
            restored_text = await _read_text(resumed_sandbox, LIVE_RESUME_CHECK_PATH)
            if restored_text != LIVE_RESUME_CHECK_CONTENT:
                raise RuntimeError(
                    "Running sandbox resume verification failed: "
                    f"expected {LIVE_RESUME_CHECK_CONTENT!r}, got {restored_text!r}"
                )
        finally:
            await resumed_sandbox.aclose()
    finally:
        await sandbox.shutdown()

    print(f"running sandbox resume ok ({workspace_persistence})")


def _fetch_url(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return cast(str, response.read().decode("utf-8"))


def _port_check_server_command() -> str:
    node_path = PORT_CHECK_NODE_SERVER_PATH.as_posix()
    python_path = PORT_CHECK_PYTHON_SERVER_PATH.as_posix()
    return (
        "if command -v node >/dev/null 2>&1; then "
        f"node {node_path}; "
        "elif command -v python3 >/dev/null 2>&1; then "
        f"python3 {python_path}; "
        "else "
        "echo 'Neither node nor python3 is available for exposed port verification.' >&2; "
        "exit 127; "
        "fi >/tmp/vercel-http.log 2>&1 &"
    )


async def _verify_exposed_port(
    *,
    manifest: Manifest,
    runtime: str | None,
    timeout_ms: int | None,
    workspace_persistence: Literal["tar", "snapshot"],
) -> None:
    client = VercelSandboxClient()
    sandbox = await client.create(
        manifest=manifest,
        options=_build_options(
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
            exposed_ports=(EXPOSED_PORT,),
        ),
    )

    try:
        await sandbox.start()
        await sandbox.write(
            PORT_CHECK_NODE_SERVER_PATH,
            io.BytesIO(PORT_CHECK_NODE_SERVER_CONTENT.encode("utf-8")),
        )
        await sandbox.write(
            PORT_CHECK_PYTHON_SERVER_PATH,
            io.BytesIO(PORT_CHECK_PYTHON_SERVER_CONTENT.encode("utf-8")),
        )
        result = await sandbox.exec(_port_check_server_command(), shell=True)
        if not result.ok():
            raise RuntimeError(
                f"Failed to start HTTP server for exposed port check: {result.stderr!r}"
            )

        endpoint = await sandbox.resolve_exposed_port(EXPOSED_PORT)
        url = f"{'https' if endpoint.tls else 'http'}://{endpoint.host}:{endpoint.port}/"

        last_error: Exception | None = None
        for _ in range(20):
            try:
                body = await asyncio.to_thread(_fetch_url, url)
            except (TimeoutError, urllib.error.URLError, ValueError) as exc:
                last_error = exc
                await asyncio.sleep(0.5)
                continue

            if PORT_CHECK_CONTENT.strip() not in body:
                raise RuntimeError(f"Exposed port returned unexpected body from {url!r}: {body!r}")
            print(f"exposed port ok ({workspace_persistence}) -> {url}")
            return

        raise RuntimeError(f"Exposed port verification failed for {url!r}") from last_error
    finally:
        await sandbox.shutdown()


async def _verify_pty_direct(
    *,
    manifest: Manifest,
    runtime: str | None,
    timeout_ms: int | None,
    workspace_persistence: Literal["tar", "snapshot"],
) -> None:
    client = VercelSandboxClient()
    sandbox = await client.create(
        manifest=manifest,
        options=_build_options(
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
            interactive=True,
        ),
    )

    try:
        await sandbox.start()
        if not sandbox.supports_pty():
            raise RuntimeError("Interactive Vercel sandbox did not report PTY support.")

        started = await sandbox.pty_exec_start("sh", shell=False, yield_time_s=0.25)
        process_id = started.process_id
        if process_id is None:
            raise RuntimeError(
                f"PTY session exited too early during startup: output={started.output!r}, "
                f"exit_code={started.exit_code!r}"
            )

        await sandbox.pty_write_stdin(
            session_id=process_id,
            chars=f"export PTY_CHECK_VALUE={PTY_CHECK_VALUE!r}\n",
            yield_time_s=0.25,
        )
        completed = await sandbox.pty_write_stdin(
            session_id=process_id,
            chars='printf "%s\\n" "$PTY_CHECK_VALUE"\nexit\n',
            yield_time_s=0.5,
        )

        if completed.exit_code != 0:
            raise RuntimeError(
                f"PTY verification exited with {completed.exit_code}: {completed.output!r}"
            )
        if PTY_CHECK_VALUE not in completed.output.decode("utf-8", errors="replace"):
            raise RuntimeError(
                f"PTY verification did not observe persisted shell state: {completed.output!r}"
            )
    finally:
        try:
            await sandbox.pty_terminate_all()
        finally:
            await sandbox.shutdown()

    print(f"pty round-trip ok ({workspace_persistence})")


async def _run_pty_demo(
    *,
    model: str,
    question: str,
    runtime: str | None,
    timeout_ms: int | None,
    workspace_persistence: Literal["tar", "snapshot"],
) -> None:
    agent = SandboxAgent(
        name="Vercel PTY Demo",
        model=model,
        instructions=(
            "Complete the task by interacting with the sandbox through the shell capability. "
            "Keep the final answer concise. "
            "Preserve process state when the task depends on it. If you start an interactive "
            "program, continue using that same process instead of launching a second one."
        ),
        default_manifest=_build_pty_manifest(),
        capabilities=[Shell()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    client = VercelSandboxClient()
    sandbox = await client.create(
        manifest=agent.default_manifest,
        options=_build_options(
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
            interactive=True,
        ),
    )

    try:
        async with sandbox:
            result = Runner.run_streamed(
                agent,
                question,
                run_config=_build_run_config(
                    sandbox=sandbox,
                    workflow_name="Vercel PTY sandbox example",
                ),
            )

            saw_text_delta = False
            saw_any_text = False
            tool_names_by_call_id: dict[str, str] = {}

            async for event in result.stream_events():
                if event.type == "raw_response_event" and isinstance(
                    event.data, ResponseTextDeltaEvent
                ):
                    if not saw_text_delta:
                        print("assistant> ", end="", flush=True)
                        saw_text_delta = True
                    print(event.data.delta, end="", flush=True)
                    saw_any_text = True
                    continue
                if event.type == "raw_response_event" and isinstance(
                    event.data, ResponseCompletedEvent
                ):
                    continue

                if event.type != "run_item_stream_event":
                    continue

                raw_item = event.item.raw_item
                banner = _stream_event_banner(event.name, raw_item)
                if banner is None:
                    continue

                if saw_text_delta:
                    print()
                    saw_text_delta = False

                if event.name == "tool_called":
                    tool_name = tool_call_name(raw_item)
                    call_id = _raw_item_call_id(raw_item)
                    if call_id is not None and tool_name:
                        tool_names_by_call_id[call_id] = tool_name
                    if tool_name:
                        banner = f"{banner} {tool_name}"
                elif event.name == "tool_output":
                    call_id = _raw_item_call_id(raw_item)
                    output_tool_name = tool_names_by_call_id.get(call_id or "")
                    if output_tool_name:
                        banner = f"{banner} {output_tool_name}"

                print(banner)

            if saw_text_delta:
                print()
            if not saw_any_text:
                print(result.final_output)
    finally:
        await client.delete(sandbox)


async def _run_standard_agent(
    *,
    model: str,
    question: str,
    runtime: str | None,
    timeout_ms: int | None,
    workspace_persistence: Literal["tar", "snapshot"],
    stream: bool,
) -> None:
    manifest = _build_manifest()
    agent = SandboxAgent(
        name="Vercel Sandbox Assistant",
        model=model,
        instructions=(
            "Answer questions about the sandbox workspace. Inspect the files before answering "
            "and keep the response concise. "
            "Do not invent files or statuses that are not present in the workspace. Cite the "
            "file names you inspected."
        ),
        default_manifest=manifest,
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    client = VercelSandboxClient()
    sandbox = await client.create(
        manifest=manifest,
        options=_build_options(
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
        ),
    )

    try:
        async with sandbox:
            run_config = _build_run_config(
                sandbox=sandbox,
                workflow_name="Vercel sandbox example",
            )
            if not stream:
                result = await Runner.run(agent, question, run_config=run_config)
                print(result.final_output)
                return

            stream_result = Runner.run_streamed(agent, question, run_config=run_config)
            saw_text_delta = False
            async for event in stream_result.stream_events():
                if event.type == "raw_response_event" and isinstance(
                    event.data, ResponseTextDeltaEvent
                ):
                    if not saw_text_delta:
                        print("assistant> ", end="", flush=True)
                        saw_text_delta = True
                    print(event.data.delta, end="", flush=True)
                elif event.type == "raw_response_event" and isinstance(
                    event.data, ResponseCompletedEvent
                ):
                    continue

            if saw_text_delta:
                print()
    finally:
        await client.delete(sandbox)


async def main(
    *,
    model: str,
    question: str,
    runtime: str | None,
    timeout_ms: int | None,
    workspace_persistence: Literal["tar", "snapshot"],
    stream: bool,
    demo: str,
) -> None:
    _require_env("OPENAI_API_KEY")
    _require_vercel_credentials()

    manifest = _build_manifest()

    if demo == "snapshot":
        await _verify_stop_resume(
            manifest=manifest,
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
        )
        return

    if demo == "resume":
        await _verify_resume_running_sandbox(
            manifest=manifest,
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
        )
        return

    if demo == "port":
        await _verify_exposed_port(
            manifest=manifest,
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
        )
        return

    if demo == "pty":
        await _run_pty_demo(
            model=model,
            question=question,
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
        )
        return

    if demo == "backend-checks":
        await _verify_stop_resume(
            manifest=manifest,
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
        )
        await _verify_resume_running_sandbox(
            manifest=manifest,
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
        )
        await _verify_exposed_port(
            manifest=manifest,
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
        )
        await _verify_pty_direct(
            manifest=manifest,
            runtime=runtime,
            timeout_ms=timeout_ms,
            workspace_persistence=workspace_persistence,
        )
        return

    await _run_standard_agent(
        model=model,
        question=question,
        runtime=runtime,
        timeout_ms=timeout_ms,
        workspace_persistence=workspace_persistence,
        stream=stream,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Run a Vercel sandbox agent with optional resume, exposed-port, and PTY demos."
        )
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--demo",
        default="agent",
        choices=["agent", "snapshot", "resume", "port", "pty", "backend-checks"],
        help="Which demo to run (default: agent).",
    )
    parser.add_argument(
        "--runtime",
        default=None,
        help="Optional Vercel runtime, for example `node22` or `python3.14`.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=120_000,
        help="Optional Vercel sandbox timeout in milliseconds.",
    )
    parser.add_argument(
        "--workspace-persistence",
        choices=("tar", "snapshot"),
        default="tar",
        help="Workspace persistence mode for the Vercel sandbox.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    args = parser.parse_args()

    default_question = (
        DEFAULT_PTY_QUESTION if args.demo == "pty" and args.question == DEFAULT_QUESTION else None
    )
    asyncio.run(
        main(
            model=args.model,
            question=default_question or args.question,
            runtime=args.runtime,
            timeout_ms=args.timeout_ms,
            workspace_persistence=cast(Literal["tar", "snapshot"], args.workspace_persistence),
            stream=args.stream,
            demo=args.demo,
        )
    )
