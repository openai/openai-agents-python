from __future__ import annotations

import logging
import warnings as warnings_module
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents import Agent, Runner
from agents.items import TResponseInputItem
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
    async def test_run_compaction_reraises_replacement_error_when_restore_fails(
        self, caplog: pytest.LogCaptureFixture
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
                raise RuntimeError("restore failed")

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
