"""A simulated user and model, the SDK client, and a loopback A2A 1.0 peer."""

import asyncio
import json
from collections.abc import Callable

from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, RunConfig, Runner
from agents.extensions.a2a import A2AClient
from agents.testing import ScriptedModel, function_call

from . import specialist

QUESTION = "Check readiness of release R42."


def local_client(url: str, token: Callable[[], str]) -> A2AClient:
    card = {
        "name": "Local release reviewer",
        "description": "Readiness checks",
        "version": "1",
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
        "supportedInterfaces": [
            {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
    }
    return A2AClient(rpc_url=url, card_json=json.dumps(card).encode(), rpc_token_provider=token)


def release_agent(url: str, user: str, grants: dict[tuple[str, str, str], int]) -> Agent[None]:
    scope = (user, url, QUESTION)
    selected_revision = grants[scope]

    def token() -> str:
        if grants.get(scope) != selected_revision:
            raise PermissionError("Grant changed.")
        return "synthetic-token"

    client = local_client(url, token)
    check = specialist(
        client, question=QUESTION, artifact_id="readiness", verdicts=("ready", "blocked")
    )
    return Agent(
        name="Release manager",
        plugins=[check],
        model=ScriptedModel(
            [
                [function_call("ask_specialist", {}, call_id="submit")],
                [function_call("refresh_specialist", {}, call_id="read")],
            ]
        ),
        tool_use_behavior="stop_on_first_tool",
    )


async def reviewer(request: web.Request) -> web.Response:
    """Simulate a pending check followed by the completed task."""
    if request.headers.get("Authorization") != "Bearer synthetic-token":
        raise web.HTTPForbidden()
    body = await request.json()
    task = {"id": "demo-task", "contextId": "demo-context"}
    result: dict[str, object]
    if body["method"] == "SendMessage" and body["params"]["message"]["parts"] == [
        {"text": QUESTION}
    ]:
        result = {"task": {**task, "status": {"state": "TASK_STATE_WORKING"}}}
    elif body["method"] == "GetTask" and body["params"]["id"] == task["id"]:
        result = {
            **task,
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "readiness", "parts": [{"text": "ready"}]}],
        }
    else:
        raise web.HTTPBadRequest()
    return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})


async def main() -> None:
    app = web.Application()
    app.router.add_post("/rpc", reviewer)
    async with TestServer(app) as peer:
        url = str(peer.make_url("/rpc"))
        user = "demo-user"
        agent = release_agent(url, user, {(user, url, QUESTION): 1})
        config = RunConfig(tracing_disabled=True)
        for operation in ("ask_specialist", "refresh_specialist"):
            step = await Runner.run(agent, operation, run_config=config)
            (call,) = step.interruptions
            assert call.name == operation
            state = step.to_state()
            # This approval is simulated only for the loopback peer and scripted model.
            state.approve(call)
            result = await Runner.run(agent, state, run_config=config)
            print(f"{operation} -> {result.final_output}")


if __name__ == "__main__":
    asyncio.run(main())
