from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
from openai.types.responses import ResponseCustomToolCall
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_reasoning_item import ResponseReasoningItem

import agents.sandbox.capabilities.memory as memory_module
import agents.sandbox.memory.manager as memory_manager_module
import agents.sandbox.memory.phase_one as phase_one_module
from agents import (
    Agent,
    ReasoningItem,
    RunConfig,
    Runner,
    ShellTool,
    SQLiteSession,
    TResponseInputItem,
)
from agents.exceptions import UserError
from agents.items import CompactionItem, MessageOutputItem, TResponseOutputItem
from agents.result import RunResultStreaming
from agents.run import _sandbox_memory_input
from agents.run_context import RunContextWrapper
from agents.sandbox import (
    Manifest,
    MemoryGenerateConfig,
    MemoryLayoutConfig,
    MemoryReadConfig,
    SandboxAgent,
    SandboxRunConfig,
)
from agents.sandbox.capabilities import Memory
from agents.sandbox.memory.manager import (
    _rollout_file_name_for_rollout_id,
    get_or_create_memory_generation_manager,
)
from agents.sandbox.memory.phase_one import render_phase_one_prompt
from agents.sandbox.memory.prompts import (
    render_memory_consolidation_prompt,
    render_rollout_extraction_prompt,
)
from agents.sandbox.memory.rollouts import (
    RolloutTerminalMetadata,
    build_rollout_payload,
    build_rollout_payload_from_result,
    dump_rollout_json,
)
from agents.sandbox.memory.storage import (
    PhaseTwoInputSelection,
    PhaseTwoSelectionItem,
    SandboxMemoryStorage,
    _updated_at_sort_key,
)
from agents.sandbox.runtime import _stream_memory_input_override
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from tests.fake_model import FakeModel
from tests.test_responses import get_final_output_message, get_text_message
from tests.utils.hitl import make_shell_call


class _DeleteTrackingUnixLocalSandboxClient(UnixLocalSandboxClient):
    def __init__(self) -> None:
        super().__init__()
        self.deleted_roots: list[Path] = []

    async def delete(self, session: Any) -> Any:
        self.deleted_roots.append(Path(session.state.manifest.root))
        return await super().delete(session)


def _phase_one_message(
    *,
    slug: str = "task_memory",
    summary: str = "# Task summary\n",
    raw_memory: str = "raw memory entry\n",
) -> Any:
    return get_final_output_message(
        json.dumps(
            {
                "rollout_slug": slug,
                "rollout_summary": summary,
                "raw_memory": raw_memory,
            }
        )
    )


def test_rollout_file_name_for_rollout_id_uses_file_safe_id_directly() -> None:
    assert _rollout_file_name_for_rollout_id("chat-session.2026_04") == "chat-session.2026_04.jsonl"


def test_rollout_file_name_for_rollout_id_rejects_path_like_ids() -> None:
    with pytest.raises(ValueError, match="file-safe ID"):
        _rollout_file_name_for_rollout_id("../chat-session")


def test_rollout_file_name_for_rollout_id_rejects_empty_ids() -> None:
    with pytest.raises(ValueError, match="file-safe ID"):
        _rollout_file_name_for_rollout_id(" ")


def _patch_update_call(call_id: str, path: str, text: str) -> Any:
    diff = "@@\n" + "".join(f"+{line}\n" for line in text.splitlines())
    return ResponseCustomToolCall(
        type="custom_tool_call",
        name="apply_patch",
        call_id=call_id,
        input=json.dumps({"type": "update_file", "path": path, "diff": diff}),
    )


def _memory_config(
    *,
    max_raw_memories_for_consolidation: int = 256,
    extra_prompt: str | None = None,
    layout: MemoryLayoutConfig | None = None,
    read: MemoryReadConfig | None = None,
    phase_one_model: FakeModel | None = None,
    phase_two_model: FakeModel | None = None,
) -> Memory:
    return Memory(
        layout=layout or MemoryLayoutConfig(),
        read=read,
        generate=MemoryGenerateConfig(
            max_raw_memories_for_consolidation=max_raw_memories_for_consolidation,
            extra_prompt=extra_prompt,
            phase_one_model=phase_one_model or FakeModel(initial_output=[_phase_one_message()]),
            phase_two_model=phase_two_model
            or FakeModel(
                initial_output=[
                    _patch_update_call("memory-md", "memories/MEMORY.md", "memory entry"),
                    _patch_update_call(
                        "memory-summary", "memories/memory_summary.md", "summary entry"
                    ),
                ]
            ),
        ),
    )


def _run_config_for_session(session: Any) -> RunConfig:
    return RunConfig(sandbox=SandboxRunConfig(session=session))


def _extract_user_text(fake_model: FakeModel) -> str:
    assert fake_model.first_turn_args is not None
    return _extract_user_text_from_turn_args(fake_model.first_turn_args)


def _extract_user_text_from_turn_args(turn_args: dict[str, Any]) -> str:
    input_items = turn_args["input"]
    assert isinstance(input_items, list)
    first_item = cast(dict[str, Any], input_items[0])
    content = first_item["content"]
    if isinstance(content, str):
        return content
    first_content = cast(dict[str, Any], content[0])
    return cast(str, first_content["text"])


def _empty_phase_two_selection() -> PhaseTwoInputSelection:
    return PhaseTwoInputSelection(selected=[], retained_rollout_ids=set(), removed=[])


