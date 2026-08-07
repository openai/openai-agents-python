from __future__ import annotations

import asyncio
import logging
import warnings as warnings_module
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai.types.responses import ResponseOutputMessage

import agents._debug as _debug
from agents import Agent, RunConfig, Runner, function_tool
from agents.items import MessageOutputItem, RunItem, TResponseInputItem
from agents.memory import (
    OpenAIResponsesCompactionSession,
    Session,
    SessionSettings,
    is_openai_responses_compaction_aware_session,
)
from agents.memory.openai_responses_compaction_session import (
    DEFAULT_COMPACTION_THRESHOLD,
    _strip_orphaned_assistant_ids,
    is_openai_model_name,
    select_compaction_candidate_items,
)
from agents.run_internal.items import (
    TOOL_CALL_SESSION_DESCRIPTION_KEY,
    TOOL_CALL_SESSION_TITLE_KEY,
)
from agents.run_internal.session_persistence import (
    resolve_session_history_limit,
    save_result_to_session,
)
from tests.fake_model import FakeModel
from tests.test_responses import get_function_tool, get_function_tool_call, get_text_message
from tests.utils.simple_session import SimpleListSession


class TestIsOpenAIModelName:
    def test_gpt_models(self) -> None:
        assert is_openai_model_name("gpt-4o") is True
        assert is_openai_model_name("gpt-4o-mini") is True
        assert is_openai_model_name("gpt-3.5-turbo") is True
        assert is_openai_model_name("gpt-4.1") is True
        assert is_openai_model_name("gpt-5") is True
        assert is_openai_model_name("gpt-5.2") is True
        assert is_openai_model_name("gpt-5-mini") is True
        assert is_openai_model_name("gpt-5-nano") is True

    def test_o_models(self) -> None:
        assert is_openai_model_name("o1") is True
        assert is_openai_model_name("o1-preview") is True
        assert is_openai_model_name("o3") is True

    def test_fine_tuned_models(self) -> None:
        assert is_openai_model_name("ft:gpt-4o-mini:org:proj:suffix") is True
        assert is_openai_model_name("ft:gpt-4.1:my-org::id") is True

    def test_invalid_models(self) -> None:
        assert is_openai_model_name("") is False
        assert is_openai_model_name("not-openai") is False


class TestSelectCompactionCandidateItems:
    def test_excludes_user_messages(self) -> None:
        items: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "hello"}),
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": "hi"}),
        ]
        result = select_compaction_candidate_items(items)
        assert len(result) == 1
        assert result[0].get("role") == "assistant"

    def test_excludes_compaction_items(self) -> None:
        items: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "compaction", "summary": "..."}),
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": "hi"}),
        ]
        result = select_compaction_candidate_items(items)
        assert len(result) == 1
        assert result[0].get("type") == "message"

    def test_excludes_easy_user_messages_without_type(self) -> None:
        items: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"content": "hi", "role": "user"}),
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": "hello"}),
        ]
        result = select_compaction_candidate_items(items)
        assert len(result) == 1
        assert result[0].get("role") == "assistant"


