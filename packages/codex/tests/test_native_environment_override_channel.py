"""Explicit SDK values use the receiver's post-filter override channel."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agents import Agent
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_executor_tools import declaration


@pytest.mark.parametrize("include_only", [False, True], ids=["default", "filtered"])
async def test_explicit_environment_values_use_post_filter_native_channel(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_only: bool,
) -> None:
    if include_only:
        from agents import _native

        create_engine = _native.create_engine

        def configured_engine(config_json: str, *args: Any, **kwargs: Any) -> Any:
            config = json.loads(config_json)
            # Select an existing Core policy at the native test boundary.
            # Explicit SDK values must not widen its configured allowlist.
            config["shell_environment_policy"] = {"include_only": ["KEEP"]}
            return create_engine(json.dumps(config), *args, **kwargs)

        monkeypatch.setattr(_native, "create_engine", configured_engine)

    selected = {
        "SDK_NORMAL": "private-explicit-value",
        "LIT*": "star",
        "LIT?": "question",
        "TERM": "xterm",
        "LANG": "fr_FR",
        "PATH": "/upper",
    }
    if sys.platform != "win32":
        selected["Path"] = "/mixed"
    async with (
        timeout(40),
        ExecutorPeer(tmp_path / "remote", synthetic_process_output="ready\n").serve() as peer,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        responses.respond(
            function_call("exec_command", {"cmd": "printf ready", "login": False}, "post-filter")
        )
        responses.respond(assistant_output("completed"))
        async with Agent(instructions="Run the command.", provider=provider(responses)).session(
            environment=declaration(peer, env=selected)
        ) as session:
            assert (await (await session.submit("Run")).get_result()).final_output == "completed"
            assert "private-explicit-value" not in (await session.save_state()).to_json()
        [request] = [item for item in peer.requests if item.get("method") == "process/start"]
        params = request["params"]
        if include_only:
            assert params["envPolicy"]["includeOnly"] == ["KEEP"]
        for name, value in selected.items():
            assert params["env"].get(name) == value
            assert name not in params["envPolicy"]["set"]