def _raw_memory_record(
    *,
    rollout_id: str,
    updated_at: str,
    rollout_summary_file: str,
    raw_memory: str,
) -> str:
    return (
        f"rollout_id: {rollout_id}\n"
        f"updated_at: {updated_at}\n"
        f"rollout_path: sessions/{rollout_id}.jsonl\n"
        f"rollout_summary_file: {rollout_summary_file}\n"
        "terminal_state: completed\n\n"
        f"{raw_memory.rstrip()}\n"
    )


async def _cleanup_session(
    client: UnixLocalSandboxClient,
    session: Any,
    *,
    close: bool = True,
) -> None:
    try:
        if close:
            await session.aclose()
    finally:
        await client.delete(session)


def test_build_rollout_payload_filters_developer_and_noisy_items() -> None:
    agent = Agent(name="test")
    assistant_message = cast(ResponseOutputMessage, get_text_message("assistant"))
    reasoning_item = ReasoningItem(
        agent=agent,
        raw_item=ResponseReasoningItem(id="rs_1", summary=[], type="reasoning"),
    )
    compaction_item = CompactionItem(
        agent=agent,
        raw_item=cast(
            TResponseInputItem,
            {
                "type": "compaction",
                "summary": "compact",
                "encrypted_content": "encrypted",
            },
        ),
    )
    message_item = MessageOutputItem(
        agent=agent,
        raw_item=assistant_message,
    )

    payload = build_rollout_payload(
        input=[
            {"role": "developer", "content": "debug"},
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
            cast(TResponseInputItem, {"type": "reasoning", "summary": []}),
            cast(
                TResponseInputItem,
                {
                    "type": "compaction",
                    "summary": "compact",
                    "encrypted_content": "encrypted",
                },
            ),
        ],
        new_items=[reasoning_item, compaction_item, message_item],
        final_output="done",
        interruptions=[],
        terminal_metadata=RolloutTerminalMetadata(
            terminal_state="completed",
            has_final_output=True,
        ),
    )

    updated_at = cast(str, payload.pop("updated_at"))
    assert datetime.fromisoformat(updated_at)
    assert list(payload) == ["input", "generated_items", "terminal_metadata", "final_output"]
    assert payload["input"] == [
        {"role": "user", "content": "hello"},
    ]
    assert payload["generated_items"] == [
        assistant_message.model_dump(exclude_unset=True),
    ]
    assert payload["final_output"] == "done"


def test_render_phase_one_prompt_truncates_large_rollout_contents() -> None:
    payload = {
        "input": [{"role": "user", "content": f"start{'a' * 700_000}middle{'z' * 700_000}end"}],
        "generated_items": [],
        "terminal_metadata": {"terminal_state": "completed", "has_final_output": False},
    }

    prompt = render_phase_one_prompt(rollout_contents=dump_rollout_json(payload))

    assert "start" in prompt
    assert "end" in prompt
    assert "middle" not in prompt
    assert "tokens truncated" in prompt
    assert "rollout content omitted" in prompt
    assert "Do not assume the rendered rollout below is complete" in prompt


def test_sandbox_memory_input_preserves_empty_session_delta() -> None:
    assert (
        _sandbox_memory_input(
            memory_input_items_for_persistence=[],
            original_user_input=[{"content": "old turn", "role": "user"}],
            original_input=[{"content": "old turn", "role": "user"}],
        )
        == []
    )


def test_sandbox_memory_input_uses_saved_session_delta_after_persistence() -> None:
    assert _sandbox_memory_input(
        memory_input_items_for_persistence=[{"content": "current turn", "role": "user"}],
        original_user_input=[{"content": "old turn", "role": "user"}],
        original_input=[{"content": "old turn", "role": "user"}],
    ) == [{"content": "current turn", "role": "user"}]


def test_streaming_memory_payload_preserves_empty_input_override() -> None:
    agent = Agent(name="test")
    result = RunResultStreaming(
        input=[{"content": "old turn", "role": "user"}],
        new_items=[],
        raw_responses=[],
        final_output="done",
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=RunContextWrapper(context=None),
        current_agent=agent,
        current_turn=0,
        max_turns=1,
        _current_agent_output_schema=None,
        trace=None,
        is_complete=True,
    )

    assert result._original_input_for_persistence is None
    result._original_input_for_persistence = []

    assert _stream_memory_input_override(result) == []
    payload = build_rollout_payload_from_result(
        result,
        input_override=_stream_memory_input_override(result),
    )

    assert payload["input"] == []


@pytest.mark.parametrize(
    ("conversation_id", "previous_response_id", "auto_previous_response_id"),
    [
        ("conversation-123", None, False),
        (None, "resp_123", False),
        (None, None, True),
    ],
)
def test_streaming_memory_payload_uses_result_input_for_server_managed_conversation(
    conversation_id: str | None,
    previous_response_id: str | None,
    auto_previous_response_id: bool,
) -> None:
    agent = Agent(name="test")
    result = RunResultStreaming(
        input=[{"content": "current turn", "role": "user"}],
        new_items=[],
        raw_responses=[],
        final_output="done",
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=RunContextWrapper(context=None),
        current_agent=agent,
        current_turn=0,
        max_turns=1,
        _current_agent_output_schema=None,
        trace=None,
        is_complete=True,
    )
    result._conversation_id = conversation_id
    result._previous_response_id = previous_response_id
    result._auto_previous_response_id = auto_previous_response_id
    result._original_input_for_persistence = []

    assert _stream_memory_input_override(result) is None
    payload = build_rollout_payload_from_result(
        result,
        input_override=_stream_memory_input_override(result),
    )

    assert payload["input"] == [{"content": "current turn", "role": "user"}]


