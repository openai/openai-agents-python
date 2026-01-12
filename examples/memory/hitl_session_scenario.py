"""
Scenario that exercises HITL approvals, rehydration, and rejections across sessions.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents import Agent, Model, ModelSettings, OpenAIConversationsSession, Runner, function_tool
from agents.items import TResponseInputItem

from .file_session import FileSession

TOOL_ECHO = "approved_echo"
TOOL_NOTE = "approved_note"
REJECTION_OUTPUT = "Tool execution was not approved."
USER_MESSAGES = [
    "Fetch profile for customer 104.",
    "Update note for customer 104.",
    "Delete note for customer 104.",
]


def tool_output_for(name: str, message: str) -> str:
    if name == TOOL_ECHO:
        return f"approved:{message}"
    if name == TOOL_NOTE:
        return f"approved_note:{message}"
    raise ValueError(f"Unknown tool name: {name}")


@function_tool(
    name_override=TOOL_ECHO,
    description_override="Echoes back the provided query after approval.",
    needs_approval=True,
)
def approval_echo(query: str) -> str:
    """Return the approved echo payload."""
    return tool_output_for(TOOL_ECHO, query)


@function_tool(
    name_override=TOOL_NOTE,
    description_override="Records the provided query after approval.",
    needs_approval=True,
)
def approval_note(query: str) -> str:
    """Return the approved note payload."""
    return tool_output_for(TOOL_NOTE, query)


@dataclass(frozen=True)
class ScenarioStep:
    name: str
    message: str
    tool_name: str
    approval: str
    expected_output: str


async def run_scenario_step(
    session: Any,
    label: str,
    step: ScenarioStep,
    *,
    model: str | Model | None = None,
) -> None:
    agent = Agent(
        name=f"{label} HITL scenario",
        instructions=(
            f"You must call {step.tool_name} exactly once before responding. "
            "Pass the user input as the 'query' argument."
        ),
        tools=[approval_echo, approval_note],
        model=model,
        model_settings=ModelSettings(tool_choice=step.tool_name),
        tool_use_behavior="stop_on_first_tool",
    )

    result = await Runner.run(agent, step.message, session=session)
    if not result.interruptions:
        raise RuntimeError(f"[{label}] expected at least one tool approval.")

    while result.interruptions:
        state = result.to_state()
        for interruption in result.interruptions:
            if step.approval == "reject":
                state.reject(interruption)
            else:
                state.approve(interruption)
        result = await Runner.run(agent, state, session=session)

    if result.final_output is None:
        raise RuntimeError(f"[{label}] expected a final output after approval.")
    if step.approval != "reject" and result.final_output != step.expected_output:
        raise RuntimeError(
            f"[{label}] expected final output '{step.expected_output}' but got "
            f"'{result.final_output}'."
        )

    items = await session.get_items()
    tool_results = [item for item in items if get_item_type(item) == "function_call_output"]
    user_messages = [item for item in items if get_user_text(item) == step.message]
    last_tool_call = find_last_item(items, is_function_call)
    last_tool_result = find_last_item(items, is_function_call_output)

    if not tool_results:
        raise RuntimeError(f"[{label}] expected tool outputs in session history.")
    if not user_messages:
        raise RuntimeError(f"[{label}] expected user input in session history.")
    if not last_tool_call:
        raise RuntimeError(f"[{label}] expected a tool call in session history.")
    if last_tool_call.get("name") != step.tool_name:
        raise RuntimeError(
            f"[{label}] expected tool call '{step.tool_name}' but got '{last_tool_call.get('name')}'."
        )
    if not last_tool_result:
        raise RuntimeError(f"[{label}] expected a tool result in session history.")

    tool_call_id = extract_call_id(last_tool_call)
    tool_result_call_id = extract_call_id(last_tool_result)
    if tool_call_id and tool_result_call_id and tool_result_call_id != tool_call_id:
        raise RuntimeError(
            f"[{label}] expected tool result call_id '{tool_call_id}' but got '{tool_result_call_id}'."
        )

    tool_output_text = format_output(last_tool_result.get("output"))
    if tool_output_text != step.expected_output:
        raise RuntimeError(
            f"[{label}] expected tool output '{step.expected_output}' but got '{tool_output_text}'."
        )

    log_session_summary(items, label)
    print(f"[{label}] final output: {result.final_output} (items: {len(items)})")


async def run_file_session_scenario(*, model: str | Model | None = None) -> None:
    tmp_root = Path.cwd() / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="hitl-scenario-", dir=tmp_root))
    session = FileSession(dir=temp_dir)
    session_id = await session.get_session_id()
    session_file = temp_dir / f"{session_id}.json"
    rehydrated_session: FileSession | None = None

    print(f"[FileSession] session id: {session_id}")
    print(f"[FileSession] file: {session_file}")
    print("[FileSession] cleanup: always")

    steps = [
        ScenarioStep(
            name="turn 1",
            message=USER_MESSAGES[0],
            tool_name=TOOL_ECHO,
            approval="approve",
            expected_output=tool_output_for(TOOL_ECHO, USER_MESSAGES[0]),
        ),
        ScenarioStep(
            name="turn 2 (rehydrated)",
            message=USER_MESSAGES[1],
            tool_name=TOOL_NOTE,
            approval="approve",
            expected_output=tool_output_for(TOOL_NOTE, USER_MESSAGES[1]),
        ),
        ScenarioStep(
            name="turn 3 (rejected)",
            message=USER_MESSAGES[2],
            tool_name=TOOL_ECHO,
            approval="reject",
            expected_output=REJECTION_OUTPUT,
        ),
    ]

    try:
        await run_scenario_step(
            session,
            f"FileSession {steps[0].name}",
            steps[0],
            model=model,
        )
        rehydrated_session = FileSession(dir=temp_dir, session_id=session_id)
        print(f"[FileSession] rehydrated session id: {session_id}")
        await run_scenario_step(
            rehydrated_session,
            f"FileSession {steps[1].name}",
            steps[1],
            model=model,
        )
        await run_scenario_step(
            rehydrated_session,
            f"FileSession {steps[2].name}",
            steps[2],
            model=model,
        )
    finally:
        await (rehydrated_session or session).clear_session()
        shutil.rmtree(temp_dir, ignore_errors=True)


async def run_openai_session_scenario(*, model: str | Model | None = None) -> None:
    existing_session_id = os.environ.get("OPENAI_SESSION_ID")
    session = OpenAIConversationsSession(conversation_id=existing_session_id)
    session_id = await get_conversation_id(session)
    should_keep = bool(os.environ.get("KEEP_OPENAI_SESSION") or existing_session_id)

    if existing_session_id:
        print(f"[OpenAIConversationsSession] reuse session id: {session_id}")
    else:
        print(f"[OpenAIConversationsSession] new session id: {session_id}")
    print(f"[OpenAIConversationsSession] cleanup: {'skip' if should_keep else 'delete'}")

    steps = [
        ScenarioStep(
            name="turn 1",
            message=USER_MESSAGES[0],
            tool_name=TOOL_ECHO,
            approval="approve",
            expected_output=tool_output_for(TOOL_ECHO, USER_MESSAGES[0]),
        ),
        ScenarioStep(
            name="turn 2 (rehydrated)",
            message=USER_MESSAGES[1],
            tool_name=TOOL_NOTE,
            approval="approve",
            expected_output=tool_output_for(TOOL_NOTE, USER_MESSAGES[1]),
        ),
        ScenarioStep(
            name="turn 3 (rejected)",
            message=USER_MESSAGES[2],
            tool_name=TOOL_ECHO,
            approval="reject",
            expected_output=REJECTION_OUTPUT,
        ),
    ]

    await run_scenario_step(
        session,
        f"OpenAIConversationsSession {steps[0].name}",
        steps[0],
        model=model,
    )

    rehydrated_session = OpenAIConversationsSession(conversation_id=session_id)
    print(f"[OpenAIConversationsSession] rehydrated session id: {session_id}")
    await run_scenario_step(
        rehydrated_session,
        f"OpenAIConversationsSession {steps[1].name}",
        steps[1],
        model=model,
    )
    await run_scenario_step(
        rehydrated_session,
        f"OpenAIConversationsSession {steps[2].name}",
        steps[2],
        model=model,
    )

    if should_keep:
        print(f"[OpenAIConversationsSession] kept session id: {session_id}")
        return

    print(f"[OpenAIConversationsSession] deleting session id: {session_id}")
    await rehydrated_session.clear_session()


async def get_conversation_id(session: OpenAIConversationsSession) -> str:
    return await session._get_session_id()


def get_user_text(item: TResponseInputItem) -> str | None:
    if not isinstance(item, dict) or item.get("role") != "user":
        return None

    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "input_text":
            parts.append(part.get("text", ""))
    return "".join(parts)


def get_item_type(item: TResponseInputItem) -> str:
    if isinstance(item, dict):
        return item.get("type") or ("message" if "role" in item else "unknown")
    return "unknown"


def is_function_call(item: TResponseInputItem) -> bool:
    return isinstance(item, dict) and item.get("type") == "function_call"


def is_function_call_output(item: TResponseInputItem) -> bool:
    return isinstance(item, dict) and item.get("type") == "function_call_output"


def find_last_item(items: list[TResponseInputItem], predicate: Any) -> dict[str, Any] | None:
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if predicate(item):
            return item  # type: ignore[return-value]
    return None


def extract_call_id(item: dict[str, Any]) -> str | None:
    return cast_str(item.get("call_id") or item.get("id"))


def cast_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def log_session_summary(items: list[TResponseInputItem], label: str) -> None:
    type_counts: dict[str, int] = {}
    for item in items:
        item_type = get_item_type(item)
        type_counts[item_type] = type_counts.get(item_type, 0) + 1

    type_summary = " ".join(f"{item_type}={count}" for item_type, count in type_counts.items())

    summary_suffix = f" ({type_summary})" if type_summary else ""
    print(f"[{label}] session summary: items={len(items)}{summary_suffix}")

    user_text = None
    for index in range(len(items) - 1, -1, -1):
        user_text = get_user_text(items[index])
        if user_text:
            break
    if user_text:
        print(f"[{label}] user: {truncate_text(user_text)}")

    tool_call = find_last_item(items, is_function_call)
    if tool_call:
        args = truncate_text(str(tool_call.get("arguments", "")))
        call_id = extract_call_id(tool_call)
        call_id_label = f" call_id={call_id}" if call_id else ""
        args_label = f" args={args}" if args else ""
        print(f"[{label}] tool call: {tool_call.get('name')}{call_id_label}{args_label}")

    tool_result = find_last_item(items, is_function_call_output)
    if tool_result:
        output = truncate_text(format_output(tool_result.get("output")))
        call_id = extract_call_id(tool_result)
        call_id_label = f" call_id={call_id}" if call_id else ""
        output_label = f" output={output}" if output else ""
        print(f"[{label}] tool result:{call_id_label}{output_label}")


def format_output(output: Any) -> str:
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    if isinstance(output, list):
        text_parts = []
        for entry in output:
            if isinstance(entry, dict) and entry.get("type") == "input_text":
                text_parts.append(entry.get("text", ""))
        if text_parts:
            return "".join(text_parts)
    try:
        return json.dumps(output)
    except TypeError:
        return str(output)


def truncate_text(text: str, max_length: int = 140) -> str:
    if len(text) <= max_length:
        return text
    suffix = "..."
    if max_length <= len(suffix):
        return suffix
    return f"{text[: max_length - len(suffix)]}{suffix}"


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY must be set to run the HITL session scenario.")
        raise SystemExit(1)

    model_override = os.environ.get("HITL_MODEL", "gpt-5.2")
    if model_override:
        print(f"Model: {model_override}")

    await run_file_session_scenario(model=model_override)
    await run_openai_session_scenario(model=model_override)


if __name__ == "__main__":
    asyncio.run(main())
