"""Check five installed plugins with loopback inference. Pass --codex to use Codex."""

import argparse
import asyncio
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from agents import Agent, RunConfig, Runner, RunState

if __package__:
    from .contract.loopback import MODEL, route
else:
    from contract.loopback import MODEL, route

REVISION = "five-plugins-v1"
CALLS = [
    ("browser_read", {"action": "restricted"}),
    ("browser_read", {"action": "hours"}),
    ("postman_list_requests", {}),
    ("postman_check_request", {"name": "Ping"}),
    ("ask_specialist", {}),
    ("refresh_specialist", {}),
]


async def verify(codex: bool, workspace: Path):
    import httpx2
    from a2a.types import Artifact, Part, Task, TaskState, TaskStatus
    from agents_browser import browser
    from agents_langsmith import langsmith
    from agents_permissions import permissions
    from agents_postman import postman
    from agents_specialist import specialist

    requests, records, effects = [], [], []
    url = "https://example.test/private-endpoint"
    collection = {
        "info": {
            "name": "Health",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": [
            {
                "name": "Monitoring",
                "item": [{"name": "Ping", "request": {"method": "GET", "url": {"raw": url}}}],
            }
        ],
    }

    class Specialist:
        card_json = b"{}"

        def rpc_token_provider(self):
            return "private-credential"

        async def send_message(self, text, *, message_id):
            effects.append(("send", text))
            assert message_id
            return Task(
                id="private-task",
                context_id="private-context",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            )

        async def get_task(self, *, task_id, context_id):
            effects.append(("refresh", task_id, context_id))
            return Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                artifacts=[Artifact(artifact_id="readiness", parts=[Part(text="ready")])],
            )

    class Sink:
        def create_run(self, **record):
            records.append(record)

    def ping(request):
        effects.append((request.method, str(request.url)))
        return httpx2.Response(200, text="private-body")

    async def hours():
        effects.append("hours")
        return "9 to 5"

    async def restricted():
        effects.append("restricted")
        return "private-page"

    def resolve(agent, policy, call, app):
        if policy != "permissions":
            return {}
        if call.owner == "browser" and call.arguments.get("action") == "restricted":
            return {"decision": "review"} if app["grant"] else {}
        if call.owner in {"browser", "postman", "specialist"}:
            return {"decision": "allow"}
        return {}

    async with (
        route(codex, workspace, requests, calls=CALLS) as provider,
        httpx2.AsyncClient(transport=httpx2.MockTransport(ping), trust_env=False) as client,
    ):
        agent = Agent(
            name="Manager",
            model=MODEL,
            plugins=[
                permissions(),
                browser({"hours": hours, "restricted": restricted}),
                postman(client, collection, approved={"Ping": url}),
                specialist(
                    Specialist(),
                    question="private-question",
                    artifact_id="readiness",
                    verdicts=("ready", "blocked"),
                ),
                langsmith(Sink(), project_name="Trial"),
            ],
        )
        config = RunConfig(
            model_provider=provider,
            policy_context=resolve,
            plugin_revision=REVISION,
            tracing_disabled=True,
        )
        result = await Runner.run(
            agent,
            "Check hours and release readiness. Refresh if pending. CHAT_MARKER",
            context={"grant": True, "tenant": "private-tenant"},
            run_config=config,
        )
        for turn, count in ((0, 0), (4, 2), (5, 3)):
            (pending,) = result.interruptions
            assert pending.name == CALLS[turn][0] and pending.call_id == f"call_{turn}"
            assert len(effects) == count
            state = result.to_state()
            state.approve(pending)
            snapshot = state.to_string()
            assert "private-" not in snapshot
            # Restore only trusted state. The host revokes the restricted action after review.
            state = await RunState.from_string(
                agent,
                snapshot,
                plugin_revision=REVISION,
                context_override={"grant": False, "tenant": "private-tenant"},
            )
            result = await Runner.run(agent, state, run_config=config)

    assert result.final_output == "done" and not result.interruptions
    assert effects == [
        "hours",
        ("GET", url),
        ("send", "private-question"),
        ("refresh", "private-task", "private-context"),
    ]
    assert len(requests) == len(CALLS) + 1
    outputs = []
    for turn, (name, _) in enumerate(CALLS):
        assert name in [tool.get("name") for tool in requests[turn]["tools"]]
        (item,) = [
            i
            for i in requests[turn + 1]["input"]
            if i.get("type") == "function_call_output" and i.get("call_id") == f"call_{turn}"
        ]
        outputs.append(item["output"])
    assert outputs[0] == "Tool call refused."
    assert json.loads(outputs[1]) == {"action": "hours", "text": "9 to 5"}
    assert json.loads(outputs[2]) == ["Ping"]
    assert outputs[3:] == ["HTTP 200", "pending", "ready"]
    assert [r["outputs"] for r in records] == [{"status": "paused"}] * 3 + [{"status": "completed"}]
    assert all(r["inputs"] == {} and r["project_name"] == "Trial" for r in records)
    assert "CHAT_MARKER" not in repr(records) and "9 to 5" not in repr(records)
    assert "private-" not in repr(records) + repr(requests) + repr(result.to_input_list())
    assert ("agents.models._codex_native" in sys.modules) == codex
    assert not {"playwright", "langsmith"}.intersection(sys.modules)
    print(
        json.dumps(
            {
                "sdk": version("openai-agents"),
                "route": "codex" if codex else "python",
                "requests": len(requests),
                "outcomes": [r["outputs"]["status"] for r in records],
                "outputs": outputs,
            }
        )
    )


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Run this check without Python optimization.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory() as tmp:
        os.environ["CODEX_HOME"] = tmp
        asyncio.run(asyncio.wait_for(verify(args.codex, Path(tmp)), 60))
        assert not (Path(tmp) / "auth.json").exists()
