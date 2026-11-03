"""Exercise an installed openai-agents wheel through its native execution owner.

Copy this script outside the checkout and run it in a fresh virtual environment:
    /path/to/venv/bin/python -I verify_installed.py --output receipt.json

The synthetic HTTP peer replaces only the provider. Codex still owns the actual
requests, tool dispatch, continuation, events and conversation history.
"""

import argparse
import asyncio
import base64
import hashlib
import importlib
import importlib.util
import json
import os
import platform
import stat
import sys
import sysconfig
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
from pathlib import Path
from typing import Any

EXPECTED_PIN = "7c2ce90716335c889a5076ded9a630459f9c9899"


def expected_helpers() -> tuple[str, ...]:
    names = ["codex-sdk-runtime", "codex-code-mode-host"]
    if sys.platform == "linux":
        names.append("codex-linux-sandbox")
    if sys.platform != "win32":
        names.append("codex-execve-wrapper")
    suffix = ".exe" if sys.platform == "win32" else ""
    return tuple(name + suffix for name in names)


def installed_distribution() -> dict[str, Any]:
    distribution = metadata.distribution("openai-agents")
    direct = distribution.read_text("direct_url.json")
    assert not (direct and json.loads(direct).get("dir_info", {}).get("editable")), (
        "installed verification requires a wheel, not an editable checkout"
    )
    libraries = {Path(sysconfig.get_path(kind)).resolve() for kind in ("purelib", "platlib")}
    records = set()
    file_count = file_bytes = 0
    for entry in distribution.files or ():
        path = Path(distribution.locate_file(entry))
        records.add(path.resolve())
        if entry.suffix.lower() in {".pyc", ".pyo"} or "__pycache__" in entry.parts:
            continue
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            file_count += 1
            file_bytes += info.st_size
            if entry.hash is not None:
                digest = hashlib.new(entry.hash.mode)
                with path.open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                observed = base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")
                assert observed == entry.hash.value, f"installed RECORD mismatch: {entry}"
    modules = {}
    for name in ("agents", "agents.agent", "agents.session", "agents.mcp", "agents._native"):
        path = Path(importlib.import_module(name).__file__).resolve()
        assert any(path.is_relative_to(base) for base in libraries), (
            f"{name} imported outside the environment: {path}"
        )
        assert path in records, f"{name} is not owned by the openai-agents RECORD: {path}"
        modules[name] = str(path)
    package = Path(modules["agents"]).parent
    for name in expected_helpers():
        helper = package / "bin" / name
        assert helper.resolve() in records, f"helper missing from RECORD: {name}"
        assert helper.is_file() and os.access(helper, os.X_OK), f"helper is not executable: {name}"
    for name in ("SDK_LICENSE", "CODEX_LICENSE", "CODEX_NOTICE"):
        path = package / "licenses" / name
        assert path.resolve() in records and path.stat().st_size, f"missing license: {name}"
    assert importlib.util.find_spec("codex_agents") is None, (
        "retired codex_agents namespace is installed"
    )
    return {
        "version": distribution.version,
        "modules": modules,
        "record_footprint": {"regular_file_count": file_count, "regular_file_bytes": file_bytes},
    }


