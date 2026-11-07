"""Command network declarations and checkpoint restore authority."""

from __future__ import annotations

import hashlib
import json
import shlex
import sys
from contextlib import AsyncExitStack
from dataclasses import FrozenInstanceError, replace
from functools import partial
from pathlib import Path
from typing import Any, Literal

import pytest
from aiohttp import ClientTimeout, web
from aiohttp.test_utils import RawTestServer, TestClient

import agents
from agents import Agent, Environment, FunctionTool, LocalStorage, NativeError, NetworkPolicy
from agents._options import RuntimeNetworkPolicy
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import function_declarations, provider
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_provider_websockets import peer as model_peer


def test_empty_policy_is_distinct_from_the_default() -> None:
    assert "NetworkPolicy" in agents.__all__
    default = Environment(url="wss://executor.invalid", cwd="/job", tools=["exec_command"])
    assert default.network is None
    assert "network" not in default._to_native()
    empty = replace(default, network={})
    assert empty.network == NetworkPolicy()
    assert empty._to_native()["network"] == {
        "allowed_domains": [],
        "denied_domains": [],
        "allow_all_unix_sockets": False,
        "allow_local_binding": False,
    }


@pytest.mark.parametrize("typed", [True, False], ids=["policy", "dictionary"])
def test_environment_captures_network_policy(typed: bool) -> None:
    allowed = ["api.example.invalid", "BLOCKED.example.invalid."]
    denied = ["blocked.example.invalid"]
    fields: RuntimeNetworkPolicy = {
        "allowed_domains": allowed,
        "denied_domains": denied,
        "allow_local_binding": True,
    }
    policy = NetworkPolicy(**fields) if typed else fields
    if typed:
        allowed.append("changed-before-environment.example.invalid")
    selected = Environment(
        url="wss://executor.invalid",
        cwd="/job",
        tools=["exec_command"],
        network=policy,
    )
    allowed.append("later.example.invalid")
    denied.clear()
    fields["allow_local_binding"] = False
    assert isinstance(selected.network, NetworkPolicy)
    with pytest.raises(FrozenInstanceError):
        selected.network.allow_local_binding = False  # type: ignore[misc]
    assert selected._to_native()["network"] == {
        "allowed_domains": ["api.example.invalid"],
        "denied_domains": ["blocked.example.invalid"],
        "allow_all_unix_sockets": False,
        "allow_local_binding": True,
    }


@pytest.mark.parametrize("address", ["::ffff:192.0.2.1", "::ffff:c000:201", "[::ffff:c000:201]"])
def test_mapped_ipv6_policy_has_stable_wire_and_checkpoint_identity(address: str) -> None:
    environment = Environment(
        url="wss://executor.invalid",
        cwd="/job",
        tools=["exec_command"],
        network=NetworkPolicy(allowed_domains=["*.*"], denied_domains=[address]),
    )
    assert environment._to_native()["network"] == {
        "allowed_domains": ["*.*"],
        "denied_domains": ["::ffff:192.0.2.1"],
        "allow_all_unix_sockets": False,
        "allow_local_binding": False,
    }
    assert (
        environment._network_policy_digest()
        == "a7ffe370b28f51bcf208970d80428d867bb55e4462627e7d8d95c85531b15ae8"
    )
    denied = NetworkPolicy(allowed_domains=[address], denied_domains=["::ffff:192.0.2.1"])
    assert denied.allowed_domains == ()


