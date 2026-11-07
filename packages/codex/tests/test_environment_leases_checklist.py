"""Public environment leases, destination roots and native checklist authority."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    CapabilityError,
    Checklist,
    ChecklistState,
    Environment,
    FunctionTool,
    LocalStorage,
    NativeAgents,
    NetworkPolicy,
    Session,
    function_tool,
)
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
from tests.test_native_agent_declarations import _observe_implicit
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_tools import declarations


def remote(peer: ExecutorPeer, *roots: Path) -> Environment[object]:
    return Environment(
        url=peer.url,
        **({"write_policy": "legacy"} if sys.platform == "win32" else {}),
        cwd=str(peer.root),
        headers={"Authorization": "Bearer " + peer.token},
        capability_directories=[str(root) for root in roots],
    )


def trust(peer: ExecutorPeer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))


def skill(root: Path, name: str, description: str) -> None:
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\nSynthetic fixture.\n")


class Source:
    def __init__(self, peer: ExecutorPeer) -> None:
        self.peer = peer
        self.opened = 0
        self.released = 0

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[Environment[object]]:
        self.opened += 1
        try:
            yield remote(self.peer)
        finally:
            # The observed peer disconnect must precede application lease release.
            if self.peer.transports:
                await self.peer.wait_disconnected()
            self.released += 1


class FailingSource(Source):
    def __init__(self, peer: ExecutorPeer) -> None:
        super().__init__(peer)
        self.error = OSError("synthetic environment release failure")

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[Environment[object]]:
        async with super().lease() as environment:
            yield environment
        raise self.error


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
async def test_acquired_environment_restore_checks_policy_and_releases_on_mismatch(
    tmp_path: Path, responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        selected = replace(
            remote(peer),
            tools=["exec_command"],
            network=NetworkPolicy(
                allowed_domains=["A.example.invalid.", "b.example.invalid"],
                allow_all_unix_sockets=True,
            ),
        )
        async with provider(responses) as shared:
            agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=shared)
            async with agent.session(environment=selected) as session:
                responses.respond(assistant_output("settled"))
                assert (await (await session.submit("Read")).get_result()).final_output == "settled"
                saved = await session.save_state()

            leases: list[str] = []

            @asynccontextmanager
            async def current(policy: NetworkPolicy) -> AsyncIterator[Environment[object]]:
                leases.append("opened")
                try:
                    yield replace(selected, network=policy)
                finally:
                    leases.append("released")

            async with agent.session(
                state=saved,
                environment=current(
                    NetworkPolicy(
                        allowed_domains=["b.example.invalid", "a.example.invalid"],
                        allow_all_unix_sockets=True,
                    )
                ),
            ) as restored:
                assert restored.id == saved.source_session_id
                assert leases == ["opened"]
            assert leases == ["opened", "released"]
            attempts = peer.attempts
            requests = len(responses.requests)
            with pytest.raises(ValueError, match="Checkpoint and Environment must agree"):
                async with agent.session(
                    state=saved, environment=current(NetworkPolicy(allow_all_unix_sockets=True))
                ):
                    pytest.fail("Restored with a different acquired network policy")
            assert leases == ["opened", "released", "opened", "released"]
            assert peer.attempts == attempts
            assert len(responses.requests) == requests
            responses.respond(assistant_output("provider reusable"))
            assert (await agent.run("Again")).final_output == "provider reusable"


@pytest.mark.parametrize("earlier_failure", ["none", "body", "native"])
async def test_source_exit_failure_retires_borrowed_provider_once_and_preserves_first_error(
    tmp_path: Path,
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    earlier_failure: str,
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = FailingSource(peer)
        borrowed_provider = provider(responses)
        agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=borrowed_provider)
        primary = ValueError("synthetic " + earlier_failure + " failure")
        responses.respond(assistant_output("opened"))
        with pytest.raises((ValueError, OSError)) as caught:
            async with agent.session(environment=source.lease()) as session:
                assert (await (await session.submit("Read")).get_result()).final_output == "opened"
                if earlier_failure == "native":
                    native = session._native

                    class FailingClose:
                        # The real fixture cannot request this native close receipt;
                        # retire the actual thread before injecting its reported error.
                        def __getattr__(self, name: str) -> Any:
                            return getattr(native, name)

                        async def close(self) -> None:
                            await native.close()
                            raise primary

                    monkeypatch.setattr(session, "_native", FailingClose())
                if earlier_failure == "body":
                    raise primary
        assert caught.value is (source.error if earlier_failure == "none" else primary)
        if earlier_failure != "none":
            assert primary.__context__ is source.error
        assert (source.opened, source.released) == (1, 1)
        # A supplied provider still belongs to the caller. A later Session can
        # use it, and the caller must be able to close it after all Sessions exit.
        responses.respond(assistant_output("borrower survived"))
        assert (await agent.run("Again")).final_output == "borrower survived"
        await borrowed_provider.aclose()
        assert (source.opened, source.released) == (1, 1)


async def test_source_exit_failure_closes_the_implicit_provider(
    tmp_path: Path, responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        implicit = _observe_implicit(monkeypatch, responses.url)
        source = FailingSource(peer)
        agent = Agent(instructions="Inspect.", model="gpt-5.5")
        responses.respond(assistant_output("opened"))
        with pytest.raises(OSError) as caught:
            async with agent.session(environment=source.lease()) as session:
                assert (await (await session.submit("Read")).get_result()).final_output == "opened"
        assert caught.value is source.error
        assert (source.opened, source.released) == (1, 1)
        assert len(implicit) == 1
        with pytest.raises(RuntimeError, match="closed"):
            async with agent.session(provider=implicit[0]):
                pytest.fail("Implicit provider survived Environment release failure")


class Report(BaseModel):
    value: str


@pytest.mark.parametrize("paused", [True, False], ids=["manual-native-schema", "idle-output"])
async def test_denied_restore_does_not_acquire_an_executor(
    tmp_path: Path,
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    paused: bool,
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = Source(peer)
        lookup = FunctionTool(
            name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
        )
        agent = Agent(
            instructions="Inspect.", model="gpt-5.5", provider=provider(responses), tools=[lookup]
        )
        if paused:
            responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
            async with agent.session() as session:
                await session.submit("Read A")
                await next_request(session.interactions(), lookup)
                saved = await session.suspend()
            assert saved.format_version == 5
            reason = "native output schema does not match"
        else:
            responses.respond(assistant_output("read"))
            async with agent.session() as session:
                await (await session.submit("Read")).get_result()
                saved = await session.save_state()
            assert saved.format_version == 4
            reason = "output contract does not match"
        # This is a real sealed state, not a forged continuation. Restoring
        # with a different output contract must fail before acquiring a lease.
        typed = replace(agent, output_type=Report)
        with pytest.raises(ValueError, match=reason):
            async with typed.session(state=saved, environment=source.lease()):
                pytest.fail("Opened an executor for a denied restore")
        assert (source.opened, source.released) == (0, 0)
        assert not peer.transports
        assert len(responses.requests) == 1


async def test_borrowed_and_acquired_executors_close_native_before_release(
    tmp_path: Path, responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = Source(peer)
        agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=provider(responses))
        responses.respond(assistant_output("borrowed"))
        borrowed = remote(peer)
        async with agent.session(environment=borrowed) as session:
            assert (await (await session.submit("Read")).get_result()).final_output == "borrowed"
        await peer.wait_disconnected()
        assert (source.opened, source.released) == (0, 0)
        # The same caller-owned descriptor is still usable after Session exit.
        responses.respond(assistant_output("again"))
        assert (await agent.run("Read", environment=borrowed)).final_output == "again"
        for answer in ("lease one", "lease two"):
            responses.respond(assistant_output(answer))
            async with agent.session(environment=source.lease()) as session:
                assert (await (await session.submit("Read")).get_result()).final_output == answer
                assert source.opened == source.released + 1
        assert (source.opened, source.released) == (2, 2)
        assert not responses.replies


async def test_lease_releases_after_native_startup_failure_and_pre_open_validation(
    tmp_path: Path, responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = Source(peer)
        agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=provider(responses))

        def failed_start(self: Session[None, str]) -> None:
            # Opening has constructed a real native thread and connected transport.
            assert peer.transports
            raise RuntimeError("synthetic publication failure")

        with monkeypatch.context() as patch:
            patch.setattr(Session, "_start_readers", failed_start)
            with pytest.raises(RuntimeError, match="synthetic publication failure"):
                async with agent.session(environment=source.lease()):
                    pytest.fail("Unexpected successful opening")
        assert (source.opened, source.released) == (1, 1)
        bad = replace(
            agent,
            tools=[*[t for t in agent.tools if not isinstance(t, NativeAgents)], NativeAgents()],
        )
        with pytest.raises(ValueError, match="all three execution tools"):
            async with bad.session(environment=source.lease()):
                pytest.fail("Invalid model settings opened")
        assert (source.opened, source.released) == (2, 2)
        assert not responses.requests


@pytest.mark.parametrize("destination_roots", ["selected", "omitted", "empty"])
async def test_destination_roots_exclude_host_unselected_and_source_after_restore(
    tmp_path: Path,
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    destination_roots: str,
) -> None:
    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        chosen = peer.root / "chosen"
        other = peer.root / "unselected"
        destination = peer.root / "destination"
        host = tmp_path / "host"
        skill(chosen, "source-skill", "SOURCE_ORCHID_ROOT")
        skill(other, "hidden-skill", "UNSELECTED_RUBY_ROOT")
        skill(destination, "dest-skill", "DESTINATION_AMBER_ROOT")
        skill(host / ".agents", "host-skill", "HOST_ZIRCON_ROOT")
        monkeypatch.chdir(host)
        # The first Environment captures its selected roots before this list changes.
        directories = [str(chosen)]
        selected = replace(remote(peer), capability_directories=directories)
        directories.append(str(other))
        agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=provider(responses))
        responses.respond(assistant_output("source"))
        async with agent.session(environment=selected) as session:
            assert (await (await session.submit("Catalog")).get_result()).final_output == "source"
            wire = json.dumps(responses.requests[-1])
            assert "SOURCE_ORCHID_ROOT" in wire
            assert "UNSELECTED_RUBY_ROOT" not in wire
            assert "HOST_ZIRCON_ROOT" not in wire
            assert "DESTINATION_AMBER_ROOT" not in wire
            saved = await session.save_state()
            assert peer.token not in saved.to_json()
            assert str(chosen) not in json.dumps(saved.agent_options)
        first_end = len(peer.requests)
        responses.respond(assistant_output("restored"))
        current = (
            remote(peer, destination)
            if destination_roots == "selected"
            else replace(remote(peer), capability_directories=None)
            if destination_roots == "omitted"
            else remote(peer)
        )
        if destination_roots == "omitted":
            assert "capability_directories" not in current._to_native()
        elif destination_roots == "empty":
            assert current._to_native()["capability_directories"] == []
        entered = False
        try:
            async with agent.session(state=saved, environment=current) as restored:
                entered = True
                assert (
                    await (await restored.submit("Catalog now")).get_result()
                ).final_output == "restored"
                wire = json.dumps(responses.requests[-1])
                assert ("DESTINATION_AMBER_ROOT" in wire) == (destination_roots == "selected")
                assert "UNSELECTED_RUBY_ROOT" not in wire
                assert "HOST_ZIRCON_ROOT" not in wire
                discovery = [
                    json.dumps(message.get("params", {}))
                    for message in peer.requests[first_end:]
                    if message.get("method", "").startswith(("capabilityRoots/", "fs/"))
                ]
                assert any(destination.as_uri() in params for params in discovery) == (
                    destination_roots == "selected"
                ), discovery
                assert not any(
                    chosen.as_uri() in params or other.as_uri() in params for params in discovery
                )
        except RuntimeError as error:
            if not entered and str(error).startswith("remote executor failed to become ready:"):
                try:
                    log_bytes = (peer.root / ".fixture-config" / "exec-server.log").read_bytes()
                except OSError:
                    server_log = {"readable": False}
                else:
                    lower_log = log_bytes.lower()
                    server_log = {
                        "readable": True,
                        "size_bytes": len(log_bytes),
                        "sha256": hashlib.sha256(log_bytes).hexdigest(),
                        "categories": {
                            marker: marker.encode() in lower_log
                            for marker in (
                                "certificate",
                                "tls",
                                "initialize",
                                "timed out",
                                "connection reset",
                                "connection refused",
                                "address already in use",
                                "panicked at",
                            )
                        },
                    }
                print(
                    "Restored executor readiness failed: "
                    + json.dumps(
                        {
                            "destination_roots": destination_roots,
                            "connection_attempts": peer.attempts,
                            "transport_closing": [
                                None if t is None else t.is_closing() for t in peer.transports
                            ],
                            "source_request_count": first_end,
                            "restore_methods": [
                                request.get("method") for request in peer.requests[first_end:]
                            ],
                            "exec_server_log": server_log,
                        }
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            raise
        assert not responses.replies


async def test_checklist_opening_authority_survives_roster_leaf_selection(
    responses: ResponsesFixture,
) -> None:
    effects: list[str] = []

    @function_tool
    def lookup(key: str) -> str:
        effects.append(key)
        return "found-" + key

    checklist = Checklist()
    roster = [checklist, lookup]
    agent = Agent(
        instructions="Inspect.", model="gpt-5.5", tools=roster, provider=provider(responses)
    )
    async with timeout(20), agent.session() as session:
        assert await session.checklist() is None
        plan = [{"step": "Read fixture", "status": "in_progress"}]
        responses.respond(function_call("lookup", {"key": "record"}, "lookup-record"))
        responses.respond(function_call("update_plan", {"plan": plan}, "first-plan"))
        responses.respond(assistant_output("selected"))
        assert (await (await session.submit("Read")).get_result()).final_output == "selected"
        names = declarations(responses.requests[-3])
        assert ("functions", "lookup") in names
        assert ("functions", "update_plan") in names
        assert effects == ["record"]
        assert tool_output(responses.requests[-2], "lookup-record") == "found-record"
        assert "Plan updated" in tool_output(responses.requests[-1], "first-plan")
        state = await session.checklist()
        assert state is not None and state.model_dump()["plan"] == plan

        done = [{"step": "Read fixture", "status": "completed"}]
        responses.respond(function_call("update_plan", {"plan": done}, "done-plan"))
        responses.respond(assistant_output("complete"))
        assert (
            await (await session.submit("Finish", tools=[checklist])).get_result()
        ).final_output == "complete"
        names = declarations(responses.requests[-2])
        assert ("functions", "lookup") not in names
        assert ("functions", "update_plan") in names
        assert "Plan updated" in tool_output(responses.requests[-1], "done-plan")
        state = await session.checklist()
        assert state is not None and state.model_dump()["plan"] == done

        responses.respond(function_call("lookup", {"key": "again"}, "lookup-again"))
        responses.respond(assistant_output("all"))
        assert (await (await session.submit("All")).get_result()).final_output == "all"
        names = declarations(responses.requests[-2])
        assert ("functions", "lookup") in names
        assert ("functions", "update_plan") in names
        assert effects == ["record", "again"]
    assert not responses.replies


async def test_checklist_cannot_be_granted_or_selected_as_a_mutable_leaf(
    responses: ResponsesFixture,
) -> None:
    @function_tool
    def lookup() -> str:
        return "record"

    checklist = Checklist()
    roster = [checklist, lookup]
    agent = Agent(
        instructions="Inspect.", model="gpt-5.5", tools=roster, provider=provider(responses)
    )
    async with timeout(20):
        async with agent.session() as session:
            with pytest.raises(CapabilityError, match="captured"):
                await session.submit("new grant", tools=[Checklist()])
            responses.respond(assistant_output("subset"))
            await (await session.submit("remove fixed", tools=[lookup])).get_result()
            assert ("functions", "update_plan") not in declarations(responses.requests[-1])
            responses.respond(assistant_output("retained"))
            await (await session.submit("lookup", tools=roster)).get_result()
        readonly = Agent(
            instructions="Inspect.", model="gpt-5.5", tools=[lookup], provider=agent.provider
        )
        async with readonly.session() as session:
            with pytest.raises(CapabilityError, match="captured"):
                await session.submit("grant", tools=roster)
            responses.respond(assistant_output("unchanged"))
            assert (
                await (await session.submit("Read", tools=[lookup])).get_result()
            ).final_output == "unchanged"
            assert ("functions", "lookup") in declarations(responses.requests[-1])
            assert ("functions", "update_plan") not in declarations(responses.requests[-1])
            assert await session.checklist() is None


async def test_checklist_updates_reads_idle_saves_and_requires_current_authority_on_restore(
    responses: ResponsesFixture,
) -> None:
    first = [
        {"step": "Read fixture", "status": "in_progress"},
        {"step": "Summarize", "status": "pending"},
    ]
    done = [
        {"step": "Read fixture", "status": "completed"},
        {"step": "Summarize", "status": "completed"},
    ]
    agent = Agent(
        instructions="Inspect.",
        model="gpt-5.5",
        tools=[Checklist()],
        provider=provider(responses),
    )
    async with timeout(40), agent.session() as session:
        assert await session.checklist() is None
        for index, plan in enumerate((first, done)):
            call_id = f"plan-{index}"
            responses.respond(
                function_call(
                    "update_plan", {"explanation": f"phase {index}", "plan": plan}, call_id
                )
            )
            responses.respond(assistant_output("accepted"))
            turn = await session.submit("Progress")
            assert (await turn.get_result()).final_output == "accepted"
            assert ("functions", "update_plan") in declarations(responses.requests[-2])
            assert "Plan updated" in tool_output(responses.requests[-1], call_id)
            latest = await session.checklist()
            assert isinstance(latest, ChecklistState)
            assert latest.model_dump() == {
                "thread_id": session.id,
                "turn_id": turn.turn_id,
                "explanation": f"phase {index}",
                "plan": plan,
            }
            # Removing advertisement leaves the accepted checklist readable.
            responses.respond(assistant_output("omitted"))
            await (await session.submit("empty", tools=[])).get_result()
            assert ("functions", "update_plan") not in declarations(responses.requests[-1])
            assert await session.checklist() == latest
        saved = await session.save_state()
        assert saved.root_checklist == latest
        assert saved.root_goal is None
    readonly = Agent(instructions="Inspect.", model="gpt-5.5", provider=agent.provider)
    async with readonly.session(state=saved) as restored:
        assert await restored.checklist() == latest
        responses.respond(
            function_call("update_plan", {"explanation": "not authorized", "plan": first}, "denied")
        )
        responses.respond(assistant_output("read only"))
        assert (await (await restored.submit("Continue")).get_result()).final_output == "read only"
        assert ("functions", "update_plan") not in declarations(responses.requests[-2])
        assert "Plan updated" not in tool_output(responses.requests[-1], "denied")
        assert await restored.checklist() == latest
        assert (await restored.save_state()).root_checklist == saved.root_checklist
        with pytest.raises(CapabilityError, match="captured"):
            await restored.submit("new grant", tools=[Checklist()])
    assert not responses.replies


async def test_local_checklist_reopens_explicit_empty_and_reports_storage_error(
    tmp_path: Path, responses: ResponsesFixture
) -> None:
    plan = [
        {"step": "Read", "status": "in_progress"},
        {"step": "Summarize", "status": "pending"},
    ]
    storage = LocalStorage(tmp_path / "profile")
    model_provider = provider(responses)
    agent = Agent(
        instructions="Inspect.", model="gpt-5.5", tools=[Checklist()], provider=model_provider
    )
    async with timeout(30), model_provider:
        async with agent.session(storage=storage) as session:
            assert await session.checklist() is None
            responses.respond(function_call("update_plan", {"plan": plan}, "plan"))
            responses.respond(assistant_output("Accepted"))
            turn = await session.submit("Record the plan")
            await turn.get_result()
            prior = await session.checklist()
            assert prior is not None and prior.turn_id == turn.turn_id
            assert prior.model_dump()["plan"] == plan

            invalid = [{"step": "Overwrite", "status": "invalid"}]
            responses.respond(function_call("update_plan", {"plan": invalid}, "invalid"))
            responses.respond(assistant_output("Rejected"))
            await (await session.submit("Reject the invalid update")).get_result()
            assert "Plan updated" not in tool_output(responses.requests[-1], "invalid")
            assert await session.checklist() == prior

            responses.respond(function_call("update_plan", {"plan": []}, "empty"))
            responses.respond(assistant_output("Cleared"))
            clear = await session.submit("Clear the plan")
            await clear.get_result()
            empty = await session.checklist()
            assert empty is not None and empty.plan == [] and empty.turn_id == clear.turn_id

        count = len(responses.requests)
        async with agent.session(storage=storage, resume=session.id, tools=[]) as reopened:
            assert await reopened.checklist() == empty
            path = storage.path / "sdk-checklists" / f"{session.id}.json"
            backup = path.rename(path.with_suffix(".saved"))
            try:
                path.write_text("unreadable checklist")
                with pytest.raises(RuntimeError, match="native checklist storage"):
                    await reopened.checklist()
            finally:
                backup.replace(path)
            assert await reopened.checklist() == empty
        assert len(responses.requests) == count


async def test_restored_checklist_with_explicit_destination_declaration(
    responses: ResponsesFixture,
) -> None:
    agent = Agent(
        instructions="Inspect.", model="gpt-5.5", tools=[Checklist()], provider=provider(responses)
    )
    first = [{"step": "Read", "status": "pending"}]
    async with agent.session() as session:
        responses.respond(function_call("update_plan", {"plan": first}, "plan-first"))
        responses.respond(assistant_output("first"))
        await (await session.submit("Read")).get_result()
        saved = await session.save_state()
    async with agent.session(state=saved) as restored:
        done = [{"step": "Read", "status": "completed"}]
        responses.respond(function_call("update_plan", {"plan": done}, "plan-next"))
        responses.respond(assistant_output("next"))
        turn = await restored.submit("Finish")
        assert (await turn.get_result()).final_output == "next"
        assert "Plan updated" in tool_output(responses.requests[-1], "plan-next")
        current = await restored.checklist()
        assert current is not None
        assert current.model_dump()["plan"] == done
        assert (await restored.save_state()).root_checklist == current


async def test_executor_example_using_installed_client_surface(
    tmp_path: Path, responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import os
    import sys

    script = Path(__file__).resolve().parents[3] / "examples" / "codex" / "remote_executor.py"
    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        roots = peer.root / "recipe-root"
        skill(roots, "recipe-skill", "RECIPE_ROOT_VIOLET")
        responses.respond(
            function_call(
                "update_plan",
                {"plan": [{"step": "Inspect example", "status": "completed"}]},
                "example-plan",
            )
        )
        responses.respond(assistant_output("example complete"))
        environment = {
            **os.environ,
            "EXECUTOR_URL": peer.url,
            "EXECUTOR_CWD": str(peer.root),
            "EXECUTOR_TOKEN": peer.token,
            "EXECUTOR_CAPABILITIES": str(roots),
            "OPENAI_BASE_URL": responses.url,
            "OPENAI_API_KEY": "synthetic-recipe-key",
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            "Inspect synthetic project",
            cwd=tmp_path,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert stdout.decode().splitlines() == ["example complete", "completed: Inspect example"]
        assert "RECIPE_ROOT_VIOLET" in json.dumps(responses.requests[0])
        offered = declarations(responses.requests[0])
        assert set(offered) >= {
            ("functions", "exec_command"),
            ("functions", "write_stdin"),
            ("functions", "update_plan"),
        }
        assert peer.token not in stdout.decode()
        await peer.wait_disconnected()