async def exercise(checks: list[dict[str, Any]]) -> None:
    from pydantic import BaseModel

    from agents import Agent, NativeProvider, Session, ToolContext, function_tool
    from agents.observation import TurnFinished

    assert Agent.__module__ == "agents.agent", f"unexpected Agent owner: {Agent.__module__}"
    assert Session.__module__ == "agents.session", f"unexpected Session owner: {Session.__module__}"
    requests = []
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                assert self.path == "/v1/responses", f"unexpected provider path: {self.path}"
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                count = len(requests)
                assert count <= 2, f"unexpected provider request {count}"
                if count == 1:
                    item = {
                        "type": "function_call",
                        "name": "lookup",
                        "call_id": "installed-call",
                        "arguments": json.dumps({"key": "record"}),
                    }
                else:
                    outputs = [
                        item for item in body["input"] if item.get("type") == "function_call_output"
                    ]
                    assert len(outputs) == 1 and outputs[0]["call_id"] == "installed-call", outputs
                    assert "installed:record" in json.dumps(outputs[0]["output"]), outputs
                    item = {
                        "type": "message",
                        "role": "assistant",
                        "id": "installed-message",
                        "content": [
                            {"type": "output_text", "text": '{"answer":"native-complete"}'}
                        ],
                    }
                response = {"id": f"installed-response-{count}"}
                events = [
                    {"type": "response.created", "response": response},
                    {"type": "response.output_item.done", "item": item},
                    {"type": "response.completed", "response": response},
                ]
                payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")
                self.send_error(500, "synthetic provider assertion failed")

    @dataclass
    class Application:
        tenant: str

    class Answer(BaseModel):
        answer: str

    calls = []

    @function_tool
    async def lookup(context: ToolContext[Application], key: str) -> str:
        """Look up one application record."""
        calls.append(context)
        return f"{context.context.tenant}:{key}"

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        application = Application("installed")
        agent = Agent(
            instructions="Use the lookup tool, then return the typed answer.",
            tools=[lookup],
            output_type=Answer,
            provider=NativeProvider(
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                api_key="synthetic-installed-key",
            ),
        )
        async with agent.session(context=application) as session:
            handle = await session.submit("Look up record.")
            result, same = await asyncio.gather(handle.get_result(), handle.get_result())
            assert result is same and result.turn is handle, "shared completion lost turn identity"
            assert result.final_output == Answer(answer="native-complete"), result.final_output
            assert len(calls) == 1 and calls[0].context is application, (
                "tool lost application context"
            )
            assert calls[0].submission is handle and calls[0].call_id == "installed-call", (
                "tool lost native call/submission identity"
            )
            events = []
            stream = session.events()
            async for event in stream:
                events.append(event)
                if isinstance(event, TurnFinished) and event.submission_id == handle.submission_id:
                    break
            await stream.aclose()
            assert any(isinstance(event, TurnFinished) for event in events), (
                "session observation did not retain submission completion"
            )
            turns = [turn async for turn in session.turns()]
            assert turns and turns[0]["id"] == handle.turn_id, (
                "native canonical history lost the submitted turn"
            )
        assert len(requests) == 2 and not errors, {"requests": len(requests), "errors": errors}
        assert not any(
            name == "codex_agents" or name.startswith("codex_agents.") for name in sys.modules
        )
        assert "agents._native_async" in sys.modules, (
            "native completion did not use the SDK-owned helper"
        )
        checks.append(
            {
                "check": "native_provider_tool_result_history_observation",
                "passed": True,
                "provider_requests": len(requests),
                "tool_effects": len(calls),
                "exact_submission_and_call_identity": True,
            }
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "synthetic provider thread did not stop"


async def run() -> dict[str, Any]:
    report: dict[str, Any] = {
        "mode": "single-native-sdk-wheel",
        "python": sys.version,
        "platform": platform.platform(),
        "source_pin_expected": EXPECTED_PIN,
        "checks": [],
        "passed": False,
        "limits": [
            "Synthetic localhost provider; no live provider or customer acceptance claim.",
            "This checks the installed native SDK path, not every host sandbox policy or platform.",
            "A local pass does not establish publication or release qualification.",
        ],
    }
    try:
        assert sys.prefix != sys.base_prefix, "probe requires a fresh virtual environment"
        assert sys.flags.isolated and not sys.flags.optimize, "run with -I and without -O"
        report["packages"] = {"openai-agents": installed_distribution()}
        from agents import _native

        report["source_pin_observed"] = _native.CODEX_REVISION
        assert _native.CODEX_REVISION == EXPECTED_PIN, "installed native source pin mismatch"
        with tempfile.TemporaryDirectory(prefix="agents-installed-") as directory:
            previous = Path.cwd()
            os.chdir(directory)
            try:
                await asyncio.wait_for(exercise(report["checks"]), timeout=60)
            finally:
                os.chdir(previous)
        report["passed"] = True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="also write the JSON result here")
    args = parser.parse_args()
    os.environ.pop("OPENAI_API_KEY", None)
    report = asyncio.run(run())
    rendered = json.dumps(report, indent=2) + "\n"
    print(rendered, end="")
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