class TestOpenAIResponsesCompactionSession:
    def create_mock_session(self) -> MagicMock:
        mock = MagicMock(spec=Session)
        mock.session_id = "test-session"
        mock.get_items = AsyncMock(return_value=[])
        mock.add_items = AsyncMock()
        mock.pop_item = AsyncMock(return_value=None)
        mock.clear_session = AsyncMock()
        return mock

    def assistant_history(self, count: int) -> list[TResponseInputItem]:
        return [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": f"item-{i}"},
            )
            for i in range(count)
        ]

    def message_run_items(self, agent: Agent[Any], count: int = 1) -> list[RunItem]:
        return [
            MessageOutputItem(
                agent=agent,
                raw_item=cast(ResponseOutputMessage, get_text_message(f"new-{i}")),
            )
            for i in range(count)
        ]

    def test_init_validates_model(self) -> None:
        mock_session = self.create_mock_session()

        with pytest.raises(ValueError, match="Unsupported model"):
            OpenAIResponsesCompactionSession(
                session_id="test",
                underlying_session=mock_session,
                model="claude-3",
            )

    def test_init_accepts_valid_model(self) -> None:
        mock_session = self.create_mock_session()
        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            model="gpt-4.1",
        )
        assert session.model == "gpt-4.1"

    @pytest.mark.asyncio
    async def test_add_items_delegates(self) -> None:
        mock_session = self.create_mock_session()
        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
        )

        items: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": "test"})
        ]
        await session.add_items(items)

        mock_session.add_items.assert_called_once_with(items)

    @pytest.mark.asyncio
    async def test_get_items_delegates(self) -> None:
        mock_session = self.create_mock_session()
        mock_session.get_items.return_value = [{"type": "message", "content": "test"}]

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
        )

        result = await session.get_items()
        assert len(result) == 1
        mock_session.get_items.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_compaction_requires_response_id(self) -> None:
        mock_session = self.create_mock_session()
        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            compaction_mode="previous_response_id",
        )

        with pytest.raises(ValueError, match="previous_response_id compaction"):
            await session.run_compaction()

    @pytest.mark.asyncio
    async def test_run_compaction_input_mode_without_response_id(self) -> None:
        mock_session = self.create_mock_session()
        items: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "hello"}),
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "world"},
            ),
        ]
        mock_session.get_items.return_value = items

        mock_compact_response = MagicMock()
        mock_compact_response.output = [
            {
                "type": "message",
                "role": "assistant",
                "content": "compacted",
            }
        ]

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
            compaction_mode="input",
        )

        await session.run_compaction({"force": True})

        mock_client.responses.compact.assert_called_once()
        call_kwargs = mock_client.responses.compact.call_args.kwargs
        assert call_kwargs.get("model") == "gpt-4.1"
        assert "previous_response_id" not in call_kwargs
        assert call_kwargs.get("input") == items

    @pytest.mark.asyncio
    async def test_run_compaction_auto_without_response_id_uses_input(self) -> None:
        mock_session = self.create_mock_session()
        items: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "hello"}),
        ]
        mock_session.get_items.return_value = items

        mock_compact_response = MagicMock()
        mock_compact_response.output = []

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
        )

        await session.run_compaction({"force": True})

        mock_client.responses.compact.assert_called_once()
        call_kwargs = mock_client.responses.compact.call_args.kwargs
        assert "previous_response_id" not in call_kwargs
        assert call_kwargs.get("input") == items

    @pytest.mark.asyncio
    async def test_run_compaction_input_mode_strips_internal_tool_call_metadata(self) -> None:
        mock_session = self.create_mock_session()
        items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "lookup_account",
                    "arguments": "{}",
                    TOOL_CALL_SESSION_DESCRIPTION_KEY: "Lookup customer records.",
                    TOOL_CALL_SESSION_TITLE_KEY: "Lookup Account",
                },
            ),
            cast(
                TResponseInputItem,
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "output": "ok",
                },
            ),
        ]
        mock_session.get_items.return_value = items

        mock_compact_response = MagicMock()
        mock_compact_response.output = []

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
            compaction_mode="input",
        )

        await session.run_compaction({"force": True})

        call_kwargs = mock_client.responses.compact.call_args.kwargs
        compact_input = cast(list[dict[str, Any]], call_kwargs["input"])
        assert compact_input[0]["type"] == "function_call"
        assert TOOL_CALL_SESSION_DESCRIPTION_KEY not in compact_input[0]
        assert TOOL_CALL_SESSION_TITLE_KEY not in compact_input[0]

    @pytest.mark.asyncio
    async def test_run_compaction_uses_sanitized_cached_items_after_add(self) -> None:
        mock_session = self.create_mock_session()
        mock_session.get_items.return_value = []

        mock_compact_response = MagicMock()
        mock_compact_response.output = []

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
            compaction_mode="input",
        )

        await session._ensure_compaction_candidates()
        await session.add_items(
            [
                cast(
                    TResponseInputItem,
                    {
                        "type": "function_call",
                        "call_id": "call_cached",
                        "name": "lookup_account",
                        "arguments": "{}",
                        TOOL_CALL_SESSION_DESCRIPTION_KEY: "Lookup customer records.",
                        TOOL_CALL_SESSION_TITLE_KEY: "Lookup Account",
                    },
                ),
                cast(
                    TResponseInputItem,
                    {
                        "type": "function_call_output",
                        "call_id": "call_cached",
                        "output": "ok",
                    },
                ),
            ]
        )

        await session.run_compaction({"force": True})

        call_kwargs = mock_client.responses.compact.call_args.kwargs
        compact_input = cast(list[dict[str, Any]], call_kwargs["input"])
        assert compact_input[0]["type"] == "function_call"
        assert TOOL_CALL_SESSION_DESCRIPTION_KEY not in compact_input[0]
        assert TOOL_CALL_SESSION_TITLE_KEY not in compact_input[0]

    @pytest.mark.asyncio
    async def test_run_compaction_auto_uses_input_when_store_false(self) -> None:
        mock_session = self.create_mock_session()
        items: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "hello"}),
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "world"},
            ),
        ]
        mock_session.get_items.return_value = items

        mock_compact_response = MagicMock()
        mock_compact_response.output = []

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
            compaction_mode="auto",
        )

        await session.run_compaction({"response_id": "resp-auto", "store": False, "force": True})

        mock_client.responses.compact.assert_called_once()
        call_kwargs = mock_client.responses.compact.call_args.kwargs
        assert call_kwargs.get("model") == "gpt-4.1"
        assert "previous_response_id" not in call_kwargs
        assert call_kwargs.get("input") == items

    @pytest.mark.asyncio
    async def test_run_compaction_auto_uses_default_store_when_unset(self) -> None:
        mock_session = self.create_mock_session()
        items: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "hello"}),
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "world"},
            ),
        ]
        mock_session.get_items.return_value = items

        mock_compact_response = MagicMock()
        mock_compact_response.output = []

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
            compaction_mode="auto",
        )

        await session.run_compaction({"response_id": "resp-auto", "store": False, "force": True})
        await session.run_compaction({"response_id": "resp-stored", "force": True})

        assert mock_client.responses.compact.call_count == 2
        first_kwargs = mock_client.responses.compact.call_args_list[0].kwargs
        second_kwargs = mock_client.responses.compact.call_args_list[1].kwargs
        assert "previous_response_id" not in first_kwargs
        assert second_kwargs.get("previous_response_id") == "resp-stored"
        assert "input" not in second_kwargs

    @pytest.mark.asyncio
    async def test_run_compaction_auto_uses_input_when_last_response_unstored(self) -> None:
        items: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "hello"}),
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "world"},
            ),
        ]
        underlying = SimpleListSession(history=items)

        mock_compact_response = MagicMock()
        mock_compact_response.output = [
            {
                "type": "message",
                "role": "assistant",
                "content": "compacted",
            }
        ]

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="auto",
        )

        await session.run_compaction(
            {"response_id": "resp-unstored", "store": False, "force": True}
        )
        await session.run_compaction({"force": True})

        assert mock_client.responses.compact.call_count == 2
        first_kwargs = mock_client.responses.compact.call_args_list[0].kwargs
        second_kwargs = mock_client.responses.compact.call_args_list[1].kwargs
        assert "previous_response_id" not in first_kwargs
        assert "previous_response_id" not in second_kwargs
        assert second_kwargs.get("input") == mock_compact_response.output

    @pytest.mark.asyncio
    async def test_run_compaction_skips_when_below_threshold(self) -> None:
        mock_session = self.create_mock_session()
        # Return fewer than threshold items
        mock_session.get_items.return_value = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": f"msg{i}"})
            for i in range(DEFAULT_COMPACTION_THRESHOLD - 1)
        ]

        mock_client = MagicMock()
        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
        )

        await session.run_compaction({"response_id": "resp-123"})

        # Should not have called the compact API
        mock_client.responses.compact.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_compaction_executes_when_threshold_met(self) -> None:
        mock_session = self.create_mock_session()
        # Return exactly threshold items (all assistant messages = candidates)
        mock_session.get_items.return_value = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": f"msg{i}"})
            for i in range(DEFAULT_COMPACTION_THRESHOLD)
        ]

        mock_compact_response = MagicMock()
        mock_compact_response.output = [{"type": "compaction", "summary": "compacted"}]

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
            model="gpt-4.1",
        )

        await session.run_compaction({"response_id": "resp-123"})

        mock_client.responses.compact.assert_called_once_with(
            previous_response_id="resp-123",
            model="gpt-4.1",
        )
        mock_session.clear_session.assert_called_once()
        mock_session.add_items.assert_called()

    @pytest.mark.asyncio
    async def test_run_compaction_restores_history_when_replacement_add_fails(self) -> None:
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "original"}),
            cast(
                TResponseInputItem,
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "lookup",
                    "arguments": "{}",
                    TOOL_CALL_SESSION_DESCRIPTION_KEY: "Lookup private records.",
                },
            ),
        ]
        compacted_items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "compacted"},
            )
        ]

        class PartiallyFailingReplacementSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.add_calls = 0
                self.clear_calls = 0

            async def add_items(self, items: list[TResponseInputItem]) -> None:
                self.add_calls += 1
                if self.add_calls == 1:
                    await super().add_items(items[:1])
                    raise RuntimeError("replacement failed")
                await super().add_items(items)

            async def clear_session(self) -> None:
                self.clear_calls += 1
                await super().clear_session()

        failing_session = PartiallyFailingReplacementSession(history=history)

        mock_compact_response = MagicMock()
        mock_compact_response.output = compacted_items

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=failing_session,
            client=mock_client,
            compaction_mode="input",
        )

        with pytest.raises(RuntimeError, match="replacement failed"):
            await session.run_compaction({"force": True})

        assert await failing_session.get_items() == history
        assert failing_session.clear_calls == 2
        assert failing_session.add_calls == 2

    @pytest.mark.asyncio
    async def test_run_compaction_restores_full_history_when_session_limit_applies(
        self,
    ) -> None:
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "oldest"}),
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": "middle"}),
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "newest"}),
        ]
        compacted_items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "compacted"},
            )
        ]

        class LimitedFailingReplacementSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.session_settings = SessionSettings(limit=1)
                self.add_calls = 0
                self.clear_calls = 0

            async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
                if limit is None and self.session_settings is not None:
                    limit = self.session_settings.limit
                return await super().get_items(limit)

            async def add_items(self, items: list[TResponseInputItem]) -> None:
                self.add_calls += 1
                if self.add_calls == 1:
                    await super().add_items(items[:1])
                    raise RuntimeError("replacement failed")
                await super().add_items(items)

            async def clear_session(self) -> None:
                self.clear_calls += 1
                await super().clear_session()

        failing_session = LimitedFailingReplacementSession(history=history)

        mock_compact_response = MagicMock()
        mock_compact_response.output = compacted_items

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=failing_session,
            client=mock_client,
            compaction_mode="input",
        )

        with pytest.raises(RuntimeError, match="replacement failed"):
            await session.run_compaction({"force": True})

        assert await failing_session.get_items(limit=10) == history
        assert failing_session.clear_calls == 2
        assert failing_session.add_calls == 2

    @pytest.mark.asyncio
    async def test_run_compaction_compacts_full_history_when_session_limit_applies(
        self,
    ) -> None:
        """Compaction must summarize the full stored history, not the limited read window."""
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": f"item-{i}"})
            for i in range(6)
        ]
        compacted_items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "compacted"},
            )
        ]

        class LimitedSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.session_settings = SessionSettings(limit=2)

            async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
                if limit is None and self.session_settings is not None:
                    limit = self.session_settings.limit
                return await super().get_items(limit)

        underlying = LimitedSession(history=history)

        mock_compact_response = MagicMock()
        mock_compact_response.output = compacted_items

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="input",
        )

        await session.run_compaction({"force": True})

        mock_client.responses.compact.assert_called_once()
        call_kwargs = mock_client.responses.compact.call_args.kwargs
        assert call_kwargs.get("input") == history
        assert await underlying.get_items(limit=10) == compacted_items

    @pytest.mark.asyncio
    async def test_decision_hook_counts_full_history_when_session_limit_applies(self) -> None:
        """The decision hook counts candidate items, so a backend limit must not hide them."""
        history: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": f"item-{i}"},
            )
            for i in range(DEFAULT_COMPACTION_THRESHOLD + 2)
        ]

        class LimitedSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.session_settings = SessionSettings(limit=2)

            async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
                if limit is None and self.session_settings is not None:
                    limit = self.session_settings.limit
                return await super().get_items(limit)

        underlying = LimitedSession(history=history)
        observed: dict[str, int] = {}

        def should_trigger_compaction(context: dict[str, Any]) -> bool:
            observed["candidates"] = len(context["compaction_candidate_items"])
            observed["session_items"] = len(context["session_items"])
            return False

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=underlying,
            client=MagicMock(),
            compaction_mode="input",
            should_trigger_compaction=should_trigger_compaction,
        )

        await session.run_compaction()

        assert observed["session_items"] == len(history)
        assert observed["candidates"] >= DEFAULT_COMPACTION_THRESHOLD

    @pytest.mark.asyncio
    async def test_run_compaction_auto_falls_back_to_input_when_history_truncated(
        self,
    ) -> None:
        history = self.assistant_history(DEFAULT_COMPACTION_THRESHOLD + 2)

        class LimitedSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.session_settings = SessionSettings(limit=3)

            async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
                if limit is None and self.session_settings is not None:
                    limit = self.session_settings.limit
                return await super().get_items(limit)

        underlying = LimitedSession(history=history)
        mock_compact_response = MagicMock()
        mock_compact_response.output = [
            {"type": "message", "role": "assistant", "content": "summary"}
        ]
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)
        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="auto",
        )

        await session.run_compaction({"response_id": "resp-limited", "store": True})

        mock_client.responses.compact.assert_called_once()
        compact_kwargs = mock_client.responses.compact.call_args.kwargs
        assert "previous_response_id" not in compact_kwargs
        assert compact_kwargs["input"] == history

    def test_history_limit_ignores_an_unrelated_delegate_attribute(self) -> None:
        """Only the compaction wrapper delegates its window; a lookalike attribute must not."""

        class Delegate(SimpleListSession):
            def __init__(self) -> None:
                super().__init__(history=[])
                self.session_settings = SessionSettings(limit=3)

        class LookalikeSession(SimpleListSession):
            def __init__(self) -> None:
                super().__init__(history=[])
                self.underlying_session = Delegate()

        assert resolve_session_history_limit(LookalikeSession(), None) is None

        wrapper = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=Delegate(),
            client=MagicMock(),
        )
        assert resolve_session_history_limit(wrapper, None) == 3

        wrapper.session_settings = SessionSettings(limit=7)
        assert resolve_session_history_limit(wrapper, None) == 7
        wrapper.underlying_session.session_settings = SessionSettings(limit=11)
        assert resolve_session_history_limit(wrapper, None) == 11

    @pytest.mark.asyncio
    async def test_a_read_that_exactly_fills_the_window_is_not_treated_as_covered(self) -> None:
        """Compaction falls back to local input when the window is exactly full."""
        history = self.assistant_history(3)
        underlying = SimpleListSession(history=history)
        underlying.session_settings = SessionSettings(limit=len(history))
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(
            return_value=SimpleNamespace(output=[{"type": "compaction", "summary": "s"}])
        )
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
        )

        await session.run_compaction({"response_id": "resp-exact", "force": True})

        compact_kwargs = mock_client.responses.compact.call_args.kwargs
        assert "previous_response_id" not in compact_kwargs
        assert "input" in compact_kwargs

    @pytest.mark.asyncio
    async def test_unknown_coverage_is_never_published_as_covered(self) -> None:
        """An attempt that never proved coverage must not let a later call reuse the id."""
        underlying = SimpleListSession(history=self.assistant_history(12))
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(
            return_value=SimpleNamespace(output=[{"type": "compaction", "summary": "s"}])
        )
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="input",
        )

        # Explicit input mode never resolves coverage, so it stays unknown.
        await session.run_compaction({"response_id": "resp-unknown", "force": True})

        context = session._last_processed_response_context
        assert context is not None
        assert context["input_covered_full_history"] is False

    @pytest.mark.asyncio
    async def test_compaction_honors_underlying_session_limit(self) -> None:
        history = self.assistant_history(DEFAULT_COMPACTION_THRESHOLD + 2)

        class LimitedSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.session_settings = SessionSettings(limit=3)

            async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
                if limit is None and self.session_settings is not None:
                    limit = self.session_settings.limit
                return await super().get_items(limit)

        underlying = LimitedSession(history=history)
        mock_compact_response = MagicMock()
        mock_compact_response.output = [
            {"type": "message", "role": "assistant", "content": "summary"}
        ]
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)
        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=lambda _context: True,
        )
        agent = Agent(name="assistant", model=FakeModel())

        await save_result_to_session(
            session,
            [],
            self.message_run_items(agent),
            None,
            response_id="resp-underlying-limited",
            store=True,
        )

        mock_client.responses.compact.assert_called_once()
        compact_kwargs = mock_client.responses.compact.call_args.kwargs
        assert "previous_response_id" not in compact_kwargs
        assert len(compact_kwargs["input"]) >= len(history)

    @pytest.mark.asyncio
    async def test_run_compaction_keeps_response_id_attempt_local(self) -> None:
        history = self.assistant_history(DEFAULT_COMPACTION_THRESHOLD + 2)
        underlying = SimpleListSession(history=history)
        mock_compact_response = MagicMock()
        mock_compact_response.output = [
            {"type": "message", "role": "assistant", "content": "summary"}
        ]
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)
        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=underlying,
            client=mock_client,
        )

        first_call_parked = asyncio.Event()
        release_first_call = asyncio.Event()
        real_ensure = session._ensure_compaction_candidates
        gate = {"parked": False}

        async def gated_ensure() -> Any:
            if not gate["parked"]:
                gate["parked"] = True
                first_call_parked.set()
                await release_first_call.wait()
            return await real_ensure()

        session._ensure_compaction_candidates = gated_ensure  # type: ignore[method-assign]

        first_task = asyncio.create_task(
            session.run_compaction(
                {
                    "response_id": "resp-covered",
                    "force": True,
                    "input_covered_full_history": True,
                }
            )
        )
        await first_call_parked.wait()

        await session.run_compaction({"force": True})
        release_first_call.set()
        await first_task

        calls = mock_client.responses.compact.call_args_list
        input_calls = [call for call in calls if "input" in call.kwargs]
        previous_ids = [
            call.kwargs["previous_response_id"]
            for call in calls
            if "previous_response_id" in call.kwargs
        ]
        assert len(input_calls) == 1
        assert len(input_calls[0].kwargs["input"]) == len(history)
        assert previous_ids == ["resp-covered"]

    @pytest.mark.asyncio
    async def test_compaction_reload_candidates_after_interleaved_add_during_replace(
        self,
    ) -> None:
        history = self.assistant_history(DEFAULT_COMPACTION_THRESHOLD + 2)
        interleaved_item = cast(
            TResponseInputItem,
            {"type": "message", "role": "assistant", "content": "B-NEW"},
        )
        underlying = SimpleListSession(history=history)
        compacted = [
            SimpleNamespace(
                output=[{"type": "message", "role": "assistant", "content": "summary-1"}]
            ),
            SimpleNamespace(
                output=[{"type": "message", "role": "assistant", "content": "summary-2"}]
            ),
        ]
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(side_effect=compacted)

        class InterleavingCompactionSession(OpenAIResponsesCompactionSession):
            async def _replace_underlying_session_items(
                self,
                *,
                output_items: list[TResponseInputItem],
                previous_items: list[TResponseInputItem],
            ) -> None:
                await super()._replace_underlying_session_items(
                    output_items=output_items,
                    previous_items=previous_items,
                )
                await self.underlying_session.add_items([interleaved_item])

        session = InterleavingCompactionSession(
            session_id="test",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="input",
        )

        await session.run_compaction({"force": True})
        await session.run_compaction({"force": True})

        second_input = mock_client.responses.compact.call_args_list[1].kwargs["input"]
        assert interleaved_item in second_input

    @pytest.mark.asyncio
    async def test_run_compaction_does_not_restore_when_clear_fails_without_mutation(
        self,
    ) -> None:
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "original"}),
        ]
        compacted_items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "compacted"},
            )
        ]

        class FailingClearBeforeMutationSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.add_calls = 0
                self.clear_calls = 0

            async def add_items(self, items: list[TResponseInputItem]) -> None:
                self.add_calls += 1
                await super().add_items(items)

            async def clear_session(self) -> None:
                self.clear_calls += 1
                raise RuntimeError("clear failed")

        failing_session = FailingClearBeforeMutationSession(history=history)

        mock_compact_response = MagicMock()
        mock_compact_response.output = compacted_items

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=failing_session,
            client=mock_client,
            compaction_mode="input",
        )

        with pytest.raises(RuntimeError, match="clear failed"):
            await session.run_compaction({"force": True})

        assert await failing_session.get_items() == history
        assert failing_session.clear_calls == 1
        assert failing_session.add_calls == 0

    @pytest.mark.asyncio
    async def test_run_compaction_restores_history_when_clear_fails_after_mutation(
        self,
    ) -> None:
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "original"}),
        ]
        compacted_items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "compacted"},
            )
        ]

        class PartiallyFailingClearSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.add_calls = 0
                self.clear_calls = 0

            async def add_items(self, items: list[TResponseInputItem]) -> None:
                self.add_calls += 1
                await super().add_items(items)

            async def clear_session(self) -> None:
                self.clear_calls += 1
                await super().clear_session()
                raise RuntimeError("clear failed")

        failing_session = PartiallyFailingClearSession(history=history)

        mock_compact_response = MagicMock()
        mock_compact_response.output = compacted_items

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=failing_session,
            client=mock_client,
            compaction_mode="input",
        )

        with pytest.raises(RuntimeError, match="clear failed"):
            await session.run_compaction({"force": True})

        assert await failing_session.get_items() == history
        assert failing_session.clear_calls == 1
        assert failing_session.add_calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("redacted", [True, False])
    async def test_run_compaction_reraises_replacement_error_when_restore_fails(
        self, monkeypatch, caplog: pytest.LogCaptureFixture, redacted: bool
    ) -> None:
        monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", redacted)
        monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redacted)
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "original"}),
        ]
        compacted_items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "compacted"},
            )
        ]

        class FailingRestoreSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.add_calls = 0
                self.clear_calls = 0

            async def add_items(self, items: list[TResponseInputItem]) -> None:
                self.add_calls += 1
                if self.add_calls == 1:
                    await super().add_items(items[:1])
                    raise RuntimeError("replacement failed")
                raise RuntimeError("SECRET_COMPACTION_RESTORE_FAILURE")

            async def clear_session(self) -> None:
                self.clear_calls += 1
                await super().clear_session()

        failing_session = FailingRestoreSession(history=history)

        mock_compact_response = MagicMock()
        mock_compact_response.output = compacted_items

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=failing_session,
            client=mock_client,
            compaction_mode="input",
        )

        with caplog.at_level(logging.WARNING, logger="openai-agents.openai.compaction"):
            with pytest.raises(RuntimeError, match="replacement failed"):
                await session.run_compaction({"force": True})

        assert (
            "Failed to restore session history after compaction replacement failed." in caplog.text
        )
        assert ("SECRET_COMPACTION_RESTORE_FAILURE" not in caplog.text) is redacted
        assert failing_session.clear_calls == 2
        assert failing_session.add_calls == 2

    @pytest.mark.asyncio
    async def test_run_compaction_force_bypasses_threshold(self) -> None:
        mock_session = self.create_mock_session()
        mock_session.get_items.return_value = []

        mock_compact_response = MagicMock()
        mock_compact_response.output = []

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
        )

        await session.run_compaction({"response_id": "resp-123", "force": True})

        mock_client.responses.compact.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_compaction_suppresses_model_dump_warnings(self) -> None:
        mock_session = self.create_mock_session()
        mock_session.get_items.return_value = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": "hi"})
            for _ in range(DEFAULT_COMPACTION_THRESHOLD)
        ]

        class WarningModel:
            def __init__(self) -> None:
                self.received_warnings_arg: bool | None = None

            def model_dump(
                self, *, exclude_unset: bool, warnings: bool | None = None
            ) -> dict[str, Any]:
                self.received_warnings_arg = warnings
                if warnings:
                    warnings_module.warn("unexpected warning", stacklevel=2)
                return {"type": "message", "role": "assistant", "content": "ok"}

        warning_model = WarningModel()
        mock_compact_response = MagicMock()
        mock_compact_response.output = [warning_model]

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
        )

        with warnings_module.catch_warnings():
            warnings_module.simplefilter("error")
            await session.run_compaction({"response_id": "resp-123"})

        assert warning_model.received_warnings_arg is False
        mock_client.responses.compact.assert_called_once_with(
            previous_response_id="resp-123",
            model="gpt-4.1",
        )

    @pytest.mark.asyncio
    async def test_run_compaction_normalizes_compacted_user_image_messages(self) -> None:
        mock_session = self.create_mock_session()
        mock_session.get_items.return_value = []

        mock_compact_response = MagicMock()
        mock_compact_response.output = [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "analyze this input"},
                    {
                        "type": "input_image",
                        "image_url": "https://example.com/image.png",
                        "file_id": None,
                        "detail": "auto",
                    },
                ],
            }
        ]

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
            compaction_mode="input",
        )

        await session.run_compaction({"force": True, "compaction_mode": "input"})

        stored_items = mock_session.add_items.call_args[0][0]
        assert stored_items == [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "analyze this input"},
                    {
                        "type": "input_image",
                        "image_url": "https://example.com/image.png",
                        "detail": "auto",
                    },
                ],
            }
        ]

    @pytest.mark.asyncio
    async def test_run_compaction_normalizes_compacted_user_file_messages(self) -> None:
        mock_session = self.create_mock_session()
        mock_session.get_items.return_value = []

        mock_compact_response = MagicMock()
        mock_compact_response.output = [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "analyze this input"},
                    {
                        "type": "input_file",
                        "file_url": "https://example.com/report.pdf",
                        "file_id": None,
                        "filename": "report.pdf",
                        "detail": "high",
                    },
                ],
            }
        ]

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
            compaction_mode="input",
        )

        await session.run_compaction({"force": True, "compaction_mode": "input"})

        stored_items = mock_session.add_items.call_args[0][0]
        assert stored_items == [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "analyze this input"},
                    {
                        "type": "input_file",
                        "file_url": "https://example.com/report.pdf",
                        "filename": "report.pdf",
                        "detail": "high",
                    },
                ],
            }
        ]

    @pytest.mark.asyncio
    async def test_run_compaction_normalizes_file_id_inputs_and_preserves_metadata(self) -> None:
        mock_session = self.create_mock_session()
        mock_session.get_items.return_value = []

        mock_compact_response = MagicMock()
        mock_compact_response.output = [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "analyze this input"},
                    {
                        "type": "input_file",
                        "file_id": "file_123",
                        "file_url": None,
                        "filename": "report.pdf",
                        "detail": "low",
                    },
                ],
            }
        ]

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
            compaction_mode="input",
        )

        await session.run_compaction({"force": True, "compaction_mode": "input"})

        stored_items = mock_session.add_items.call_args[0][0]
        assert stored_items == [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "analyze this input"},
                    {
                        "type": "input_file",
                        "file_id": "file_123",
                        "filename": "report.pdf",
                        "detail": "low",
                    },
                ],
            }
        ]

    @pytest.mark.asyncio
    async def test_run_compaction_preserves_history_when_output_normalization_fails(self) -> None:
        history = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "world"}],
            },
        ]
        underlying = SimpleListSession(history=cast(list[TResponseInputItem], history))

        mock_compact_response = MagicMock()
        mock_compact_response.output = [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "hello"},
                    {"type": "input_image", "detail": "auto"},
                ],
            }
        ]

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="input",
        )

        with pytest.raises(
            ValueError, match="Compaction input_image item missing image_url or file_id."
        ):
            await session.run_compaction({"force": True, "compaction_mode": "input"})

        assert await session.get_items() == history

    @pytest.mark.asyncio
    async def test_compaction_runs_during_runner_flow(self) -> None:
        """Ensure Runner triggers compaction when using a compaction-aware session."""
        underlying = SimpleListSession()
        compacted = SimpleNamespace(
            output=[{"type": "compaction", "encrypted_content": "enc"}],
        )
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=compacted)

        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=lambda ctx: True,
        )

        model = FakeModel(initial_output=[get_text_message("ok")])
        agent = Agent(name="assistant", model=model)

        await Runner.run(agent, "hello", session=session)

        mock_client.responses.compact.assert_awaited_once()
        items = await session.get_items()
        assert any(isinstance(item, dict) and item.get("type") == "compaction" for item in items)

    @pytest.mark.asyncio
    async def test_runner_compaction_reuses_previous_response_id_when_pre_save_history_fits(
        self,
    ) -> None:
        underlying = SimpleListSession(history=self.assistant_history(5))
        compacted = SimpleNamespace(
            output=[{"type": "compaction", "encrypted_content": "enc"}],
        )
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=compacted)
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=lambda _ctx: True,
        )
        model = FakeModel(initial_output=[get_text_message("ok")])
        agent = Agent(name="assistant", model=model)

        await Runner.run(
            agent,
            "hello",
            session=session,
            run_config=RunConfig(session_settings=SessionSettings(limit=6)),
        )

        mock_client.responses.compact.assert_awaited_once()
        compact_kwargs = mock_client.responses.compact.call_args.kwargs
        assert compact_kwargs.get("previous_response_id") == "resp-789"
        assert "input" not in compact_kwargs

    @pytest.mark.asyncio
    async def test_runner_compaction_callback_filtered_history_falls_back_to_input(
        self,
    ) -> None:
        history = self.assistant_history(5)
        underlying = SimpleListSession(history=history)
        compacted = SimpleNamespace(
            output=[{"type": "compaction", "encrypted_content": "enc"}],
        )
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=compacted)
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=lambda _ctx: True,
        )
        model = FakeModel(initial_output=[get_text_message("ok")])
        agent = Agent(name="assistant", model=model)

        await Runner.run(
            agent,
            "hello",
            session=session,
            run_config=RunConfig(session_input_callback=lambda _history, new_items: new_items),
        )

        mock_client.responses.compact.assert_awaited_once()
        compact_kwargs = mock_client.responses.compact.call_args.kwargs
        assert "previous_response_id" not in compact_kwargs
        assert len(compact_kwargs["input"]) >= len(history)

    @pytest.mark.asyncio
    async def test_runner_compaction_call_model_filter_falls_back_to_input(
        self,
    ) -> None:
        history = self.assistant_history(5)
        underlying = SimpleListSession(history=history)
        compacted = SimpleNamespace(
            output=[{"type": "compaction", "encrypted_content": "enc"}],
        )
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=compacted)
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=lambda _ctx: True,
        )
        model = FakeModel(initial_output=[get_text_message("ok")])
        agent = Agent(name="assistant", model=model)

        def drop_history(data: Any) -> Any:
            data.model_data.input = data.model_data.input[-1:]
            return data.model_data

        await Runner.run(
            agent,
            "hello",
            session=session,
            run_config=RunConfig(call_model_input_filter=drop_history),
        )

        mock_client.responses.compact.assert_awaited_once()
        compact_kwargs = mock_client.responses.compact.call_args.kwargs
        assert "previous_response_id" not in compact_kwargs
        assert len(compact_kwargs["input"]) >= len(history)

    @pytest.mark.asyncio
    async def test_runner_compaction_underlying_limit_falls_back_to_input(
        self,
    ) -> None:
        history = self.assistant_history(DEFAULT_COMPACTION_THRESHOLD + 2)

        class LimitedSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.session_settings = SessionSettings(limit=3)

            async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
                if limit is None and self.session_settings is not None:
                    limit = self.session_settings.limit
                return await super().get_items(limit)

        underlying = LimitedSession(history=history)
        compacted = SimpleNamespace(
            output=[{"type": "compaction", "encrypted_content": "enc"}],
        )
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=compacted)
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=lambda _ctx: True,
        )
        model = FakeModel(initial_output=[get_text_message("ok")])
        agent = Agent(name="assistant", model=model)

        await Runner.run(agent, "hello", session=session)

        mock_client.responses.compact.assert_awaited_once()
        compact_kwargs = mock_client.responses.compact.call_args.kwargs
        assert "previous_response_id" not in compact_kwargs
        assert len(compact_kwargs["input"]) >= len(history)

    @pytest.mark.asyncio
    async def test_runner_does_not_probe_non_compaction_session_above_limit(self) -> None:
        class StrictLimitSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.requested_limits: list[int | None] = []

            async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
                self.requested_limits.append(limit)
                if limit is not None and limit > 100:
                    raise RuntimeError(f"limit too high: {limit}")
                return await super().get_items(limit)

        session = StrictLimitSession(history=self.assistant_history(5))
        model = FakeModel(initial_output=[get_text_message("ok")])
        agent = Agent(name="assistant", model=model)

        await Runner.run(
            agent,
            "hello",
            session=session,
            run_config=RunConfig(session_settings=SessionSettings(limit=100)),
        )

        assert 101 not in session.requested_limits

    @pytest.mark.asyncio
    async def test_runner_resumed_compaction_falls_back_to_input(self) -> None:
        async def approval_tool() -> str:
            return "tool_result"

        tool = function_tool(approval_tool, name_override="approval_tool", needs_approval=True)
        underlying = SimpleListSession(history=self.assistant_history(5))
        compacted = SimpleNamespace(
            output=[{"type": "compaction", "encrypted_content": "enc"}],
        )
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=compacted)
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=lambda _ctx: True,
        )
        model = FakeModel()
        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("approval_tool", "{}", call_id="call-resume")],
                [get_text_message("done")],
            ]
        )
        agent = Agent(name="assistant", model=model, tools=[tool])

        first = await Runner.run(agent, "hello", session=session)
        assert first.interruptions
        state = first.to_state()
        state.approve(first.interruptions[0])

        resumed = await Runner.run(agent, state, session=session)

        assert resumed.final_output == "done"
        assert mock_client.responses.compact.call_count >= 1
        compact_kwargs = mock_client.responses.compact.call_args.kwargs
        assert "previous_response_id" not in compact_kwargs
        assert "input" in compact_kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "call_type",
        ["function_call", "custom_tool_call", "shell_call", "apply_patch_call"],
    )
    async def test_auto_input_fallback_defers_with_pending_tool_call(self, call_type: str) -> None:
        """Do not replace history while an approval call is waiting for its output."""
        pending_call = cast(
            TResponseInputItem,
            {
                "type": call_type,
                "call_id": "call-pending",
            },
        )
        underlying = SimpleListSession(
            history=self.assistant_history(3) + [pending_call],
        )
        underlying.session_settings = SessionSettings(limit=4)
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(
            return_value=SimpleNamespace(output=[{"type": "compaction", "summary": "s"}])
        )
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=lambda _ctx: True,
        )

        await session.run_compaction({"response_id": "resp-pending"})

        mock_client.responses.compact.assert_not_called()
        assert await underlying.get_items() == self.assistant_history(3) + [pending_call]

    @pytest.mark.asyncio
    async def test_resumed_compaction_falls_back_on_a_fresh_wrapper(self) -> None:
        """A wrapper built only for the resumed run has no memory of the original window."""
        history = self.assistant_history(DEFAULT_COMPACTION_THRESHOLD + 2)

        def build_session(underlying: SimpleListSession, client: MagicMock) -> Any:
            return OpenAIResponsesCompactionSession(
                session_id="demo",
                underlying_session=underlying,
                client=client,
                should_trigger_compaction=lambda _ctx: True,
            )

        underlying = SimpleListSession(history=history)
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(
            return_value=SimpleNamespace(output=[{"type": "compaction", "summary": "s"}])
        )

        async def approval_tool() -> str:
            return "tool_result"

        tool = function_tool(approval_tool, name_override="approval_tool", needs_approval=True)
        model = FakeModel()
        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("approval_tool", "{}", call_id="call-fresh")],
                [get_text_message("done")],
            ]
        )
        agent = Agent(name="assistant", model=model, tools=[tool])

        first = await Runner.run(agent, "hello", session=build_session(underlying, mock_client))
        assert first.interruptions
        state = first.to_state()
        state.approve(first.interruptions[0])

        # The resumed run gets a wrapper that never prepared input for the original response.
        resumed = await Runner.run(agent, state, session=build_session(underlying, mock_client))

        assert resumed.final_output == "done"
        compact_kwargs = mock_client.responses.compact.call_args.kwargs
        assert "previous_response_id" not in compact_kwargs
        assert "input" in compact_kwargs

    @pytest.mark.asyncio
    async def test_manual_compaction_reuses_the_last_processed_response_id(self) -> None:
        """A manual call reuses the last id the session saw, even when the hook declined it."""
        underlying = SimpleListSession(history=self.assistant_history(12))
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(
            return_value=SimpleNamespace(output=[{"type": "compaction", "summary": "s"}])
        )
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
            should_trigger_compaction=lambda _ctx: False,
        )

        await session.run_compaction({"response_id": "resp-first"})
        mock_client.responses.compact.assert_not_called()

        await session.run_compaction({"force": True})

        assert mock_client.responses.compact.call_args.kwargs["previous_response_id"] == (
            "resp-first"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("explicit_response_id", [False, True])
    async def test_manual_compaction_reuses_the_last_processed_response_context(
        self,
        explicit_response_id: bool,
    ) -> None:
        """A reused response id keeps the replay context from its completed attempt."""
        stale = cast(TResponseInputItem, {"type": "reasoning", "id": "rs_stale", "summary": []})
        history = [stale, *self.assistant_history(11)]
        underlying = SimpleListSession(history=history)
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(
            return_value=SimpleNamespace(output=[{"type": "compaction", "summary": "s"}])
        )
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=lambda _ctx: False,
        )

        await session.run_compaction(
            {
                "response_id": "resp-filtered",
                "input_covered_full_history": False,
                "reasoning_item_id_policy": "omit",
            }
        )
        mock_client.responses.compact.assert_not_called()

        if explicit_response_id:
            await session.run_compaction({"response_id": "resp-filtered", "force": True})
        else:
            await session.run_compaction({"force": True})

        compact_kwargs = mock_client.responses.compact.call_args.kwargs
        assert "previous_response_id" not in compact_kwargs
        reasoning = [item for item in compact_kwargs["input"] if item.get("type") == "reasoning"]
        assert reasoning == [{"type": "reasoning", "summary": []}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("explicit_response_id", [False, True])
    async def test_manual_compaction_reuses_inferred_history_coverage(
        self,
        explicit_response_id: bool,
    ) -> None:
        """A session change cannot reverse the coverage resolved for a processed response."""

        class LimitedSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.session_settings = SessionSettings(limit=2)

            async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
                if limit is None and self.session_settings is not None:
                    limit = self.session_settings.limit
                return await super().get_items(limit)

        underlying = LimitedSession(history=self.assistant_history(12))
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(
            return_value=SimpleNamespace(output=[{"type": "compaction", "summary": "s"}])
        )
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=lambda _ctx: False,
        )

        await session.run_compaction({"response_id": "resp-limited"})
        mock_client.responses.compact.assert_not_called()

        underlying.session_settings = SessionSettings()
        if explicit_response_id:
            await session.run_compaction({"response_id": "resp-limited", "force": True})
        else:
            await session.run_compaction({"force": True})

        compact_kwargs = mock_client.responses.compact.call_args.kwargs
        assert "previous_response_id" not in compact_kwargs
        assert "input" in compact_kwargs

    @pytest.mark.asyncio
    async def test_manual_compaction_ignores_an_in_flight_response_id(self) -> None:
        """An id is only reusable once its own attempt finished, not while it is compacting."""
        underlying = SimpleListSession(history=self.assistant_history(12))
        compacting = asyncio.Event()
        release = asyncio.Event()

        seen: list[dict[str, Any]] = []

        async def parked_compact(**kwargs: Any) -> Any:
            seen.append(kwargs)
            if len(seen) == 1:
                compacting.set()
                await release.wait()
            return SimpleNamespace(output=[{"type": "compaction", "summary": "s"}])

        mock_client = MagicMock()
        mock_client.responses.compact = parked_compact
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        parked = asyncio.create_task(
            session.run_compaction({"response_id": "resp-inflight", "force": True})
        )
        try:
            await compacting.wait()
            with pytest.raises(ValueError, match="requires a response_id"):
                await asyncio.wait_for(session.run_compaction({"force": True}), timeout=1)
        finally:
            release.set()
            await parked

        # Once it finished, the same id becomes reusable by a manual call.
        await session.run_compaction({"force": True})
        assert seen[-1]["previous_response_id"] == "resp-inflight"

    @pytest.mark.asyncio
    async def test_a_failed_attempt_does_not_suppress_a_slower_successful_one(self) -> None:
        """A newer attempt failing must not stop an older one from publishing its id."""
        underlying = SimpleListSession(history=self.assistant_history(12))
        started = asyncio.Event()
        release = asyncio.Event()
        seen: list[dict[str, Any]] = []

        async def compact(**kwargs: Any) -> Any:
            seen.append(kwargs)
            if len(seen) == 1:
                started.set()
                await release.wait()
            elif len(seen) == 2:
                raise RuntimeError("compact failed")
            return SimpleNamespace(output=[{"type": "compaction", "summary": "s"}])

        mock_client = MagicMock()
        mock_client.responses.compact = compact
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        slow = asyncio.create_task(session.run_compaction({"response_id": "slow", "force": True}))
        try:
            await started.wait()
            with pytest.raises(RuntimeError, match="compact failed"):
                await session.run_compaction({"response_id": "fast", "force": True})
        finally:
            release.set()
            await slow

        await session.run_compaction({"force": True})
        assert seen[-1]["previous_response_id"] == "slow"

    @pytest.mark.asyncio
    async def test_input_fallback_applies_the_reasoning_item_id_policy(self) -> None:
        """Runner compaction must not reintroduce ids the model request omitted."""
        stale = cast(TResponseInputItem, {"type": "reasoning", "id": "rs_stale", "summary": []})
        history = [stale, *self.assistant_history(11)]
        underlying = SimpleListSession(history=history)
        underlying.session_settings = SessionSettings(limit=2)
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(
            return_value=SimpleNamespace(output=[{"type": "compaction", "summary": "s"}])
        )

        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
        )
        model = FakeModel(initial_output=[get_text_message("ok")])
        agent = Agent(name="assistant", model=model)

        await Runner.run(
            agent,
            "hello",
            session=session,
            run_config=RunConfig(reasoning_item_id_policy="omit"),
        )

        mock_client.responses.compact.assert_awaited_once()
        sent = mock_client.responses.compact.call_args.kwargs["input"]
        reasoning = [item for item in sent if item.get("type") == "reasoning"]
        assert reasoning == [{"type": "reasoning", "summary": []}]

    @pytest.mark.asyncio
    async def test_run_compaction_validates_response_id_before_reading_history(self) -> None:
        """An unusable request must fail before the session is read."""
        underlying = SimpleListSession(history=self.assistant_history(4))
        reads: list[Any] = []
        real_get_items = underlying.get_items

        async def counting_get_items(limit: int | None = None) -> Any:
            reads.append(limit)
            return await real_get_items(limit)

        underlying.get_items = counting_get_items  # type: ignore[method-assign]
        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=MagicMock(),
            compaction_mode="previous_response_id",
        )

        with pytest.raises(ValueError, match="requires a response_id"):
            await session.run_compaction({"force": True})

        assert reads == []

    @pytest.mark.asyncio
    async def test_explicit_previous_response_id_keeps_the_id_under_a_limit(self) -> None:
        """The auto fallback must not change an explicitly requested compaction mode."""
        underlying = SimpleListSession(history=self.assistant_history(12))
        underlying.session_settings = SessionSettings(limit=2)
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(
            return_value=SimpleNamespace(output=[{"type": "compaction", "summary": "s"}])
        )

        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        await session.run_compaction({"response_id": "resp-explicit", "force": True})

        compact_kwargs = mock_client.responses.compact.call_args.kwargs
        assert compact_kwargs.get("previous_response_id") == "resp-explicit"
        assert "input" not in compact_kwargs

    @pytest.mark.asyncio
    async def test_compaction_skips_when_tool_outputs_present(self) -> None:
        underlying = SimpleListSession()
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock()

        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=lambda ctx: True,
        )

        tool = get_function_tool(name="do_thing", return_value="done")
        model = FakeModel(initial_output=[get_function_tool_call("do_thing")])
        agent = Agent(
            name="assistant",
            model=model,
            tools=[tool],
            tool_use_behavior="stop_on_first_tool",
        )

        await Runner.run(agent, "hello", session=session)

        mock_client.responses.compact.assert_not_called()

    @pytest.mark.asyncio
    async def test_deferred_compaction_includes_compaction_mode_in_context(self) -> None:
        underlying = SimpleListSession()
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock()
        observed = {}

        def should_trigger_compaction(context: dict[str, Any]) -> bool:
            observed["mode"] = context["compaction_mode"]
            return False

        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="input",
            should_trigger_compaction=should_trigger_compaction,
        )

        tool = get_function_tool(name="do_thing", return_value="done")
        model = FakeModel(initial_output=[get_function_tool_call("do_thing")])
        agent = Agent(
            name="assistant",
            model=model,
            tools=[tool],
            tool_use_behavior="stop_on_first_tool",
        )

        await Runner.run(agent, "hello", session=session)

        assert observed["mode"] == "input"
        mock_client.responses.compact.assert_not_called()

    @pytest.mark.asyncio
    async def test_compaction_runs_after_deferred_tool_outputs_when_due(self) -> None:
        underlying = SimpleListSession()
        compacted = SimpleNamespace(
            output=[{"type": "compaction", "summary": "compacted"}],
        )
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=compacted)

        def should_trigger_compaction(context: dict[str, Any]) -> bool:
            return any(
                isinstance(item, dict) and item.get("type") == "function_call_output"
                for item in context["session_items"]
            )

        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=should_trigger_compaction,
        )

        tool = get_function_tool(name="do_thing", return_value="done")
        model = FakeModel()
        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("do_thing")],
                [get_text_message("ok")],
            ]
        )
        agent = Agent(
            name="assistant",
            model=model,
            tools=[tool],
            tool_use_behavior="stop_on_first_tool",
        )

        await Runner.run(agent, "hello", session=session)
        await Runner.run(agent, "followup", session=session)

        mock_client.responses.compact.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deferred_compaction_persists_across_tool_turns(self) -> None:
        underlying = SimpleListSession()
        compacted = SimpleNamespace(
            output=[{"type": "compaction", "summary": "compacted"}],
        )
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=compacted)

        should_compact_calls = {"count": 0}

        def should_trigger_compaction(context: dict[str, Any]) -> bool:
            should_compact_calls["count"] += 1
            return should_compact_calls["count"] == 1

        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=should_trigger_compaction,
        )

        tool = get_function_tool(name="do_thing", return_value="done")
        model = FakeModel()
        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("do_thing")],
                [get_function_tool_call("do_thing")],
                [get_text_message("ok")],
            ]
        )
        agent = Agent(
            name="assistant",
            model=model,
            tools=[tool],
            tool_use_behavior="stop_on_first_tool",
        )

        await Runner.run(agent, "hello", session=session)
        await Runner.run(agent, "again", session=session)
        await Runner.run(agent, "final", session=session)

        mock_client.responses.compact.assert_awaited_once()


