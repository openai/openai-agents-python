"""Serve synthetic Responses turns and check which client sends each request."""

import asyncio
import json
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from openai import AsyncOpenAI

from agents.models.openai_provider import OpenAIProvider

MODEL = "gpt-5.4"
KEY = "loopback-fixture"


def reply(turn: int, calls) -> dict:
    if turn < len(calls):
        name, arguments = calls[turn]
        item = {
            "id": f"fc_{turn}",
            "type": "function_call",
            "call_id": f"call_{turn}",
            "name": name,
            "arguments": json.dumps(arguments),
            "status": "completed",
        }
    else:
        item = {
            "id": "msg_final",
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "done", "annotations": []}],
            "status": "completed",
        }
    return {
        "id": f"resp_{turn}",
        "object": "response",
        "created_at": 1,
        "model": MODEL,
        "status": "completed",
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "tools": [],
        "output": [item],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


@asynccontextmanager
async def route(codex, workspace, requests, calls):
    # ScriptedModel bypasses the native provider. Check the HTTP client on every turn.
    prefix = "codex_cli_rs/" if codex else "Agents/Python "

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if (
                self.path != "/v1/responses"
                or self.headers.get("Authorization") != f"Bearer {KEY}"
                or not self.headers.get("User-Agent", "").startswith(prefix)
            ):
                self.send_error(400)
                return
            response = reply(len(requests), calls)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                events = [
                    {
                        "type": "response.created",
                        "response": {**response, "output": [], "status": "in_progress"},
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": response["output"][0],
                    },
                    {"type": "response.completed", "response": response},
                ]
                for event in events:
                    self.wfile.write(
                        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                    )
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

    async with AsyncExitStack() as stack:
        server = stack.enter_context(HTTPServer(("127.0.0.1", 0), Handler))
        endpoint = f"http://127.0.0.1:{server.server_port}/v1"
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
        )
        thread.start()
        stack.callback(thread.join)
        stack.push_async_callback(asyncio.to_thread, server.shutdown)
        if not codex:
            client = await stack.enter_async_context(
                AsyncOpenAI(api_key=KEY, base_url=endpoint, max_retries=0)
            )
            provider = OpenAIProvider(
                openai_client=client, use_responses=True, use_responses_websocket=False
            )
        else:
            from agents.models.codex import CodexProvider

            model_spec = {
                "slug": MODEL,
                "display_name": "Local fixture",
                "base_instructions": "Use available tools.",
                "shell_type": "unified_exec",
                "apply_patch_tool_type": "freeform",
                "tool_mode": "direct",
                "supported_reasoning_levels": [],
                "visibility": "list",
                "supported_in_api": True,
                "support_verbosity": False,
                "experimental_supported_tools": [],
                "priority": 0,
                "truncation_policy": {"mode": "tokens", "limit": 10000},
            }
            catalog = workspace.resolve() / ".peer-models.json"
            catalog.write_text(json.dumps({"models": [model_spec]}))
            provider = CodexProvider(
                cwd=workspace,
                api_key=KEY,
                config_overrides=[
                    'model_provider="offline"',
                    f"sqlite_home={json.dumps(str(workspace.resolve()))}",
                    f"model_catalog_json={json.dumps(str(catalog))}",
                    'model_providers.offline.name="Local fixture"',
                    f"model_providers.offline.base_url={json.dumps(endpoint)}",
                    'model_providers.offline.wire_api="responses"',
                    "model_providers.offline.requires_openai_auth=true",
                    "model_providers.offline.request_max_retries=0",
                    "model_providers.offline.stream_max_retries=0",
                    'approval_policy="never"',
                    'sandbox_mode="read-only"',
                    "check_for_update_on_startup=false",
                    "features.plugins=false",
                    "features.code_mode=false",
                    "features.hooks=false",
                    "agents.enabled=false",
                    "features.multi_agent_v2=false",
                ],
            )
        stack.push_async_callback(provider.aclose)
        yield provider
