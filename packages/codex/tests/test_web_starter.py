"""Exercise real HTTP, native Sessions, user boundaries and lifecycle ownership."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from xml.etree import ElementTree

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, Completed, InteractionExpiredError, Session, Turn
from agents.testing import ScriptedResponses, text, tool_call

pytest.importorskip("fcntl", reason="The starter requires POSIX flock and private directory modes")

from examples.codex.web_starter.app import Final, Identity, RunContext, Service, create_app
from examples.codex.web_starter.store import Store
from tests.native_fixtures import responses as responses

HEADERS = {"X-Web-Starter": "1"}
TERMINAL = {"completed", "error", "cancelled", "expired", "unknown"}


@asynccontextmanager
async def running(tmp_path, **limits):
    app = create_app(tmp_path, **limits)
    async with TestServer(app) as server:
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True), headers=HEADERS
        ) as client:

            async def post(path, body):
                return await client.post(server.make_url(path), json=body)

            async def get(path):
                return await client.get(server.make_url(path))

            yield app["service"], server, client, post, get


async def state_until(get, receipt, statuses):
    async def wait():
        while True:
            response = await get(f"/api/receipts/{receipt}")
            assert response.status == 200
            state = await response.json()
            if state["status"] in statuses:
                return state
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(wait(), 15)


async def login(post, user="alice"):
    assert (await post("/api/login", {"user": user})).status == 200


async def create(post, key="one", **changes):
    response = await post("/api/receipts", {"key": key, "prompt": "My report", **changes})
    assert response.status == 200, await response.text()
    return await response.json()


async def test_http_two_users_receipts_approval_download_checkpoint_restore(tmp_path):
    async with running(tmp_path) as (service, server, client, post, get):
        await login(post)
        receipt = (await create(post))["id"]
        pending = await state_until(get, receipt, {"pending"})
        token = pending["approval"]["id"]
        assert (await create(post))["id"] == receipt
        assert (await post("/api/receipts", {"key": "one", "prompt": "Different"})).status == 400
        # A second identity cannot read another identity's data or reply to its live grant.
        await login(post, "bob")
        assert (await get(f"/api/receipts/{receipt}")).status == 404
        assert (await get(f"/api/objects/report/{pending['card']['handle']}")).status == 404
        assert (
            await post(f"/api/receipts/{receipt}/reply", {"id": token, "allow": True})
        ).status == 404
        bob = (await create(post))["id"]
        assert bob != receipt
        await login(post)
        decision = {"id": token, "allow": True}
        assert (await post(f"/api/receipts/{receipt}/reply", decision)).status == 200
        assert (await post(f"/api/receipts/{receipt}/reply", decision)).status == 409
        final = await state_until(get, receipt, TERMINAL)
        assert final["status"] == "completed"
        assert final["final"] == {
            "title": "Report complete",
            "summary": "Reviewed 10000 synthetic rows.",
        }
        report = await (await get(f"/api/objects/report/{final['card']['handle']}")).json()
        assert report["owner"] == "alice" and len(report["rows"]) == 10000
        download = await get(f"/api/objects/artifact/{final['artifact']['id']}")
        assert await download.read() == b"<presentation><slide>Report</slide></presentation>"
        assert download.headers["Content-Type"] == "application/xml"
        assert (await get(f"/api/objects/checkpoint/{final['checkpoint']}")).status == 200
        await login(post, "bob")
        for kind, handle in [
            ("checkpoint", final["checkpoint"]),
            ("artifact", final["artifact"]["id"]),
        ]:
            assert (await get(f"/api/objects/{kind}/{handle}")).status == 404
        assert (
            await post(
                "/api/receipts",
                {"key": "foreign", "prompt": "Restore", "checkpoint": final["checkpoint"]},
            )
        ).status == 404
        assert (await post(f"/api/receipts/{bob}/stop", {})).status == 200
        await state_until(get, bob, TERMINAL)
        await login(post)
        restored = (await create(post, "restore", checkpoint=final["checkpoint"]))["id"]
        restored_pending = await state_until(get, restored, {"pending"})
        assert (
            await post(
                f"/api/receipts/{restored}/reply",
                {"id": restored_pending["approval"]["id"], "allow": False},
            )
        ).status == 200
        restored_final = await state_until(get, restored, TERMINAL)
        assert restored_final["status"] == "completed"
        assert restored_final["artifact"] is None
        # Denial did not execute publication, even though the scripted final text is unchanged.
        service.store.db.execute(
            "UPDATE objects SET expires=0 WHERE id=?", (final["artifact"]["id"],)
        )
        service.store.db.commit()
        assert (await get(f"/api/objects/artifact/{final['artifact']['id']}")).status == 404
        assert (await get("/api/objects/artifact/../../etc/passwd")).status == 404


async def test_disconnect_keeps_worker_stop_joins_and_stale_reply_denied(tmp_path):
    async with running(tmp_path) as (service, server, client, post, get):
        await login(post)
        receipt = (await create(post))["id"]
        pending = await state_until(get, receipt, {"pending"})
        response = await post(f"/api/receipts/{receipt}/stream", {})
        assert response.headers["x-vercel-ai-ui-message-stream"] == "v1"
        assert b'"type": "start"' in await response.content.readuntil(b"\n\n")
        response.close()
        assert service.store.snapshot("alice", receipt)["status"] == "pending"
        assert (await post(f"/api/receipts/{receipt}/stop", {})).status == 200
        final = await state_until(get, receipt, TERMINAL)
        assert final["status"] == "cancelled" and final["artifact"] is None
        assert not service.pending
        assert (
            await post(
                f"/api/receipts/{receipt}/reply", {"id": pending["approval"]["id"], "allow": True}
            )
        ).status == 409
        await asyncio.wait_for(service.queue.join(), 5)
        assert not service.work


async def test_finite_queue_deadline_and_shutdown(tmp_path):
    async with running(tmp_path, queue_size=1, deadline=1.5) as (
        service,
        server,
        client,
        post,
        get,
    ):
        await login(post)
        first = (await create(post, "first"))["id"]
        await state_until(get, first, {"pending"})
        second = (await create(post, "second"))["id"]
        assert (await post("/api/receipts", {"key": "third", "prompt": "My report"})).status == 503
        assert (await state_until(get, first, TERMINAL))["status"] == "expired"
        assert (await state_until(get, second, TERMINAL))["status"] == "expired"
        assert not service.pending
        await asyncio.wait_for(service.queue.join(), 5)
        assert not service.work
    assert all(task.done() for task in service.tasks)


def test_restart_retains_receipt_unknown_and_payload_conflict(tmp_path):
    store = Store(tmp_path)
    payload = json.dumps({"prompt": "My report", "checkpoint": None}, sort_keys=True)
    receipt = store.create("alice", "one", payload, prompt="My report")["id"]
    store.update("alice", receipt, status="running", artifact={"id": "already-performed"})
    with pytest.raises(RuntimeError, match="Another service"):
        Store(tmp_path)
    store.close()
    service = Service(tmp_path)
    try:
        resumed = service.submit(Identity("alice"), "one", "My report")
        assert resumed["status"] == "unknown" and resumed["id"] == receipt
        assert resumed["artifact"] == {"id": "already-performed"}
        assert service.queue.empty()
        with pytest.raises(ValueError, match="different payload"):
            service.submit(Identity("alice"), "one", "Different")
    finally:
        service.store.close()


async def test_revocation_at_reply_and_mcp_boundary_and_credential_scope(tmp_path):
    async with running(tmp_path) as (service, server, client, post, get):
        await login(post)
        receipt = (await create(post))["id"]
        pending = await state_until(get, receipt, {"pending"})
        alice = Identity("alice")
        declaration = service.mcp_for(alice)
        assert declaration.bearer_token != service.credentials["bob"]
        assert "synthetic-" not in repr(declaration)
        # Actual HTTP MCP credential determines the peer's user; no model owner argument exists.
        async with aiohttp.ClientSession() as wire:
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "read_record"},
            }
            response = await wire.post(
                service.mcp_url,
                json=body,
                headers={"Authorization": f"Bearer {declaration.bearer_token}"},
            )
            assert (await response.json())["result"]["content"][0][
                "text"
            ] == "Synthetic record for alice"
            assert (
                await wire.post(
                    service.mcp_url,
                    json=body,
                    headers={"Authorization": "Bearer synthetic-invalid"},
                )
            ).status == 403
        service.revoked.add("alice")
        assert (
            await post(
                f"/api/receipts/{receipt}/reply", {"id": pending["approval"]["id"], "allow": True}
            )
        ).status == 403
        context = type("Context", (), {"context": RunContext(alice, receipt, service)})()
        assert await declaration.approval(context, None) is False
        assert service.store.snapshot("alice", receipt)["artifact"] is None


async def test_model_summary_is_bounded_and_does_not_contain_full_data(tmp_path):
    # Exercise the public SDK and inspect the actual subsequent model request.
    from examples.codex.web_starter.app import read_report

    async with running(tmp_path) as (service, server, client, post, get):
        state = service.store.create("alice", "inspection", "payload", prompt="Read")
        async with ScriptedResponses(
            [tool_call("read_report", {}, call_id="read")],
            [text('{"title":"Done","summary":"Read report"}')],
        ) as peer:
            result = await Agent(
                instructions="Read.", tools=[read_report], output_type=Final, provider=peer.provider
            ).run("Read", context=RunContext(Identity("alice"), state["id"], service))
            assert result.final_output.title == "Done"
            output = peer.tool_output("read")
            assert output is not None and len(output.encode()) < 200
            assert "Synthetic row 9999" not in json.dumps(peer.requests)
            card = service.store.snapshot("alice", state["id"])["card"]
            assert len(service.store.get("alice", card["handle"], "report")) > 400000


async def test_csrf_and_auth_boundaries(tmp_path):
    async with running(tmp_path) as (service, server, client, post, get):
        assert (await get("/api/receipts/missing")).status == 401
        assert (
            await client.post(
                server.make_url("/api/login"),
                json={"user": "alice"},
                headers={"Origin": "https://foreign.invalid"},
            )
        ).status == 403
        async with aiohttp.ClientSession() as other:
            assert (
                await other.post(server.make_url("/api/login"), json={"user": "alice"})
            ).status == 403


async def test_native_mcp_dispatch_uses_authenticated_user_credential(tmp_path, responses):
    from agents import NativeProvider
    from tests.native_fixtures import assistant_output, function_call

    async with running(tmp_path) as (service, server, client, post, get):
        for owner in ("alice", "bob"):
            event = function_call("read_record", {}, f"record-{owner}")
            event["item"]["namespace"] = "mcp__records"
            responses.respond(event)
            responses.respond(assistant_output("Read"))
            async with NativeProvider(
                base_url=responses.url, api_key="synthetic", supports_websockets=False
            ) as provider:
                declaration = service.mcp_for(Identity(owner))
                async with Agent(
                    instructions="Read.", tools=[declaration], provider=provider
                ).session(
                    context=RunContext(Identity(owner), "synthetic-receipt", service)
                ) as session:
                    turn = await session.submit("Read my record")
                    assert (await turn.get_result()).final_output == "Read"
            assert f"Synthetic record for {owner}" in json.dumps(responses.requests[-1])
            assert service.credentials[owner] not in json.dumps(responses.requests)


async def test_native_gap_recovers_from_application_snapshot(tmp_path, responses):
    from agents import NativeProvider
    from examples.codex.web_starter.app import Work
    from tests.native_fixtures import text_response

    async with running(tmp_path) as (service, server, client, post, get):
        receipt = service.store.create("alice", "gap", "payload", prompt="User-owned prompt")["id"]
        responses.respond(*text_response("flood", *(["x"] * 350)))
        async with NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider:
            async with Agent(instructions="Reply.", provider=provider).session() as session:
                turn = await session.submit("Flood observations")
                assert (await turn.get_result()).final_output == "x" * 350
                work = Work(Identity("alice"), receipt, float("inf"), asyncio.Event(), None)
                await service.observe(work, turn)
        service.store.update(
            "alice",
            receipt,
            status="completed",
            final={"title": "Settled", "summary": "Known app result"},
        )
        await login(post)
        response = await post(f"/api/receipts/{receipt}/stream", {})
        data = await response.text()
        assert '"resync_required": true' in data and '"User-owned prompt"' in data
        assert "Known app result" in data and "x" * 350 not in data


async def test_stale_native_approval_has_no_artifact_effect(tmp_path):
    from examples.codex.web_starter.app import Pending, Work, publish_report

    async with running(tmp_path) as (service, server, client, post, get):
        receipt = service.store.create("alice", "stale", "payload", prompt="Publish")["id"]
        # Cancel a real native grant, then present its retained registry entry.
        async with ScriptedResponses(
            [
                tool_call(
                    "publish_report",
                    {"xml": "<presentation><slide /></presentation>"},
                    call_id="publish",
                )
            ]
        ) as peer:
            async with Agent(
                instructions="Publish.", tools=[publish_report], provider=peer.provider
            ).session() as session:
                turn = await session.submit("Publish")
                requests = await turn.wait_for_request_or_result()
                request = publish_report.match(requests.requests[0])
                assert request is not None
                work = Work(Identity("alice"), receipt, float("inf"), asyncio.Event(), None)
                service.pending["retained"] = Pending(work, request, asyncio.Event())
                await turn.cancel()
                with pytest.raises(InteractionExpiredError):
                    await service.reply(Identity("alice"), receipt, "retained", True)
                service.pending["retained-http"] = Pending(work, request, asyncio.Event())
                await login(post)
                stale = await post(
                    f"/api/receipts/{receipt}/reply", {"id": "retained-http", "allow": True}
                )
                assert stale.status == 409 and await stale.text() == "Native approval expired"
                assert service.store.snapshot("alice", receipt)["artifact"] is None
                assert (
                    service.store.db.execute(
                        "SELECT count(*) FROM objects WHERE kind='artifact'"
                    ).fetchone()[0]
                    == 0
                )


@pytest.mark.parametrize("failure", ["artifact_write", "publication_snapshot"])
@pytest.mark.parametrize("late_stop", [False, True])
async def test_publication_failure_cannot_become_success(tmp_path, monkeypatch, failure, late_stop):
    async with running(tmp_path) as (service, server, client, post, get):
        await login(post)
        receipt = (await create(post))["id"]
        pending = await state_until(get, receipt, {"pending"})
        original_put, original_update = service.store.put, service.store.update

        def put(owner, receipt, kind, content, **options):
            if failure == "artifact_write" and kind == "artifact":
                if late_stop:
                    service.work[receipt].stop.set()
                raise RuntimeError("synthetic-private-storage-detail")
            return original_put(owner, receipt, kind, content, **options)

        def update(owner, receipt, **changes):
            if failure == "publication_snapshot" and "artifact" in changes:
                if late_stop:
                    service.work[receipt].stop.set()
                raise RuntimeError("synthetic-private-storage-detail")
            return original_update(owner, receipt, **changes)

        monkeypatch.setattr(service.store, "put", put)
        monkeypatch.setattr(service.store, "update", update)
        response = await post(
            f"/api/receipts/{receipt}/reply", {"id": pending["approval"]["id"], "allow": True}
        )
        assert response.status == 500
        assert "synthetic-private" not in await response.text()
        final = await state_until(get, receipt, TERMINAL)
        assert final["status"] == "error" and final["artifact"] is None
        assert final["final"] is None and "checkpoint" not in final
        if failure == "artifact_write":
            assert (
                service.store.db.execute(
                    "SELECT count(*) FROM objects WHERE kind='artifact'"
                ).fetchone()[0]
                == 0
            )


async def test_invalid_generated_xml_publishes_nothing(tmp_path):
    from examples.codex.web_starter.app import Pending, Work, publish_report

    async with running(tmp_path) as (service, server, client, post, get):
        receipt = service.store.create("alice", "invalid", "payload", prompt="Publish")["id"]
        async with ScriptedResponses(
            [tool_call("publish_report", {"xml": "<wrong-root />"}, call_id="publish")],
            [text("Denied")],
        ) as peer:
            async with Agent(
                instructions="Publish.", tools=[publish_report], provider=peer.provider
            ).session() as session:
                turn = await session.submit("Publish")
                requests = await turn.wait_for_request_or_result()
                request = publish_report.match(requests.requests[0])
                assert request is not None
                work = Work(Identity("alice"), receipt, float("inf"), asyncio.Event(), None)
                service.pending["invalid-xml"] = Pending(work, request, asyncio.Event())
                with pytest.raises(web.HTTPUnprocessableEntity):
                    await service.reply(Identity("alice"), receipt, "invalid-xml", True)
                assert "invalid-xml" in service.pending  # Invalid XML remains visibly pending.
                assert service.store.snapshot("alice", receipt)["artifact"] is None
                assert (
                    service.store.db.execute(
                        "SELECT count(*) FROM objects WHERE kind='artifact'"
                    ).fetchone()[0]
                    == 0
                )
                await service.reply(Identity("alice"), receipt, "invalid-xml", False)
                assert (await turn.get_result()).final_output == "Denied"


@pytest.mark.parametrize("encoding", [None, "utf-8", "utf-16", "iso-8859-1"])
async def test_downloaded_xml_preserves_text_across_encoding_declarations(tmp_path, encoding):
    from examples.codex.web_starter.app import Pending, Work, publish_report

    declaration = f'<?xml version="1.0" encoding="{encoding}"?>' if encoding else ""
    value = "café&#13;line2&#10;line3&#9;tail"
    expected = "café\rline2\nline3\ttail"
    xml = declaration + f'<presentation><slide label="{value}">{value}</slide></presentation>'
    async with running(tmp_path) as (service, server, client, post, get):
        await login(post)
        receipt = service.store.create("alice", "encoding", "payload", prompt="Publish")["id"]
        async with ScriptedResponses(
            [tool_call("publish_report", {"xml": xml}, call_id="publish")], [text("Published")]
        ) as peer:
            async with Agent(
                instructions="Publish.", tools=[publish_report], provider=peer.provider
            ).session() as session:
                turn = await session.submit("Publish")
                requests = await turn.wait_for_request_or_result()
                request = publish_report.match(requests.requests[0])
                assert request is not None
                work = Work(Identity("alice"), receipt, float("inf"), asyncio.Event(), None)
                service.pending["encoding"] = Pending(work, request, asyncio.Event())
                response = await post(
                    f"/api/receipts/{receipt}/reply", {"id": "encoding", "allow": True}
                )
                assert response.status == 200
                assert (await turn.get_result()).final_output == "Published"
                artifact = service.store.snapshot("alice", receipt)["artifact"]
                download = await get(f"/api/objects/artifact/{artifact['id']}")
                assert download.status == 200
                slide = ElementTree.fromstring(await download.read()).find("slide")
                assert slide is not None and slide.text == expected
                assert slide.attrib["label"] == expected


def test_live_mode_requires_server_key_before_opening_state(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Live mode requires OPENAI_API_KEY"):
        Service(tmp_path, live=True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("save_fails", [False, True])
async def test_stop_after_completion_during_checkpoint(tmp_path, monkeypatch, save_fails):
    # Hold the checkpoint after the real native turn has completed.
    saving = asyncio.Event()
    release_save = asyncio.Event()
    save_state = Session.save_state

    async def held_save(session):
        saving.set()
        await release_save.wait()
        if save_fails:
            raise OSError("synthetic checkpoint failure")
        return await save_state(session)

    monkeypatch.setattr(Session, "save_state", held_save)
    async with running(tmp_path) as (service, server, client, post, get):
        await login(post)
        receipt = (await create(post))["id"]
        pending = await state_until(get, receipt, {"pending"})
        try:
            assert (
                await post(
                    f"/api/receipts/{receipt}/reply",
                    {"id": pending["approval"]["id"], "allow": False},
                )
            ).status == 200
            await asyncio.wait_for(saving.wait(), 5)
            assert (await post(f"/api/receipts/{receipt}/stop", {})).status == 200
        finally:
            release_save.set()
        final = await state_until(get, receipt, TERMINAL)
        if save_fails:
            assert final["status"] == "error"
            assert final["final"] is None
            assert final.get("checkpoint") is None
        else:
            assert final["final"] == {
                "title": "Report complete",
                "summary": "Reviewed 10000 synthetic rows.",
            }
            assert final.get("checkpoint")
            assert (await get(f"/api/objects/checkpoint/{final['checkpoint']}")).status == 200
            assert final["status"] == "completed"
        await asyncio.wait_for(service.queue.join(), 5)
        assert not service.work


@pytest.mark.parametrize("completed", [False, True])
async def test_stop_before_selected_result_returns_settles_without_stale_approval(
    tmp_path, monkeypatch, completed
):
    selected = asyncio.Event()
    release_requests = asyncio.Event()
    stopped = asyncio.Event()
    wait_for_request_or_result = Turn.wait_for_request_or_result
    stop_when_requested = Service.stop_when_requested

    async def held_requests(turn):
        result = await wait_for_request_or_result(turn)
        # The SDK also yields in cleanup after selecting its pending requests.
        if isinstance(result, Completed) == completed:
            selected.set()
            await release_requests.wait()
        return result

    async def joined_stop(service, work, turn):
        await stop_when_requested(service, work, turn)
        stopped.set()

    monkeypatch.setattr(Turn, "wait_for_request_or_result", held_requests)
    monkeypatch.setattr(Service, "stop_when_requested", joined_stop)
    async with running(tmp_path, deadline=5) as (service, server, client, post, get):
        await login(post)
        receipt = (await create(post))["id"]
        try:
            if completed:
                pending = await state_until(get, receipt, {"pending"})
                assert (
                    await post(
                        f"/api/receipts/{receipt}/reply",
                        {"id": pending["approval"]["id"], "allow": False},
                    )
                ).status == 200
            await asyncio.wait_for(selected.wait(), 2)
            assert not service.pending
            assert (await post(f"/api/receipts/{receipt}/stop", {})).status == 200
            await asyncio.wait_for(stopped.wait(), 2)
            assert not service.pending
        finally:
            release_requests.set()
        await asyncio.wait_for(service.queue.join(), 2)
        final = await (await get(f"/api/receipts/{receipt}")).json()
        assert final["status"] == ("completed" if completed else "cancelled")
        assert final["approval"] is None and final["artifact"] is None
        if completed:
            assert final["final"]["title"] == "Report complete"
            assert (await get(f"/api/objects/checkpoint/{final['checkpoint']}")).status == 200
        else:
            assert final["final"] is None and "checkpoint" not in final
        assert not service.pending and not service.work