class TestStripOrphanedAssistantIds:
    def test_noop_when_empty(self) -> None:
        assert _strip_orphaned_assistant_ids([]) == []

    def test_strips_id_from_assistant_when_no_reasoning(self) -> None:
        items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "id": "msg_abc", "content": "hi"},
            ),
            cast(
                TResponseInputItem,
                {"type": "message", "role": "user", "content": "hello"},
            ),
        ]
        result = _strip_orphaned_assistant_ids(items)
        assert "id" not in result[0]
        # user message untouched
        assert result[1] == items[1]

    def test_preserves_id_when_reasoning_present(self) -> None:
        items: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "reasoning", "id": "rs_123", "content": "..."}),
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "id": "msg_abc", "content": "hi"},
            ),
        ]
        result = _strip_orphaned_assistant_ids(items)
        assert result[1].get("id") == "msg_abc"

    def test_preserves_assistant_without_id(self) -> None:
        items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "hi"},
            ),
        ]
        result = _strip_orphaned_assistant_ids(items)
        assert result == items

    def test_strips_multiple_assistant_ids(self) -> None:
        items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "id": "msg_1", "content": "a"},
            ),
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "id": "msg_2", "content": "b"},
            ),
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "id": "msg_3", "content": "c"},
            ),
        ]
        result = _strip_orphaned_assistant_ids(items)
        for item in result:
            assert "id" not in item