@pytest.mark.parametrize(
    "network",
    [
        ["api.example.invalid"],
        {"proxy_port": 8080},
        {"allowed_domains": "api.example.invalid"},
        {"allowed_domains": ["https://api.example.invalid/job"]},
        {"allowed_domains": ["api.example.invalid:443"]},
        {"allowed_domains": ["::ffff:192.0.2.1%eth0"]},
        {"allowed_domains": ["*.[api].example.invalid"]},
        {"denied_domains": [True]},
        {"allow_local_binding": "false"},
    ],
)
def test_invalid_policy_rejected_at_declaration(network: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        Environment(
            url="wss://executor.invalid", cwd="/job", tools=["exec_command"], network=network
        )


@pytest.mark.parametrize("typed", [True, False], ids=["policy", "dictionary"])
@pytest.mark.parametrize("field", ["allowed_domains", "denied_domains"])
@pytest.mark.parametrize(
    "rule",
    [
        "\0private.example.invalid",
        "private.\0example.invalid",
        "private.example.invalid\0",
        "private.example.invalid\x1b",
        "private.example.invalid\x7f",
        "\tprivate.example.invalid",
        "private.exämple.invalid",
    ],
)
def test_malformed_host_rejected_at_declaration(
    rule: str, field: Literal["allowed_domains", "denied_domains"], typed: bool
) -> None:
    network: RuntimeNetworkPolicy = {"allowed_domains": ["*.example.invalid"]}
    network[field] = [rule]
    with pytest.raises(ValueError, match="ASCII host or host pattern") as failure:
        if typed:
            NetworkPolicy(**network)
        else:
            Environment(
                url="wss://executor.invalid", cwd="/job", tools=["exec_command"], network=network
            )
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("typed", [True, False], ids=["policy", "dictionary"])
@pytest.mark.parametrize("paths", [[], ["/run/job.sock"]], ids=["empty", "nonempty"])
def test_removed_socket_paths_rejected_at_declaration(typed: bool, paths: list[str]) -> None:
    network: Any = {"allow_unix_sockets": paths}
    with pytest.raises(TypeError, match="unexpected keyword argument 'allow_unix_sockets'"):
        if typed:
            NetworkPolicy(**network)
        else:
            Environment(
                url="wss://executor.invalid", cwd="/job", tools=["exec_command"], network=network
            )


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
async def test_checkpoint_requires_the_current_effective_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        provider_key = "synthetic-model-credential"
        agent = Agent(
            instructions="Answer.", provider=model.provider(websocket=False, key=provider_key)
        )
        policy = NetworkPolicy(
            allowed_domains=["b.example.invalid", "A.example.invalid.", "blocked.example.invalid"],
            denied_domains=["blocked.example.invalid", "::ffff:192.0.2.1"],
            allow_all_unix_sockets=True,
            allow_local_binding=True,
        )
        selected = Environment(
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            network=policy,
        )
        async with agent.session(environment=selected) as session:
            model.respond(assistant_output("ready"))
            assert (await (await session.submit("Start.")).get_result()).final_output == "ready"
            encoded = (await session.save_state()).to_json()
            [receipt] = [turn["receipt"] async for turn in session.turns() if "receipt" in turn]
            assert receipt["executors"]
            network = receipt["executors"][0]["network_policy"]
            assert network is not None and network["domains"] is not None
            assert network["domains"]["a.example.invalid"] == "allow"
        for private_value in ("example.invalid", "192.0.2.1", executor.token, provider_key):
            assert private_value not in encoded
        saved = SessionState.from_json(encoded)
        equivalent = replace(
            policy,
            allowed_domains=["a.example.invalid", "b.example.invalid", "a.example.invalid"],
            denied_domains=["::ffff:c000:201", "blocked.example.invalid"],
        )
        async with agent.session(
            state=saved, environment=replace(selected, network=equivalent)
        ) as restored:
            assert restored.id == saved.source_session_id
            assert [turn["receipt"] async for turn in restored.turns() if "receipt" in turn] == [
                {**receipt, "executors": None}
            ]
        attempts = executor.attempts
        changes = [
            replace(policy, denied_domains=[]),
            replace(policy, allow_local_binding=False),
            replace(policy, allow_all_unix_sockets=False),
        ]
        for changed in changes:
            with pytest.raises(ValueError, match="Checkpoint and Environment must agree"):
                async with agent.session(
                    state=saved, environment=replace(selected, network=changed)
                ):
                    pytest.fail("Opened a session with a different network policy")
            assert executor.attempts == attempts


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
@pytest.mark.parametrize(
    "mode", ["excluded", "legacy-excluded", "selected", "default", "strict", "network"]
)
async def test_manual_checkpoint_binds_admitted_executor_tools(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(
            instructions="Use lookup.", tools=[lookup], provider=model.provider(websocket=False)
        )
        environment = Environment(
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            writable_roots=[] if mode in ("strict", "default") else None,
            write_policy="strict" if mode in ("strict", "default") else "legacy",
            network=NetworkPolicy(allow_all_unix_sockets=True) if mode == "network" else None,
        )
        model.respond(function_call("lookup", {"key": "A"}, "call-A"))
        async with agent.session(environment=environment) as session:
            tools = (
                None
                if mode == "default"
                else [lookup, environment]
                if mode == "selected"
                else [lookup]
            )
            await session.submit("Look up A", tools=tools)
            await next_request(session.interactions(), lookup)
            saved = await session.suspend()

        if mode == "legacy-excluded":
            saved = saved.model_copy(
                update={
                    "executor_policy_digest": hashlib.sha256(
                        b"implicit-remote-workspace"
                    ).hexdigest()
                }
            )
        saved = SessionState.from_json(saved.to_json())
        excluded = mode in ("excluded", "legacy-excluded")
        attempts = executor.attempts
        if not excluded:
            with pytest.raises(
                ValueError, match="Checkpoint and Environment must agree|must have been allowed"
            ):
                async with agent.session(state=saved):
                    pytest.fail("Restored a checkpoint without its executor policy")
        model.respond(assistant_output("continued"))
        async with agent.session(
            state=saved, environment=None if excluded else environment
        ) as restored:
            current = await next_request(restored.interactions(), lookup)
            assert current.call_id == "call-A"
            await current.respond("answer-A")
            assert (await restored.restored_turn.get_result()).final_output == "continued"
        assert tool_output(model.requests[1], "call-A") == "answer-A"
        for request in model.requests:
            assert any(
                tool.get("name") == "exec_command" for tool in function_declarations(request)
            ) == (mode in ("selected", "default"))
        assert (saved.executor_policy_digest is None) == (mode == "excluded")
        assert (saved.network_policy_digest is not None) == (mode == "network")
        if excluded:
            assert executor.attempts == attempts


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
async def test_checkpoint_requires_explicit_strict_writes_even_without_extra_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        selected = Environment(
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            writable_roots=[],
        )
        async with agent.session(environment=selected) as session:
            saved = await session.save_state()
        with pytest.raises(ValueError, match="agree on the applied executor policy"):
            async with agent.session(
                state=saved,
                environment=replace(selected, write_policy="legacy", writable_roots=None),
            ):
                pytest.fail("Opened a checkpoint with legacy write permissions")
        extra = executor.root / "replacement"
        extra.mkdir()
        assert saved.executor_policy_digest is not None
        incomplete = SessionState.from_json(
            saved.model_copy(update={"executor_policy_digest": None}).to_json()
        )
        before = len(executor.requests)
        with pytest.raises(ValueError, match="agree on the applied executor policy"):
            async with agent.session(
                state=incomplete, environment=replace(selected, writable_roots=[extra])
            ):
                pytest.fail("Opened a strict checkpoint without its policy identity")
        assert len(executor.requests) == before
        async with agent.session(state=saved, environment=selected) as restored:
            assert restored.id == saved.source_session_id
        async with agent.session(
            state=saved, environment=replace(selected, writable_roots=None)
        ) as restored:
            assert restored.id == saved.source_session_id


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
@pytest.mark.parametrize("recorded_mode", ["implicit", "strict", "legacy"])
async def test_local_resume_matches_recorded_write_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded_mode: str
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        # Host storage and remote executor paths are separate namespaces.
        storage = LocalStorage(executor.root / "profile")
        implicit = Environment(
            write_policy="legacy",
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
        )
        strict = replace(implicit, write_policy="strict", writable_roots=[])
        default_strict = Environment(
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
        )
        source, changed = (strict, implicit) if recorded_mode == "strict" else (implicit, strict)
        model.respond(assistant_output("original"))
        async with agent.session(storage=storage, environment=source) as session:
            session_id = session.id
            assert (await (await session.submit("Start.")).get_result()).final_output == "original"

        async with agent.session(
            storage=storage, resume=session_id, environment=source
        ) as reopened:
            assert reopened.id == session_id

        if recorded_mode == "strict":
            async with agent.session(
                storage=storage, resume=session_id, environment=default_strict
            ) as reopened:
                assert reopened.id == session_id

        [rollout] = (storage.path / "sessions").rglob(f"*{session_id}.jsonl")
        rows = [json.loads(line) for line in rollout.read_text().splitlines()]
        [receipt] = [
            row["payload"]["sdk_receipt"]
            for row in rows
            if row["type"] == "turn_context" and row["payload"].get("sdk_receipt")
        ]
        assert receipt["strict_executor_writes"] is (recorded_mode == "strict")
        assert len(receipt["executors"]) == 1
        if recorded_mode == "legacy":
            # Pre-marker records can still reopen with the same applied profile.
            del receipt["strict_executor_writes"]
            rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))
            async with agent.session(
                storage=storage, resume=session_id, environment=implicit
            ) as reopened:
                assert reopened.id == session_id
            with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
                async with agent.session(
                    storage=storage, resume=session_id, environment=default_strict
                ):
                    pytest.fail("Upgraded a legacy receipt to strict writes")
        else:
            with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
                async with agent.session(storage=storage, resume=session_id, environment=changed):
                    pytest.fail("Reopened a session with a different recorded write mode")
        assert len(model.requests) == 1


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
async def test_local_resume_requires_explicit_strict_writes_even_without_extra_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        storage = LocalStorage(tmp_path / "profile")
        selected = Environment(
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            writable_roots=[],
        )
        extra = executor.root / "extra"
        extra.mkdir()
        opening = (
            selected if sys.platform == "darwin" else replace(selected, writable_roots=[extra])
        )
        model.respond(assistant_output("ready"))
        model.respond(assistant_output("cleared"))
        async with agent.session(storage=storage, environment=opening) as session:
            session_id = session.id
            assert (await (await session.submit("Start.")).get_result()).final_output == "ready"
            if sys.platform == "darwin":
                added = await session.submit("Add an extra root.", writable_roots=[extra.resolve()])
                with pytest.raises(
                    NativeError, match="macOS executors require fixed writable roots"
                ) as refused:
                    await added.get_result()
                assert refused.value.effects == "not_started"
                assert len(model.requests) == 1
            changed = await session.submit("Clear extra roots.", writable_roots=[])
            assert (await changed.get_result()).final_output == "cleared"
            first, last = [turn["receipt"] async for turn in session.turns() if "receipt" in turn]
            assert first["executors"] and last["executors"]
            opening_roots = {executor.root.resolve().as_uri()}
            if sys.platform == "linux":
                opening_roots.add(extra.resolve().as_uri())
            assert set(first["executors"][0]["writable_roots"]) == opening_roots
            assert last["executors"][0]["writable_roots"] == [executor.root.resolve().as_uri()]
        async with agent.session(
            storage=storage, resume=session_id, environment=selected
        ) as resumed:
            assert resumed.id == session_id
        with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
            async with agent.session(
                storage=storage,
                resume=session_id,
                environment=replace(selected, write_policy="legacy", writable_roots=None),
            ):
                pytest.fail("Reopened a Session with legacy write permissions")
        assert len(model.requests) == 2


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
@pytest.mark.parametrize(
    "corruption",
    [
        "empty",
        "missing",
        "empty_downgrade",
        "missing_policy_fields",
        "identity_missing",
        "identity_malformed",
        "identity_replaced",
        "identity_missing_with_only_executor_list",
    ],
)
async def test_local_resume_rejects_incomplete_strict_executor_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        storage = LocalStorage(tmp_path / "profile")
        selected = Environment(
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            writable_roots=[],
        )
        async with agent.session(storage=storage, environment=selected) as session:
            session_id = session.id
            for text in ("First", "Second"):
                model.respond(assistant_output(text))
                assert (await (await session.submit(text)).get_result()).final_output == text

        [rollout] = (storage.path / "sessions").rglob(f"*{session_id}.jsonl")
        rows = [json.loads(line) for line in rollout.read_text().splitlines()]
        receipts = [
            row["payload"]["sdk_receipt"]
            for row in rows
            if row["type"] == "turn_context"
            and row["payload"].get("sdk_receipt", {}).get("thread_id") == session_id
        ]
        assert len(receipts) == 2
        first, latest = receipts
        assert latest["strict_executor_writes"] is True
        assert latest["executors"]
        if corruption.startswith("identity_"):
            if corruption == "identity_missing":
                del latest["thread_id"]
            elif corruption == "identity_malformed":
                latest["thread_id"] = 42
            elif corruption == "identity_replaced":
                latest["thread_id"] = "00000000-0000-4000-8000-000000000000"
            else:
                del latest["thread_id"]
                del latest["codex_source_tree"]
                del latest["strict_executor_writes"]
        elif corruption in ("missing", "missing_policy_fields"):
            del latest["executors"]
            if corruption == "missing_policy_fields":
                del latest["strict_executor_writes"]
        else:
            latest["executors"] = []
            if corruption == "empty_downgrade":
                latest["strict_executor_writes"] = False
        if corruption.startswith("identity_") or corruption == "missing_policy_fields":
            rows = [row for row in rows if row["payload"].get("sdk_receipt") is not first]
        rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

        for environment in (
            None,
            selected,
            replace(selected, write_policy="legacy", writable_roots=None),
        ):
            with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
                async with agent.session(
                    storage=storage, resume=session_id, environment=environment
                ):
                    pytest.fail("Reopened a Session with an incomplete strict executor receipt")
        assert len(model.requests) == 2


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
@pytest.mark.parametrize(
    "history", ["strict", "strict_extra", "implicit", "model_only", "before_sampling", "empty"]
)
async def test_local_resume_without_executor_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history: str
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        storage = LocalStorage(tmp_path / "profile")
        implicit = Environment(
            write_policy="legacy",
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
        )
        extra = tmp_path / "extra"
        extra.mkdir()
        strict = replace(
            implicit,
            write_policy="strict",
            writable_roots=[str(extra)] if history == "strict_extra" else [],
        )
        source: Environment[Any] | None = implicit
        if history == "model_only":
            source = None
        elif history in ("strict", "strict_extra"):
            source = strict
        async with agent.session(storage=storage, environment=source) as session:
            session_id = session.id
            model.respond(assistant_output("Original"))
            assert (await (await session.submit("Start.")).get_result()).final_output == "Original"

        [rollout] = (storage.path / "sessions").rglob(f"*{session_id}.jsonl")
        rows = [json.loads(line) for line in rollout.read_text().splitlines()]
        [receipt_index] = [
            index
            for index, row in enumerate(rows)
            if row["type"] == "turn_context"
            and row["payload"].get("sdk_receipt", {}).get("thread_id") == session_id
        ]
        if history == "empty":
            assert rows[0]["type"] == "session_meta"
            rows = rows[:1]
        elif history == "before_sampling":
            completion = next(
                row
                for row in rows
                if row["type"] == "event_msg" and row["payload"]["type"] == "task_complete"
            )
            completion["payload"] = {
                "type": "task_complete",
                "turn_id": completion["payload"]["turn_id"],
                "last_agent_message": None,
                "error": {"message": "Failed before sampling"},
            }
            rows = [*rows[:receipt_index], completion]
        else:
            del rows[receipt_index]
        rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

        async def reopen(environment: Environment[Any] | None) -> None:
            async with agent.session(
                storage=storage, resume=session_id, environment=environment
            ) as resumed:
                assert resumed.id == session_id

        policy_error = "recorded executor write and network policy"
        for environment in (None, implicit):
            if history == "strict_extra":
                with pytest.raises(RuntimeError, match=policy_error):
                    await reopen(environment)
            else:
                await reopen(environment)
        if history == "implicit":
            explicit_network = replace(implicit, network=NetworkPolicy(allow_all_unix_sockets=True))
            with pytest.raises(RuntimeError, match=policy_error):
                await reopen(explicit_network)
        if history in ("before_sampling", "empty"):
            await reopen(strict)
        else:
            with pytest.raises(RuntimeError, match=policy_error):
                await reopen(strict)
        assert len(model.requests) == 1


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
@pytest.mark.parametrize("explicit_network", [False, True])
async def test_local_resume_allows_implicit_remote_workspace_to_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_network: bool
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        storage = LocalStorage(tmp_path / "profile")
        network = NetworkPolicy(allow_all_unix_sockets=True) if explicit_network else None
        original = Environment(
            write_policy="legacy",
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            network=network,
        )
        relocated = executor.root / "relocated"
        relocated.mkdir()
        current = replace(original, cwd=str(relocated))
        model.respond(assistant_output("ready"))
        model.respond(assistant_output("reopened"))
        async with agent.session(storage=storage, environment=original) as session:
            session_id = session.id
            assert (await (await session.submit("Start.")).get_result()).final_output == "ready"

        changed_network = None if explicit_network else NetworkPolicy(allow_all_unix_sockets=True)
        with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
            async with agent.session(
                storage=storage,
                resume=session_id,
                environment=replace(current, network=changed_network),
            ):
                pytest.fail("Reopened a Session with different network authority")

        [rollout] = (storage.path / "sessions").rglob(f"*{session_id}.jsonl")
        history = rollout.read_text()
        rows = [json.loads(line) for line in history.splitlines()]
        [receipt] = [
            row["payload"]["sdk_receipt"]
            for row in rows
            if row["type"] == "turn_context" and row["payload"].get("sdk_receipt")
        ]
        [recorded] = receipt["executors"]
        # A later receipt format may add authority fields. Only a known,
        # cwd-derived policy may move with the workspace.
        authority_parts = [recorded, recorded["permission_profile"]]
        if explicit_network:
            authority_parts.append(recorded["network_policy"])
        for mapping in authority_parts:
            mapping["future_authority"] = "requires-review"
            rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
                async with agent.session(storage=storage, resume=session_id, environment=current):
                    pytest.fail("Reopened a Session with unknown executor authority")
            with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
                async with agent.session(storage=storage, resume=session_id):
                    pytest.fail("Dropped an executor with unknown authority")
            del mapping["future_authority"]

        for field, malformed in (("executors", None), ("strict_executor_writes", "false")):
            original_value = receipt[field]
            receipt[field] = malformed
            rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
                async with agent.session(storage=storage, resume=session_id):
                    pytest.fail("Dropped an executor with a malformed policy receipt")
            receipt[field] = original_value

        # Older receipts omitted the strict-write marker.
        del receipt["strict_executor_writes"]
        rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

        async with agent.session(
            storage=storage, resume=session_id, environment=current
        ) as resumed:
            assert resumed.id == session_id
            result = await (await resumed.submit("Continue.")).get_result()
            assert result.final_output == "reopened"
            receipts = [turn["receipt"] async for turn in resumed.turns() if "receipt" in turn]
            assert receipts[-1]["executors"]
            assert receipts[-1]["executors"][0]["cwd"] == relocated.resolve().as_uri()


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
@pytest.mark.parametrize("explicit_network", [False, True])
async def test_local_resume_can_drop_an_implicit_remote_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_network: bool
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        storage = LocalStorage(tmp_path / "profile")
        selected = Environment(
            write_policy="legacy",
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            network=NetworkPolicy(allow_all_unix_sockets=True) if explicit_network else None,
        )
        model.respond(assistant_output("ready"))
        async with agent.session(storage=storage, environment=selected) as session:
            session_id = session.id
            assert (await (await session.submit("Start.")).get_result()).final_output == "ready"

        model.respond(assistant_output("model only"))
        async with agent.session(storage=storage, resume=session_id) as resumed:
            assert resumed.id == session_id
            result = await (await resumed.submit("Continue without command tools.")).get_result()
            assert result.final_output == "model only"
            receipts = [turn["receipt"] async for turn in resumed.turns() if "receipt" in turn]
            assert receipts[-1]["executors"] == []
            assert "exec_command" not in {
                tool.get("name") for tool in model.requests[-1].get("tools", [])
            }

        with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
            async with agent.session(
                storage=storage,
                resume=session_id,
                environment=replace(selected, write_policy="strict", writable_roots=[]),
            ):
                pytest.fail("Reopened a model-only Session with strict write grants")
        changed_network = None if explicit_network else NetworkPolicy(allow_all_unix_sockets=True)
        with pytest.raises(RuntimeError, match="recorded executor write and network policy"):
            async with agent.session(
                storage=storage,
                resume=session_id,
                environment=replace(selected, network=changed_network),
            ):
                pytest.fail("A model-only turn replaced the earlier executor's network policy")
        model.respond(assistant_output("still model-only"))
        async with agent.session(storage=storage, resume=session_id) as resumed:
            result = await (await resumed.submit("Continue without an executor.")).get_result()
            assert result.final_output == "still model-only"


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
@pytest.mark.parametrize("retarget_cwd", [False, True], ids=["additional-root", "workspace"])
async def test_supplied_compaction_keeps_the_applied_write_policy_after_retargeting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retarget_cwd: bool
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        original, replacement = tmp_path / "original", tmp_path / "replacement"
        original.mkdir()
        replacement.mkdir()
        alias = executor.root / "write-root"
        alias.symlink_to(original, target_is_directory=True)
        selected = Environment(
            url=executor.url,
            cwd=str(alias if retarget_cwd else executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            writable_roots=[executor.root] if retarget_cwd else [alias],
        )
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        model.respond(assistant_output("ready"))
        model.respond(assistant_output("continued"))
        async with agent.session(environment=selected) as session:
            initial = await session.submit("Start.", writable_roots=selected.writable_roots)
            assert (await initial.get_result()).final_output == "ready"
            alias.unlink()
            alias.symlink_to(replacement, target_is_directory=True)
            if retarget_cwd:
                with pytest.raises(
                    NativeError, match="executor working directory changed"
                ) as refused:
                    await (
                        await session.submit("Clear extra roots.", writable_roots=[])
                    ).get_result()
                assert refused.value.effects == "not_started"
                with pytest.raises(RuntimeError, match="compaction cannot change the applied"):
                    await session.compact(summary="A caller supplied this summary.")
            else:
                await session.compact(summary="A caller supplied this summary.")
            assert len(model.requests) == 1
            assert (
                await (await session.submit("Continue.")).get_result()
            ).final_output == "continued"
            policies = [
                turn["receipt"]["executors"][0]
                async for turn in session.turns()
                if "receipt" in turn and turn["receipt"]["executors"]
            ]
            assert len(policies) == 2
            expected_roots = {original.resolve().as_uri(), executor.root.resolve().as_uri()}
            assert set(policies[0]["writable_roots"]) == expected_roots
            assert policies[1] == policies[0]


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
@pytest.mark.parametrize("transition", ["live", "compaction", "reopen"])
async def test_repeated_write_root_alias_cannot_grant_a_new_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    async with (
        timeout(45),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
        AsyncExitStack() as sessions,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        original, replacement, added = [
            tmp_path / name for name in ("original", "replacement", "added")
        ]
        for directory in (original, replacement, added):
            directory.mkdir()
        replacement, added = replacement.resolve(), added.resolve()
        alias = executor.root / "write-root"
        alias.symlink_to(original, target_is_directory=True)
        selected = Environment(
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            writable_roots=[alias, added] if sys.platform == "darwin" else [alias],
        )
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        storage = LocalStorage(tmp_path / "profile") if transition == "reopen" else None
        session = await sessions.enter_async_context(
            agent.session(environment=selected, storage=storage)
        )
        model.respond(assistant_output("original"))
        assert (await (await session.submit("Start.")).get_result()).final_output == "original"
        model.respond(assistant_output("stable alias"))
        mixed = await session.submit("Select roots with the alias.", writable_roots=[alias, added])
        assert (await mixed.get_result()).final_output == "stable alias"
        if transition == "compaction":
            await session.compact(summary="A caller supplied this summary.")
        elif transition == "reopen":
            identity = session.id
            await sessions.aclose()
            session = await sessions.enter_async_context(
                agent.session(
                    environment=replace(selected, writable_roots=[alias, added]),
                    storage=storage,
                    resume=identity,
                )
            )
        alias.unlink()
        alias.symlink_to(replacement, target_is_directory=True)
        model.respond(assistant_output("kept prior grants"))
        repeated = await session.submit("Keep the same roots.", writable_roots=[alias, added])
        try:
            await repeated.get_result()
        except NativeError as error:
            assert "executor write root changed" in str(error)
            assert error.effects == "not_started"
        else:
            policies = [turn["receipt"]["executors"][0] async for turn in session.turns()]
            pytest.fail(f"Repeated alias selected {policies[-1]['writable_roots']}")
        assert len(model.requests) == 2
        implicit = await session.submit("Keep the current grants.")
        assert (await implicit.get_result()).final_output == "kept prior grants"
        model.respond(assistant_output("explicit replacement"))
        explicit = await session.submit(
            "Grant the replacement directory explicitly.", writable_roots=[replacement, added]
        )
        if sys.platform == "darwin":
            with pytest.raises(
                NativeError, match="macOS executors require fixed writable roots"
            ) as refused:
                await explicit.get_result()
            assert refused.value.effects == "not_started"
            assert len(model.requests) == 3
        else:
            assert (await explicit.get_result()).final_output == "explicit replacement"
        policies = [turn["receipt"]["executors"][0] async for turn in session.turns()]
        workspace = executor.root.resolve().as_uri()
        common = {workspace, added.resolve().as_uri()}
        assert set(policies[-2]["writable_roots"]) == common | {original.resolve().as_uri()}
        final_root = original if sys.platform == "darwin" else replacement
        assert set(policies[-1]["writable_roots"]) == common | {final_root.resolve().as_uri()}


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
@pytest.mark.parametrize("regrant", [False, True], ids=["first-grant", "regrant"])
async def test_new_write_root_alias_cannot_widen_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, regrant: bool
) -> None:
    if regrant and sys.platform == "darwin":
        pytest.skip("requires revoking a root between turns")
    async with (
        timeout(45),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        alias = executor.root / "output"
        original = tmp_path / "original"
        original.mkdir()
        if regrant:
            alias.symlink_to(original, target_is_directory=True)
        environment = Environment(
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            writable_roots=[alias] if regrant else [],
        )
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        async with agent.session(environment=environment) as session:
            model.respond(assistant_output("ready"))
            assert (await (await session.submit("Start.")).get_result()).final_output == "ready"
            if regrant:
                model.respond(assistant_output("revoked"))
                revoked = await session.submit("Revoke the additional root.", writable_roots=[])
                assert (await revoked.get_result()).final_output == "revoked"
                alias.unlink()

            # The executor workspace stays writable. A new alias there may name
            # a sibling outside the workspace or target its protected metadata.
            target = tmp_path / "other" if regrant else executor.root / ".git"
            target.mkdir()
            alias.symlink_to(target, target_is_directory=True)
            prior_requests = len(model.requests)
            model.respond(assistant_output("canonical target"))
            refused = await session.submit("Grant the output path.", writable_roots=[alias])
            with pytest.raises(
                NativeError, match="new executor write roots must use their canonical"
            ) as error:
                await refused.get_result()
            assert error.value.effects == "not_started"
            assert len(model.requests) == prior_requests

            canonical = await session.submit(
                "Grant the target explicitly.", writable_roots=[target.resolve()]
            )
            if sys.platform == "darwin":
                with pytest.raises(
                    NativeError, match="macOS executors require fixed writable roots"
                ) as refused:
                    await canonical.get_result()
                assert refused.value.effects == "not_started"
                assert len(model.requests) == prior_requests
                return
            assert (await canonical.get_result()).final_output == "canonical target"
            model.respond(assistant_output("same grant"))
            repeated = await session.submit("Alias the same target.", writable_roots=[alias])
            assert (await repeated.get_result()).final_output == "same grant"
            policies = [turn["receipt"]["executors"][0] async for turn in session.turns()]
            expected_roots = {executor.root.resolve().as_uri(), target.resolve().as_uri()}
            assert set(policies[-2]["writable_roots"]) == expected_roots
            assert policies[-1] == policies[-2]


async def test_checkpoint_without_network_policy_restores_only_with_the_default() -> None:
    async with timeout(20), model_peer() as model:
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        async with agent.session() as session:
            encoded = (await session.save_state()).to_json()
        assert "network_policy_digest" not in encoded
        assert "executor_policy_digest" not in encoded
        legacy = SessionState.from_json(encoded)
        async with agent.session(state=legacy) as restored:
            assert restored.id == legacy.source_session_id
        empty = Environment(
            url="wss://unused.invalid", cwd="/job", tools=["exec_command"], network={}
        )
        with pytest.raises(ValueError, match="Checkpoint and Environment must agree"):
            async with agent.session(state=legacy, environment=empty):
                pytest.fail("Opened a legacy checkpoint with an explicit policy")


_LINUX_NETWORK_PROBE = """\
import errno
import os
import socket
import sys
from contextlib import closing
from http.client import HTTPConnection
from urllib.parse import urlsplit

allowed, overlap, unlisted = sys.argv[1:]
proxy = urlsplit(os.environ["HTTP_PROXY"])
for url, expected in [(allowed, 200), (overlap, 403), (unlisted, 403)]:
    with closing(HTTPConnection(proxy.hostname, proxy.port, timeout=3)) as connection:
        connection.request("GET", url)
        response = connection.getresponse()
        assert response.status == expected, (url, response.status)
        if expected == 200:
            assert response.read() == b"127.0.0.1"

target = urlsplit(allowed)
try:
    with socket.create_connection((target.hostname, target.port), timeout=3):
        raise AssertionError("Direct connection to the allowed destination succeeded")
except OSError as error:
    assert error.errno in (
        errno.EPERM, errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH
    ), error
"""


@pytest.mark.requires_native_linux_sandbox
async def test_linux_network_policy_enforces_proxy_and_blocks_direct_access(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed, overlap, unlisted = "127.0.0.1", "127.0.0.2", "127.0.0.3"
    received: list[str] = []

    async def record(host: str, _request: web.BaseRequest) -> web.Response:
        received.append(host)
        return web.Response(text=host)

    async with (
        timeout(30),
        AsyncExitStack() as listeners,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        urls = []
        for host in (allowed, overlap, unlisted):
            server = RawTestServer(partial(record, host), host=host)
            control = await listeners.enter_async_context(TestClient(server))
            async with control.get("/", timeout=ClientTimeout(total=3)) as response:
                assert response.status == 200 and await response.text() == host
            urls.append(str(control.make_url("/")))
        received.clear()

        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        environment = Environment(
            url=executor.url,
            cwd=str(executor.root),
            headers={"Authorization": "Bearer " + executor.token},
            tools=["exec_command"],
            network=NetworkPolicy(
                allowed_domains=[allowed, overlap],
                denied_domains=[overlap],
                allow_all_unix_sockets=True,
            ),
        )
        command = shlex.join(["python3", "-I", "-c", _LINUX_NETWORK_PROBE, *urls])
        responses.respond(
            function_call("exec_command", {"cmd": command, "yield_time_ms": 20_000}, "probe")
        )
        responses.respond(assistant_output("done"))
        agent = Agent(instructions="Probe the network policy.", provider=provider(responses))
        await agent.run("Run the probe.", environment=environment)
        assert "Process exited with code 0" in tool_output(responses.requests[-1], "probe")
        assert received == [allowed]


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
async def test_default_strict_rejects_unsupported_remote_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote", advertised_platform="windows").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        with pytest.raises(
            RuntimeError, match="strict executor write policy requires a Linux or macOS executor"
        ):
            async with agent.session(
                environment=Environment(
                    url=executor.url,
                    cwd=str(executor.root),
                    headers={"Authorization": "Bearer " + executor.token},
                )
            ):
                pytest.fail("Opened a strict Environment on an unsupported executor")
        assert not model.requests
        assert not any(request.get("method") == "process/start" for request in executor.requests)


# Strict file effects launch the real platform filesystem sandbox.
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
async def test_default_strict_allows_workspace_patch_but_denies_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        inside, outside = executor.root / "allowed.txt", tmp_path / "denied.txt"
        for path, call_id in ((Path("allowed.txt"), "inside"), (outside, "outside")):
            model.respond(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "custom_tool_call",
                        "name": "apply_patch",
                        "call_id": call_id,
                        "input": (
                            f"*** Begin Patch\n*** Add File: {path}\n"
                            "+synthetic-policy-effect\n*** End Patch"
                        ),
                    },
                }
            )
        model.respond(assistant_output("done"))
        agent = Agent(instructions="Apply patches.", provider=model.provider(websocket=False))
        async with agent.session(
            environment=Environment(
                url=executor.url,
                cwd=str(executor.root),
                tools=["apply_patch"],
                headers={"Authorization": "Bearer " + executor.token},
            )
        ) as session:
            assert (
                await (await session.submit("Apply patches.")).get_result()
            ).final_output == "done"
        assert inside.exists(), tool_output(model.requests[1], "inside", "custom_tool_call_output")
        assert inside.read_text() == "synthetic-policy-effect\n"
        assert not outside.exists()


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
async def test_saved_legacy_checkpoint_requires_explicit_legacy_before_executor_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        selected = Environment(
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            write_policy="legacy",
        )
        async with agent.session(environment=selected) as session:
            saved = SessionState.from_json((await session.save_state()).to_json())
        attempts = executor.attempts
        with pytest.raises(ValueError, match="agree on the applied executor policy"):
            async with agent.session(
                state=saved,
                environment=Environment(
                    url=executor.url,
                    cwd=str(executor.root),
                    tools=["exec_command"],
                    headers={"Authorization": "Bearer " + executor.token},
                ),
            ):
                pytest.fail("Opened legacy state with the strict default")
        assert executor.attempts == attempts
        async with agent.session(state=saved, environment=selected) as reopened:
            assert reopened.id == saved.source_session_id
        assert not model.requests
