"""Execute maintained documentation bodies with real native owners and synthetic peers."""

import ast
import json
import re
from pathlib import Path

import pytest

from agents import NativeProvider
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_codex_login import ACCOUNT, saved_login
from tests.test_native_environment_protocol import delayed_native_join as delayed_native_join

ROOT = Path(__file__).resolve().parents[3]
_SNIPPETS = {
    "saved_login": ("configuration.md", "NativeProvider(auth=CodexLogin())"),
    "opening_cleanup": ("configuration.md", 'recovery = getattr(error, "provider", None)'),
    "cleanup_owner": ("sessions.md", "cleanup = get_cleanup(error)"),
    "timeout": ("troubleshooting.md", "timeout=30"),
    "stdio": ("mcp.md", 'command="/opt/docs-server/bin/server"'),
    "writer": ("mcp.md", "writer = HttpMCP("),
    "apps": ("mcp.md", "registration = ChatGPTAppsMCP("),
    "native_children": ("delegation.md", "Delegate independent workspace tasks"),
    "child_results": ("delegation.md", "async for child in session.subagents()"),
    "child_approval": ("delegation.md", "def approve_call("),
    "capability": ("execution.md", 'capability_directories=["/workspace/capabilities"]'),
    "lease": ("execution.md", "async def executor_lease()"),
}


def _code(name: str) -> str:
    page, marker = _SNIPPETS[name]
    source = (ROOT / "docs/sdk" / page).read_text()
    matches = [
        body for body in re.findall(r"^```python\n(.*?)^```", source, re.M | re.S) if marker in body
    ]
    assert len(matches) == 1, (page, marker)
    return matches[0]


@pytest.fixture
def codex_endpoint(responses, monkeypatch):
    from _agents_codex import session
    from agents import _native

    monkeypatch.setattr(session, "_CODEX_RESPONSES_BASE_URL", responses.url)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    # The shared synthetic peer does not decode optional Zstandard compression.
    def without_compression(original):
        def create(config, *args, **kwargs):
            options = json.loads(config)
            options["features.enable_request_compression"] = False
            return original(json.dumps(options), *args, **kwargs)

        return create

    for name in ("create_engine", "create_checkpoint_engine"):
        monkeypatch.setattr(_native, name, without_compression(getattr(_native, name)))
    return responses


async def test_exact_saved_oauth_doc_body(codex_endpoint, tmp_path, monkeypatch, capsys):
    home = saved_login(tmp_path / "codex")
    monkeypatch.setenv("CODEX_HOME", str(home))
    codex_endpoint.respond(
        assistant_output(
            "def is_prime(n): return n > 1\n# Synthetic answer; not an algorithm evaluation."
        )
    )
    ns = {"__name__": "snippet_test"}
    exec(_code("saved_login"), ns)
    await ns["main"]()
    printed = capsys.readouterr().out
    assert "Saved workspace ID: " + ACCOUNT in printed and "Saved plan: pro" in printed
    assert "def is_prime" in printed
    assert len(codex_endpoint.requests) == 1
    assert codex_endpoint.request_headers[0]["authorization"] == "Bearer saved-token"
    assert codex_endpoint.request_headers[0]["chatgpt-account-id"] == ACCOUNT


