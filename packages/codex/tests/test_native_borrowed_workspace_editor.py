"""Native handoff keeps the selected workspace editor on borrowed Chat providers."""

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    AnyLLMModel,
    Environment,
    LitellmModel,
    NativeProvider,
    sandbox_apply_patch_tool,
)
from agents.classic.sandbox.entries import File
from agents.sandbox import Manifest, sandbox_environment
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.test_native_workspace_patch_format import ReplaceFormat

if sys.platform in ("linux", "darwin"):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="UnixLocal resource"
)


@pytest.mark.parametrize("backend", ["litellm", "anyllm"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("granted", [True, False])
async def test_selected_workspace_editor_chat_provider(
    tmp_path, monkeypatch, backend, stream, granted
):
    root = tmp_path / "root"
    destination = tmp_path / "destination"
    for workspace in (root, destination):
        (workspace / "nested").mkdir(parents=True)
        (workspace / "nested/value.txt").write_text(workspace.name)
    editor = ReplaceFormat()
    wire = []
    patch = {"type": "update_file", "path": "value.txt", "diff": " edited"}

    async def handle(request):
        body = await request.json()
        wire.append(body)
        message = {"role": "assistant", "content": None}
        finish = "tool_calls"
        if len(wire) <= 2:
            name, call_id, arguments = (
                ("transfer_to_editor", "handoff-call", {})
                if len(wire) == 1
                else ("apply_patch", "editor-call", {"operation": patch})
            )
            call = {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            if body.get("stream"):
                call["index"] = 0
            message["tool_calls"] = [call]
        else:
            message["content"] = "Done."
            finish = "stop"
        shared = {"id": f"chat_{len(wire)}", "model": "gpt-5.5", "created": 1}
        if not body.get("stream"):
            return web.json_response(
                {
                    **shared,
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 2,
                        "total_tokens": 6,
                    },
                }
            )
        frames = [
            {
                **shared,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            }
            for delta, reason in ((message, None), ({}, finish))
        ]
        return web.Response(
            text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n",
            content_type="text/event-stream",
        )

    def source(workspace: Path, tools: list[str]):
        @asynccontextmanager
        async def connect(_resource):
            async with ExecutorPeer(tmp_path / uuid4().hex).serve() as peer:
                assert peer.ca is not None
                trust_bundle = tmp_path / "trusted.pem"
                with trust_bundle.open("ab") as certificates:
                    certificates.write(peer.ca.read_bytes())
                empty = tmp_path / "empty-certs"
                empty.mkdir(exist_ok=True)
                monkeypatch.setenv("SSL_CERT_FILE", str(trust_bundle))
                monkeypatch.setenv("SSL_CERT_DIR", str(empty))
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(workspace / "nested"),
                    tools=tools,
                )

        return sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(
                root=str(workspace),
                entries={
                    Path("guide.txt"): File(
                        content=b"guide",
                        description=f"WORKSPACE_{workspace.name.upper()}_ONLY",
                    )
                },
            ),
            options=UnixLocalSandboxClientOptions(),
            connect=connect,
        )

    async def run(adapter):
        async with NativeProvider(model_backend=adapter) as provider:
            target = Agent(
                name="Editor",
                instructions="Edit in the selected workspace.",
                environment=source(destination, ["apply_patch"] if granted else []),
                tools=[sandbox_apply_patch_tool(editor)],
            )
            agent = Agent(
                name="Source",
                model="gpt-5.5",
                provider=provider,
                instructions="Transfer to the editor.",
                handoffs=[target],
            )
            async with agent.session(environment=source(root, [])) as session:
                turn = await session.submit("Route and edit.")
                assert (await turn.get_result()).final_output == "Done."
                history = json.dumps((await session.history_snapshot()).items)
                assert "WORKSPACE_ROOT_ONLY" not in history
                assert "WORKSPACE_DESTINATION_ONLY" not in history

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    async with timeout(40), TestServer(app) as server:
        base = str(server.make_url("/v1"))
        if backend == "anyllm":
            any_llm = pytest.importorskip("any_llm")
            client = any_llm.AnyLLM.create(
                "openai", api_base=base, api_key="synthetic-editor", max_retries=0
            )
            try:
                await run(AnyLLMModel(client, "gpt-5.5", stream=stream))
            finally:
                await client.client.close()
        else:
            monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
            litellm = pytest.importorskip("litellm")
            monkeypatch.setattr(litellm, "telemetry", False)
            async with AsyncOpenAI(
                base_url=base, api_key="synthetic-editor", max_retries=0
            ) as client:

                async def create(**options):
                    return await litellm.acompletion(
                        **options,
                        client=client,
                        api_base=base,
                        api_key="synthetic-editor",
                        allowed_openai_params=["tool_choice", "reasoning_effort"],
                    )

                await run(
                    LitellmModel(
                        SimpleNamespace(acompletion=create),
                        "openai/gpt-4o-mini",
                        stream=stream,
                    )
                )

    def declarations(request):
        return {tool["function"]["name"] for tool in request.get("tools", [])}

    for request, expected in zip(
        wire,
        ["WORKSPACE_ROOT_ONLY", *["WORKSPACE_DESTINATION_ONLY"] * (len(wire) - 1)],
        strict=True,
    ):
        messages = request["messages"]
        assert json.dumps(messages).count(expected) == 1
        foreign = (
            "WORKSPACE_DESTINATION_ONLY"
            if expected == "WORKSPACE_ROOT_ONLY"
            else "WORKSPACE_ROOT_ONLY"
        )
        assert foreign not in json.dumps(messages)
        [guidance] = [message for message in messages if expected in json.dumps(message)]
        assert guidance["role"] == "user"

    assert declarations(wire[0]) == {"transfer_to_editor"}
    assert [bool(request.get("stream")) for request in wire] == [stream] * len(wire)
    assert (root / "nested/value.txt").read_text() == "root"
    assert len(wire) == 3
    assert (destination / "nested/value.txt").read_text() == (
        "destination edited" if granted else "destination"
    )
    if not granted:
        assert "apply_patch" not in declarations(wire[1])
        assert not editor.calls
        [rejected] = [
            item for item in wire[2]["messages"] if item.get("tool_call_id") == "editor-call"
        ]
        assert rejected["content"] == "unsupported call: apply_patch"
        return

    assert "apply_patch" in declarations(wire[1])
    assert editor.calls == [("destination", " edited", "default")]
    [result] = [item for item in wire[2]["messages"] if item.get("tool_call_id") == "editor-call"]
    assert json.loads(result["content"])["status"] == "completed"