def test_render_memory_prompts_omit_extra_prompt_section_by_default() -> None:
    rollout_prompt = render_rollout_extraction_prompt()
    consolidation_prompt = render_memory_consolidation_prompt(
        memory_root="memory",
        selection=_empty_phase_two_selection(),
    )

    assert "{{ extra_prompt_section }}" not in rollout_prompt
    assert "{{ extra_prompt_section }}" not in consolidation_prompt
    assert "DEVELOPER-SPECIFIC EXTRA GUIDANCE" not in rollout_prompt
    assert "DEVELOPER-SPECIFIC EXTRA GUIDANCE" not in consolidation_prompt


def test_render_memory_prompts_include_extra_prompt_section() -> None:
    rollout_prompt = render_rollout_extraction_prompt(extra_prompt="Focus on user preferences.")
    consolidation_prompt = render_memory_consolidation_prompt(
        memory_root="memory",
        selection=_empty_phase_two_selection(),
        extra_prompt="Focus on user preferences.",
    )

    assert "DEVELOPER-SPECIFIC EXTRA GUIDANCE" in rollout_prompt
    assert "Focus on user preferences." in rollout_prompt
    assert "DEVELOPER-SPECIFIC EXTRA GUIDANCE" in consolidation_prompt
    assert "Focus on user preferences." in consolidation_prompt


def test_updated_at_sort_key_places_unknown_timestamps_last() -> None:
    assert _updated_at_sort_key("updated_at: 2025-03-01T00:00:00Z\n") > _updated_at_sort_key(
        "updated_at: unknown\n"
    )
    assert _updated_at_sort_key("updated_at: unknown\n") == _updated_at_sort_key("updated_at:\n")
    assert _updated_at_sort_key("updated_at: unknown\n") == _updated_at_sort_key("no metadata\n")


@pytest.mark.asyncio
async def test_phase_two_selection_tracks_added_retained_and_removed_rollouts() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())

    try:
        storage = SandboxMemoryStorage(session=session, layout=MemoryLayoutConfig())
        await storage.ensure_layout()
        old_item = PhaseTwoSelectionItem(
            rollout_id="old-rollout",
            updated_at="2025-03-01T00:00:00Z",
            rollout_path="sessions/old-rollout.jsonl",
            rollout_summary_file="rollout_summaries/old-rollout.md",
            terminal_state="completed",
        )
        await storage.write_text(
            storage.raw_memories_dir / "old-rollout.md",
            _raw_memory_record(
                rollout_id=old_item.rollout_id,
                updated_at=old_item.updated_at,
                rollout_summary_file=old_item.rollout_summary_file,
                raw_memory="old raw",
            ),
        )
        await storage.write_text(
            storage.raw_memories_dir / "new-rollout.md",
            _raw_memory_record(
                rollout_id="new-rollout",
                updated_at="2025-03-02T00:00:00Z",
                rollout_summary_file="rollout_summaries/new-rollout.md",
                raw_memory="new raw",
            ),
        )
        await storage.write_phase_two_selection(selected_items=[old_item])

        selection = await storage.build_phase_two_input_selection(
            max_raw_memories_for_consolidation=1
        )

        assert [item.rollout_id for item in selection.selected] == ["new-rollout"]
        assert selection.retained_rollout_ids == set()
        assert [item.rollout_id for item in selection.removed] == ["old-rollout"]
    finally:
        await _cleanup_session(client, session)


@pytest.mark.asyncio
async def test_runner_memory_generation_sanitizes_and_truncates_phase_one_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phase_one_module, "_PHASE_ONE_ROLLOUT_TOKEN_LIMIT", 1000)
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    phase_one_model = FakeModel(initial_output=[_phase_one_message()])
    memory = _memory_config(phase_one_model=phase_one_model)
    agent = SandboxAgent(
        name="worker",
        model=FakeModel(
            initial_output=[
                ResponseReasoningItem(id="rs_1", summary=[], type="reasoning"),
                cast(
                    TResponseOutputItem,
                    {
                        "id": "compaction_1",
                        "type": "compaction",
                        "summary": "compacted-so-far",
                        "encrypted_content": "encrypted",
                    },
                ),
                get_text_message("done"),
            ]
        ),
        instructions="Worker.",
        capabilities=[memory],
    )

    closed = False
    try:
        result = await Runner.run(
            agent,
            [
                {"role": "developer", "content": "developer debug"},
                {"role": "system", "content": "system note"},
                {"role": "user", "content": f"start{'a' * 20_000}middle{'z' * 20_000}end"},
                cast(TResponseInputItem, {"type": "reasoning", "summary": []}),
                cast(
                    TResponseInputItem,
                    {
                        "type": "compaction",
                        "summary": "input-compact",
                        "encrypted_content": "encrypted",
                    },
                ),
            ],
            run_config=_run_config_for_session(session),
        )

        assert result.final_output == "done"
        assert phase_one_model.first_turn_args is None

        await session.aclose()
        closed = True

        prompt = _extract_user_text(phase_one_model)
        assert "developer debug" not in prompt
        assert "system note" not in prompt
        assert "reasoning" not in prompt
        assert "encrypted_content" not in prompt
        assert "input-compact" not in prompt
        assert "compacted-so-far" not in prompt
        assert "start" in prompt
        assert "middle" not in prompt
        assert "end" in prompt
        assert "tokens truncated" in prompt
        assert "rollout content omitted" in prompt
    finally:
        await _cleanup_session(client, session, close=not closed)


