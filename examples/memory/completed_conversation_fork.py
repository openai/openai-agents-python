"""Fork an authorized completed local history into an independent SQLite session.

Run ``python -m examples.memory.completed_conversation_fork`` for an offline demo.
This intentionally narrow recipe supports text messages and completed bare SDK
function calls only. It rejects native/hosted tools, approvals, files, reasoning
and opaque server references. It neither copies workspace files nor rolls them
back. The host authorizes the entire exported content, including tool results.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from agents import Agent, RunConfig, Runner, SQLiteSession
from agents.items import TResponseInputItem
from agents.testing import ScriptedModel, assistant_message


def completed_text_prefix(
    items: list[TResponseInputItem], *, tools: set[str]
) -> list[TResponseInputItem]:
    """Validate the narrow replay format without inferring hidden conversation state."""
    if not items:
        raise ValueError("A fork requires a nonempty completed local history.")
    pending: set[str] = set()
    seen: set[str] = set()
    for item in items:
        kind = item.get("type", "message")
        if kind == "message":
            if (
                pending
                or item.get("role") not in {"user", "assistant"}
                or item.get("status", "completed") != "completed"
                or set(item) - {"type", "role", "content", "id", "status", "phase"}
                or (
                    item.get("phase") is not None
                    and (
                        item.get("role") != "assistant"
                        or item["phase"] not in {"commentary", "final_answer"}
                    )
                )
            ):
                raise ValueError("Only complete user/assistant text messages are portable.")
            content = item.get("content")
            if isinstance(content, str):
                continue
            if not isinstance(content, list) or not content:
                raise ValueError("A text message needs text content.")
            for part in content:
                if (
                    not isinstance(part, dict)
                    or part.get("type") not in {"input_text", "output_text"}
                    or not isinstance(part.get("text"), str)
                    or set(part) - {"type", "text", "annotations", "logprobs"}
                    or part.get("annotations")
                    or part.get("logprobs")
                ):
                    raise ValueError(
                        "Files, annotations, and opaque content require another export."
                    )
        elif kind == "function_call":
            call_id = item.get("call_id")
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in seen
                or item.get("name") not in tools
                or item.get("namespace") is not None
                or item.get("status", "completed") != "completed"
                or set(item) - {"type", "id", "call_id", "name", "arguments", "status", "namespace"}
            ):
                raise ValueError("The SDK tool call is not portable.")
            arguments = item.get("arguments")
            if not isinstance(arguments, str) or not isinstance(json.loads(arguments), dict):
                raise ValueError("The tool arguments require a JSON object.")
            pending.add(call_id)
            seen.add(call_id)
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if (
                not isinstance(call_id, str)
                or call_id not in pending
                or not isinstance(item.get("output"), str)
                or item.get("status", "completed") != "completed"
                or set(item) - {"type", "id", "call_id", "output", "status"}
            ):
                raise ValueError("A tool result requires exactly one preceding call.")
            pending.remove(call_id)
        else:
            raise ValueError("Opaque state and pending approvals cannot be forked by this recipe.")
    if (
        pending
        or items[-1].get("role") != "assistant"
        or items[-1].get("phase") not in (None, "final_answer")
    ):
        raise ValueError("Fork only after a completed assistant turn with no pending tools.")
    # Deep copies prevent mutations of the export from changing the source snapshot.
    return copy.deepcopy(items)


async def fork_completed(
    source: SQLiteSession,
    destination: Path,
    *,
    authorize: Callable[[str, list[TResponseInputItem]], bool],
    tools: set[str],
    validate_destination: Callable[[list[TResponseInputItem]], None],
) -> SQLiteSession:
    """Copy one approved local snapshot without copying session or approval authority.

    The host's validate_destination callback checks the actual destination adapter,
    model settings and declared tools before importing anything. Unknown adapter
    support remains unknown; successful local format validation is not a live
    provider compatibility guarantee.
    """
    if source.session_settings is not None and source.session_settings.limit is not None:
        raise ValueError("Use an unlimited local session; a truncated prefix is not a full export.")
    items = await source.get_items()
    if not authorize(source.session_id, items):
        raise PermissionError("The host has not authorized this history export.")
    portable = completed_text_prefix(items, tools=tools)
    validate_destination(portable)
    child = SQLiteSession(uuid4().hex, destination)
    try:
        await child.add_items(portable)
    except BaseException:
        child.close()
        raise
    return child


async def main() -> None:
    with TemporaryDirectory() as directory:
        parent = SQLiteSession("parent", Path(directory) / "parent.db")
        child = None
        try:
            agent = Agent(name="Assistant", model=ScriptedModel([[assistant_message("Original")]]))
            config = RunConfig(tracing_disabled=True)
            await Runner.run(agent, "Begin.", session=parent, run_config=config)
            child = await fork_completed(
                parent,
                Path(directory) / "child.db",
                authorize=lambda session_id, items: session_id == "parent",
                tools=set(),
                validate_destination=lambda items: None,
            )
            agent.model = ScriptedModel([[assistant_message("Parent branch")]])
            await Runner.run(agent, "Continue parent.", session=parent, run_config=config)
            agent.model = ScriptedModel([[assistant_message("Child branch")]])
            await Runner.run(agent, "Continue child.", session=child, run_config=config)
            print(
                "Independent histories:",
                len(await parent.get_items()),
                len(await child.get_items()),
            )
        finally:
            parent.close()
            if child is not None:
                child.close()


if __name__ == "__main__":
    asyncio.run(main())
