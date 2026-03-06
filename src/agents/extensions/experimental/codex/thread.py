from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Union, cast

from typing_extensions import Literal, TypeAlias, TypedDict

from .codex_options import CodexOptions
from .events import (
    ItemCompletedEvent,
    ThreadError,
    ThreadErrorEvent,
    ThreadEvent,
    ThreadStartedEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
    Usage,
    coerce_thread_event,
)
from .exec import CodexExec, CodexExecArgs
from .items import ThreadItem, is_agent_message_item
from .output_schema_file import create_output_schema_file
from .thread_options import ThreadOptions
from .turn_options import TurnOptions


@contextlib.asynccontextmanager
async def _aclosing(
    generator: AsyncGenerator[str, None],
) -> AsyncGenerator[AsyncGenerator[str, None], None]:
    try:
        yield generator
    finally:
        await generator.aclose()


class TextInput(TypedDict):
    type: Literal["text"]
    text: str


class LocalImageInput(TypedDict):
    type: Literal["local_image"]
    path: str


UserInput: TypeAlias = Union[TextInput, LocalImageInput]
Input: TypeAlias = Union[str, list[UserInput]]


@dataclass(frozen=True)
class Turn:
    items: list[ThreadItem]
    final_response: str
    usage: Usage | None


RunResult = Turn


@dataclass(frozen=True)
class StreamedTurn:
    events: AsyncGenerator[ThreadEvent, None]


RunStreamedResult = StreamedTurn


class Thread:
    def __init__(
        self,
        *,
        exec_client: CodexExec,
        options: CodexOptions,
        thread_options: ThreadOptions,
        thread_id: str | None = None,
    ) -> None:
        self._exec = exec_client
        self._options = options
        self._id = thread_id
        self._thread_options = thread_options

    @property
    def id(self) -> str | None:
        return self._id

    async def run_streamed(
        self, input: Input, turn_options: TurnOptions | None = None
    ) -> StreamedTurn:
        options = turn_options or TurnOptions()
        return StreamedTurn(events=self._run_streamed_internal(input, options))

    async def _run_streamed_internal(
        self, input: Input, turn_options: TurnOptions
    ) -> AsyncGenerator[ThreadEvent, None]:
        # The Codex CLI expects an output schema file path for structured output.
        output_schema_file = create_output_schema_file(turn_options.output_schema)
        options = self._thread_options
        prompt, images = _normalize_input(input)
        idle_timeout = turn_options.idle_timeout_seconds
        signal = turn_options.signal
        if idle_timeout is not None and signal is None:
            signal = asyncio.Event()
        generator = self._exec.run(
            CodexExecArgs(
                input=prompt,
                base_url=self._options.base_url,
                api_key=self._options.api_key,
                thread_id=self._id,
                images=images,
                model=options.model,
                sandbox_mode=options.sandbox_mode,
                working_directory=options.working_directory,
                skip_git_repo_check=options.skip_git_repo_check,
                output_schema_file=output_schema_file.schema_path,
                model_reasoning_effort=options.model_reasoning_effort,
                signal=signal,
                idle_timeout_seconds=idle_timeout,
                network_access_enabled=options.network_access_enabled,
                web_search_mode=options.web_search_mode,
                web_search_enabled=options.web_search_enabled,
                approval_policy=options.approval_policy,
                additional_directories=list(options.additional_directories)
                if options.additional_directories
                else None,
            )
        )

        try:
            async with _aclosing(generator) as stream:
                while True:
                    try:
                        if idle_timeout is None or isinstance(self._exec, CodexExec):
                            item = await stream.__anext__()
                        else:
                            item = await asyncio.wait_for(
                                stream.__anext__(),
                                timeout=idle_timeout,
                            )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError as exc:
                        if signal is not None:
                            signal.set()
                        raise RuntimeError(
                            f"Codex stream idle for {idle_timeout} seconds."
                        ) from exc
                    try:
                        parsed = _parse_event(item)
                    except Exception as exc:  # noqa: BLE001
                        raise RuntimeError(f"Failed to parse event: {item}") from exc
                    if isinstance(parsed, ThreadStartedEvent):
                        # Capture the thread id so callers can resume later.
                        self._id = parsed.thread_id
                    yield parsed
        finally:
            output_schema_file.cleanup()

    async def run(self, input: Input, turn_options: TurnOptions | None = None) -> Turn:
        # Aggregate events into a single Turn result (matching the TS SDK behavior).
        options = turn_options or TurnOptions()
        generator = self._run_streamed_internal(input, options)
        items: list[ThreadItem] = []
        final_response = ""
        usage: Usage | None = None
        turn_failure: ThreadError | None = None

        async for event in generator:
            if isinstance(event, ItemCompletedEvent):
                item = event.item
                if is_agent_message_item(item):
                    final_response = item.text
                items.append(item)
            elif isinstance(event, TurnCompletedEvent):
                usage = event.usage
            elif isinstance(event, TurnFailedEvent):
                turn_failure = event.error
                break
            elif isinstance(event, ThreadErrorEvent):
                raise RuntimeError(f"Codex stream error: {event.message}")

        if turn_failure:
            raise RuntimeError(turn_failure.message)

        return Turn(items=items, final_response=final_response, usage=usage)


def _normalize_input(input: Input) -> tuple[str, list[str]]:
    # Merge text items into a single prompt and collect image paths.
    if isinstance(input, str):
        return input, []

    prompt_parts: list[str] = []
    images: list[str] = []
    for item in input:
        if item["type"] == "text":
            text = item.get("text", "")
            prompt_parts.append(text)
        elif item["type"] == "local_image":
            path = item.get("path", "")
            if path:
                images.append(path)

    return "\n\n".join(prompt_parts), images


def _parse_event(raw: str) -> ThreadEvent:
    import json

    parsed = json.loads(raw)
    return coerce_thread_event(cast(dict[str, Any], parsed))