class TestCompactionStripsOrphanedIds:
    """Regression test for #2727: gpt-5.4 compact retains assistant msg IDs after
    stripping reasoning items, causing 400 errors on the next responses.create call."""

    def create_mock_session(self) -> MagicMock:
        mock = MagicMock(spec=Session)
        mock.session_id = "test-session"
        mock.get_items = AsyncMock(return_value=[])
        mock.add_items = AsyncMock()
        mock.pop_item = AsyncMock(return_value=None)
        mock.clear_session = AsyncMock()
        return mock

    @pytest.mark.asyncio
    async def test_run_compaction_strips_orphaned_assistant_ids(self) -> None:
        """Compacted output with assistant IDs but no reasoning items should
        have those IDs removed before being stored."""
        mock_session = self.create_mock_session()
        mock_session.get_items.return_value = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": f"m{i}"})
            for i in range(DEFAULT_COMPACTION_THRESHOLD)
        ]

        # Simulate gpt-5.4 compact output: assistant msgs WITH ids, NO reasoning items
        mock_compact_response = MagicMock()
        mock_compact_response.output = [
            {"type": "message", "role": "assistant", "id": "msg_aaa", "content": "summary 1"},
            {"type": "message", "role": "assistant", "id": "msg_bbb", "content": "summary 2"},
            {"type": "message", "role": "assistant", "id": "msg_ccc", "content": "summary 3"},
        ]

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
        )

        await session.run_compaction({"response_id": "resp-123"})

        # Verify stored items have no orphaned ids
        stored_items = mock_session.add_items.call_args[0][0]
        for item in stored_items:
            assert "id" not in item, f"orphaned id not stripped: {item}"

    @pytest.mark.asyncio
    async def test_run_compaction_keeps_ids_when_reasoning_present(self) -> None:
        """When compact output includes reasoning items, assistant IDs should be kept."""
        mock_session = self.create_mock_session()
        mock_session.get_items.return_value = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": f"m{i}"})
            for i in range(DEFAULT_COMPACTION_THRESHOLD)
        ]

        mock_compact_response = MagicMock()
        mock_compact_response.output = [
            {"type": "reasoning", "id": "rs_111", "content": "thinking..."},
            {"type": "message", "role": "assistant", "id": "msg_aaa", "content": "answer"},
        ]

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_session,
            client=mock_client,
        )

        await session.run_compaction({"response_id": "resp-123"})

        stored_items = mock_session.add_items.call_args[0][0]
        assistant_items = [i for i in stored_items if i.get("role") == "assistant"]
        assert assistant_items[0]["id"] == "msg_aaa"


class TestTypeGuard:
    def test_is_compaction_aware_session_true(self) -> None:
        mock_underlying = MagicMock(spec=Session)
        mock_underlying.session_id = "test"
        mock_underlying.get_items = AsyncMock(return_value=[])
        mock_underlying.add_items = AsyncMock()
        mock_underlying.pop_item = AsyncMock(return_value=None)
        mock_underlying.clear_session = AsyncMock()

        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=mock_underlying,
        )
        assert is_openai_responses_compaction_aware_session(session) is True

    def test_is_compaction_aware_session_false(self) -> None:
        mock_session = MagicMock(spec=Session)
        assert is_openai_responses_compaction_aware_session(mock_session) is False

    def test_is_compaction_aware_session_none(self) -> None:
        assert is_openai_responses_compaction_aware_session(None) is False
