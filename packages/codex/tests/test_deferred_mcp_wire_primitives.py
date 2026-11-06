"""Check incoming Task primitives through the public provider and a loopback peer."""

from collections import Counter
from dataclasses import replace

import pytest

from tests import test_deferred_mcp_tasks as mcp
from tests.test_deferred_mcp_tasks import reservations as reservations


class TaskPeer(mcp.Peer):
    def __init__(self, directory, method, fields):
        super().__init__(directory)
        task = self.task("working")
        del task["ttlMs"], task["pollIntervalMs"]
        tag = "task" if method == "tools/call" else "complete"
        self.method = method
        self.reply = {"resultType": tag, **task, **fields}

    async def audit(self, request, call_next):
        payload = await request.json() if request.method == "POST" else {}
        response = await super().audit(request, call_next)
        if payload.get("method") != self.method or response.status_code != 200:
            return response
        # Send the exact wire fields after the server has accepted the request.
        return mcp.JSONResponse({"jsonrpc": "2.0", "id": payload["id"], "result": self.reply})


@pytest.mark.parametrize(
    "fields, accepted",
    [
        pytest.param({"ttlMs": 60000}, True, id="integer"),
        pytest.param({"ttlMs": None}, True, id="null"),
        pytest.param(
            {
                "ttlMs": 60000.0,
                "pollIntervalMs": 0.0,
                "statusMessage": "queued",
                "_meta": {"progress": 1},
            },
            True,
            id="integral-numbers",
        ),
        pytest.param({"ttlMs": -(2**53 - 1)}, True, id="minimum-ttl"),
        pytest.param({}, False, id="missing-ttl"),
        pytest.param({"ttlMs": "60000"}, False, id="string-ttl"),
        pytest.param({"ttlMs": False}, False, id="boolean-ttl"),
        pytest.param({"ttlMs": 1.5}, False, id="fractional-ttl"),
        pytest.param({"ttlMs": -0.5}, False, id="negative-fractional-ttl"),
        pytest.param({"ttlMs": 2**53}, False, id="oversized-ttl"),
        pytest.param({"ttlMs": None, "pollIntervalMs": None}, False, id="null-poll"),
        pytest.param({"ttlMs": None, "pollIntervalMs": -1}, False, id="negative-poll"),
        # The content also satisfies CallToolResult, but the peer still tags the reply as a task.
        pytest.param({"ttlMs": None, "pollIntervalMs": "0", "content": []}, False, id="fallback"),
        pytest.param({"ttlMs": None, "taskId": 123}, False, id="numeric-id"),
        pytest.param({"ttlMs": None, "createdAt": True}, False, id="boolean-timestamp"),
    ],
)
async def test_create_task_primitives(tmp_path, fields, accepted):
    peer = TaskPeer(tmp_path, "tools/call", fields)
    claim = mcp.starting()
    async with mcp.serving(peer) as endpoint:
        provider = peer.provider(endpoint=endpoint, tool="render", authorize=mcp.authorize)
        created = await provider.start(claim, mcp.Args(pages=3))
        assert peer.creates == 1
        assert peer.counts == {"server/discover": 1, "tools/list": 1, "tools/call": 1}
        if created.receipt is not None:
            observed = await provider.observe(replace(claim, action="observe", update=created))
            assert observed.status == "completed" and observed.receipt == created.receipt
            assert peer.counts == {
                "server/discover": 2,
                "tools/list": 1,
                "tools/call": 1,
                "tasks/get": 1,
            }
    if accepted:
        assert created.status == "pending" and created.receipt
    else:
        assert (created.status, created.receipt, created.value) == ("unknown", None, None)


async def test_complete_result_ignores_task_fields(tmp_path):
    peer = TaskPeer(
        tmp_path, "tools/call", {"resultType": "complete", "content": [], "ttlMs": -0.5}
    )
    async with mcp.serving(peer) as endpoint:
        provider = mcp.MCPTasksProvider(endpoint=endpoint, tool="render", authorize=mcp.authorize)
        result = await provider.start(mcp.starting(), mcp.Args(pages=3))
    assert result.status == "completed" and result.receipt is None
    assert result.value == {"content": [], "isError": False}
    assert peer.creates == 1
    assert peer.counts == {"server/discover": 1, "tools/list": 1, "tools/call": 1}


@pytest.mark.parametrize(
    "action, fields, expected, cancels",
    [
        pytest.param("observe", {"ttlMs": -0.5}, "unknown", 0, id="fractional-ttl"),
        pytest.param(
            "observe", {"ttlMs": None, "pollIntervalMs": 2**53}, "unknown", 0, id="oversized-poll"
        ),
        pytest.param(
            "cancel", {"ttlMs": None, "pollIntervalMs": False}, "unknown", 0, id="boolean-poll"
        ),
        pytest.param(
            "cancel", {"ttlMs": None, "pollIntervalMs": 0.0}, "pending", 1, id="integral-poll"
        ),
    ],
)
async def test_task_detail_before_observe_or_cancel(
    tmp_path, reservations, action, fields, expected, cancels
):
    peer = TaskPeer(tmp_path, "tasks/get", fields)
    claim = mcp.starting()
    async with mcp.serving(peer) as endpoint:
        provider = peer.provider(
            endpoint=endpoint, tool="render", authorize=mcp.authorize, reserve_mutation=reservations
        )
        seed = await provider.start(claim, mcp.Args(pages=3))
        assert seed.status == "pending" and seed.receipt
        control = replace(claim, action=action, update=seed)
        result = await (
            provider.observe(control) if action == "observe" else provider.cancel(control)
        )
    assert peer.creates == 1
    assert peer.counts == Counter(
        {
            "server/discover": 2,
            "tools/list": 1,
            "tools/call": 1,
            "tasks/get": 1,
            "tasks/cancel": cancels,
        }
    )
    assert reservations.attempts == cancels
    assert result.status == expected and result.receipt == seed.receipt
