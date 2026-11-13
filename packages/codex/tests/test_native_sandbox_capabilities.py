"""Application capabilities use the existing provider and native opening owners."""

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import Agent, Environment, function_tool
from agents.classic.sandbox.entries import File
from agents.sandbox import Manifest, SandboxCapability, sandbox_environment
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider
from tests.test_sandbox_environment import Client, Options


class WorkspaceGuidance(SandboxCapability):
    name = "workspace-guide"

    def __init__(self):
        self.bound = None

    def prepare_manifest(self, manifest):
        manifest.entries[Path("provided.txt")] = File(content=b"source")
        return manifest

    async def bind(self, session, *, user, workspace):
        assert self.bound is None
        self.bound = session
        assert workspace.cwd.as_posix() == "child"

    async def instructions(self, manifest):
        assert self.bound is not None
        assert manifest.entries[Path("provided.txt")].content == b"source"
        return "Read the record contributed by this capability."

    def tools(self):
        @function_tool
        def sandbox_record() -> str:
            assert self.bound is not None
            return "bound-sandbox-value"

        return (sandbox_record,)

    def sampling_settings(self, settings):
        return {"temperature": 0.25}

    async def transform_context(self, request):
        return (
            *request.items,
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "sandbox-context-replacement"}],
            },
        )


@pytest.mark.parametrize("executor_platform", [None, "windows"])
async def test_sandbox_capability_reaches_the_live_resource_and_model(
    tmp_path, monkeypatch, responses, executor_platform
):
    declaration = WorkspaceGuidance()
    root = tmp_path / "executor"
    child = root / "child"
    manifest = Manifest(root=str(root))
    client = Client([])
    native = provider(responses)
    responses.respond(function_call("sandbox_record", {}, "sandbox-first"))
    responses.respond(assistant_output("done"))
    responses.respond(function_call("sandbox_record", {}, "sandbox-second"))
    responses.respond(assistant_output("done again"))
    try:
        async with ExecutorPeer(root, advertised_platform=executor_platform).serve() as peer:
            child.mkdir()
            trust(peer, tmp_path, monkeypatch)

            @asynccontextmanager
            async def connect(sandbox):
                assert sandbox is client.session
                assert sandbox.state.manifest.entries[Path("provided.txt")].content == b"source"
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(child),
                    write_policy=(
                        "legacy"
                        if sys.platform == "win32" or executor_platform == "windows"
                        else "strict"
                    ),
                    tools=(),
                )
                await peer.wait_disconnected()

            agent = Agent(
                instructions="Read the workspace.",
                model="fixture-unlisted-sandbox-capability",
                provider=native,
            )
            for expected in ("done", "done again"):
                source = sandbox_environment(
                    client,
                    manifest=manifest,
                    options=Options(),
                    connect=connect,
                    capabilities=[declaration],
                )
                async with agent.session(environment=source) as session:
                    result = await (await session.submit("Run")).get_result()
                    assert result.final_output == expected
        assert declaration.bound is None and not manifest.entries
        assert client.events == ["create", "start", "close", "delete"] * 2
        assert all(
            "Read the record contributed by this capability." in str(request["input"])
            for request in responses.requests
        )
        assert len(responses.requests) == 4
        assert all(request["temperature"] == 0.25 for request in responses.requests)
        assert all(
            "sandbox-context-replacement" in json.dumps(request["input"])
            for request in responses.requests
        )
        assert all(
            "bound-sandbox-value" in json.dumps(responses.requests[index]["input"])
            for index in (1, 3)
        )
    finally:
        await native.aclose()


def test_missing_sandbox_capability_rejects_before_provisioning():
    declaration = WorkspaceGuidance()
    declaration.requires = ("compaction",)
    client = Client([])
    with pytest.raises(ValueError, match="workspace-guide requires compaction"):
        sandbox_environment(
            client,
            manifest=Manifest(root="/workspace"),
            options=Options(),
            connect=None,
            capabilities=[declaration],
        )
    assert not client.events