@pytest.mark.parametrize("delayed_native_join", [3, 4], indirect=True)
async def test_exact_implicit_open_failure_recovery_body(monkeypatch, delayed_native_join):
    from contextlib import asynccontextmanager

    from agents import Agent, Environment, _native, get_cleanup

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-no-model-work")

    async def forbid(*a, **k):
        raise AssertionError("Failed opening submitted model work")

    monkeypatch.setattr(_native.NativeThread, "_submit", forbid)
    entered = []
    exited = []

    @asynccontextmanager
    async def executor_lease():
        entered.append(True)
        try:
            yield Environment(url="wss://127.0.0.1:1/exec", cwd="/workspace", connect_timeout=0.15)
        finally:
            exited.append(True)

    ns = {"agent": Agent(instructions="Inspect"), "executor_lease": executor_lease}
    caught = None
    try:
        with pytest.raises(Exception) as result:
            await eval(
                compile(
                    _code("opening_cleanup"),
                    "configuration-recovery",
                    "exec",
                    flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
                ),
                ns,
            )
        caught = result.value
        recovery = get_cleanup(caught)
        assert isinstance(recovery, NativeProvider)
        assert len(delayed_native_join) == 4
        if len(exited) == 0:
            assert isinstance(caught.__cause__, RuntimeError)
            assert "controlled native retirement" in str(caught.__cause__)
        else:
            assert recovery._closed
    finally:
        if caught is not None:
            recovery = get_cleanup(caught)
            if recovery is not None:
                await recovery.aclose()
    assert entered == exited == [True]


@pytest.mark.parametrize("delayed_native_join", [3], indirect=True)
async def test_exact_get_cleanup_retained_owner_branch(monkeypatch, delayed_native_join):
    from contextlib import asynccontextmanager

    from agents import Agent, Environment, _native, get_cleanup

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-no-model-work")

    async def forbid(*a, **k):
        raise AssertionError("Failed opening submitted model work")

    monkeypatch.setattr(_native.NativeThread, "_submit", forbid)
    exited = []

    @asynccontextmanager
    async def manager():
        try:
            yield Environment(url="wss://127.0.0.1:1/exec", cwd="/workspace", connect_timeout=0.15)
        finally:
            exited.append(True)

    with pytest.raises(Exception) as result:
        await Agent(instructions="Inspect").run("Inspect", environment=manager())
    original = result.value
    cleanup = get_cleanup(original)
    try:
        assert isinstance(cleanup, NativeProvider) and not exited
        ns = {"error": original}
        await eval(
            compile(
                _code("cleanup_owner"),
                "sessions-get-cleanup",
                "exec",
                flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            ),
            ns,
        )
        assert ns["cleanup"] is cleanup and cleanup._closed
        assert exited == [True] and len(delayed_native_join) == 4
    finally:
        await cleanup.aclose()


async def test_exact_timeout_script_takes_timeout_branch(responses, monkeypatch):
    import asyncio

    from agents import Agent
    from tests.test_native_agent import provider

    release = asyncio.Event()
    entered = asyncio.Event()

    async def hold(event):
        if event.get("item", {}).get("type") == "message":
            entered.set()
            await release.wait()

    responses.before_event = hold
    responses.respond(assistant_output("held until timeout"))
    owner = provider(responses)
    init = Agent.__init__

    def configure(self, *a, **k):
        k.setdefault("provider", owner)
        init(self, *a, **k)

    monkeypatch.setattr(Agent, "__init__", configure)
    ns = {"__name__": "snippet_test"}
    exec(_code("timeout"), ns)
    try:
        with pytest.raises(TimeoutError):
            await ns["main"]()
        assert entered.is_set() and len(responses.requests) == 1
    finally:
        release.set()
        responses.before_event = None
        await owner.aclose()


async def test_stdio_declaration_launches_selected_search(responses, tmp_path, monkeypatch):
    import sys
    from dataclasses import replace

    from agents import Agent
    from tests.test_native_agent import provider
    from tests.test_native_mcp_stdio import SERVER, assert_stopped, records
    from tests.test_native_mcp_tools import mcp_call

    ns = {}
    exec(_code("stdio"), ns)
    monkeypatch.setenv("DOCS_API_KEY", "synthetic-docs-key")
    log = tmp_path / "stdio-log"
    server = replace(
        ns["server"],
        command=sys.executable,
        args=("-I", "-B", str(SERVER), "--log", str(log), "--env-var", "DOCS_API_KEY"),
        cwd=str(tmp_path),
    )
    responses.respond(mcp_call("search", "mcp__docs", "doc-search"))
    responses.respond(assistant_output("Search complete"))
    owner = provider(responses)
    try:
        assert (
            await Agent(instructions="Search", tools=[server], provider=owner).run("Search")
        ).final_output == "Search complete"
        await assert_stopped(log)
        start = next(row for row in records(log) if row["event"] == "start")
        assert start["cwd"] == str(tmp_path) and start["selected_env"] == "synthetic-docs-key"
        calls = [
            row["message"]
            for row in records(log)
            if row.get("message", {}).get("method") == "tools/call"
        ]
        assert len(calls) == 1 and calls[0]["params"]["name"] == "search"
    finally:
        await owner.aclose()


