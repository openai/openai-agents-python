"""Local process restart never treats descendant receipts as current authority."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Literal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    Environment,
    LocalRecoveryRequired,
    LocalStorage,
    NativeAgents,
    NativeError,
    NativeProvider,
    NetworkPolicy,
    ReconciledCall,
    ToolContext,
    _native,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, native_options, tool_output
from tests.test_native_child_agents import ChildPeer, collaboration, handles, metadata
from tests.test_native_local_storage import _collect

DESCENDANT_MODEL = "gpt-6.1-sol"


class LocalFamilyPeer(ChildPeer):
    def __init__(self, depth: int, *, fork_turns: Literal["none", "all"] = "none") -> None:
        super().__init__(("default",))
        self.depth = depth
        self.fork_turns = fork_turns
        self.followup = False
        for release in self.release.values():
            release.set()

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        path = metadata(body, self.request_headers[-1])["agent_name"]
        received = {
            item.get("call_id")
            for item in body.get("input", [])
            if item.get("type") in {"function_call_output", "custom_tool_call_output"}
        }
        if self.request_headers[-1]["authorization"] == "Bearer destination":
            if self.followup:
                if path == "/root/alpha":
                    assert (
                        tool_output(body, "source-effect") == "Confirmed by the application journal"
                    )
                    return [function_call("lookup", {"key": "duplicate"}, "source-effect")]
                assert path == "/root"
                if "wake-reconciled" not in received:
                    return [
                        collaboration(
                            "followup_task",
                            {"target": "/root/alpha", "message": "Start new work."},
                            "wake-reconciled",
                        )
                    ]
                return [assistant_output("follow-up submitted")]
            assert path == "/root", "Inspecting descendants restarted old native work"
            if "fresh-current" not in received:
                return [function_call("lookup", {"key": "fresh"}, "fresh-current")]
            return [assistant_output("destination finished")]
        level = path.count("/") - 1
        if level < self.depth:
            name = "alpha" if level == 0 else "grandchild"
            if "spawn-" + name not in received:
                return [
                    collaboration(
                        "spawn_agent",
                        {
                            "task_name": name,
                            "message": "Record a memory.",
                            "fork_turns": self.fork_turns,
                        },
                        "spawn-" + name,
                    )
                ]
            # A denied spawn cannot create an effect. Surface its real receipt
            # instead of silently waiting for a child that did not start.
            returned = tool_output(body, "spawn-" + name)
            assert "/root/" in returned, (path, returned)
        elif "source-effect" not in received:
            return [function_call("lookup", {"key": path}, "source-effect")]
        return [assistant_output(path + " finished")]


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX executor")
@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_local_descendant_receipts_include_the_selected_proxy_policy(tmp_path, monkeypatch):
    peer = LocalFamilyPeer(depth=2)
    original_response = peer.response

    def response():
        body, headers = peer.requests[-1], peer.request_headers[-1]
        if metadata(body, headers)["agent_name"] != "/root/alpha/grandchild":
            return original_response()
        if any(item.get("call_id") == "probe-proxy" for item in body.get("input", [])):
            assert "proxy active" in tool_output(body, "probe-proxy")
            return [assistant_output("Grandchild used the proxy.")]
        return [
            function_call(
                "exec_command",
                {"cmd": 'test -n "$HTTP_PROXY" && printf "proxy active"'},
                "probe-proxy",
            )
        ]

    monkeypatch.setattr(peer, "response", response)
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    workspace, extra = tmp_path / "work", tmp_path / "extra"
    workspace.mkdir()
    extra.mkdir()
    storage = LocalStorage(tmp_path / "profile")
    async with timeout(75), TestServer(app, shutdown_timeout=1) as server:
        peer.url = str(server.make_url("/v1"))
        agent = Agent(
            instructions="Delegate the work.",
            model=DESCENDANT_MODEL,
            provider=peer.provider(websocket=False),
            programmatic_tool_calling=False,
            tools=[NativeAgents(max_children=2, max_depth=2)],
        )
        environment = Environment.local(
            cwd=workspace,
            writable_roots=[extra],
            tools=["exec_command", "write_stdin", "apply_patch"],
            network=NetworkPolicy(
                allowed_domains=["allowed.example.invalid"],
                denied_domains=["blocked.example.invalid"],
                allow_all_unix_sockets=True,
            ),
        )
        async with agent.session(storage=storage, environment=environment) as session:
            assert (await (await session.submit("Delegate")).get_result()).final_output == (
                "/root finished"
            )
            for child in await handles(session, 2):
                await child.get_result()
            identities = {
                identity["agent_name"]: identity["thread_id"]
                for body, headers in zip(peer.requests, peer.request_headers, strict=True)
                if (identity := metadata(body, headers))
            }
            assert identities.keys() == {"/root", "/root/alpha", "/root/alpha/grandchild"}
            for path, thread_id in identities.items():
                child_id = None if thread_id == session.id else thread_id
                [receipt] = [
                    row["receipt"]
                    async for row in session.turns(subagent_id=child_id)
                    if "receipt" in row
                ]
                [executor] = receipt["executors"]
                assert executor["transport"] == "local", path
                assert executor["cwd"] == workspace.resolve().as_uri(), path
                assert set(executor["writable_roots"]) == {
                    workspace.resolve().as_uri(),
                    extra.resolve().as_uri(),
                }, path
                assert executor["network_policy"]["domains"] == {
                    "allowed.example.invalid": "allow",
                    "blocked.example.invalid": "deny",
                }, path

    for path, thread_id in identities.items():
        [rollout] = (storage.path / "sessions").rglob(f"*{thread_id}.jsonl")
        receipts = [
            row["payload"]["sdk_receipt"]
            for line in rollout.read_text().splitlines()
            if (row := json.loads(line))["type"] == "turn_context"
            and row["payload"].get("sdk_receipt")
            and row["payload"]["sdk_receipt"]["thread_id"] == thread_id
        ]
        assert receipts and all(row["strict_executor_writes"] is True for row in receipts), path


async def worker() -> None:
    request = json.loads(sys.argv[1])
    path = Path(request["path"])
    finished_root = asyncio.Event()

    @function_tool
    async def lookup(context: ToolContext[str], key: str) -> str:
        """Write one receipt-controlled memory."""
        with (path / "effects").open("a") as file:
            file.write(context.context + ":" + key + "\n")
            file.flush()
            os.fsync(file.fileno())
        if request["mode"] in {"unknown", "hold-unknown"}:
            # Do not kill before the root has its spawn receipt and final text.
            await finished_root.wait()
            if request["mode"] == "hold-unknown":
                print(json.dumps({"pending_effect": key}), flush=True)
                await asyncio.to_thread(sys.stdin.readline)
            os._exit(67)
        return context.context + ":" + key

    connection = NativeProvider(
        base_url=request["url"],
        api_key="destination" if request["resume"] else "source",
        supports_websockets=False,
    )
    native = None if request["no_children"] else NativeAgents(max_children=2, max_depth=2)
    agent = Agent(
        instructions="Record a memory.",
        model=request["model"],
        provider=connection,
        programmatic_tool_calling=False,
        tools=[lookup] if native is None else [lookup, native],
    )
    try:
        async with (
            connection,
            agent.session(
                context="destination" if request["resume"] else "source",
                storage=LocalStorage(path / "profile"),
                resume=request["resume"] or None,
                reconciled_calls=[ReconciledCall(**row) for row in request["receipts"]],
                environment=Environment(
                    url=request["executor"],
                    cwd=request["cwd"],
                    write_policy=request["write_policy"],
                    headers={"Authorization": "Bearer " + request["token"]},
                    tools=["exec_command", "write_stdin", "apply_patch"],
                ),
            ) as session,
        ):
            print(json.dumps({"opened": session.id, "pid": os.getpid()}), flush=True)
            if request["mode"] == "inspect":
                async for child in session.subagents():
                    items = [item async for item in session.items(subagent_id=child.id)]
                    turns = [turn async for turn in session.turns(subagent_id=child.id)]
                    print(
                        json.dumps(
                            {
                                "id": child.id,
                                "is_live": child.is_live,
                                "items": items,
                                "turns": turns,
                            }
                        ),
                        flush=True,
                    )
                return
            result = await (await session.submit("Run requested work.")).get_result()
            root_items = [item async for item in session.items()]
            print(json.dumps({"output": result.final_output, "root_items": root_items}), flush=True)
            finished_root.set()
            if request["mode"] == "followup":
                [handle] = await handles(session, 1)
                try:
                    await handle.get_result()
                except NativeError:
                    turns = [turn async for turn in session.turns(subagent_id=handle.child_id)]
                    (failed,) = [turn for turn in turns if turn["id"] == handle.turn_id]
                    print(json.dumps({"followup": failed}), flush=True)
            elif not request["resume"]:
                for handle in await handles(session, request["depth"]):
                    await handle.get_result()
    except LocalRecoveryRequired as error:
        print(
            json.dumps({"uncertain": [asdict(call) for call in error.calls], "turns": error.turns}),
            flush=True,
        )
        raise
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}), flush=True)
        raise


async def start(
    peer,
    executor,
    directory,
    mode,
    depth,
    *,
    identity="",
    no_children=False,
    model=DESCENDANT_MODEL,
    write_policy="legacy" if sys.platform == "win32" else "strict",
    receipts=(),
):
    request = {
        "path": str(directory),
        "url": peer.url,
        "mode": mode,
        "depth": depth,
        "resume": identity,
        "no_children": no_children,
        "model": model,
        "write_policy": write_policy,
        "executor": executor.url,
        "cwd": str(executor.root),
        "token": executor.token,
        "receipts": [asdict(receipt) for receipt in receipts],
    }
    root = str(Path(__file__).resolve().parents[1])
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (root, os.environ.get("PYTHONPATH")))),
        "SSL_CERT_FILE": str(executor.ca),
        "SSL_CERT_DIR": str(directory / "empty-certs"),
    }
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "tests.test_native_local_descendant_receipts",
        json.dumps(request),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )


def canonical_completed_activity(
    directory: Path, identity: str, child: str
) -> list[dict[str, Any]]:
    rollouts = list((directory / "profile").rglob(f"*{identity}.jsonl"))
    assert len(rollouts) == 1, rollouts
    records = [json.loads(line) for line in rollouts[0].read_text().splitlines() if line.strip()]
    return [
        row["payload"]
        for row in records
        if row.get("type") == "event_msg"
        and row.get("payload", {}).get("type") == "item_completed"
        and row["payload"].get("item", {}).get("type") == "SubAgentActivity"
        and row["payload"]["item"].get("kind") == "completed"
        and row["payload"]["item"].get("agent_thread_id") == child
    ]


async def test_local_recovery_preserves_crashed_child_native_command(tmp_path, monkeypatch):
    peer = LocalFamilyPeer(depth=1)
    original_response = peer.response
    arguments = {"cmd": "printf fixture-effect"}

    def response():
        body, headers = peer.requests[-1], peer.request_headers[-1]
        if (
            headers["authorization"] == "Bearer source"
            and metadata(body, headers)["agent_name"] == "/root/alpha"
        ):
            return [function_call("exec_command", arguments, "source-command")]
        return original_response()

    effect = tmp_path / "command-journal"
    recorded, release = asyncio.Event(), asyncio.Event()

    async def apply_synthetic_effect(process_id: str) -> None:
        with effect.open("a") as journal:
            journal.write(process_id + "\n")
        recorded.set()
        await release.wait()

    monkeypatch.setattr(peer, "response", response)
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(55),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(
            tmp_path / "remote",
            synthetic_process_output="fixture-effect",
            before_synthetic_process_reply=apply_synthetic_effect,
        ).serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        source = await start(peer, executor, tmp_path, "known", 1)
        try:
            assert source.stdout is not None
            opened, root = [json.loads(await source.stdout.readline()) for _ in range(2)]
            assert root["output"] == "/root finished"
            await recorded.wait()
            source.kill()
            code, _, err = await _collect(source)
            assert code != 0, err
        finally:
            release.set()
            if source.returncode is None:
                source.kill()
                await _collect(source)

        [execution] = effect.read_text().splitlines()
        child = next(
            item["subagent_id"]
            for item in root["root_items"]
            if item["type"] == "create_subagent_call"
        )
        root_id = opened["opened"]
        before = len(peer.requests)
        unopened = await start(peer, executor, tmp_path, "inspect", 1, identity=root_id)
        code, rows, err = await _collect(unopened)
        assert code != 0 and len(rows) == 1 and "uncertain" in rows[0], (rows, err)
        (call,) = rows[0]["uncertain"]
        assert call["thread_id"] == child and call["turn_id"]
        assert call["call_id"] == "source-command" and call["name"] == "exec_command"
        assert call["arguments"] == arguments
        receipt = ReconciledCall(
            thread_id=child,
            turn_id=call["turn_id"],
            call_id="source-command",
            outcome="applied",
            result="fixture-effect",
        )
        (rollout,) = (tmp_path / "profile").rglob(f"*{child}.jsonl")
        original = rollout.read_bytes()
        staging = rollout.with_name(f".sdk-recovery-{child}")
        # A process dying during publication can leave a partial staging file.
        staging.write_bytes(original + b'{"type":"response_item"')
        recovered = await start(
            peer, executor, tmp_path, "inspect", 1, identity=root_id, receipts=[receipt]
        )
        code, rows, err = await _collect(recovered)
        assert code == 0 and rows[0]["opened"] == root_id, (rows, err)
        (leaf,) = rows[1:]
        assert leaf["id"] == child and not leaf["is_live"]
        (result,) = [item for item in leaf["items"] if item["type"] == "function_call_output"]
        assert (result["turn_id"], result["call_id"]) == (call["turn_id"], "source-command")
        assert result["reconciliation"] == "applied" and result["output"] == "fixture-effect"
        assert rollout.read_bytes().startswith(original)
        assert not staging.exists()
        assert len(peer.requests) == before

        continued = await start(peer, executor, tmp_path, "destination", 1, identity=root_id)
        code, rows, err = await _collect(continued)
        assert code == 0 and rows[0]["opened"] == root_id, (rows, err)
        assert rows[1]["output"] == "destination finished"
        assert executor.synthetic_process_starts == [execution]
        assert effect.read_text() == execution + "\n"


@pytest.mark.parametrize("depth", [1, 2], ids=["child", "grandchild"])
@pytest.mark.parametrize("known", [False, True], ids=["unknown", "receipted"])
@pytest.mark.parametrize(
    "no_children", [False, True], ids=["current-children", "no-current-children"]
)
async def test_local_descendant_admission_checks_receipts_without_resuming_work(
    tmp_path,
    depth,
    known,
    no_children,
):
    peer = LocalFamilyPeer(depth, fork_turns="all" if depth == 2 else "none")
    hold_parent = depth == 2 and not known and no_children
    release_parent = asyncio.Event()
    if hold_parent:
        parent_waiting = asyncio.Event()

        async def hold_spawning_turn(event):
            item = event.get("item", {})
            if item.get("content") == [{"type": "output_text", "text": "/root/alpha finished"}]:
                parent_waiting.set()
                await release_parent.wait()
            elif item.get("call_id") == "source-effect":
                # The spawning turn has durably recorded the grandchild and
                # is waiting on its next model response when the effect runs.
                await parent_waiting.wait()

        peer.before_event = hold_spawning_turn
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(55),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        hold_owner = not known and not no_children
        mode = "known" if known else "hold-unknown" if hold_owner else "unknown"
        first = await start(peer, executor, tmp_path, mode, depth)
        rows = []
        if hold_owner:
            try:
                assert first.stdout is not None
                rows = [json.loads(await first.stdout.readline()) for _ in range(2)]
                assert "pending_effect" in json.loads(await first.stdout.readline())
                before = len(peer.requests)
                competing = await start(
                    peer, executor, tmp_path, "inspect", depth, identity=rows[0]["opened"]
                )
                code, refused, err = await _collect(competing)
                assert code != 0 and len(refused) == 1, (refused, err)
                assert "already has an active writer" in refused[0]["message"], (refused, err)
                assert len(peer.requests) == before
            except BaseException:
                if first.returncode is None:
                    first.kill()
                await _collect(first)
                raise
            assert first.stdin is not None
            first.stdin.write(b"stop\n")
            await first.stdin.drain()
        try:
            code, rest, err = await _collect(first)
        finally:
            release_parent.set()
        rows += rest
        assert code == (0 if known else 67), (rows, err)
        assert len(rows) == 2 and rows[0].get("opened"), (rows, err)
        assert rows[1]["output"] == "/root finished"
        unfinished = [
            item
            for item in rows[1]["root_items"]
            if item["type"] == "function_call" and item["status"] in ("in_progress", "incomplete")
        ]
        assert not unfinished, rows[1]
        direct_child = next(
            item["subagent_id"]
            for item in rows[1]["root_items"]
            if item["type"] == "create_subagent_call"
        )
        if known:
            # Child handles settle before their parent completion notification.
            # Close must retain that accepted canonical event as well as effects.
            assert len(canonical_completed_activity(tmp_path, rows[0]["opened"], direct_child)) == 1
        origin = "/root/alpha" if depth == 1 else "/root/alpha/grandchild"
        old_effect = "source:" + origin + "\n"
        assert (tmp_path / "effects").read_text() == old_effect
        original_count = len(peer.requests)
        if known and depth == 1 and not no_children:
            # The root writer is available while an independent opener owns the
            # saved child. Recovery must still acquire that child's native lease.
            options = native_options(tmp_path, peer)
            options["config"].update(model=DESCENDANT_MODEL, sandbox_mode="read-only")
            options["runtime"].update(native_thread_store=str(tmp_path / "profile"))
            owner = await _native.create_engine(
                json.dumps(options["config"]), json.dumps(options["runtime"])
            )
            try:
                child = await owner.resume_thread(direct_child)
                competing = await start(
                    peer, executor, tmp_path, "inspect", depth, identity=rows[0]["opened"]
                )
                code, refused, err = await _collect(competing)
                assert code != 0 and len(refused) == 1, (refused, err)
                assert "already has an active writer" in refused[0]["message"], (refused, err)
                assert len(peer.requests) == original_count
                await child.close()
            finally:
                await owner.close()
        second = await start(
            peer,
            executor,
            tmp_path,
            "destination",
            depth,
            identity=rows[0]["opened"],
            no_children=no_children,
        )
        code, new, err = await _collect(second)
        if known:
            assert code == 0 and new[0]["opened"] == rows[0]["opened"], (new, err)
            assert new[0]["pid"] != rows[0]["pid"] and new[1]["output"] == "destination finished"
            assert (tmp_path / "effects").read_text() == old_effect + "destination:fresh\n"
            assert len(peer.requests) == original_count + 2
            assert len(canonical_completed_activity(tmp_path, rows[0]["opened"], direct_child)) == 1
        else:
            assert code != 0 and len(new) == 1 and "uncertain" in new[0], (new, err)
            (call,) = new[0]["uncertain"]
            assert call["thread_id"] != rows[0]["opened"]
            if depth == 1:
                assert call["thread_id"] == direct_child
            else:
                assert call["thread_id"] != direct_child
            assert call["turn_id"] and call["call_id"] == "source-effect"
            assert call["name"] == "lookup" and call["arguments"] == {"key": origin}
            (original_turn,) = [turn for turn in new[0]["turns"] if turn["id"] == call["turn_id"]]
            assert original_turn["subagent_id"] == call["thread_id"]
            parent_turn_id = None
            if hold_parent:
                (parent_turn,) = [
                    turn for turn in new[0]["turns"] if turn["subagent_id"] == direct_child
                ]
                assert parent_turn["status"] == "in_progress"
                parent_turn_id = parent_turn["id"]
            assert len(peer.requests) == original_count
            assert (tmp_path / "effects").read_text() == old_effect
            receipt = ReconciledCall(
                thread_id=call["thread_id"],
                turn_id=call["turn_id"],
                call_id=call["call_id"],
                outcome="applied",
                result="Confirmed by the application journal",
            )
            wrong_owner = await start(
                peer,
                executor,
                tmp_path,
                "inspect",
                depth,
                identity=rows[0]["opened"],
                no_children=no_children,
                receipts=[replace(receipt, thread_id=rows[0]["opened"])],
            )
            code, refused, err = await _collect(wrong_owner)
            assert code != 0 and refused[0]["error"] == "ValueError", (refused, err)
            if sys.platform != "win32" and not no_children:
                wrong_policy = await start(
                    peer,
                    executor,
                    tmp_path,
                    "inspect",
                    depth,
                    identity=rows[0]["opened"],
                    receipts=[receipt],
                    write_policy="legacy",
                )
                code, refused, err = await _collect(wrong_policy)
                assert code != 0 and "recorded executor" in refused[0]["message"], (refused, err)

            async def inspect(confirmations=()):
                retry = await start(
                    peer,
                    executor,
                    tmp_path,
                    "inspect",
                    depth,
                    identity=rows[0]["opened"],
                    no_children=no_children,
                    receipts=confirmations,
                )
                return await _collect(retry)

            if parent_turn_id is not None:
                (rollout,) = (tmp_path / "profile").rglob(f"*{call['thread_id']}.jsonl")
                staging = rollout.with_name(f".sdk-recovery-{call['thread_id']}")
                staging.mkdir()
                try:
                    code, failed, err = await inspect((receipt,))
                    assert code != 0 and len(failed) == 1 and "error" in failed[0], (failed, err)
                finally:
                    staging.rmdir()
                code, pending, err = await inspect()
                assert code != 0 and pending[0].get("uncertain") == [call], (pending, err)
                (aborted_parent,) = [
                    turn for turn in pending[0]["turns"] if turn["id"] == parent_turn_id
                ]
                assert aborted_parent["status"] == "cancelled"

            for confirmations in ((receipt,), (), (receipt,)):
                code, recovered, err = await inspect(confirmations)
                assert code == 0 and recovered[0]["opened"] == rows[0]["opened"], (recovered, err)
                (leaf,) = [row for row in recovered[1:] if row["id"] == call["thread_id"]]
                (invocation,) = [
                    item
                    for item in leaf["items"]
                    if item.get("call_id") == call["call_id"] and item["type"] == "function_call"
                ]
                assert invocation["turn_id"] == call["turn_id"] and invocation["name"] == "lookup"
                (result,) = [
                    item
                    for item in leaf["items"]
                    if item.get("call_id") == call["call_id"]
                    and item["type"] == "function_call_output"
                ]
                assert result["turn_id"] == call["turn_id"]
                assert result["reconciliation"] == "applied" and result["output"] == receipt.result
                (turn,) = [turn for turn in leaf["turns"] if turn["id"] == call["turn_id"]]
                assert turn["status"] == "cancelled"
                assert not leaf["is_live"]
                if parent_turn_id is not None:
                    (parent,) = [row for row in recovered[1:] if row["id"] == direct_child]
                    (parent_turn,) = [
                        turn for turn in parent["turns"] if turn["id"] == parent_turn_id
                    ]
                    assert parent_turn["status"] == "cancelled" and not parent["is_live"]
                    (spawn,) = [
                        item for item in parent["items"] if item["type"] == "create_subagent_call"
                    ]
                    assert (spawn["turn_id"], spawn["subagent_id"], spawn["status"]) == (
                        parent_turn_id,
                        call["thread_id"],
                        "completed",
                    )
            conflict = await start(
                peer,
                executor,
                tmp_path,
                "inspect",
                depth,
                identity=rows[0]["opened"],
                no_children=no_children,
                receipts=[replace(receipt, result="Conflicting result")],
            )
            code, refused, err = await _collect(conflict)
            assert code != 0 and "conflicts with a retained result" in refused[0]["message"], (
                refused,
                err,
            )
            assert len(peer.requests) == original_count
            assert (tmp_path / "effects").read_text() == old_effect
            destination = await start(
                peer,
                executor,
                tmp_path,
                "destination",
                depth,
                identity=rows[0]["opened"],
                no_children=no_children,
            )
            code, finished, err = await _collect(destination)
            assert code == 0 and finished[0]["opened"] == rows[0]["opened"], (finished, err)
            assert finished[1]["output"] == "destination finished"
            assert (tmp_path / "effects").read_text() == old_effect + "destination:fresh\n"
            assert len(peer.requests) == original_count + 2
            if depth == 1 and not no_children:
                peer.followup = True
                followup = await start(
                    peer, executor, tmp_path, "followup", depth, identity=rows[0]["opened"]
                )
                code, finished, err = await _collect(followup)
                assert code == 0 and finished[1]["output"] == "follow-up submitted", (finished, err)
                failed = finished[2]["followup"]
                assert failed["status"] == "failed"
                assert "Cannot reuse an original call identity" in failed["error"]["message"]
                assert (tmp_path / "effects").read_text() == old_effect + "destination:fresh\n"


@pytest.mark.parametrize("depth", [1, 2], ids=["child", "grandchild"])
@pytest.mark.parametrize(
    "no_children", [False, True], ids=["current-children", "no-current-children"]
)
async def test_local_reopen_refuses_cold_active_child_without_tool_receipt(
    tmp_path, monkeypatch, depth, no_children
):
    peer = LocalFamilyPeer(depth, fork_turns="all" if depth == 2 else "none")
    entered, release = asyncio.Event(), asyncio.Event()
    original_response = peer.response

    def response():
        path = metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"]
        if (
            peer.request_headers[-1]["authorization"] == "Bearer source"
            and path.count("/") - 1 == depth
        ):
            # The child exists, but never attempts a function call. Hold only
            # its final message so it cannot finish before the process dies.
            message = assistant_output("leaf not yet settled")
            message["item"]["id"] = "held-leaf-without-tool"
            return [message]
        return original_response()

    async def hold(event):
        if event.get("item", {}).get("id") == "held-leaf-without-tool":
            entered.set()
            await release.wait()

    monkeypatch.setattr(peer, "response", response)
    peer.before_event = hold
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(55),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        source = await start(peer, executor, tmp_path, "known", depth)
        try:
            assert source.stdout is not None
            opened = json.loads(await source.stdout.readline())
            assert opened.get("opened"), opened
            await entered.wait()
            root = json.loads(await source.stdout.readline())
            assert root["output"] == "/root finished"
            assert not any(
                item["type"] == "function_call" and item["status"] in ("in_progress", "incomplete")
                for item in root["root_items"]
            )
            assert not (tmp_path / "effects").exists()
            source.kill()
            code, _, err = await _collect(source)
            assert code != 0, err
        finally:
            release.set()
            if source.returncode is None:
                source.kill()
                await _collect(source)
        before = len(peer.requests)
        for _ in range(2):
            restored = await start(
                peer,
                executor,
                tmp_path,
                "destination",
                depth,
                identity=opened["opened"],
                no_children=no_children,
            )
            code, rows, err = await _collect(restored)
            assert code != 0 and rows and "active descendant" in rows[0].get("message", ""), (
                rows,
                err,
            )
            assert "opened" not in rows[0], rows
            assert len(peer.requests) == before
            assert not (tmp_path / "effects").exists()


if __name__ == "__main__":
    asyncio.run(worker())