async def test_failed_sandbox_capability_binding_retires_its_resource():
    class Failing(WorkspaceGuidance):
        async def bind(self, session, *, user, workspace):
            raise LookupError("bind failed")

    client = Client([])

    @asynccontextmanager
    async def connect(sandbox):
        assert sandbox is client.session
        try:
            yield Environment(url="wss://executor.invalid", cwd="/workspace/child")
        finally:
            client.events.append("disconnect")

    source = sandbox_environment(
        client,
        manifest=Manifest(root="/workspace"),
        options=Options(),
        connect=connect,
        capabilities=[Failing()],
    )
    with pytest.raises(LookupError, match="bind failed"):
        async with source:
            pytest.fail("A failed acquisition was published")
    assert client.events == ["create", "start", "disconnect", "close", "delete"]


@pytest.mark.parametrize("policy", ["error", "warn"])
async def test_sandbox_capability_tools_use_normal_catalog_collision_rules(
    tmp_path, monkeypatch, responses, policy
):
    @function_tool
    def sandbox_record() -> str:
        return "original"

    client = Client([])
    native = provider(responses)
    try:
        async with ExecutorPeer(tmp_path / "executor").serve() as peer:
            trust(peer, tmp_path, monkeypatch)
            child = peer.root / "child"
            child.mkdir()

            @asynccontextmanager
            async def connect(sandbox):
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(child),
                    write_policy="legacy" if sys.platform == "win32" else "strict",
                )
                client.events.append("disconnect")

            source = sandbox_environment(
                client,
                manifest=Manifest(root=str(peer.root)),
                options=Options(),
                connect=connect,
                capabilities=[WorkspaceGuidance()],
            )
            agent = Agent(
                instructions="Read the workspace.",
                model="fixture-unlisted-sandbox-capability",
                provider=native,
                tools=[sandbox_record],
                tool_name_collision_policy=policy,
            )
            if policy == "error":
                with pytest.raises(ValueError, match="collid|[Dd]uplicate"):
                    async with agent.session(environment=source):
                        pytest.fail("Duplicate capability tool was published")
                assert not responses.requests
            else:
                responses.respond(function_call("sandbox_record", {}, "sandbox-collision"))
                responses.respond(assistant_output("done"))
                async with agent.session(environment=source) as session:
                    turn = await session.submit("Read the selected record.")
                    assert (await turn.get_result()).final_output == "done"
                assert len(responses.requests) == 2
                assert "bound-sandbox-value" in json.dumps(responses.requests[1]["input"])
        assert client.events == ["create", "start", "disconnect", "close", "delete"]
    finally:
        await native.aclose()


@pytest.mark.parametrize(
    ("root", "cwd", "relative"),
    [
        (r"D:\workspace", "file:///D:/workspace/child", "child"),
        (r"D:\workspace", r"D:\workspace\child", "child"),
        (r"D:\workspace", "D:/workspace/child", "child"),
        ("/workspace", "/workspace/child", "child"),
        (r"D:\work space", "file:///D:/work%20space/child", "child"),
        (r"D:\WORKSPACE", "file:///d:/workspace/child", "child"),
        (r"D:\workspace", "file://localhost/D:/workspace", None),
        ("/workspace", "file:///workspace/child", "child"),
    ],
)
def test_sandbox_capability_workspace_has_been_retaining_executor_path_flavor(root, cwd, relative):
    from _agents_codex.sandbox import _capability_workspace

    session = SimpleNamespace(state=SimpleNamespace(manifest=SimpleNamespace(root=root)))
    environment = Environment(url="wss://executor.invalid", cwd=cwd)
    scope = _capability_workspace(session, environment)
    assert (scope.cwd.as_posix() if scope.cwd is not None else None) == relative


@pytest.mark.parametrize(
    ("root", "cwd"),
    [
        (r"D:\workspace", "file:///E:/workspace/child"),
        (r"D:\workspace", "file:///D:/workspace-other/child"),
        (r"D:\workspace", "file://other/D:/workspace/child"),
        (r"D:\workspace", "file:///D:/workspace/child/../../escape"),
        ("/workspace", "file://other/workspace/child"),
        ("/workspace", "file:///workspace/child/../../escape"),
    ],
)
def test_sandbox_capability_workspace_has_been_rejecting_other_roots_and_authorities(root, cwd):
    from _agents_codex.sandbox import _capability_workspace

    session = SimpleNamespace(state=SimpleNamespace(manifest=SimpleNamespace(root=root)))
    environment = Environment(url="wss://executor.invalid", cwd=cwd)
    with pytest.raises(ValueError, match="gateway cwd within the workspace"):
        _capability_workspace(session, environment)