async def test_preapproved_http_writer_reaches_writable_tool(responses):
    from dataclasses import replace

    from agents import Agent, HttpMCP
    from tests.test_native_agent import provider
    from tests.test_native_mcp_tools import Peer, mcp_call

    ns = {"HttpMCP": HttpMCP}
    exec(_code("writer"), ns)
    peer = Peer(
        names=("write_record",),
        annotations={"readOnlyHint": False},
        label="synthetic-write-recorded",
    )
    async with peer.serve() as url:
        writer = replace(ns["writer"], url=url)
        responses.respond(mcp_call("write_record", "mcp__records", "doc-write"))
        responses.respond(assistant_output("Write complete"))
        owner = provider(responses)
        try:
            assert (
                await Agent(instructions="Write", tools=[writer], provider=owner).run("Write")
            ).final_output == "Write complete"
            assert len(peer.calls) == 1 and peer.calls[0]["name"] == "write_record"
            assert peer.calls[0]["arguments"] == {"key": "43"}
        finally:
            await owner.aclose()


async def test_exact_apps_registration_uses_synthetic_native_backend(responses, monkeypatch):
    from contextlib import aclosing

    from agents import Agent, ElicitationRequest
    from tests.test_native_agent import provider
    from tests.test_native_connector_auth import (
        ConnectorBackend,
        access_token,
        select,
        synthetic_backend,
    )

    peer = ConnectorBackend()
    peer.connected.set()
    async with peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        monkeypatch.setenv("CHATGPT_APPS_ACCESS_TOKEN", access_token())
        monkeypatch.setenv("CHATGPT_APPS_ACCOUNT_ID", "synthetic-account-A")
        owner = provider(responses)
        init = Agent.__init__

        def configure(self, *a, **k):
            k.setdefault("provider", owner)
            init(self, *a, **k)

        monkeypatch.setattr(Agent, "__init__", configure)
        ns = {}
        exec(_code("apps"), ns)
        responses.respond(select("doc-apps"))
        responses.respond(assistant_output("Apps completed"))
        try:
            async with ns["agent"].session() as session:
                turn = await session.submit("Search the synthetic calendar")
                async with aclosing(session.interactions()) as cursor:
                    question = await anext(cursor)
                assert isinstance(question, ElicitationRequest)
                await question.accept_url()
                peer.complete.set()
                await question.wait_url_completion(timeout=5)
                assert (await turn.get_result()).final_output == "Apps completed"
            assert len(peer.calls) == 1 and len(peer.init_requests) == 1
        finally:
            await owner.aclose()


