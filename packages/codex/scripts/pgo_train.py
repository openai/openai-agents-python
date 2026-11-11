"""Unscored, loopback-only input for Maturin's instrumented wheel training."""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import os
import sys
import sysconfig
from contextlib import AsyncExitStack
from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
PRIVATE_SRC = ROOT / "packages/codex/src"
# Counts include the original prefill: one per case, three for session reuse.
CASES = (
    ("text-short", 121, 96, 80, 1),
    ("text-varied", 121, 768, 384, 1),
    ("tool", 33, 224, 192, 1),
    ("default-sse", 49, 256, 512, 7),
    ("public-stream", 17, 512, 1536, 24),
    ("fallback-input", 17, 49152, 288, 1),
    ("fallback-output", 17, 320, 98304, 96),
    ("session-reuse", 27, 384, 320, 1),
    ("held-late-read", 25, 256, 256, 4),
)


def prepare_imports() -> None:
    if sys.flags.optimize or "PYTHONOPTIMIZE" in os.environ:
        raise RuntimeError("PGO training requires Python assertions")
    if not os.environ.get("LLVM_PROFILE_FILE"):
        raise RuntimeError("Run this script through Maturin --pgo")
    prefix = Path(sys.prefix).resolve()
    active = os.environ.get("VIRTUAL_ENV")
    if sys.prefix == sys.base_prefix or not active or Path(active).resolve() != prefix:
        raise RuntimeError("PGO training requires the active Maturin virtual environment")
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    os.environ["OPENAI_AGENTS_DONT_LOG_MODEL_DATA"] = "1"
    # Loopback training must not inherit a network proxy.
    proxy_names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    for name in proxy_names:
        os.environ.pop(name, None)
    # The temporary Maturin wheel lacks this private Python runtime. Supply only
    # its matching source; agents, _agents_shared and the extension must remain installed.
    sys.path.insert(0, str(PRIVATE_SRC))
    agents = importlib.import_module("agents")
    native = importlib.import_module("agents._native")
    private = importlib.import_module("_agents_codex")
    distribution = importlib.metadata.distribution("openai-agents")
    public_file = Path(agents.__file__).resolve()
    assert public_file == Path(distribution.locate_file("agents/__init__.py")).resolve()
    sites = {Path(sysconfig.get_path(key)).resolve() for key in ("purelib", "platlib")}
    assert public_file.parent.parent in sites
    assert all(site.is_relative_to(prefix) for site in sites)
    native_file = Path(native.__file__).resolve()
    assert native_file.parent == public_file.parent
    assert any(native_file.name.endswith(suffix) for suffix in EXTENSION_SUFFIXES)
    recorded = {file.as_posix() for file in distribution.files or ()}
    assert native_file.relative_to(public_file.parent.parent).as_posix() in recorded
    for name, relative in (
        ("_agents_shared", "_agents_shared/__init__.py"),
        ("_agents_shared.strict_schema", "_agents_shared/strict_schema.py"),
    ):
        assert relative in recorded
        module = importlib.import_module(name)
        assert Path(module.__file__).resolve() == public_file.parent.parent / relative
    assert Path(private.__file__).resolve() == PRIVATE_SRC / "_agents_codex/__init__.py"
    sys.path.insert(0, str(ROOT / "packages/codex/tests"))