async def test_sandbox_capability_keeps_strict_windows_executor_rejection(
    tmp_path, monkeypatch, responses
):
    client = Client([])
    native = provider(responses)
    declaration = WorkspaceGuidance()
    try:
        async with ExecutorPeer(
            tmp_path / "executor", advertised_platform="windows"
        ).serve() as peer:
            trust(peer, tmp_path, monkeypatch)
            child = peer.root / "child"
            child.mkdir()

            @asynccontextmanager
            async def connect(sandbox):
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(child),
                )
                client.events.append("disconnect")

            source = sandbox_environment(
                client,
                manifest=Manifest(root=str(peer.root)),
                options=Options(),
                connect=connect,
                capabilities=[declaration],
            )
            agent = Agent(instructions="Read the workspace.", provider=native)
            with pytest.raises(
                RuntimeError,
                match="strict executor write policy requires a Linux or macOS executor",
            ):
                async with agent.session(environment=source):
                    pytest.fail("Published a strict environment on a Windows executor")
            assert not responses.requests
            assert not any(request.get("method") == "process/start" for request in peer.requests)
        assert declaration.bound is None
        assert client.events == ["create", "start", "disconnect", "close", "delete"]
    finally:
        await native.aclose()


@pytest.mark.parametrize("destination", ["capability", "agent"])
async def test_native_cancel_does_not_enter_next_composed_context_callback(
    tmp_path, monkeypatch, responses, destination
):
    import asyncio

    from agents import TurnStoppedError
    from tests._asyncio import timeout

    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    class First(SandboxCapability):
        name = "first"

        async def transform_context(self, request):
            effects.append("first")
            entered.set()
            await release.wait()
            return request.items

    class Second(SandboxCapability):
        name = "second"

        async def transform_context(self, request):
            effects.append("late-capability")
            return request.items

    async def original(request):
        effects.append("late-agent")
        return request.items

    native = provider(responses)
    client = Client([])
    try:
        async with timeout(25), ExecutorPeer(tmp_path / "executor").serve() as peer:
            trust(peer, tmp_path, monkeypatch)

            @asynccontextmanager
            async def connect(sandbox):
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(peer.root),
                    write_policy="legacy" if sys.platform == "win32" else "strict",
                )
                await peer.wait_disconnected()

            source = sandbox_environment(
                client,
                manifest=Manifest(root=str(peer.root)),
                options=Options(),
                connect=connect,
                capabilities=[First(), Second()] if destination == "capability" else [First()],
            )
            agent = Agent(
                instructions="Answer.",
                provider=native,
                transform_context=original if destination == "agent" else None,
            )
            async with agent.session(environment=source) as session:
                delayed = []
                monkeypatch.setattr(
                    session,
                    "_cancel_native_callback",
                    lambda receipt, **kwargs: delayed.append(receipt),
                )
                turn = await session.submit("Cancel before the next transform.")
                try:
                    await entered.wait()
                    owners = [
                        owner for owner in session._active.callbacks.values() if owner is not None
                    ]
                    assert owners
                    interrupted = asyncio.ensure_future(
                        session._native._interrupt_session(turn.turn_id)
                    )
                    for _ in range(500):
                        if any(
                            owner.native._python_callback_cancelled(json.dumps(owner.receipt))
                            for owner in owners
                        ):
                            break
                        await asyncio.sleep(0.01)
                    else:
                        pytest.fail("Native callback receipt did not retire")
                finally:
                    release.set()
                await interrupted
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
            assert effects == ["first"]
            assert not responses.requests
        assert client.events == ["create", "start", "close", "delete"]
    finally:
        release.set()
        await native.aclose()
