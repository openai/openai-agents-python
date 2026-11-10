"""External Codex CLI delegation through a native function tool."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import aclosing, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any
from weakref import WeakKeyDictionary

from _agents_shared.codex_cli.codex import Codex
from _agents_shared.codex_cli.codex_options import (
    CodexOptions,
    coerce_codex_options,
)
from _agents_shared.codex_cli.events import (
    ItemCompletedEvent,
    ThreadErrorEvent,
    ThreadEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
)
from _agents_shared.codex_cli.items import is_agent_message_item
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
    if codex is not None and codex_options is not None:
        raise ValueError("supply codex or codex_options, not both")
    if not isinstance(persist_session, bool):
        raise TypeError("persist_session must be a bool")
    if on_event is not None and not callable(on_event):
        raise TypeError("on_event must be callable or None")
    if resolve_image is not None and not callable(resolve_image):
        raise TypeError("resolve_image must be callable or None")
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
    ) -> dict[str, Any]:
        nonlocal client
        if not prompt or (isinstance(prompt, str) and not prompt.strip()):
            raise ValueError("prompt must not be empty")
        check()
        if (
            thread_id is not None
            and thread_id != initial_thread_id
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
        thread = (
            client.resume_thread(thread_id, captured_thread_options)
            if thread_id is not None
            else state.thread
            if persist_session and state.thread is not None
            else client.resume_thread(initial_thread_id, captured_thread_options)
            if initial_thread_id is not None
            else client.start_thread(captured_thread_options)
        )
        stream = await thread.run_streamed(prompt, captured_turn_options)
        response = ""
        usage = None
        completed = False
        async with aclosing(stream.events):
            async for event in stream.events:
                if on_event is not None:
                    try:
                        observed = on_event(event)
                        if inspect.isawaitable(observed):
                            await observed
                    except Exception:
                        # Observers cannot change CLI success or persistent thread state.
                        pass
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
        completed_thread_id = thread.id
        if completed_thread_id is not None:
            state.issued_ids.add(completed_thread_id)
        if persist_session:
            state.thread = thread
        return {"thread_id": completed_thread_id, "response": response, "usage": usage}

    @function_tool(name=name, description=description)
    async def invoke(
        context: ToolContext[object],
        prompt: Input,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
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
        if persist_session:
            async with _session_slot(
                state.lock, captured_turn_options.signal if captured_turn_options else None
            ):
                return await execute(prompt, thread_id, state, check)
        return await execute(prompt, thread_id, state, check)

    return invoke


__all__ = ["Codex", "CodexOptions", "ThreadOptions", "TurnOptions", "codex_tool"]


_preserve_module_identity(globals(), "agents.codex")
