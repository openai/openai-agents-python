"""A host's durable credential authority admits peer data before storage or release."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.helpers.proto_helpers import new_text_message
from a2a.types import a2a_pb2 as p
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from agents import Agent, Turn
from agents.a2a import A2AJobProvider, connect_a2a
from agents.experimental.deferred import JobBinding, JobUpdate
from tests.a2a_http import card, serve
from tests.native_fixtures import assistant_output, responses as responses, tool_output
from tests.test_a2a_client import endpoint
from tests.test_deferred_jobs import Journal, Row, declared, prepare_one
from tests.test_native_manual_tools import next_request, provider as native_provider

TOKENS = {"A": "synthetic-still-valid-bearer-A", "B": "synthetic-still-valid-bearer-B"}


class Authority:
    """Test application authority; persisted IDs describe overlapping token lifetimes."""

    def __init__(self, directory):
        self.path = directory / "authority.json"
        if not self.path.exists():
            self.path.write_text(json.dumps({"current": "A", "live": ["A", "B"]}))
        self.denied = []

    def policy(self):
        return json.loads(self.path.read_text())

    async def token(self):
        return TOKENS[self.policy()["current"]]

    def valid(self, token):
        return token in [TOKENS[label] for label in self.policy()["live"]]

    def rotate(self):
        self.path.write_text(json.dumps({"current": "B", "live": ["A", "B"]}))

    def admit(self, value, stage):
        # The host consults its authority, not credentials remembered by an SDK instance.
        raw = json.dumps(value)
        if any(TOKENS[label] in raw for label in self.policy()["live"]):
            self.denied.append(stage)
            raise PermissionError("Host credential admission denied")


class AdmissionJournal(Journal):
    """Persist one test call; retain the existing journal's binding/generation checks."""

    def __init__(self, directory, authority):
        super().__init__()
        self.path, self.authority = directory / "journal.json", authority
        self.writes = 0
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            data = saved["row"]
            data["update"] = JobUpdate(**data["update"]) if data["update"] else None
            data["mutations"] = set(data["mutations"])
            self.rows[JobBinding(**saved["binding"])] = Row(**data)
            self.state_json = saved["state"]

    def payload(self):
        ((binding, row),) = self.rows.items()
        data = asdict(row)
        data["mutations"] = sorted(row.mutations)
        return {"binding": asdict(binding), "row": data, "state": self.state_json}

    def persist(self):
        value = self.payload()
        self.authority.admit(value, "persist")
        self.path.write_text(json.dumps(value))
        self.writes += 1

    async def prepare(self, state_json, bindings):
        self.authority.admit(state_json, "checkpoint")
        await super().prepare(state_json, bindings)
        self.persist()

    async def claim(self, binding, operation):
        # Legacy rows must pass current policy before the SDK can project or deliver them.
        self.authority.admit(self.payload(), "claim")
        claim = await super().claim(binding, operation)
        self.persist()
        return claim

    async def authorize(self, claim, operation):
        self.authority.admit(
            {"update": asdict(claim.update) if claim.update else None, "result": claim.result_json},
            "authorize",
        )
        await super().authorize(claim, operation)

    async def record(self, claim, update):
        self.authority.admit(asdict(update), "record")
        await super().record(claim, update)
        self.persist()

    async def save_result(self, claim, result_json):
        self.authority.admit(result_json, "save_result")
        await super().save_result(claim, result_json)
        self.persist()


def configured(directory, url):
    authority = Authority(directory)
    journal = AdmissionJournal(directory, authority)
    projected = []

    async def reserve(claim, operation, action):
        await journal.authorize(claim, operation)
        return True  # Each test issues exactly one creation; no retry guarantee is claimed.

    def project(task):
        authority.admit(MessageToDict(task), "project")
        projected.append(task.id)
        return {"public": task.artifacts[0].parts[0].text}

    backend = A2AJobProvider(
        endpoint(url),
        authorize=journal.authorize,
        authorization=authority.token,
        reserve=reserve,
        encode=lambda value: new_text_message(str(value.pages), role=p.ROLE_USER),
        project=project,
    )
    jobs, tool = declared(
        journal, backend, tenant="alice", provider_id=backend.id, require_reconciliation=False
    )
    return authority, journal, jobs, tool, projected


def peer(url, authority, state):
    async def rpc(request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        assert authority.valid(token)
        body = await request.json()
        state["calls"].append((body["method"], "A" if token == TOKENS["A"] else "B"))
        task = state["task"]
        result = {"task": task} if body["method"] == "SendMessage" else task
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})

    return Starlette(
        routes=[
            Route(
                "/.well-known/agent-card.json",
                lambda request: JSONResponse(MessageToDict(card(url))),
            ),
            Route("/rpc", rpc, methods=["POST"]),
        ]
    )