@pytest.mark.asyncio
async def test_sandbox_agent_without_memory_capability_skips_memory_generation() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    agent = SandboxAgent(
        name="worker",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Worker.",
    )

    try:
        result = await Runner.run(
            agent,
            "hello",
            run_config=_run_config_for_session(session),
        )

        root = Path(session.state.manifest.root)
        assert result.final_output == "done"
        assert not (root / "sessions").exists()
        assert not (root / "memories").exists()
    finally:
        await _cleanup_session(client, session)


@pytest.mark.asyncio
async def test_memory_capability_returns_none_without_memory_summary() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    capability = Memory(generate=None)

    try:
        async with session:
            capability.bind(session)

            assert await capability.instructions(session.state.manifest) is None

            await session.mkdir("memories", parents=True)
            await session.write(
                Path("memories/memory_summary.md"),
                io.BytesIO(b""),
            )

            assert await capability.instructions(session.state.manifest) is None
    finally:
        await client.delete(session)


@pytest.mark.parametrize(
    ("memories_dir", "match"),
    [
        ("/memory", "memories_dir must be relative"),
        ("../memory", "memories_dir must not escape root"),
        ("", "memories_dir must be non-empty"),
        (".", "memories_dir must be non-empty"),
    ],
)
def test_memory_capability_rejects_invalid_memories_dir(
    memories_dir: str,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        Memory(layout=MemoryLayoutConfig(memories_dir=memories_dir), generate=None)


@pytest.mark.parametrize(
    ("sessions_dir", "match"),
    [
        ("/sessions", "sessions_dir must be relative"),
        ("../sessions", "sessions_dir must not escape root"),
        ("", "sessions_dir must be non-empty"),
        (".", "sessions_dir must be non-empty"),
    ],
)
def test_memory_capability_rejects_invalid_sessions_dir(
    sessions_dir: str,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        Memory(layout=MemoryLayoutConfig(sessions_dir=sessions_dir), generate=None)


def test_memory_capability_requires_read_or_generate() -> None:
    with pytest.raises(ValueError, match="Memory requires at least one of `read` or `generate`"):
        Memory(read=None, generate=None)


def test_memory_generate_config_rejects_non_positive_recent_rollout_limit() -> None:
    with pytest.raises(
        ValueError,
        match=("MemoryGenerateConfig.max_raw_memories_for_consolidation must be greater than 0"),
    ):
        MemoryGenerateConfig(max_raw_memories_for_consolidation=0)


def test_memory_layout_config_defaults_match_codex_names() -> None:
    config = MemoryLayoutConfig()

    assert config.memories_dir == "memories"
    assert config.sessions_dir == "sessions"


def test_memory_generate_config_accepts_renamed_limit_field() -> None:
    config = MemoryGenerateConfig(max_raw_memories_for_consolidation=123)

    assert config.max_raw_memories_for_consolidation == 123


def test_memory_generate_config_rejects_too_many_raw_memories() -> None:
    with pytest.raises(
        ValueError,
        match=(
            "MemoryGenerateConfig.max_raw_memories_for_consolidation "
            "must be less than or equal to 4096"
        ),
    ):
        MemoryGenerateConfig(max_raw_memories_for_consolidation=4097)


@pytest.mark.asyncio
async def test_memory_capability_injects_truncated_memory_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    capability = Memory(generate=None)

    try:
        async with session:
            monkeypatch.setattr(memory_module, "_MEMORY_SUMMARY_MAX_TOKENS", 1)
            await session.mkdir("memories", parents=True)
            await session.write(
                Path("memories/memory_summary.md"),
                io.BytesIO(b"abcdefg"),
            )
            capability.bind(session)

            instructions = await capability.instructions(session.state.manifest)

            assert instructions is not None
            assert (
                "memories/memory_summary.md (already provided below; do NOT open again)"
                in instructions
            )
            assert "MEMORY_SUMMARY BEGINS" in instructions
            assert "tokens truncated" in instructions
    finally:
        await client.delete(session)


@pytest.mark.asyncio
async def test_memory_capability_live_update_instructions() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    capability = Memory(generate=None)

    try:
        async with session:
            await session.mkdir("memories", parents=True)
            await session.write(
                Path("memories/memory_summary.md"),
                io.BytesIO(b"summary entry"),
            )
            capability.bind(session)

            instructions = await capability.instructions(session.state.manifest)

            assert instructions is not None
            assert "Memory is writable." in instructions
            assert "memories/MEMORY.md" in instructions
            assert "same turn" in instructions
            assert "Never update memories." not in instructions
    finally:
        await client.delete(session)


@pytest.mark.asyncio
async def test_sandbox_memory_writes_rollouts_and_memory_files() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    phase_one_model = FakeModel(initial_output=[_phase_one_message()])
    phase_two_model = FakeModel(
        initial_output=[
            _patch_update_call("memory-md", "memories/MEMORY.md", "memory entry"),
            _patch_update_call("memory-summary", "memories/memory_summary.md", "summary entry"),
        ]
    )
    phase_two_model.set_next_output([get_final_output_message("consolidated")])
    memory = _memory_config(
        extra_prompt="Track durable user preferences.",
        phase_one_model=phase_one_model,
        phase_two_model=phase_two_model,
    )
    agent = SandboxAgent(
        name="worker",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Worker.",
        capabilities=[memory],
    )

    closed = False
    try:
        result = await Runner.run(
            agent,
            "hello",
            run_config=_run_config_for_session(session),
        )

        root = Path(session.state.manifest.root)
        rollouts = sorted((root / "sessions").glob("*.jsonl"))

        assert result.final_output == "done"
        assert len(rollouts) == 1
        assert phase_one_model.first_turn_args is None

        await session.aclose()
        closed = True

        raw_memories = sorted((root / "memories" / "raw_memories").glob("*.md"))
        rollout_summaries = sorted((root / "memories" / "rollout_summaries").glob("*.md"))

        assert len(raw_memories) == 1
        assert len(rollout_summaries) == 1
        assert (root / "memories" / "MEMORY.md").read_text() == "memory entry\n"
        assert (root / "memories" / "memory_summary.md").read_text() == "summary entry\n"
        assert "rollout_id: " in (root / "memories" / "raw_memories.md").read_text()
        assert "updated_at: " in (root / "memories" / "raw_memories.md").read_text()
        assert "rollout_path: sessions/" in (root / "memories" / "raw_memories.md").read_text()
        assert (
            "rollout_summary_file: rollout_summaries/"
            in (root / "memories" / "raw_memories.md").read_text()
        )
        assert "terminal_state: completed" in (root / "memories" / "raw_memories.md").read_text()
        assert "session_id: " in rollout_summaries[0].read_text()
        assert "updated_at: " in rollout_summaries[0].read_text()
        assert "rollout_path: sessions/" in rollout_summaries[0].read_text()
        assert "terminal_state: completed" in rollout_summaries[0].read_text()
        assert '"terminal_state":"completed"' in _extract_user_text(phase_one_model)
        assert phase_one_model.first_turn_args is not None
        assert (
            "DEVELOPER-SPECIFIC EXTRA GUIDANCE"
            in phase_one_model.first_turn_args["system_instructions"]
        )
        assert (
            "Track durable user preferences."
            in phase_one_model.first_turn_args["system_instructions"]
        )
        assert phase_two_model.first_turn_args is not None
        assert "DEVELOPER-SPECIFIC EXTRA GUIDANCE" in _extract_user_text(phase_two_model)
        assert "Track durable user preferences." in _extract_user_text(phase_two_model)
    finally:
        await _cleanup_session(client, session, close=not closed)


@pytest.mark.asyncio
async def test_sandbox_memory_uses_custom_layout() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    phase_two_model = FakeModel(
        initial_output=[
            _patch_update_call("memory-md", "agent_memory/MEMORY.md", "memory entry"),
            _patch_update_call("memory-summary", "agent_memory/memory_summary.md", "summary entry"),
        ]
    )
    phase_two_model.set_next_output([get_final_output_message("consolidated")])
    memory = Memory(
        layout=MemoryLayoutConfig(memories_dir="agent_memory", sessions_dir="agent_sessions"),
        read=None,
        generate=MemoryGenerateConfig(
            phase_one_model=FakeModel(initial_output=[_phase_one_message()]),
            phase_two_model=phase_two_model,
        ),
    )
    agent = SandboxAgent(
        name="worker",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Worker.",
        capabilities=[memory],
    )

    closed = False
    try:
        await Runner.run(
            agent,
            "hello",
            run_config=_run_config_for_session(session),
        )

        root = Path(session.state.manifest.root)
        assert len(list((root / "agent_sessions").glob("*.jsonl"))) == 1

        await session.aclose()
        closed = True

        assert (root / "agent_memory" / "MEMORY.md").read_text() == "memory entry\n"
        assert (root / "agent_memory" / "memory_summary.md").read_text() == "summary entry\n"
    finally:
        await _cleanup_session(client, session, close=not closed)


@pytest.mark.asyncio
async def test_sandbox_memory_supports_multiple_generating_layouts_in_one_session() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    phase_two_model_a = FakeModel(
        initial_output=[
            _patch_update_call("a-memory", "agent_a_memory/MEMORY.md", "agent a entry"),
            _patch_update_call(
                "a-summary",
                "agent_a_memory/memory_summary.md",
                "agent a summary",
            ),
        ]
    )
    phase_two_model_a.set_next_output([get_final_output_message("agent a consolidated")])
    phase_two_model_b = FakeModel(
        initial_output=[
            _patch_update_call("b-memory", "agent_b_memory/MEMORY.md", "agent b entry"),
            _patch_update_call(
                "b-summary",
                "agent_b_memory/memory_summary.md",
                "agent b summary",
            ),
        ]
    )
    phase_two_model_b.set_next_output([get_final_output_message("agent b consolidated")])
    memory_a = _memory_config(
        layout=MemoryLayoutConfig(memories_dir="agent_a_memory", sessions_dir="agent_a_sessions"),
        phase_one_model=FakeModel(initial_output=[_phase_one_message(raw_memory="agent a raw\n")]),
        phase_two_model=phase_two_model_a,
    )
    memory_b = _memory_config(
        layout=MemoryLayoutConfig(memories_dir="agent_b_memory", sessions_dir="agent_b_sessions"),
        phase_one_model=FakeModel(initial_output=[_phase_one_message(raw_memory="agent b raw\n")]),
        phase_two_model=phase_two_model_b,
    )
    agent_a = SandboxAgent(
        name="agent-a",
        model=FakeModel(initial_output=[get_final_output_message("a done")]),
        instructions="Agent A.",
        capabilities=[memory_a],
    )
    agent_b = SandboxAgent(
        name="agent-b",
        model=FakeModel(initial_output=[get_final_output_message("b done")]),
        instructions="Agent B.",
        capabilities=[memory_b],
    )

    closed = False
    try:
        await Runner.run(agent_a, "first", run_config=_run_config_for_session(session))
        await Runner.run(agent_b, "second", run_config=_run_config_for_session(session))

        root = Path(session.state.manifest.root)
        assert len(list((root / "agent_a_sessions").glob("*.jsonl"))) == 1
        assert len(list((root / "agent_b_sessions").glob("*.jsonl"))) == 1

        await session.aclose()
        closed = True

        assert (root / "agent_a_memory" / "MEMORY.md").read_text() == "agent a entry\n"
        assert (root / "agent_b_memory" / "MEMORY.md").read_text() == "agent b entry\n"
    finally:
        await _cleanup_session(client, session, close=not closed)


@pytest.mark.asyncio
async def test_sandbox_memory_rejects_different_generate_configs_for_same_layout() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    memory = _memory_config()
    different_memory = _memory_config(
        phase_one_model=FakeModel(initial_output=[_phase_one_message(raw_memory="different\n")])
    )

    try:
        get_or_create_memory_generation_manager(session=session, memory=memory)

        with pytest.raises(UserError, match="different Memory generation config"):
            get_or_create_memory_generation_manager(session=session, memory=different_memory)
    finally:
        await _cleanup_session(client, session)


@pytest.mark.asyncio
async def test_sandbox_memory_rollout_payload_uses_validated_rollout_id() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    memory = _memory_config()

    try:
        manager = get_or_create_memory_generation_manager(session=session, memory=memory)
        await manager.enqueue_rollout_payload(
            {
                "updated_at": "2026-04-15T00:00:00+00:00",
                "rollout_id": "payload-id",
                "input": [],
                "generated_items": [],
                "terminal_metadata": {"terminal_state": "completed", "has_final_output": False},
            },
            rollout_id="canonical-id",
        )

        root = Path(session.state.manifest.root)
        rollout_path = root / "sessions" / "canonical-id.jsonl"
        payload = json.loads(rollout_path.read_text())
        assert payload["rollout_id"] == "canonical-id"
    finally:
        await client.delete(session)


@pytest.mark.asyncio
async def test_sandbox_memory_rejects_different_sessions_dirs_for_same_memories_dir() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    first_memory = _memory_config(
        layout=MemoryLayoutConfig(memories_dir="shared_memory", sessions_dir="sessions_a")
    )
    second_memory = _memory_config(
        layout=MemoryLayoutConfig(memories_dir="shared_memory", sessions_dir="sessions_b")
    )

    try:
        get_or_create_memory_generation_manager(session=session, memory=first_memory)

        with pytest.raises(UserError, match="already has a Memory generation capability"):
            get_or_create_memory_generation_manager(session=session, memory=second_memory)
    finally:
        await _cleanup_session(client, session)


@pytest.mark.asyncio
async def test_sandbox_memory_rejects_shared_sessions_dir_for_different_memories_dirs() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    first_memory = _memory_config(
        layout=MemoryLayoutConfig(memories_dir="memory_a", sessions_dir="shared_sessions")
    )
    second_memory = _memory_config(
        layout=MemoryLayoutConfig(memories_dir="memory_b", sessions_dir="shared_sessions")
    )

    try:
        get_or_create_memory_generation_manager(session=session, memory=first_memory)

        with pytest.raises(UserError, match="sessions_dir='shared_sessions'"):
            get_or_create_memory_generation_manager(session=session, memory=second_memory)
    finally:
        await _cleanup_session(client, session)


@pytest.mark.asyncio
async def test_sandbox_memory_groups_segments_by_sdk_session_until_close() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    phase_one_model = FakeModel(initial_output=[_phase_one_message(raw_memory="joined raw\n")])
    phase_two_model = FakeModel(
        initial_output=[
            _patch_update_call("memory-md", "memories/MEMORY.md", "joined entry"),
            _patch_update_call("memory-summary", "memories/memory_summary.md", "joined summary"),
        ]
    )
    phase_two_model.set_next_output([get_final_output_message("joined")])
    memory = _memory_config(
        phase_one_model=phase_one_model,
        phase_two_model=phase_two_model,
    )
    first_agent = SandboxAgent(
        name="first-worker",
        model=FakeModel(initial_output=[get_final_output_message("first done")]),
        instructions="Worker.",
        capabilities=[memory],
    )
    second_agent = SandboxAgent(
        name="second-worker",
        model=FakeModel(initial_output=[get_final_output_message("second done")]),
        instructions="Worker.",
        capabilities=[memory],
    )

    closed = False
    try:
        chat_session = SQLiteSession("chat-session")
        run_config = _run_config_for_session(session)
        first = await Runner.run(
            first_agent,
            "first",
            session=chat_session,
            run_config=run_config,
        )
        second = await Runner.run(
            second_agent,
            "second",
            session=chat_session,
            run_config=run_config,
        )

        root = Path(session.state.manifest.root)
        rollouts = sorted((root / "sessions").glob("*.jsonl"))
        assert first.final_output == "first done"
        assert second.final_output == "second done"
        assert len(rollouts) == 1
        assert rollouts[0].name == "chat-session.jsonl"
        assert len(rollouts[0].read_text().splitlines()) == 2
        segments = [json.loads(line) for line in rollouts[0].read_text().splitlines()]
        assert list(segments[0])[:4] == [
            "updated_at",
            "rollout_id",
            "input",
            "generated_items",
        ]
        assert segments[0]["input"] == [{"content": "first", "role": "user"}]
        assert segments[1]["input"] == [{"content": "second", "role": "user"}]
        assert phase_one_model.first_turn_args is None

        await session.aclose()
        closed = True

        prompt = _extract_user_text(phase_one_model)
        assert "first" in prompt
        assert "second" in prompt
        assert '"segment_count":2' in prompt
        raw_memory_files = list((root / "memories" / "raw_memories").glob("*.md"))
        assert len(raw_memory_files) == 1
        assert f"updated_at: {segments[-1]['updated_at']}\n" in raw_memory_files[0].read_text()
        assert (root / "memories" / "MEMORY.md").read_text() == "joined entry\n"
    finally:
        await _cleanup_session(client, session, close=not closed)


@pytest.mark.asyncio
async def test_sandbox_memory_fallback_does_not_mutate_run_config() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    agent_model = FakeModel()
    agent_model.add_multiple_turn_outputs(
        [
            [get_final_output_message("first done")],
            [get_final_output_message("second done")],
        ]
    )
    agent = SandboxAgent(
        name="worker",
        model=agent_model,
        instructions="Worker.",
        capabilities=[_memory_config()],
    )

    try:
        run_config = _run_config_for_session(session)
        await Runner.run(
            agent,
            "first",
            session=SQLiteSession("first-chat"),
            run_config=run_config,
        )
        await Runner.run(
            agent,
            "second",
            session=SQLiteSession("second-chat"),
            run_config=run_config,
        )

        root = Path(session.state.manifest.root)
        rollouts = sorted(path.name for path in (root / "sessions").glob("*.jsonl"))
        assert rollouts == ["first-chat.jsonl", "second-chat.jsonl"]
    finally:
        await _cleanup_session(client, session)


@pytest.mark.asyncio
async def test_sandbox_memory_uses_conversation_id_when_sdk_session_is_absent() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    agent = SandboxAgent(
        name="worker",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Worker.",
        capabilities=[_memory_config()],
    )

    try:
        result = await Runner.run(
            agent,
            "remember this conversation",
            conversation_id="conversation-123",
            run_config=_run_config_for_session(session),
        )

        root = Path(session.state.manifest.root)
        rollouts = sorted((root / "sessions").glob("*.jsonl"))
        assert result.final_output == "done"
        assert len(rollouts) == 1
        assert rollouts[0].name == "conversation-123.jsonl"
    finally:
        await _cleanup_session(client, session)


@pytest.mark.asyncio
async def test_sandbox_memory_uses_group_id_when_sdk_session_is_absent() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    agent_model = FakeModel()
    agent_model.add_multiple_turn_outputs(
        [
            [get_final_output_message("first done")],
            [get_final_output_message("second done")],
        ]
    )
    agent = SandboxAgent(
        name="worker",
        model=agent_model,
        instructions="Worker.",
        capabilities=[_memory_config()],
    )

    try:
        run_config = RunConfig(
            sandbox=SandboxRunConfig(session=session),
            group_id="trace-thread-123",
        )
        first = await Runner.run(agent, "first", run_config=run_config)
        second = await Runner.run(agent, "second", run_config=run_config)

        root = Path(session.state.manifest.root)
        rollouts = sorted((root / "sessions").glob("*.jsonl"))
        assert first.final_output == "first done"
        assert second.final_output == "second done"
        assert len(rollouts) == 1
        assert rollouts[0].name == "trace-thread-123.jsonl"
        assert len(rollouts[0].read_text().splitlines()) == 2
    finally:
        await _cleanup_session(client, session)


@pytest.mark.asyncio
async def test_sandbox_memory_uses_per_run_conversation_when_no_conversation_id() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    agent_model = FakeModel()
    agent_model.add_multiple_turn_outputs(
        [
            [get_final_output_message("first done")],
            [get_final_output_message("second done")],
        ]
    )
    agent = SandboxAgent(
        name="worker",
        model=agent_model,
        instructions="Worker.",
        capabilities=[_memory_config()],
    )

    try:
        run_config = _run_config_for_session(session)
        first = await Runner.run(agent, "first", run_config=run_config)
        second = await Runner.run(agent, "second", run_config=run_config)

        root = Path(session.state.manifest.root)
        rollouts = sorted(path.name for path in (root / "sessions").glob("*.jsonl"))
        assert first.final_output == "first done"
        assert second.final_output == "second done"
        assert len(rollouts) == 2
        assert all(name.startswith("run-") and name.endswith(".jsonl") for name in rollouts)
    finally:
        await _cleanup_session(client, session)


@pytest.mark.asyncio
async def test_sandbox_memory_caps_phase_two_selection_and_surfaces_removed_rollouts() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    phase_one_model = FakeModel()
    phase_one_model.add_multiple_turn_outputs(
        [
            [_phase_one_message(slug="first", raw_memory="first raw\n")],
            [_phase_one_message(slug="second", raw_memory="second raw\n")],
        ]
    )
    phase_two_model = FakeModel(
        initial_output=[
            _patch_update_call("memory-md", "memories/MEMORY.md", "first entry"),
            _patch_update_call("memory-summary", "memories/memory_summary.md", "first summary"),
        ]
    )
    phase_two_model.set_next_output([get_final_output_message("consolidated")])
    memory = _memory_config(
        max_raw_memories_for_consolidation=1,
        phase_one_model=phase_one_model,
        phase_two_model=phase_two_model,
    )
    agent_model = FakeModel()
    agent_model.add_multiple_turn_outputs(
        [
            [get_final_output_message("first done")],
            [get_final_output_message("second done")],
        ]
    )
    agent = SandboxAgent(
        name="worker",
        model=agent_model,
        instructions="Worker.",
        capabilities=[memory],
    )

    closed = False
    try:
        root = Path(session.state.manifest.root)
        await Runner.run(
            agent,
            "first",
            run_config=RunConfig(
                sandbox=SandboxRunConfig(session=session),
                group_id="first-chat",
            ),
        )
        await Runner.run(
            agent,
            "second",
            run_config=RunConfig(
                sandbox=SandboxRunConfig(session=session),
                group_id="second-chat",
            ),
        )

        assert len(list((root / "sessions").glob("*.jsonl"))) == 2

        await session.aclose()
        closed = True

        selection_payload = json.loads((root / "memories" / "phase_two_selection.json").read_text())
        selected_rollout_ids = [
            cast(str, item["rollout_id"]) for item in selection_payload["selected"]
        ]
        assert len(selected_rollout_ids) == 1

        merged_raw_memories = (root / "memories" / "raw_memories.md").read_text()
        assert "second raw" in merged_raw_memories
        assert "first raw" not in merged_raw_memories

        assert phase_two_model.first_turn_args is not None
        prompt = _extract_user_text_from_turn_args(phase_two_model.first_turn_args)
        assert "newly added since the last successful Phase 2 run: 1" in prompt
        assert f"rollout_id={selected_rollout_ids[0]}" in prompt
    finally:
        await _cleanup_session(client, session, close=not closed)


@pytest.mark.asyncio
async def test_sandbox_memory_runs_phase_one_and_phase_two_on_session_close() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    phase_one_model = FakeModel(initial_output=[_phase_one_message()])
    phase_two_model = FakeModel(
        initial_output=[
            _patch_update_call("memory-md", "memories/MEMORY.md", "shutdown entry"),
            _patch_update_call("memory-summary", "memories/memory_summary.md", "shutdown summary"),
        ]
    )
    phase_two_model.set_next_output([get_final_output_message("shutdown")])
    memory = _memory_config(
        phase_one_model=phase_one_model,
        phase_two_model=phase_two_model,
    )
    agent = SandboxAgent(
        name="worker",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Worker.",
        capabilities=[memory],
    )

    root = Path(session.state.manifest.root)
    try:
        await Runner.run(agent, "hello", run_config=_run_config_for_session(session))
        manager = get_or_create_memory_generation_manager(session=session, memory=memory)
        await manager._queue.join()
        assert (root / "memories" / "MEMORY.md").read_text() == ""

        await session.aclose()

        assert (root / "memories" / "MEMORY.md").read_text() == "shutdown entry\n"
        assert (root / "memories" / "memory_summary.md").read_text() == "shutdown summary\n"
    finally:
        await client.delete(session)


@pytest.mark.asyncio
async def test_sandbox_memory_unregisters_manager_on_session_close() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    memory = _memory_config()

    try:
        manager = get_or_create_memory_generation_manager(session=session, memory=memory)

        managers_by_layout = memory_manager_module._MEMORY_GENERATION_MANAGERS.get(session)
        assert managers_by_layout is not None
        assert manager in managers_by_layout.values()

        await session.aclose()

        assert memory_manager_module._MEMORY_GENERATION_MANAGERS.get(session) is None
    finally:
        await client.delete(session)


@pytest.mark.asyncio
async def test_sandbox_memory_enqueue_failure_still_cleans_up_owned_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_write_rollout(*args: Any, **kwargs: Any) -> Path:
        _ = args, kwargs
        raise RuntimeError("write_rollout failed")

    monkeypatch.setattr(memory_manager_module, "write_rollout", _raise_write_rollout)

    client = _DeleteTrackingUnixLocalSandboxClient()
    agent = SandboxAgent(
        name="worker",
        model=FakeModel(initial_output=[get_final_output_message("done")]),
        instructions="Worker.",
        capabilities=[_memory_config()],
    )

    result = await Runner.run(
        agent,
        "hello",
        run_config=RunConfig(sandbox=SandboxRunConfig(client=client)),
    )

    assert result.final_output == "done"
    assert len(client.deleted_roots) == 1
    assert not client.deleted_roots[0].exists()


@pytest.mark.asyncio
async def test_sandbox_memory_marks_interrupted_runs_in_phase_one_prompt() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(manifest=Manifest())
    phase_one_model = FakeModel(initial_output=[_phase_one_message()])
    phase_two_model = FakeModel(
        initial_output=[
            _patch_update_call("memory-md", "memories/MEMORY.md", "interrupted entry"),
            _patch_update_call(
                "memory-summary", "memories/memory_summary.md", "interrupted summary"
            ),
        ]
    )
    phase_two_model.set_next_output([get_final_output_message("done")])
    memory = _memory_config(
        phase_one_model=phase_one_model,
        phase_two_model=phase_two_model,
    )
    agent = SandboxAgent(
        name="worker",
        model=FakeModel(initial_output=[make_shell_call("approval-call")]),
        instructions="Worker.",
        tools=[ShellTool(executor=lambda _request: "ok", needs_approval=True)],
        capabilities=[memory],
    )

    closed = False
    try:
        result = await Runner.run(
            agent,
            "interrupt me",
            run_config=_run_config_for_session(session),
        )

        assert result.interruptions
        await session.aclose()
        closed = True

        assert '"terminal_state":"interrupted"' in _extract_user_text(phase_one_model)
    finally:
        await _cleanup_session(client, session, close=not closed)
