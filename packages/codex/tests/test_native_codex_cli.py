"""Native declarations and real subprocess delegation with a synthetic CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agents import Agent, FunctionTool, ToolContext, function_tool
from agents.codex import CodexOptions, TurnOptions, codex_tool
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Synthetic executable uses a Unix shebang")


async def _run_in_native_session(
    responses: Any, callback: Callable[[ToolContext[object]], Awaitable[Any]]
) -> Any:
    result: Any = None

    @function_tool
    async def invoke(context: ToolContext[object]) -> str:
        nonlocal result
        result = await callback(context)
        return "CLI callback completed"

    responses.replies.extend(
        [[function_call("invoke", {}, "cli-callback")], [assistant_output("done")]]
    )
    async with Agent(
        instructions="Run the callback.", provider=provider(responses), tools=[invoke]
    ).session() as session:
        assert (await (await session.submit("Run")).get_result()).final_output == "done"
    return result


def cli(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic-codex"
    events = [
        {"type": "thread.started", "thread_id": "synthetic-thread"},
        {
            "type": "item.completed",
            "item": {
                "id": "item-1",
                "type": "agent_message",
                "text": "CLI completed",
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 3,
                "cached_input_tokens": 1,
                "output_tokens": 2,
            },
        },
    ]
    path.write_text(
        f"#!{sys.executable}\nimport json, sys\ninput = sys.stdin.read()\n"
        f"open({str(tmp_path / 'stdin.txt')!r}, 'w').write(input)\n"
        f"open({str(tmp_path / 'arguments.jsonl')!r}, 'a').write("
        "json.dumps(sys.argv[1:]) + '\\n')\n"
        f"events = {events!r}\n"
        "for event in events: print(json.dumps(event), flush=True)\n"
    )
    path.chmod(0o700)
    return path


async def test_native_cli_forwards_structured_text_and_local_images(tmp_path: Path, responses):
    executable = cli(tmp_path)
    image = "  authorized diagram.png  "
    (tmp_path / image).write_bytes(b"synthetic image")
    references = []

    async def resolve_image(reference):
        references.append(reference)
        await asyncio.sleep(0)
        return image

    declaration = codex_tool(
        codex_options={"codex_path_override": str(executable)},
        thread_options={"working_directory": str(tmp_path)},
        resolve_image=resolve_image,
    )
    responses.replies.extend(
        [
            [
                function_call(
                    "codex",
                    {
                        "prompt": [
                            {"type": "text", "text": "  First\n"},
                            {"type": "local_image", "path": " asset:diagram "},
                            {"type": "text", "text": "\nSecond  "},
                        ]
                    },
                    "structured-cli",
                )
            ],
            [assistant_output("done")],
        ]
    )
    async with Agent(
        instructions="Use codex.", provider=provider(responses), tools=[declaration]
    ).session() as session:
        assert (
            await (await session.submit("Inspect the diagram")).get_result()
        ).final_output == "done"
    arguments = json.loads((tmp_path / "arguments.jsonl").read_text())
    assert arguments[arguments.index("--image") + 1] == image
    assert references == [" asset:diagram "]
    assert (tmp_path / "stdin.txt").read_text() == "  First\n\n\n\nSecond  "


@pytest.mark.parametrize("structured", [False, True])
async def test_native_cli_preserves_exact_text_whitespace(tmp_path, responses, structured):
    declaration = codex_tool(codex_options={"codex_path_override": str(cli(tmp_path))})
    binding = declaration._bind(None, None, declaration)
    original = "  indented\n\ntext with trailing space  \n"
    prompt = [{"type": "text", "text": original}] if structured else original
    await _run_in_native_session(
        responses, lambda context: binding.callback(context, prompt=prompt, thread_id=None)
    )
    assert (tmp_path / "stdin.txt").read_text() == original


@pytest.mark.parametrize("denial", ["missing", "denied"])
@pytest.mark.parametrize("initial_thread", [None, "trusted-existing"])
async def test_native_cli_rejects_unauthorized_images_before_cli(
    tmp_path, responses, denial, initial_thread
):
    kwargs = {"resolve_image": lambda _: None} if denial == "denied" else {}
    declaration = codex_tool(
        codex_options={"codex_path_override": str(cli(tmp_path))},
        thread_id=initial_thread,
        **kwargs,
    )
    binding = declaration._bind(None, None, declaration)

    async def invoke(context):
        with pytest.raises(ValueError, match="resolve_image|not authorized"):
            await binding.callback(
                context,
                prompt=[{"type": "local_image", "path": "/private/ungranted.png"}],
                thread_id=None,
            )

    await _run_in_native_session(responses, invoke)
    assert not (tmp_path / "arguments.jsonl").exists()


async def test_native_cli_rechecks_cancellation_after_image_resolution(tmp_path, responses):
    signal = asyncio.Event()

    async def resolve_image(reference):
        signal.set()
        await asyncio.sleep(0)
        return str(tmp_path / "authorized.png")

    declaration = codex_tool(
        codex_options={"codex_path_override": str(cli(tmp_path))},
        turn_options=TurnOptions(signal=signal),
        resolve_image=resolve_image,
    )
    binding = declaration._bind(None, None, declaration)

    async def invoke(context):
        with pytest.raises(RuntimeError, match="cancelled before execution"):
            await binding.callback(
                context, prompt=[{"type": "local_image", "path": "asset"}], thread_id=None
            )

    await _run_in_native_session(responses, invoke)
    assert not (tmp_path / "arguments.jsonl").exists()


@pytest.mark.parametrize("persist", [False, True])
async def test_native_cli_timeout_revokes_image_resolution_even_when_cancel_is_suppressed(
    tmp_path, responses, persist
):
    from tests._asyncio import timeout

    cancelled = False

    async def resolve_image(reference):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            task = asyncio.current_task()
            assert task is not None
            uncancel = getattr(task, "uncancel", None)
            if uncancel is not None:
                uncancel()
        return str(tmp_path / "authorized.png")

    declaration = replace(
        codex_tool(
            codex_options={"codex_path_override": str(cli(tmp_path))},
            resolve_image=resolve_image,
            persist_session=persist,
        ),
        callback_timeout=0.05,
        timeout_behavior="error_as_result",
    )
    responses.respond(
        function_call(
            "codex", {"prompt": [{"type": "local_image", "path": "asset"}]}, "image-timeout"
        )
    )
    responses.respond(assistant_output("timed out"))
    async with (
        timeout(10),
        Agent(
            instructions="Use codex.", provider=provider(responses), tools=[declaration]
        ).session() as session,
    ):
        assert (
            await (await session.submit("Inspect image")).get_result()
        ).final_output == "timed out"
    assert cancelled
    assert not (tmp_path / "arguments.jsonl").exists()


@pytest.mark.parametrize("stream_limit", [65536, 262144])
async def test_native_cli_preserves_process_environment_schema_and_buffer_controls(
    tmp_path: Path, responses, monkeypatch, stream_limit
):
    monkeypatch.setenv("CLI_NOT_INHERITED", "synthetic-private")
    path = cli(tmp_path)
    record = tmp_path / "metadata.json"
    source = path.read_text().replace(
        "for event in events:",
        "import os\n"
        "schema_path = sys.argv[sys.argv.index('--output-schema') + 1]\n"
        f"json.dump({{'schema':json.load(open(schema_path)), 'path':schema_path, "
        "'visible':os.getenv('CLI_ONLY'), 'private':os.getenv('CLI_NOT_INHERITED')}, "
        f"open({str(record)!r}, 'w'))\n"
        "events[1]['item']['text'] = 'x' * 100000\n"
        "for event in events:",
    )
    path.write_text(source)
    schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    declaration = codex_tool(
        codex_options={
            "codex_path_override": str(path),
            "env": {"CLI_ONLY": "configured"},
            "codex_subprocess_stream_limit_bytes": stream_limit,
        },
        turn_options={"output_schema": schema},
    )
    schema["properties"]["value"]["type"] = "number"
    binding = declaration._bind(None, None, declaration)

    def call(context):
        return binding.callback(context, prompt="Run", thread_id=None)

    if stream_limit == 65536:
        with pytest.raises(ValueError, match="limit"):
            await _run_in_native_session(responses, call)
    else:
        result = await _run_in_native_session(responses, call)
        assert len(result["response"]) == 100000
    captured = json.loads(record.read_text())
    assert captured["schema"]["properties"]["value"]["type"] == "string"
    assert captured["visible"] == "configured"
    assert captured["private"] is None
    assert not Path(captured["path"]).exists()


@pytest.mark.parametrize("thread_id", [None, "selected-thread"])
async def test_cli_tool_uses_native_binding_and_real_cli_stream(
    tmp_path: Path,
    thread_id: str | None,
    responses,
) -> None:
    events = []
    declaration = codex_tool(
        codex_options=CodexOptions(codex_path_override=str(cli(tmp_path))),
        turn_options=TurnOptions(idle_timeout_seconds=5),
        thread_id=thread_id,
        on_event=events.append,
    )
    assert isinstance(declaration, FunctionTool)
    binding = declaration._bind(None, None, declaration)
    result = await _run_in_native_session(
        responses,
        lambda context: binding.callback(context, prompt="Do the task", thread_id=thread_id),
    )
    assert result == {
        "thread_id": "synthetic-thread",
        "response": "CLI completed",
        "usage": {"input_tokens": 3, "cached_input_tokens": 1, "output_tokens": 2},
    }
    assert [event.type for event in events] == [
        "thread.started",
        "item.completed",
        "turn.completed",
    ]
    arguments = json.loads((tmp_path / "arguments.jsonl").read_text())
    if thread_id is None:
        assert "resume" not in arguments
    else:
        assert "resume" in arguments
        assert thread_id in arguments


@pytest.mark.parametrize("name", ["codex", "engineer"])
async def test_native_agent_delegates_to_external_cli(tmp_path: Path, responses, name: str) -> None:
    responses.replies.extend(
        [
            [function_call(name, {"prompt": "Do the task", "thread_id": None}, "cli-1")],
            [assistant_output("done")],
        ]
    )
    declaration = codex_tool(
        name=name, codex_options=CodexOptions(codex_path_override=str(cli(tmp_path)))
    )
    async with Agent(
        instructions="Use codex.", provider=provider(responses), tools=[declaration]
    ).session() as session:
        result = await (await session.submit("Do the task")).get_result()
        assert result.final_output == "done"
    assert "CLI completed" in json.dumps(responses.requests[1])


async def test_cli_idle_timeout_closes_the_subprocess(tmp_path: Path, responses) -> None:
    path = tmp_path / "idle-codex"
    pid_path = tmp_path / "pid"
    path.write_text(
        f"#!{sys.executable}\nimport os, time\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    path.chmod(0o700)
    declaration = codex_tool(
        codex_options={"codex_path_override": str(path)},
        turn_options={"idle_timeout_seconds": 0.1},
    )
    binding = declaration._bind(None, None, declaration)

    async def invoke(context: ToolContext[object]) -> None:
        with pytest.raises(RuntimeError, match="idle"):
            await binding.callback(context, prompt="Do the task", thread_id=None)

    await _run_in_native_session(responses, invoke)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)


async def test_cli_callback_cancellation_joins_the_subprocess(tmp_path: Path, responses) -> None:
    path = tmp_path / "blocking-codex"
    pid_path = tmp_path / "pid"
    path.write_text(
        f"#!{sys.executable}\nimport json, os, time\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'blocking'}), flush=True)\n"
        "time.sleep(30)\n"
    )
    path.chmod(0o700)
    started = asyncio.Event()
    declaration = codex_tool(
        codex_options=CodexOptions(codex_path_override=str(path)),
        on_event=lambda event: started.set(),
    )
    binding = declaration._bind(None, None, declaration)

    async def invoke(context: ToolContext[object]) -> None:
        task = asyncio.create_task(binding.callback(context, prompt="Do the task", thread_id=None))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await _run_in_native_session(responses, invoke)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)


async def test_persisted_cli_threads_are_scoped_to_the_invoking_native_session(
    tmp_path: Path,
    responses,
) -> None:
    calls = tmp_path / "calls.jsonl"
    path = cli(tmp_path)
    source = path.read_text()
    source = source.replace(
        "sys.stdin.read()\n",
        f"sys.stdin.read()\nopen({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n",
    )
    path.write_text(source)
    declaration = codex_tool(
        codex_options=CodexOptions(codex_path_override=str(path)),
        persist_session=True,
    )
    responses.replies.extend(
        [
            [function_call("codex", {"prompt": "A first", "thread_id": None}, "cli-a1")],
            [assistant_output("A first done")],
            [function_call("codex", {"prompt": "B first", "thread_id": None}, "cli-b1")],
            [assistant_output("B first done")],
            [function_call("codex", {"prompt": "A next", "thread_id": None}, "cli-a2")],
            [assistant_output("A next done")],
        ]
    )
    agent = Agent(instructions="Use codex.", provider=provider(responses), tools=[declaration])
    async with agent.session() as first, agent.session() as second:
        assert (await (await first.submit("A first")).get_result()).final_output == "A first done"
        assert (await (await second.submit("B first")).get_result()).final_output == "B first done"
        assert (await (await first.submit("A next")).get_result()).final_output == "A next done"
    arguments = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(arguments) == 3
    assert "resume" not in arguments[0]
    assert "resume" not in arguments[1]
    assert "resume" in arguments[2]
    assert "synthetic-thread" in arguments[2]


@pytest.mark.parametrize("mode", ["throw", "reject"])
async def test_observer_failures_preserve_native_success_and_thread_reuse(
    tmp_path: Path, responses, mode
) -> None:
    calls = tmp_path / "calls.jsonl"
    path = cli(tmp_path)
    path.write_text(
        path.read_text().replace(
            "sys.stdin.read()\n",
            f"sys.stdin.read()\nopen({str(calls)!r}, 'a').write("
            "json.dumps(sys.argv[1:]) + '\\n')\n",
        )
    )
    observed = []

    async def rejected():
        raise RuntimeError("observer failure")

    def observer(event):
        observed.append(event.type)
        if mode == "throw":
            raise RuntimeError("observer failure")
        return rejected()

    declaration = codex_tool(
        codex_options=CodexOptions(codex_path_override=str(path)),
        persist_session=True,
        on_event=observer,
    )
    responses.replies.extend(
        [
            [function_call("codex", {"prompt": "First", "thread_id": None}, "observer-1")],
            [assistant_output("first done")],
            [function_call("codex", {"prompt": "Next", "thread_id": None}, "observer-2")],
            [assistant_output("next done")],
        ]
    )
    agent = Agent(instructions="Use codex.", provider=provider(responses), tools=[declaration])
    async with agent.session() as session:
        assert (await (await session.submit("First")).get_result()).final_output == "first done"
        assert (await (await session.submit("Next")).get_result()).final_output == "next done"
    argv = [json.loads(line) for line in calls.read_text().splitlines()]
    assert "resume" not in argv[0]
    assert "resume" in argv[1]
    assert len(observed) == 6


async def test_configured_signal_cancels_queued_call_without_releasing_active_owner(
    tmp_path: Path, responses
) -> None:
    import asyncio

    signal = asyncio.Event()
    entered = asyncio.Event()
    finish = asyncio.Event()
    failures: list[BaseException] = []
    observed = False

    async def observe(event) -> None:
        nonlocal observed
        if event.type == "thread.started" and not observed:
            observed = True
            entered.set()
            await finish.wait()

    declaration = codex_tool(
        codex_options=CodexOptions(codex_path_override=str(cli(tmp_path))),
        turn_options=TurnOptions(signal=signal),
        persist_session=True,
        on_event=observe,
    )
    binding = declaration._bind(None, None, declaration)

    @function_tool
    async def batch(context: ToolContext[object]) -> str:
        tasks = []
        try:
            first = asyncio.create_task(binding.callback(context, prompt="First", thread_id=None))
            tasks.append(first)
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(binding.callback(context, prompt="Queued", thread_id=None))
            tasks.append(second)
            await asyncio.sleep(0)
            signal.set()
            with pytest.raises(RuntimeError, match="cancelled before execution"):
                await asyncio.wait_for(asyncio.shield(second), 1)
            assert not first.done()
            assert len((tmp_path / "arguments.jsonl").read_text().splitlines()) == 1
            signal.clear()
            third = asyncio.create_task(binding.callback(context, prompt="Third", thread_id=None))
            tasks.append(third)
            await asyncio.sleep(0)
            assert not third.done()
            assert len((tmp_path / "arguments.jsonl").read_text().splitlines()) == 1
            finish.set()
            await asyncio.gather(first, return_exceptions=True)
            assert (await third)["response"] == "CLI completed"
            return "batch complete"
        except BaseException as error:
            failures.append(error)
            raise
        finally:
            signal.clear()
            finish.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    responses.replies.extend(
        [
            [function_call("batch", {}, "configured-signal")],
            [assistant_output("done")],
        ]
    )
    async with Agent(
        instructions="Run batch.", provider=provider(responses), tools=[batch]
    ).session() as session:
        await (await session.submit("Run.")).get_result()
    assert not failures
    assert len((tmp_path / "arguments.jsonl").read_text().splitlines()) == 2


@pytest.mark.parametrize("cause", ["configured", "task"])
async def test_cancelled_queue_admission_does_not_release_another_owner(cause: str) -> None:
    import asyncio

    from _agents_codex.codex import _session_slot

    lock = asyncio.Lock()
    await lock.acquire()
    signal = asyncio.Event()
    entered = False

    async def use() -> None:
        nonlocal entered
        async with _session_slot(lock, signal):
            entered = True

    queued = asyncio.create_task(use())
    await asyncio.sleep(0)
    if cause == "configured":
        signal.set()
        with pytest.raises(RuntimeError, match="cancelled before execution"):
            await queued
    else:
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
    assert not entered
    assert lock.locked()
    lock.release()
    signal.clear()
    async with _session_slot(lock, signal):
        assert lock.locked()
    assert not lock.locked()


@pytest.mark.parametrize("cause", ["configured", "task"])
async def test_cancelled_admission_releases_its_simultaneously_acquired_lock(cause: str) -> None:
    import asyncio

    from _agents_codex.codex import _session_slot

    lock = asyncio.Lock()
    signal = asyncio.Event()
    entered = False

    async def use() -> None:
        nonlocal entered
        async with _session_slot(lock, signal):
            entered = True

    admission = asyncio.create_task(use())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert lock.locked()
    if cause == "configured":
        signal.set()
        with pytest.raises(RuntimeError, match="cancelled before execution"):
            await admission
    else:
        admission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await admission
    assert not entered
    assert not lock.locked()


async def test_cancelling_prompt_backpressure_joins_cli_and_stderr(
    tmp_path: Path, responses
) -> None:
    pid_path = tmp_path / "pid"
    path = tmp_path / "blocked-stdin-codex"
    path.write_text(
        f"#!{sys.executable}\nimport os, time\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    path.chmod(0o700)
    declaration = codex_tool(
        codex_options=CodexOptions(codex_path_override=str(path)),
        turn_options=TurnOptions(signal=asyncio.Event()),
    )
    binding = declaration._bind(None, None, declaration)

    async def invoke(context: ToolContext[object]) -> None:
        previous_tasks = asyncio.all_tasks()
        task = asyncio.create_task(
            binding.callback(context, prompt="x" * (512 * 1024), thread_id=None)
        )
        pid = None
        try:

            async def started() -> None:
                while not pid_path.exists() or not pid_path.read_text().strip():
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(started(), 5)
            pid = int(pid_path.read_text())
            # The child never reads stdin, so this prompt cannot pass the pipe's drain.
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            assert not (asyncio.all_tasks() - previous_tasks)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if pid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, 9)
            # Also contain the failed pre-fix regression's orphaned stderr reader.
            remaining = asyncio.all_tasks() - previous_tasks
            if remaining:
                await asyncio.wait_for(asyncio.gather(*remaining, return_exceptions=True), 5)

    await _run_in_native_session(responses, invoke)


async def test_context_thread_authorization_and_stream_readback(tmp_path: Path, responses):
    executable = cli(tmp_path)
    events = []
    readback = []

    async def resolve(context):
        assert context.call_id == "cli-callback"
        return "application-thread"

    def observe(event, context):
        events.append((event.type, context.call_id))

    async def completed(thread_id, outcome, context):
        readback.append((thread_id, outcome, context.call_id))
        raise RuntimeError("Observer failure must not change success")

    declaration = codex_tool(
        codex_options={"codex_path_override": str(executable)},
        resolve_thread=resolve,
        on_stream=observe,
        on_thread=completed,
    )
    binding = declaration._bind(None, None, declaration)
    result = await _run_in_native_session(
        responses, lambda context: binding.callback(context, prompt="Run", thread_id=None)
    )
    assert result["response"] == "CLI completed"
    arguments = json.loads((tmp_path / "arguments.jsonl").read_text())
    assert "resume" in arguments and "application-thread" in arguments
    assert [kind for kind, _ in events] == ["thread.started", "item.completed", "turn.completed"]
    assert readback == [("synthetic-thread", "completed", "cli-callback")]


async def test_failed_thread_readback_does_not_authorize_model_reuse(tmp_path: Path, responses):
    executable = cli(tmp_path)
    executable.write_text(executable.read_text().replace("'turn.completed'", "'turn.failed'"))
    # A valid failure event still exposes its known thread after joined cleanup.
    executable.write_text(
        executable.read_text().replace(
            "'usage': {'input_tokens': 3, 'cached_input_tokens': 1, 'output_tokens': 2}",
            "'error': {'message': 'synthetic failure'}",
        )
    )
    readback = []

    def observe(thread_id, outcome, context):
        readback.append((thread_id, outcome))
        raise RuntimeError("Observer must not mask the CLI error")

    declaration = codex_tool(
        codex_options={"codex_path_override": str(executable)}, on_thread=observe
    )
    binding = declaration._bind(None, None, declaration)

    async def invoke(context):
        with pytest.raises(RuntimeError, match="synthetic failure"):
            await binding.callback(context, prompt="Run", thread_id=None)
        with pytest.raises(ValueError, match="not issued"):
            await binding.callback(context, prompt="Run", thread_id="synthetic-thread")

    await _run_in_native_session(responses, invoke)
    assert readback == [("synthetic-thread", "failed")]
    assert len((tmp_path / "arguments.jsonl").read_text().splitlines()) == 1


async def test_context_thread_resolver_rechecks_signal_before_cli(tmp_path: Path, responses):
    executable = cli(tmp_path)
    signal = asyncio.Event()

    async def resolve(context):
        signal.set()
        return "application-thread"

    declaration = codex_tool(
        codex_options={"codex_path_override": str(executable)},
        turn_options=TurnOptions(signal=signal),
        resolve_thread=resolve,
    )
    binding = declaration._bind(None, None, declaration)

    async def invoke(context):
        with pytest.raises(RuntimeError, match="cancelled before execution"):
            await binding.callback(context, prompt="Run", thread_id=None)

    await _run_in_native_session(responses, invoke)
    assert not (tmp_path / "arguments.jsonl").exists()


async def test_custom_cli_parameters_validate_and_use_existing_native_owner(tmp_path, responses):
    from typing import Literal

    from pydantic import BaseModel, Field

    class Parameters(BaseModel):
        inputs: list[dict[str, str]] = Field(min_length=1)
        ticket: Literal["validated"]
        thread_id: str | None = None

    declaration = codex_tool(
        codex_options={"codex_path_override": str(cli(tmp_path))}, parameters=Parameters
    )
    responses.replies.extend(
        [
            [
                function_call(
                    "codex",
                    {"inputs": [{"type": "text", "text": "  exact text  "}], "ticket": "validated"},
                    "custom-cli",
                )
            ],
            [assistant_output("done")],
        ]
    )
    async with Agent(
        instructions="Use codex.", provider=provider(responses), tools=[declaration]
    ).session() as session:
        assert (await (await session.submit("Run")).get_result()).final_output == "done"
    assert (tmp_path / "stdin.txt").read_text() == "  exact text  "
    assert declaration.input_type is Parameters


async def test_cli_failure_formatter_runs_after_cleanup(tmp_path, responses):
    executable = cli(tmp_path)
    executable.write_text(
        executable.read_text()
        .replace("'turn.completed'", "'turn.failed'")
        .replace(
            "'usage': {'input_tokens': 3, 'cached_input_tokens': 1, 'output_tokens': 2}",
            "'error': {'message': 'synthetic failure'}",
        )
    )
    observed = []

    async def format_error(context, error):
        observed.append((context.call_id, str(error)))
        return "Application-selected failure message"

    declaration = codex_tool(
        codex_options={"codex_path_override": str(executable)},
        failure_error_function=format_error,
    )
    binding = declaration._bind(None, None, declaration)
    result = await _run_in_native_session(
        responses, lambda context: binding.callback(context, prompt="Run", thread_id=None)
    )
    assert result == "Application-selected failure message"
    assert observed == [("cli-callback", "synthetic failure")]


@pytest.mark.parametrize(
    "policy", ["sensitive", "metadata", "disabled", "zero-budget", "oversized-unicode"]
)
def test_cli_item_tracing_native(policy):
    import subprocess

    from agents import _native

    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH")))
    )
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            (
                "import asyncio,sys,agents; agents.__path__.append(sys.argv[1]); "
                "from tests.test_native_codex_cli import _cli_item_tracing_case; "
                "asyncio.run(_cli_item_tracing_case(sys.argv[2]))"
            ),
            str(Path(_native.__file__).parent),
            policy,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def _cli_item_tracing_case(policy):
    import tempfile
    from collections.abc import Mapping

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from agents import NativeProvider, telemetry
    from tests.native_fixtures import ResponsesFixture

    def plain(value):
        if isinstance(value, Mapping):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [plain(item) for item in value]
        return value

    class Recorder:
        def __init__(self):
            self.events = []

        def on_trace_event(self, event):
            self.events.append(plain(event))

    recorder = Recorder()
    await telemetry.configure(processors=[recorder])
    fixture = ResponsesFixture()
    application = web.Application()
    application.router.add_post("/v1/responses", fixture.handle)
    with tempfile.TemporaryDirectory() as directory:
        executable = cli(Path(directory))

        def item(identifier, status):
            if policy == "oversized-unicode" and status == "completed":
                status = "failed"
            return {
                "id": identifier,
                "type": "command_execution",
                "command": "PRIVATE_COMMAND🙂" * 1200
                if policy == "oversized-unicode"
                else "PRIVATE_COMMAND",
                "aggregated_output": "PRIVATE_OUTPUT🙂" * 1200
                if policy == "oversized-unicode"
                else "PRIVATE_OUTPUT" * 100,
                "status": status,
                "exit_code": 0 if status == "completed" else None,
            }

        events = [
            {"type": "item.started", "item": item("a", "in_progress")},
            {"type": "item.started", "item": item("b", "in_progress")},
            {"type": "item.completed", "item": item("a", "completed")},
            {"type": "item.completed", "item": item("b", "completed")},
        ]
        executable.write_text(
            executable.read_text().replace(
                "for event in events:", f"events = {events!r} + events\nfor event in events:"
            )
        )
        fixture.replies.extend(
            [[function_call("codex", {"prompt": "Run"}, "trace-cli")], [assistant_output("done")]]
        )
        async with TestServer(application) as server:
            async with NativeProvider(
                base_url=str(server.make_url("/v1")), api_key="synthetic"
            ) as selected:
                declaration = codex_tool(
                    codex_options={"codex_path_override": str(executable)},
                    span_data_max_chars=None
                    if policy == "oversized-unicode"
                    else 0
                    if policy == "zero-budget"
                    else 128,
                )
                with telemetry.trace(
                    "CLI parent",
                    options=telemetry.TraceOptions(
                        disabled=policy == "disabled",
                        include_sensitive_data=policy
                        in ("sensitive", "zero-budget", "oversized-unicode"),
                    ),
                ):
                    async with Agent(
                        instructions="Use codex.", provider=selected, tools=[declaration]
                    ).session() as session:
                        assert (
                            await (await session.submit("Run")).get_result()
                        ).final_output == "done"
    assert (await telemetry.force_flush()).status == "success"
    records = [event for event in recorder.events if event["name"] == "Codex command execution"]
    if policy == "disabled":
        assert records == []
    else:
        ended = [event for event in records if event["lifecycle"] == "end"]
        assert len(ended) == 2
        assert len({event["parent_span_id"] for event in ended}) == 1
        assert len({event["span_id"] for event in ended}) == 2
        if policy == "oversized-unicode":
            assert all(event["outcome"] == "error" for event in ended)
            for event in ended:
                assert event["attributes"].get("sdk.outcome.category") != "trace_end_error"
                payload = event["attributes"]["sdk.outcome.data"]
                assert len(payload.encode("utf-8")) <= 4096
                assert json.loads(payload)["status"] == "failed"

        payload = json.dumps(records)
        assert ("PRIVATE_COMMAND" in payload) is (policy in ("sensitive", "oversized-unicode"))
        assert ("PRIVATE_OUTPUT" in payload) is (policy in ("sensitive", "oversized-unicode"))
    await telemetry.shutdown()


async def test_cli_stream_observer_applies_backpressure_and_joins(tmp_path, responses):
    entered, release, terminal = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []

    def on_event(event):
        if event.type == "turn.completed":
            terminal.set()

    async def on_stream(event, context):
        entered.set()
        await release.wait()
        observed.append((event.type, context.call_id))

    declaration = codex_tool(
        codex_options={"codex_path_override": str(cli(tmp_path))},
        on_event=on_event,
        on_stream=on_stream,
    )
    binding = declaration._bind(None, None, declaration)

    async def invoke(context):
        task = asyncio.create_task(binding.callback(context, prompt="Run", thread_id=None))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not terminal.is_set() and not task.done()
            release.set()
            return await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    result = await _run_in_native_session(responses, invoke)
    assert result["response"] == "CLI completed"
    assert terminal.is_set()
    assert observed == [
        (kind, "cli-callback") for kind in ("thread.started", "item.completed", "turn.completed")
    ]


async def test_cli_stream_observer_cancellation_joins_before_readback(tmp_path, responses):
    entered, retired = asyncio.Event(), asyncio.Event()
    readback = []

    async def on_stream(event, context):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            retired.set()

    def on_thread(identifier, outcome, context):
        assert retired.is_set()
        readback.append((identifier, outcome))

    declaration = codex_tool(
        codex_options={"codex_path_override": str(cli(tmp_path))},
        on_stream=on_stream,
        on_thread=on_thread,
    )
    binding = declaration._bind(None, None, declaration)

    async def invoke(context):
        task = asyncio.create_task(binding.callback(context, prompt="Run", thread_id=None))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert retired.is_set()
            assert readback == [("synthetic-thread", "cancelled")]
            with pytest.raises(ValueError, match="not issued"):
                await binding.callback(context, prompt="Run", thread_id="synthetic-thread")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    await _run_in_native_session(responses, invoke)