def task_payload(kind):
    task = {
        "id": "remote-task",
        "contextId": "remote-context",
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"artifactId": "final", "parts": [{"text": "benign result"}]}],
    }
    if kind == "completed":
        task["artifacts"][0]["parts"][0]["text"] = TOKENS["A"]
    elif kind == "receipt":
        task["id"] += TOKENS["A"]
        task["status"] = {"state": "TASK_STATE_WORKING"}
        task.pop("artifacts")
    elif kind in {"input", "auth"}:
        task.pop("artifacts")
        parts = (
            [
                {
                    "data": {
                        "challenge": "current",
                        "generation": 1,
                        "arguments": {"credential": TOKENS["A"]},
                    }
                }
            ]
            if kind == "input"
            else [{"text": TOKENS["A"]}]
        )
        task["status"] = {
            "state": "TASK_STATE_INPUT_REQUIRED" if kind == "input" else "TASK_STATE_AUTH_REQUIRED",
            "message": {"parts": parts},
        }
    return task


async def rotate_with_overlap(authority, url, state):
    async def old_token():
        return TOKENS["A"]

    authority.rotate()
    # A remains a usable credential after B becomes the host's selected bearer.
    async with connect_a2a(endpoint(url), authorization=old_token) as client:
        await client.get_task(p.GetTaskRequest(id="remote-task"))
    assert state["calls"][-1] == ("GetTask", "A")
    assert authority.valid(TOKENS["A"]) and authority.valid(TOKENS["B"])


@pytest.mark.parametrize("kind", ["completed", "input", "auth", "receipt"])
async def test_host_rejects_rotated_live_bearer_before_storage_or_model(tmp_path, responses, kind):
    authority = Authority(tmp_path)
    state = {"task": task_payload("benign"), "calls": []}
    async with serve(lambda url: peer(url, authority, state)) as url:
        await rotate_with_overlap(authority, url, state)
        authority, journal, jobs, tool, projected = configured(tmp_path, url)
        agent = Agent(instructions="Synthetic", tools=[tool], provider=native_provider(responses))
        prepared, _ = await prepare_one(agent, jobs, tool, responses)
        responses.respond(assistant_output("Must not receive rejected data"))
        state["task"] = task_payload(kind)
        async with agent.session(state=prepared) as session:
            request = await next_request(session.interactions(), tool)
            if kind == "completed":
                assert await jobs.advance(session, request) == "unknown"
                assert authority.denied == ["project"] and not projected
            else:
                with pytest.raises(PermissionError, match="denied"):
                    await jobs.advance(session, request)
                assert authority.denied == ["record"] and not projected
        assert state["calls"] == [("GetTask", "A"), ("SendMessage", "B")]
        assert len(responses.requests) == 1
        assert all(
            secret not in journal.path.read_text() + json.dumps(responses.requests)
            for secret in TOKENS.values()
        )
        assert next(iter(journal.rows.values())).result is None


async def test_benign_result_is_delivered_after_overlapping_rotation(tmp_path, responses):
    authority = Authority(tmp_path)
    state = {"task": task_payload("benign"), "calls": []}
    async with serve(lambda url: peer(url, authority, state)) as url:
        await rotate_with_overlap(authority, url, state)
        authority, journal, jobs, tool, projected = configured(tmp_path, url)
        agent = Agent(instructions="Synthetic", tools=[tool], provider=native_provider(responses))
        prepared, _ = await prepare_one(agent, jobs, tool, responses)
        responses.respond(assistant_output("finished"))
        async with agent.session(state=prepared) as session:
            request = await next_request(session.interactions(), tool)
            turn = await jobs.advance(session, request)
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "finished"
        assert len(responses.requests) == 2
        assert json.loads(tool_output(responses.requests[1], "render-A")) == {
            "report": "benign result"
        }
        assert projected == ["remote-task"] and not authority.denied
        assert all(
            secret not in journal.path.read_text() + json.dumps(responses.requests)
            for secret in TOKENS.values()
        )


async def test_fresh_worker_rejects_legacy_row_under_restored_authority(tmp_path, responses):
    authority = Authority(tmp_path)
    state = {"task": task_payload("benign"), "calls": []}
    async with serve(lambda url: peer(url, authority, state)) as url:
        await rotate_with_overlap(authority, url, state)
        _, journal, jobs, tool, _ = configured(tmp_path, url)
        agent = Agent(instructions="Synthetic", tools=[tool], provider=native_provider(responses))
        await prepare_one(agent, jobs, tool, responses)
        # Simulate data written before this host's admission policy was deployed.
        saved = json.loads(journal.path.read_text())
        saved["row"]["update"] = asdict(
            JobUpdate(
                "completed",
                json.dumps({"task_id": "remote-task", "context_id": "remote-context"}),
                {"public": TOKENS["A"]},
            )
        )
        saved["row"]["result"] = json.dumps({"report": TOKENS["A"]})
        journal.path.write_text(json.dumps(saved))
        before = journal.path.read_bytes()
        root = Path(__file__).resolve().parents[1]
        env = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
        env["PYTHONPATH"] = os.pathsep.join(
            str(path) for path in (root, root / "src", root.parents[1] / "src") if path.is_dir()
        )
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.a2a_admission_worker",
            str(tmp_path),
            url,
            cwd=root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(worker.communicate(), 30)
        finally:
            if worker.returncode is None:
                worker.kill()
                await worker.communicate()
        assert all(secret not in (stdout + stderr).decode() for secret in TOKENS.values())
        assert worker.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        assert result == {"denied": True, "model_requests": 0, "writes": 0, "policy": "B"}
        assert journal.path.read_bytes() == before
        assert state["calls"] == [("GetTask", "A")]
