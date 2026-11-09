"""Public native enforcement with independent file oracles."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, CapabilityError, Environment, NativeAgents, NativeProvider, NetworkPolicy
from agents.state import SessionState
from agents.testing import ScriptedResponse, ScriptedResponses, text, tool_call
from tests.native_executor_fixtures import ExecutorPeer
from tests.test_native_executor_isolation import trust_certificates

SUPPORTED = sys.platform in ("linux", "darwin")
native_sandbox = (
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)


def command_response(command, call_id="command", **extra):
    return ScriptedResponse(
        [tool_call("exec_command", {"cmd": command, "login": False, **extra}, call_id=call_id)]
    )


def output(request, call_id="command"):
    outputs = [
        item["output"]
        for item in request["input"]
        if item.get("type") == "function_call_output" and item.get("call_id") == call_id
    ]
    assert len(outputs) == 1
    value = outputs[0]
    return value if isinstance(value, str) else "".join(item.get("text", "") for item in value)


@asynccontextmanager
async def workspace(transport, tmp_path, monkeypatch):
    if transport == "local":
        root = tmp_path / "workspace"
        root.mkdir()
        yield root, lambda **options: Environment.local(cwd=root, tools=["exec_command"], **options)
    else:
        async with ExecutorPeer(tmp_path / "remote").serve() as peer:
            assert peer.ca is not None
            trust_certificates(monkeypatch, tmp_path, peer.ca)
            yield (
                peer.root,
                lambda **options: Environment(
                    url=peer.url,
                    cwd=str(peer.root),
                    headers={"Authorization": "Bearer " + peer.token},
                    tools=["exec_command"],
                    **options,
                ),
            )


def test_read_only_declaration_rejects_conflicts():
    assert Environment(url="wss://executor.invalid", cwd="/workspace").read_only is False
    with pytest.raises(ValueError, match="strict"):
        Environment(
            url="wss://executor.invalid", cwd="/workspace", write_policy="legacy", read_only=True
        )
    with pytest.raises(ValueError, match="writable_roots"):
        Environment(
            url="wss://executor.invalid",
            cwd="/workspace",
            writable_roots=["/output"],
            read_only=True,
        )
    with pytest.raises(ValueError, match="apply_patch"):
        Environment(
            url="wss://executor.invalid", cwd="/workspace", tools=["apply_patch"], read_only=True
        )
    with pytest.raises(TypeError, match="read_only"):
        Environment(url="wss://executor.invalid", cwd="/workspace", read_only="yes")  # type: ignore[arg-type]


@native_sandbox
@pytest.mark.skipif(not SUPPORTED, reason="requires native Unix sandbox")
@pytest.mark.parametrize("transport", ["local", "wss"])
@pytest.mark.parametrize("operation", ["create", "append", "unlink", "rename", "absolute", "alias"])
async def test_read_only_denies_real_mutations(transport, operation, tmp_path, monkeypatch):
    async with workspace(transport, tmp_path, monkeypatch) as (root, environment):
        seed = root / "seed"
        seed.write_text("ORIGINAL\n")
        alias = root.parent / "workspace-alias"
        alias.symlink_to(root, target_is_directory=True)
        target = root / "new"
        mutations = {
            "create": "printf changed > new",
            "append": "printf changed >> seed",
            "unlink": "rm seed",
            "rename": "mv seed moved",
            "absolute": "printf changed > " + shlex.quote(str(target)),
            "alias": "printf changed > " + shlex.quote(str(alias / "new")),
        }
        before_entries = sorted(path.name for path in root.iterdir())
        command = (
            "printf READ:; cat seed; if ("
            + mutations[operation]
            + ") 2>/dev/null; then printf UNEXPECTED_WRITE; else printf DENIED; fi"
        )
        async with ScriptedResponses(
            command_response(command), ScriptedResponse([text("done")])
        ) as peer:
            agent = Agent(
                model="gpt-5.5", instructions="Use the selected command.", provider=peer.provider
            )
            result = await asyncio.wait_for(
                agent.run(
                    "Run.",
                    environment=environment(
                        read_only=True, approval=lambda context, arguments: True
                    ),
                ),
                20,
            )
            assert result.final_output == "done" and len(peer.requests) == 2
            actual = output(peer.requests[1])
            assert (
                "READ:ORIGINAL" in actual
                and "DENIED" in actual
                and "UNEXPECTED_WRITE" not in actual
            )
        assert seed.read_text() == "ORIGINAL\n"
        assert not target.exists() and not (root / "moved").exists()
        assert sorted(path.name for path in root.iterdir()) == before_entries


@native_sandbox
@pytest.mark.skipif(not SUPPORTED, reason="requires native Unix sandbox")
@pytest.mark.parametrize("read_only", [False, True])
async def test_root_update_restore_compaction_keep_the_ceiling(read_only, tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "seed").write_text("ORIGINAL\n")
    extra = tmp_path / "extra"
    extra.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("PRIVATE_OUTSIDE_SENTINEL")
    metadata = root / ".git"
    metadata.mkdir()
    protected = metadata / "config"
    protected.write_text("[core]\n\trepositoryformatversion = 0\n")

    def write_probe(name):
        return (
            f"cat seed; if (printf changed > {name}) 2>/dev/null; "
            "then printf WROTE; else printf DENIED; fi; "
            f"(cat {shlex.quote(str(outside))}) 2>/dev/null; "
            "(printf bad > .git/config) 2>/dev/null; "
            f"(printf bad > {shlex.quote(str(extra / name))}) 2>/dev/null; "
            f"(printf bad > {shlex.quote(str(outside))}) 2>/dev/null; true"
        )

    async with ScriptedResponses(
        command_response(
            "cat seed"
            if read_only
            else "cat seed; printf allowed > " + shlex.quote(str(extra / "initial")),
            "initial",
        ),
        ScriptedResponse([text("initial")]),
        command_response(write_probe("after-empty"), "empty"),
        ScriptedResponse([text("empty")]),
        command_response(write_probe("after-compact"), "compact"),
        ScriptedResponse([text("compact")]),
    ) as peer:
        agent = Agent(
            model="gpt-5.5", instructions="Use the selected command.", provider=peer.provider
        )
        selected = Environment.local(cwd=root, tools=["exec_command"], read_only=read_only)
        async with agent.session(environment=selected) as session:
            if read_only:
                with pytest.raises(CapabilityError, match="Read-only"):
                    await session.submit("Do not expand.", writable_roots=[extra.resolve()])
            assert len(peer.requests) == 0
            assert (
                await (
                    await session.submit(
                        "Read.", **({} if read_only else {"writable_roots": [extra.resolve()]})
                    )
                ).get_result()
            ).final_output == "initial"
            state = await session.save_state()
        assert (extra / "initial").exists() is not read_only
        selected = Environment.local(
            cwd=root,
            tools=["exec_command"],
            read_only=read_only,
            writable_roots=[] if read_only else [extra.resolve()],
        )
        encoded = json.loads(state.to_json())
        assert encoded.get("executor_read_only", False) is read_only
        assert ("executor_read_only" in encoded) is read_only
        opposite = Environment.local(cwd=root, tools=["exec_command"], read_only=not read_only)
        # Trap the real provider resource boundary; this check does not replace native enforcement.
        with monkeypatch.context() as patch:

            def forbidden_acquire(*args, **kwargs):
                pytest.fail("Mode mismatch reached provider acquisition")

            patch.setattr(NativeProvider, "_acquire_lease", forbidden_acquire)
            with pytest.raises(ValueError, match="read-only execution"):
                async with agent.session(state=state, environment=opposite):
                    pytest.fail("Mismatched checkpoint opened")
        forged = SessionState.from_json(
            json.dumps({**encoded, "executor_read_only": not read_only})
        )
        with pytest.raises(ValueError, match="applied executor policy"):
            async with agent.session(state=forged, environment=opposite):
                pytest.fail("Changing only the marker bypassed the full policy digest")
        assert len(peer.requests) == 2
        async with agent.session(state=state, environment=selected) as restored:
            assert (
                await (
                    await restored.submit("Replace with empty roots.", writable_roots=[])
                ).get_result()
            ).final_output == "empty"
            await restored.compact(summary="Keep the supplied seed and command boundary.")
            assert (
                await (await restored.submit("After compaction.")).get_result()
            ).final_output == "compact"
            assert (await restored.save_state()).executor_read_only is read_only
        for name in ["after-empty", "after-compact"]:
            assert (root / name).exists() is not read_only
            assert not (extra / name).exists()
        assert ("DENIED" if read_only else "WROTE") in output(peer.requests[3], "empty")
        assert ("DENIED" if read_only else "WROTE") in output(peer.requests[5], "compact")
        assert "PRIVATE_OUTSIDE_SENTINEL" not in output(peer.requests[3], "empty")
        assert "PRIVATE_OUTSIDE_SENTINEL" not in output(peer.requests[5], "compact")
        assert (
            outside.read_text() == "PRIVATE_OUTSIDE_SENTINEL"
            and protected.read_text() == "[core]\n\trepositoryformatversion = 0\n"
        )


@native_sandbox
@pytest.mark.skipif(not SUPPORTED, reason="requires native Unix sandbox")
async def test_argument_transform_and_approval_cannot_grant_writes(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "seed").write_text("ORIGINAL\n")
    calls = []

    def transform(context, arguments):
        calls.append(arguments["cmd"])
        return {
            **arguments,
            "cmd": (
                "cat seed; if (printf bad > transformed) 2>/dev/null; "
                "then printf UNEXPECTED_WRITE; else printf DENIED; fi"
            ),
        }

    async with ScriptedResponses(
        command_response("printf harmless"), ScriptedResponse([text("done")])
    ) as peer:
        result = await Agent(
            model="gpt-5.5", instructions="Use the selected command.", provider=peer.provider
        ).run(
            "Run.",
            environment=Environment.local(
                cwd=root,
                tools=["exec_command"],
                read_only=True,
                approval=lambda context, arguments: True,
                transform_arguments=transform,
            ),
        )
        assert result.final_output == "done" and calls == ["printf harmless"]
        actual = output(peer.requests[1])
        assert "ORIGINAL" in actual and "DENIED" in actual and "UNEXPECTED_WRITE" not in actual
    assert not (root / "transformed").exists()


@native_sandbox
@pytest.mark.skipif(not SUPPORTED, reason="requires native Unix sandbox")
async def test_require_escalated_cannot_grant_writes(tmp_path):
    async with ScriptedResponses(
        command_response("cat seed", "read"),
        command_response(
            "printf bad > elevated", "elevated", sandbox_permissions="require_escalated"
        ),
        ScriptedResponse([text("done")]),
    ) as peer:
        root = tmp_path / "workspace"
        root.mkdir()
        (root / "seed").write_text("ORIGINAL")
        result = await Agent(
            model="gpt-5.5", instructions="Use the selected commands.", provider=peer.provider
        ).run(
            "Run.", environment=Environment.local(cwd=root, tools=["exec_command"], read_only=True)
        )
        assert result.final_output == "done" and "ORIGINAL" in output(peer.requests[1], "read")
        denied = output(peer.requests[2], "elevated").lower()
        assert "approval policy" in denied and "reject command" in denied
        assert not (root / "elevated").exists()


@native_sandbox
@pytest.mark.skipif(not SUPPORTED, reason="requires native Unix sandbox")
@pytest.mark.parametrize("read_only", [False, True])
async def test_read_only_keeps_explicit_network_scope(read_only, tmp_path):
    hits = []
    app = web.Application()

    async def handle(request):
        hits.append(request.path)
        return web.Response(text="LOOPBACK_ALLOWED")

    app.router.add_get("/{path:.*}", handle)
    async with TestServer(app) as server:
        url = str(server.make_url("/allowed"))
        curl = "curl --silent --show-error --fail --max-time 3 --proxy \"$HTTP_PROXY\" --noproxy ''"
        async with ScriptedResponses(
            command_response(curl + " " + shlex.quote(url), "allow"),
            command_response(
                curl + " " + shlex.quote(url.replace("127.0.0.1", "localhost")), "deny"
            ),
            ScriptedResponse([text("done")]),
        ) as peer:
            selected = Environment.local(
                cwd=tmp_path,
                tools=["exec_command"],
                read_only=read_only,
                network=NetworkPolicy(
                    allowed_domains=["127.0.0.1"],
                    denied_domains=["localhost"],
                    allow_all_unix_sockets=sys.platform == "linux",
                    allow_local_binding=True,
                ),
            )
            result = await Agent(
                model="gpt-5.5", instructions="Use the selected commands.", provider=peer.provider
            ).run("Run.", environment=selected)
            assert result.final_output == "done"
            assert "LOOPBACK_ALLOWED" in output(peer.requests[1], "allow")
            assert "blocked" in output(peer.requests[2], "deny").lower()
    assert hits == ["/allowed"]


@pytest.mark.parametrize("platform", ["win32", "freebsd"])
def test_read_only_local_rejects_unsupported_host(platform, monkeypatch):
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(ValueError, match="Linux or macOS"):
        Environment.local(cwd="/workspace", read_only=True)


async def test_read_only_native_agents_rejects_before_provider_acquisition(monkeypatch):
    async with ScriptedResponses() as peer:
        agent = Agent(
            model="gpt-5.5", instructions="Work.", provider=peer.provider, tools=[NativeAgents()]
        )

        def forbidden_acquire(*args, **kwargs):
            pytest.fail("Unsupported NativeAgents reached provider acquisition")

        monkeypatch.setattr(NativeProvider, "_acquire_lease", forbidden_acquire)
        with pytest.raises(ValueError, match="NativeAgents"):
            async with agent.session(
                environment=Environment(
                    url="wss://executor.invalid", cwd="/workspace", read_only=True
                )
            ):
                pytest.fail("Unsupported NativeAgents opened")
        assert len(peer.requests) == 0
