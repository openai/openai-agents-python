"""Local process restart never treats descendant receipts as current authority."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    Environment,
    LocalStorage,
    NativeAgents,
    NativeProvider,
    ToolContext,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.test_native_child_agents import ChildPeer, collaboration, handles, metadata
from tests.test_native_local_storage import _collect


class LocalFamilyPeer(ChildPeer):
    def __init__(self, depth: int) -> None:
        super().__init__(("default",))
        self.depth = depth
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
                            "fork_turns": "none",
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
        if request["mode"] == "unknown":
            # Do not kill before the root has its spawn receipt and final text.
            await finished_root.wait()
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
        model="gpt-6.1-sol",
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
                environment=Environment(
                    url=request["executor"],
                    cwd=request["cwd"],
                    headers={"Authorization": "Bearer " + request["token"]},
                    tools=["exec_command", "write_stdin", "apply_patch"],
                ),
            ) as session,
        ):
            print(json.dumps({"opened": session.id, "pid": os.getpid()}), flush=True)
            result = await (await session.submit("Run requested work.")).get_result()
            root_items = [item async for item in session.items()]
            print(json.dumps({"output": result.final_output, "root_items": root_items}), flush=True)
            finished_root.set()
            if not request["resume"]:
                for handle in await handles(session, request["depth"]):
                    await handle.get_result()
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}), flush=True)
        raise


async def start(peer, executor, directory, mode, depth, *, identity="", no_children=False):
    request = {
        "path": str(directory),
        "url": peer.url,
        "mode": mode,
        "depth": depth,
        "resume": identity,
        "no_children": no_children,
        "executor": executor.url,
        "cwd": str(executor.root),
        "token": executor.token,
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
    peer = LocalFamilyPeer(depth)
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
        first = await start(peer, executor, tmp_path, "known" if known else "unknown", depth)
        code, rows, err = await _collect(first)
        assert code == (0 if known else 67), (rows, err)
        assert len(rows) == 2 and rows[0].get("opened"), (rows, err)
        assert rows[1]["output"] == "/root finished"
        unfinished = [
            item
            for item in rows[1]["root_items"]
            if item["type"] == "function_call" and item["status"] in ("in_progress", "incomplete")
        ]
        assert not unfinished, rows[1]
        origin = "/root/alpha" if depth == 1 else "/root/alpha/grandchild"
        old_effect = "source:" + origin + "\n"
        assert (tmp_path / "effects").read_text() == old_effect
        original_count = len(peer.requests)
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
        else:
            assert code != 0 and new and new[0]["error"] == "RuntimeError", (new, err)
            assert "cannot reconcile descendant calls" in new[0]["message"]
            assert len(peer.requests) == original_count
            assert (tmp_path / "effects").read_text() == old_effect
            retried = await start(
                peer,
                executor,
                tmp_path,
                "destination",
                depth,
                identity=rows[0]["opened"],
                no_children=no_children,
            )
            code, again, err = await _collect(retried)
            assert code != 0 and again == new, (again, err)
            assert len(peer.requests) == original_count
            assert (tmp_path / "effects").read_text() == old_effect


@pytest.mark.parametrize("depth", [1, 2], ids=["child", "grandchild"])
@pytest.mark.parametrize(
    "no_children", [False, True], ids=["current-children", "no-current-children"]
)
async def test_local_reopen_refuses_cold_active_child_without_tool_receipt(
    tmp_path, monkeypatch, depth, no_children
):
    peer = LocalFamilyPeer(depth)
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