async def test_exact_native_delegation_and_enumeration_without_commands(
    tmp_path, monkeypatch, capsys
):
    from agents import Agent
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import child_peer, metadata, trust
    from tests.test_native_executor_tools import declaration

    async with (
        child_peer(("default", "default")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        original = peer.response

        def model_reply():
            result = original()
            meta = metadata(peer.requests[-1], peer.request_headers[-1])
            if meta["agent_name"] != "/root":
                name = meta["agent_name"].rsplit("/", 1)[-1]
                return [assistant_output(name + " complete")]
            return result

        monkeypatch.setattr(peer, "response", model_reply)
        for release in peer.release.values():
            release.set()
        owner = peer.provider(websocket=False)
        init = Agent.__init__

        def configure(self, *a, **k):
            k.setdefault("provider", owner)
            init(self, *a, **k)

        monkeypatch.setattr(Agent, "__init__", configure)
        tree = ast.parse(_code("native_children"))
        scope = next(node for node in tree.body if isinstance(node, ast.AsyncWith))
        scope.body.extend(ast.parse(_code("child_results")).body)
        ast.fix_missing_locations(tree)
        ns = {"environment": declaration(executor)}
        try:
            await eval(
                compile(
                    tree, "native-delegation-excerpts", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
                ),
                ns,
            )
            assert ns["result"].final_output in {"alpha complete", "beta complete"}
            output = capsys.readouterr().out
            assert "alpha complete" in output and "beta complete" in output
            assert set(peer.child_requests) == {"alpha", "beta"}
            assert not any(
                req.get("method") in {"process/start", "fs/writeFile"} for req in executor.requests
            )
        finally:
            await owner.aclose()


async def test_exact_native_collaboration_policy_dispatch(responses, tmp_path, monkeypatch):
    from agents import Agent
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_agent import provider
    from tests.test_native_child_agents import collaboration, trust
    from tests.test_native_executor_tools import declaration

    ns = {}
    exec(_code("child_approval"), ns)
    owner = provider(responses)
    async with ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        responses.respond(
            collaboration(
                "spawn_agent",
                {
                    "task_name": "alpha",
                    "message": "Read",
                    "agent_type": "default",
                    "fork_turns": "none",
                },
                "policy-spawn",
            )
        )

        try:
            async with Agent(
                instructions="Spawn", model="gpt-5.5", tools=[ns["children"]], provider=owner
            ).session(environment=declaration(executor)) as session:
                from agents import NativeError

                with pytest.raises(
                    NativeError, match="Native tool interception failed or was denied"
                ):
                    await (await session.submit("Spawn")).get_result()
                assert [item async for item in session.subagents()] == []
            assert len(responses.requests) == 1
            assert not any(
                req.get("method") in {"process/start", "fs/writeFile"} for req in executor.requests
            )
        finally:
            await owner.aclose()


async def test_documented_capability_fragment_discovers_remote_skill(
    responses, tmp_path, monkeypatch
):
    import re

    from agents import Agent
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_agent import provider
    from tests.test_native_child_agents import trust
    from tests.test_native_executor_tools import declaration

    root = ROOT
    async with ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        directory = executor.root / "capabilities"
        skill = directory / "skills/report-style/SKILL.md"
        skill.parent.mkdir(parents=True)
        markdown = next(
            body
            for body in re.findall(
                r"^```markdown\n(.*?)^```",
                (root / "docs/sdk/execution.md").read_text(),
                re.M | re.S,
            )
            if "name: report-style" in body
        )
        skill.write_text(markdown)
        source = _code("capability")
        assert source.count('"/workspace/capabilities"') == 1
        source = source.replace('"/workspace/capabilities"', repr(str(directory)))
        owner = provider(responses)
        responses.respond(assistant_output("Evidence: assigned-file.txt"))
        ns = {
            "agent": Agent(instructions="Inspect", provider=owner),
            "environment": declaration(executor),
        }
        try:
            await eval(
                compile(
                    source, "execution-capabilities", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
                ),
                ns,
            )
            assert ns["result"].final_output == "Evidence: assigned-file.txt"
            wire = json.dumps(responses.requests[0])
            assert "Write reports with an evidence section." in wire
            assert "report-style" in wire
            assert not any(
                req.get("method") in {"process/start", "fs/writeFile"} for req in executor.requests
            )
        finally:
            await owner.aclose()


async def test_exact_executor_lease_fragment_releases_after_native_connection(
    responses, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from agents import Agent
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_agent import provider
    from tests.test_native_child_agents import trust

    events = []
    async with ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        job = SimpleNamespace(
            url=executor.url,
            cwd=str(executor.root),
            headers={"Authorization": "Bearer " + executor.token},
        )

        async def acquire_executor():
            events.append("acquired")
            return job

        async def release_executor(found):
            assert found is job
            await executor.wait_disconnected()
            events.append("released")

        owner = provider(responses)
        responses.respond(assistant_output("Inspected"))
        ns = {
            "agent": Agent(instructions="Inspect", provider=owner),
            "acquire_executor": acquire_executor,
            "release_executor": release_executor,
        }
        try:
            await eval(
                compile(
                    _code("lease"), "execution-lease", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
                ),
                ns,
            )
            assert ns["result"].final_output == "Inspected" and events == ["acquired", "released"]
            assert executor.attempts > 0
            assert not any(
                req.get("method") in {"process/start", "fs/writeFile"} for req in executor.requests
            )
        finally:
            await owner.aclose()


@pytest.mark.requires_native_linux_sandbox
async def test_project_search_main_reads_structured_matches(
    responses, tmp_path, monkeypatch, capsys
):
    from functools import partial

    from agents import Agent
    from examples.codex import project_search
    from tests.native_fixtures import function_call, tool_output
    from tests.test_native_agent import provider
    from tests.test_native_mcp_tools import declarations

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "invoice.py").write_text("class Invoice:\n    pass\n")
    responses.respond(function_call("project_glob", {"pattern": "**/*.py"}, "example-glob"))
    responses.respond(
        function_call("project_grep", {"pattern": "Invoice", "glob": "**/*.py"}, "example-grep")
    )
    responses.respond(assistant_output("Invoice appears in invoice.py:1."))
    async with provider(responses) as owner:
        monkeypatch.setattr(project_search, "Agent", partial(Agent, provider=owner))
        await project_search.main(workspace, "Find the Invoice class.")
    assert capsys.readouterr().out == "Invoice appears in invoice.py:1.\n"
    assert set(declarations(responses.requests[0])) == {
        ("functions", "project_glob"),
        ("functions", "project_grep"),
    }
    glob = json.loads(tool_output(responses.requests[1], "example-glob"))
    grep = json.loads(tool_output(responses.requests[2], "example-grep"))
    assert glob["matches"] == ["invoice.py"] and not glob["truncated"] and not glob["errors"]
    assert grep["matches"] == [
        {
            "path": "invoice.py",
            "line_number": 1,
            "line": "class Invoice:",
            "before": [],
            "after": [],
        }
    ]
    assert not grep["truncated"] and not grep["errors"]
    assert not responses.replies


@pytest.mark.requires_native_linux_sandbox
async def test_storage_mount_main_reads_without_mutating_caller_data(
    responses, tmp_path, monkeypatch, capsys
):
    import shlex
    import sys
    from functools import partial

    from agents import Agent
    from examples.codex.storage_mounts import main as storage_mounts
    from tests.native_fixtures import function_call, tool_output
    from tests.test_native_agent import provider

    workspace, data = tmp_path / "workspace", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    source = data / "input.txt"
    source.write_text("caller data\n")
    command = (
        f"set -e; cat {shlex.quote(str(source))}; "
        f"if (printf changed >> {shlex.quote(str(source))}) 2>/dev/null; then exit 11; fi; "
        "printf 'read-only verified' > observed.txt"
    )
    responses.respond(function_call("exec_command", {"cmd": command, "login": False}, "read-mount"))
    responses.respond(assistant_output("Mounted data checked."))
    monkeypatch.setattr(sys, "argv", ["storage_mounts", str(workspace), str(data)])
    async with provider(responses) as owner:
        monkeypatch.setattr(storage_mounts, "Agent", partial(Agent, provider=owner))
        await storage_mounts.main()
    assert capsys.readouterr().out == "Mounted data checked.\n"
    output = tool_output(responses.requests[1], "read-mount")
    assert "Process exited with code 0" in output and "caller data" in output
    assert source.read_text() == "caller data\n"
    assert (workspace / "observed.txt").read_text() == "read-only verified"
    assert not responses.replies


@pytest.mark.parametrize("failure", ["", "start"], ids=["success", "startup-failure"])
async def test_provider_storage_example_main_owns_gateway_and_api(
    responses, tmp_path, monkeypatch, capsys, failure
):
    import functools
    import sys
    import types
    from contextlib import asynccontextmanager

    import docker

    from agents import Agent, Environment
    from agents.classic.sandbox.sandboxes import docker as implementation
    from examples.codex.storage_mounts import provider as example
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_agent import provider
    from tests.test_native_child_agents import trust
    from tests.test_sandbox_environment import Client

    # Resource creation is synthetic; the native gateway and session lifetime are real.
    # This test does not establish S3 data access or Rclone mount enforcement.
    events = []
    client = Client(events, failure)
    api = types.SimpleNamespace(close=lambda: events.append("api.close"))
    monkeypatch.setattr(docker, "from_env", lambda: api)

    def client_for_api(actual):
        assert actual is api
        return client

    monkeypatch.setattr(implementation, "DockerSandboxClient", client_for_api)
    gateway = types.ModuleType("synthetic_provider_gateway")

    @asynccontextmanager
    async def connect(session):
        assert session is client.session
        assert session.state.manifest.root == "/workspace"
        async with ExecutorPeer(tmp_path / "executor").serve() as peer:
            trust(peer, tmp_path, monkeypatch)
            events.append("connect")
            yield Environment(
                url=peer.url, cwd=str(peer.root), headers={"Authorization": "Bearer " + peer.token}
            )
            await peer.wait_disconnected()
            assert not any(
                req.get("method") in {"process/start", "fs/writeFile"} for req in peer.requests
            )
            events.append("retired")

    gateway.connect = connect
    monkeypatch.setitem(sys.modules, gateway.__name__, gateway)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "provider.py",
            "--connect",
            gateway.__name__ + ":connect",
            "--image",
            "prepared-fixture-image",
            "--bucket",
            "example-bucket",
        ],
    )
    for key, value in [
        ("AWS_ACCESS_KEY_ID", "example-id-sentinel"),
        ("AWS_SECRET_ACCESS_KEY", "example-secret-sentinel"),
        ("AWS_SESSION_TOKEN", "example-token-sentinel"),
    ]:
        monkeypatch.setenv(key, value)
    responses.respond(assistant_output("Synthetic listing: data.csv"))
    async with provider(responses) as owner:
        monkeypatch.setattr(example, "Agent", functools.partial(Agent, provider=owner))
        if failure:
            with pytest.raises(RuntimeError, match="start failed"):
                await example.main()
            assert responses.requests == []
        else:
            await example.main()
            assert capsys.readouterr().out == "Synthetic listing: data.csv\n"
            wire = json.dumps(responses.requests)
            assert "/workspace/data" in wire and "read_only" in wire
            for secret in (
                "example-id-sentinel",
                "example-secret-sentinel",
                "example-token-sentinel",
            ):
                assert secret not in wire
    assert client.manifest is not None
    mount = client.manifest.entries["data"]
    assert mount.bucket == "example-bucket" and mount.read_only is True
    assert mount.access_key_id == "example-id-sentinel"
    assert mount.secret_access_key == "example-secret-sentinel"
    assert mount.session_token == "example-token-sentinel"
    assert mount.mount_strategy.driver == "rclone"
    assert isinstance(client.options, implementation.DockerSandboxClientOptions)
    assert client.options.image == "prepared-fixture-image"
    assert events == (
        ["create", "start", "close", "delete", "api.close"]
        if failure
        else ["create", "start", "connect", "retired", "close", "delete", "api.close"]
    )