def prompt_for(identity: str, length: int) -> str:
    marker = f"PGO_RUN={identity} "
    alphabet = 'alpha09_\\"é☃ beta|'
    return marker + (alphabet * (length // len(alphabet) + 1))[: length - len(marker)]


async def check_events(turn, result) -> None:
    events = [event async for event in turn.events()]
    assert events[0].type == "turn_started"
    assert events[-1].type == "turn_finished" and events[-1].status == "completed"
    assert not any(event.type == "gap" for event in events)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert all(
        event.session_id == turn.session_id and event.turn_id == turn.turn_id for event in events
    )
    deltas = "".join(event.delta for event in events if event.type == "output_text_delta")
    assert deltas == result.final_output
    assert await turn.get_result() is result


async def train_case(case) -> tuple[int, int, int]:
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from native_fixtures import ResponsesFixture, function_call, text_response, tool_output

    from agents import Agent, NativeProvider, function_tool

    name, count, input_chars, output_chars, chunks = case
    streaming = name in ("default-sse", "public-stream")
    fixture = ResponsesFixture(finite_http_response=not streaming)
    app = web.Application()

    async def handle(request):
        assert (await request.json())["stream"] is streaming
        response = await fixture.handle(request)
        if streaming:
            return response
        # Finite SSE bytes still use the streaming wire format. Buffered
        # providers require one JSON Response with the completed output items.
        events = [json.loads(line[6:]) for line in response.body.splitlines() if line]
        completed = events[-1]["response"]
        return web.json_response(
            {
                **completed,
                "object": "response",
                "status": "completed",
                "output": [
                    event["item"]
                    for event in events
                    if event["type"] == "response.output_item.done"
                ],
            }
        )

    app.router.add_post("/v1/responses", handle)
    callbacks, held = [], []
    sessions, turns = set(), set()

    @function_tool
    async def echo(value: str) -> str:
        """Return the supplied synthetic value unchanged."""
        callbacks.append(value)
        return value

    async with TestServer(app, host="127.0.0.1") as server:
        base_url = str(server.make_url("/v1"))
        url = urlsplit(base_url)
        assert url.scheme == "http" and url.hostname == "127.0.0.1" and url.port
        options = {} if streaming else {"response_mode": "buffered"}
        async with NativeProvider(
            base_url=base_url, api_key="synthetic-pgo-key", supports_websockets=False, **options
        ) as provider:
            agent = Agent(
                name="pgo-training",
                model="gpt-5.1-codex",
                provider=provider,
                instructions="Follow the synthetic request and return its result.",
                tools=[echo] if name == "tool" else [],
            )
            async with AsyncExitStack() as stack:
                reused = (
                    await stack.enter_async_context(agent.session())
                    if name == "session-reuse"
                    else None
                )
                history = []
                index = -1
                try:
                    for index in range(count):
                        identity = f"{name}-{index}"
                        prompt = prompt_for(identity, input_chars)
                        seed = f"verified:{identity}|"
                        expected = (seed * (output_chars // len(seed) + 1))[:output_chars]
                        start = len(fixture.requests)
                        if name == "tool":
                            call = function_call("echo", {"value": identity}, identity)
                            call["item"]["id"] = f"fc_{identity}"
                            fixture.respond(call)
                        fixture.respond(
                            *text_response(
                                f"msg_{identity}",
                                *(
                                    expected[
                                        i * output_chars // chunks : (i + 1)
                                        * output_chars
                                        // chunks
                                    ]
                                    for i in range(chunks)
                                ),
                            )
                        )
                        if name == "public-stream":
                            async with agent.session() as session:
                                turn = await session.submit(prompt)
                                # Open the public cursor before awaiting completion.
                                events = [event async for event in turn.events()]
                                result = await turn.get_result()
                                deltas = "".join(
                                    e.delta for e in events if e.type == "output_text_delta"
                                )
                                assert deltas == expected
                                assert not any(e.type == "gap" for e in events)
                        elif reused is not None:
                            turn = await reused.submit(prompt)
                            result = await turn.get_result()
                        else:
                            result = await agent.run(prompt)
                            turn = result.turn
                        assert result.final_output == expected
                        assert await turn.get_result() is result
                        turn_identity = (turn.session_id, turn.turn_id)
                        assert turn_identity not in turns
                        turns.add(turn_identity)
                        if reused is None:
                            assert turn.session_id not in sessions
                        sessions.add(turn.session_id)
                        history = [*history, prompt] if reused is not None else [prompt]
                        requests = fixture.requests[start:]
                        assert len(requests) == (2 if name == "tool" else 1)
                        for request in requests:
                            users = [
                                "".join(part["text"] for part in item["content"])
                                for item in request["input"]
                                if item.get("role") == "user"
                            ]
                            assert users == history
                        if name == "tool":
                            assert callbacks == [f"tool-{i}" for i in range(index + 1)]
                            assert tool_output(requests[1], identity) == identity
                        if name == "held-late-read":
                            held.append(result)
                except Exception as error:
                    raise RuntimeError(
                        f"PGO case={name} index={index} received={len(fixture.requests)} "
                        f"queued={len(fixture.replies)}"
                    ) from error
            assert not fixture.replies
        # The borrowed Provider and every Session have closed before late replay.
        for result in held:
            await check_events(result.turn, result)
    assert len(sessions) == (1 if name == "session-reuse" else count)
    assert len(fixture.requests) == count * (2 if name == "tool" else 1)
    assert len(callbacks) == (count if name == "tool" else 0)
    return count, len(fixture.requests), len(callbacks)


async def main() -> None:
    totals = [0, 0, 0]
    for case in CASES:
        print(f"PGO training: {case[0]} ({case[1]} calls)", file=sys.stderr, flush=True)
        totals = [left + right for left, right in zip(totals, await train_case(case), strict=True)]
    assert totals == [427, 460, 33]


if __name__ == "__main__":
    prepare_imports()
    # Return normally so LLVM can flush its process-exit profile.
    asyncio.run(main())
