"""External Codex CLI delegation through a native function tool."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import aclosing, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from weakref import WeakKeyDictionary

from pydantic import BaseModel

from _agents_shared.codex_cli.codex import Codex
from _agents_shared.codex_cli.codex_options import (
    CodexOptions,
    coerce_codex_options,
)
from _agents_shared.codex_cli.events import (
    ItemCompletedEvent,
    ItemStartedEvent,
    ItemUpdatedEvent,
    ThreadErrorEvent,
    ThreadEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
)
from _agents_shared.codex_cli.items import CommandExecutionItem, is_agent_message_item
from _agents_shared.codex_cli.thread import Input, Thread, UserInput
from _agents_shared.codex_cli.thread_options import (
    ThreadOptions,
    coerce_thread_options,
)
from _agents_shared.codex_cli.turn_options import (
    TurnOptions,
    coerce_turn_options,
)

from ._compat import cancellation_requested
from ._function_tools import ToolContext, _PythonTool, function_tool
from ._lifecycle import _join
from ._public import preserve_module_identity as _preserve_module_identity
from .telemetry import TraceScope, current_context
from .usage import TokenUsage

_CLI_ENVIRONMENT_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "SHELL",
        "LOGNAME",
        "USER",
        "USERNAME",
        "USERDOMAIN",
        "TERM",
        "CODEX_HOME",
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    }
)


@dataclass
class _SessionThread:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    thread: Thread | None = None
    issued_ids: set[str] = field(default_factory=set)


def _thread_id(value: str | None) -> str | None:
    if value is not None:
        if not isinstance(value, str):
            raise TypeError("thread_id must be a string or None")
        value = value.strip()
        if not value or value.startswith("-"):
            raise ValueError("thread_id must be nonempty and cannot start with a dash")
    return value


@asynccontextmanager
async def _session_slot(lock: asyncio.Lock, signal: asyncio.Event | None) -> AsyncIterator[None]:
    if signal is None:
        async with lock:
            yield
        return
    if signal.is_set():
        raise RuntimeError("Codex CLI call cancelled before execution")
    acquired = asyncio.create_task(lock.acquire())
    aborted = asyncio.create_task(signal.wait())
    try:
        done, _ = await asyncio.wait((acquired, aborted), return_when=asyncio.FIRST_COMPLETED)
        if aborted in done or signal.is_set():
            raise RuntimeError("Codex CLI call cancelled before execution")
        acquired.result()
        yield
    finally:
        acquired.cancel()
        aborted.cancel()
        try:
            await _join(asyncio.gather(acquired, aborted, return_exceptions=True))
        finally:
            if (
                acquired.done()
                and not acquired.cancelled()
                and acquired.exception() is None
                and acquired.result()
            ):
                lock.release()


def _cli_trace_data(item: CommandExecutionItem, limit: int | None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "command": item.command,
        "output": item.aggregated_output,
        "status": item.status,
        "exit_code": item.exit_code,
    }
    # Native outcome data has a fixed 4 KiB UTF-8 envelope, independent of
    # the optional application text limit. Keep status on the span itself.
    limit = min(4096, 4096 if limit is None else limit)
    if limit <= 0:
        return {}
    for key, value in data.items():
        if isinstance(value, str):
            data[key] = value[:4096]
    while len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > limit:
        strings = [key for key, value in data.items() if isinstance(value, str) and value]
        if not strings:
            return {}
        key = max(strings, key=lambda key: len(data[key].encode("utf-8")))
        data[key] = data[key][: len(data[key]) // 2]
    return data


class _CliItemSpans:
    def __init__(self, limit: int | None):
        self.limit = limit
        self.spans: dict[str, tuple[TraceScope, dict[str, Any]]] = {}

    def observe(self, event: ThreadEvent) -> None:
        if (
            current_context() is None
            or not isinstance(event, (ItemStartedEvent, ItemUpdatedEvent, ItemCompletedEvent))
            or not isinstance(event.item, CommandExecutionItem)
        ):
            return
        item = event.item
        if isinstance(event, ItemStartedEvent) and item.id not in self.spans:
            scope = TraceScope("Codex command execution", "custom", None)._start_recording()
            self.spans[item.id] = (scope, {})
        if item.id not in self.spans:
            return
        scope, _ = self.spans[item.id]
        data = _cli_trace_data(item, self.limit)
        self.spans[item.id] = (scope, data)
        if isinstance(event, ItemCompletedEvent):
            self.spans.pop(item.id)
            try:
                scope.set_outcome(status="error" if item.status == "failed" else "ok", data=data)
            finally:
                scope.end()

    def close(self, outcome: str) -> None:
        try:
            for scope, data in self.spans.values():
                try:
                    scope.set_outcome(
                        status="ok"
                        if outcome == "completed"
                        else "cancelled"
                        if outcome == "cancelled"
                        else "error",
                        data=data,
                    )
                except Exception:
                    pass
                finally:
                    try:
                        scope.end()
                    except Exception:
                        pass
        finally:
            self.spans.clear()


def codex_tool(
    *,
    codex: Codex | None = None,
    codex_options: CodexOptions | Mapping[str, Any] | None = None,
    thread_options: ThreadOptions | Mapping[str, Any] | None = None,
    turn_options: TurnOptions | Mapping[str, Any] | None = None,
    name: str = "codex",
    description: str = "Run an external Codex CLI task in the configured workspace.",
    persist_session: bool = False,
    thread_id: str | None = None,
    on_event: Callable[[ThreadEvent], None | Awaitable[None]] | None = None,
    resolve_image: Callable[[str], str | None | Awaitable[str | None]] | None = None,
    parameters: type[BaseModel] | None = None,
    span_data_max_chars: int | None = 8192,
    resolve_thread: Callable[[ToolContext[object]], str | None | Awaitable[str | None]]
    | None = None,
    on_stream: Callable[[ThreadEvent, ToolContext[object]], None | Awaitable[None]] | None = None,
    is_enabled: bool | Callable[[Any, Any], bool | Awaitable[bool]] = True,
    failure_error_function: Callable[[ToolContext[object], Exception], str | Awaitable[str]]
    | None = None,
    on_thread: Callable[
        [str, Literal["completed", "failed", "cancelled"], ToolContext[object]],
        None | Awaitable[None],
    ]
    | None = None,
) -> _PythonTool[Any, object]:
    """Declare external CLI delegation without starting a subprocess.

    Prompts: a string or an ordered list of text/local_image inputs, for example
    [{"type": "text", "text": "Inspect this image"},
     {"type": "local_image", "path": "/workspace/diagram.png"}].
    Local images require resolve_image. Its input is the model's image reference;
    its returned path authorizes a CLI read. None denies the image. The application
    owns safe asset resolution or staging, including filesystem race protection.
    Working directories and additional directories do not grant image-read authority.
    Structured text retains its original whitespace.

    The application authorizes CLI credentials, executable and workspace through
    CodexOptions and ThreadOptions. Native Environment grants do not constrain
    this application function. The returned tool uses the native function route;
    the CLI owns its own task and its usage is returned separately in the result.
    resolve_thread authorizes a context-selected CLI thread for the current call.
    on_stream receives each event and its native ToolContext. on_thread receives
    the known CLI ID and completed/failed/cancelled outcome after stream cleanup;
    readback failures are isolated and do not admit failed IDs for model reuse.
    on_event observes the CLI stream. Python task cancellation closes and joins
    the stream's subprocess. TurnOptions supports stdout inactivity timeouts.
    Its signal also cancels calls awaiting persistent-session admission.

    Default subprocess environment: platform paths, Codex credentials and
    transport settings only. Explicit CodexOptions.env mappings and supplied
    Codex clients retain their application-owned environment policy. For full
    inheritance, pass CodexOptions(env=dict(os.environ)).

    persist_session reuses the most recently completed CLI thread within the
    invoking native Session and serializes that Session's calls.
    Model thread_id values have been selecting only IDs issued by successful
    calls in the invoking Session or the trusted application initial ID.
    The application thread_id option has been
    authorizing an initial existing CLI thread for this declaration's Sessions.
    Omitted model IDs have been reusing the persistent Session thread, then the
    configured initial thread, or starting fresh. IDs have been requiring
    nonempty values without a leading dash.
    """
    if span_data_max_chars is not None and type(span_data_max_chars) is not int:
        raise TypeError("span_data_max_chars must be an integer or None")
    if parameters is not None and (
        not isinstance(parameters, type)
        or not issubclass(parameters, BaseModel)
        or not ({"prompt", "inputs"} & parameters.model_fields.keys())
    ):
        raise TypeError("parameters must be a Pydantic model with prompt or inputs")
    if codex is not None and codex_options is not None:
        raise ValueError("supply codex or codex_options, not both")
    if not isinstance(persist_session, bool):
        raise TypeError("persist_session must be a bool")
    if on_event is not None and not callable(on_event):
        raise TypeError("on_event must be callable or None")
    if resolve_image is not None and not callable(resolve_image):
        raise TypeError("resolve_image must be callable or None")
    for callback_name, callback in (
        ("resolve_thread", resolve_thread),
        ("on_stream", on_stream),
        ("on_thread", on_thread),
    ):
        if callback is not None and not callable(callback):
            raise TypeError(f"{callback_name} must be callable or None")
    if failure_error_function is not None and not callable(failure_error_function):
        raise TypeError("failure_error_function must be callable or None")
    initial_thread_id = _thread_id(thread_id)
    client = codex
    captured_codex_options = deepcopy(coerce_codex_options(codex_options))
    if client is None and (captured_codex_options is None or captured_codex_options.env is None):
        captured_codex_options = replace(
            captured_codex_options or CodexOptions(),
            env={
                name: value
                for name, value in os.environ.items()
                if name.upper() in _CLI_ENVIRONMENT_NAMES
            },
        )
    captured_thread_options = deepcopy(coerce_thread_options(thread_options))
    turn_options = coerce_turn_options(turn_options)
    captured_turn_options = (
        TurnOptions(
            output_schema=deepcopy(turn_options.output_schema),
            signal=turn_options.signal,
            idle_timeout_seconds=turn_options.idle_timeout_seconds,
        )
        if turn_options is not None
        else None
    )
    sessions: WeakKeyDictionary[object, _SessionThread] = WeakKeyDictionary()

    async def execute(
        prompt: Input,
        thread_id: str | None,
        state: _SessionThread,
        check: Callable[[], None],
        context: ToolContext[object],
    ) -> dict[str, Any]:
        nonlocal client
        if not prompt or (isinstance(prompt, str) and not prompt.strip()):
            raise ValueError("prompt must not be empty")
        check()
        trusted_thread_id = None
        if resolve_thread is not None:
            selected = resolve_thread(context)
            if inspect.isawaitable(selected):
                selected = await selected
            check()
            trusted_thread_id = _thread_id(selected)
        if (
            thread_id is not None
            and thread_id != initial_thread_id
            and thread_id != trusted_thread_id
            and thread_id not in state.issued_ids
        ):
            raise ValueError("thread_id was not issued to this native Session")
        if isinstance(prompt, list):
            normalized: list[UserInput] = []
            for item in prompt:
                if item["type"] == "text":
                    text = item["text"]
                    if not text.strip():
                        raise ValueError("text inputs must not be empty")
                    normalized.append({"type": "text", "text": text})
                else:
                    path = item["path"]
                    if not path.strip():
                        raise ValueError("local image paths must not be empty")
                    if resolve_image is None:
                        raise ValueError(
                            "Local images require an application resolve_image callback"
                        )
                    check()
                    resolved = resolve_image(path)
                    if inspect.isawaitable(resolved):
                        resolved = await resolved
                    check()
                    if resolved is None:
                        raise ValueError("Local image was not authorized by the application")
                    if not isinstance(resolved, str) or not resolved.strip() or "\0" in resolved:
                        raise ValueError("resolve_image must return a nonempty path string or None")
                    normalized.append({"type": "local_image", "path": resolved})
            prompt = normalized
        check()
        if client is None:
            client = Codex(captured_codex_options)
        thread_id = thread_id or trusted_thread_id
        thread = (
            client.resume_thread(thread_id, captured_thread_options)
            if thread_id is not None
            else state.thread
            if persist_session and state.thread is not None
            else client.resume_thread(initial_thread_id, captured_thread_options)
            if initial_thread_id is not None
            else client.start_thread(captured_thread_options)
        )
        outcome: Literal["completed", "failed", "cancelled"] = "failed"
        spans = _CliItemSpans(span_data_max_chars)
        try:
            stream = await thread.run_streamed(prompt, captured_turn_options)
            response = ""
            usage = None
            completed = False
            async with aclosing(stream.events):
                async for event in stream.events:
                    try:
                        spans.observe(event)
                    except Exception:
                        # Optional item tracing cannot change execution authority.
                        pass
                    if on_event is not None:
                        try:
                            observed = on_event(event)
                            if inspect.isawaitable(observed):
                                await observed
                        except Exception:
                            # Observers cannot change CLI success or thread authority.
                            pass
                    if on_stream is not None:
                        try:
                            observed = on_stream(event, context)
                            if inspect.isawaitable(observed):
                                await observed
                        except Exception:
                            pass
                    check()
                    if isinstance(event, ItemCompletedEvent) and is_agent_message_item(event.item):
                        response = event.item.text
                    elif isinstance(event, TurnCompletedEvent):
                        completed = True
                        usage = event.usage.as_dict() if event.usage is not None else None
                    elif isinstance(event, TurnFailedEvent):
                        raise RuntimeError(event.error.message)
                    elif isinstance(event, ThreadErrorEvent):
                        raise RuntimeError(event.message)
            check()
            if not completed:
                raise RuntimeError("Codex CLI stream ended before successful completion")
            context.report_usage(
                None
                if usage is None
                else TokenUsage(
                    usage["input_tokens"],
                    usage.get("cached_input_tokens", 0),
                    0,
                    usage["output_tokens"],
                    0,
                    usage["input_tokens"] + usage["output_tokens"],
                )
            )
            outcome = "completed"
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError) or (
                captured_turn_options is not None
                and captured_turn_options.signal is not None
                and captured_turn_options.signal.is_set()
            ):
                outcome = "cancelled"
            raise
        finally:
            try:
                spans.close(outcome)
            except Exception:
                pass
            if on_thread is not None and thread.id is not None:
                try:
                    observed = on_thread(thread.id, outcome, context)
                    if inspect.isawaitable(observed):
                        await observed
                except BaseException:
                    # Readback must not replace CLI errors or grant thread authority.
                    pass
        check()
        completed_thread_id = thread.id
        if completed_thread_id is not None:
            state.issued_ids.add(completed_thread_id)
        if persist_session:
            state.thread = thread
        return {"thread_id": completed_thread_id, "response": response, "usage": usage}

    @function_tool(name=name, description=description, is_enabled=is_enabled)
    async def invoke(
        context: ToolContext[object],
        prompt: Input,
        thread_id: str | None = None,
    ) -> dict[str, Any] | str:
        from .session import Session, _Invocation, _invocation, _native_callback_cancelled

        thread_id = _thread_id(thread_id)
        invocation = _invocation.get()
        if (
            invocation is None
            or not invocation.active
            or not isinstance(invocation.session, Session)
            or invocation.session.id != context.submission.session_id
        ):
            raise RuntimeError("Codex tools require an active native Session callback")

        def check() -> None:
            task = asyncio.current_task()
            if task is not None and cancellation_requested(task):
                raise asyncio.CancelledError
            if captured_turn_options is not None and captured_turn_options.signal is not None:
                if captured_turn_options.signal.is_set():
                    raise RuntimeError("Codex CLI call cancelled before execution")
            owner: _Invocation | None = invocation
            while owner is not None:
                if not owner.active:
                    raise RuntimeError("Codex CLI callback is no longer active")
                if owner.native_callback is not None and _native_callback_cancelled(
                    owner.native_callback[0]
                ):
                    raise asyncio.CancelledError
                owner = owner.parent

        # Values retain CLI state only, never the weakly held native Session key.
        state = sessions.setdefault(invocation.session, _SessionThread())
        try:
            if persist_session:
                async with _session_slot(
                    state.lock, captured_turn_options.signal if captured_turn_options else None
                ):
                    return await execute(prompt, thread_id, state, check, context)
            return await execute(prompt, thread_id, state, check, context)
        except Exception as error:
            check()
            if failure_error_function is None:
                raise
            result = failure_error_function(context, error)
            if inspect.isawaitable(result):
                result = await result
            check()
            if not isinstance(result, str):
                raise TypeError("failure_error_function must return a string") from error
            return result

    if parameters is None:
        return invoke
    original = invoke
    input_model = parameters

    def bind(context, policy_context, declaration):
        assert original._bind is not None
        captured = original._bind(
            context, policy_context, replace(declaration, input_type=original.input_type)
        )

        async def custom(context, /, **values):
            prompt = values.get("prompt", values.get("inputs"))
            if isinstance(prompt, list):
                prompt = [
                    item.model_dump(exclude_none=True) if isinstance(item, BaseModel) else item
                    for item in prompt
                ]
            normalized = original.input_type.model_validate(
                {"prompt": prompt, "thread_id": values.get("thread_id")}
            )
            return await captured.callback(
                context, prompt=normalized.prompt, thread_id=normalized.thread_id
            )

        return replace(
            captured,
            model=input_model,
            field_names=tuple(input_model.model_fields),
            spec_json=declaration._spec_json(),
            callback=custom,
        )

    return replace(original, input_type=input_model, _bind=bind)


__all__ = ["Codex", "CodexOptions", "ThreadOptions", "TurnOptions", "codex_tool"]


_preserve_module_identity(globals(), "agents.codex")
