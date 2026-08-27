from __future__ import annotations

import asyncio
import logging
import warnings as warnings_module
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
    ResponseUsage,
)

import agents._debug as _debug
from agents import Agent, Runner
from agents.items import RunItem, TResponseInputItem
from agents.memory import (
    OpenAIResponsesCompactionArgs,
    OpenAIResponsesCompactionSession,
    Session,
    SessionSettings,
    SQLiteSession,
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
from agents.run_internal.session_persistence import save_result_to_session
from agents.testing import ScriptedModel
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
    def test_client_preserves_falsy_default_client(self) -> None:
        mock_client = MagicMock()
        mock_client.__bool__.return_value = False
        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=self.create_mock_session(),
        )

        with patch(
            "agents.memory.openai_responses_compaction_session.get_default_openai_client",
            return_value=mock_client,
        ):
            assert session.client is mock_client

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
    async def test_run_compaction_honors_falsey_decision_hook(self) -> None:
        class FalseyDecisionHook:
            def __init__(self) -> None:
                self.calls = 0

            def __bool__(self) -> bool:
                return False

            def __call__(self, context: dict[str, Any]) -> bool:
                self.calls += 1
                return False

        items = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": f"message {index}"},
            )
            for index in range(DEFAULT_COMPACTION_THRESHOLD)
        ]
        underlying = SimpleListSession(history=items)
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock()
        decision_hook = FalseyDecisionHook()
        session = OpenAIResponsesCompactionSession(
            session_id="test",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="input",
            should_trigger_compaction=decision_hook,
        )

        await session.run_compaction()

        assert decision_hook.calls == 1
        mock_client.responses.compact.assert_not_awaited()

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
    async def test_run_compaction_restores_history_when_replacement_add_is_cancelled(
        self,
    ) -> None:
        """CancelledError after clear must restore history (BaseException, not Exception)."""
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

        class CancelOnReplacementAddSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.add_calls = 0
                self.clear_calls = 0

            async def add_items(self, items: list[TResponseInputItem]) -> None:
                self.add_calls += 1
                if self.add_calls == 1:
                    raise asyncio.CancelledError()
                await super().add_items(items)

            async def clear_session(self) -> None:
                self.clear_calls += 1
                await super().clear_session()

        failing_session = CancelOnReplacementAddSession(history=history)

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

        with pytest.raises(asyncio.CancelledError):
            await session.run_compaction({"force": True})

        assert await failing_session.get_items() == history
        assert failing_session.clear_calls == 2
        assert failing_session.add_calls == 2

    @pytest.mark.asyncio
    async def test_run_compaction_restores_history_when_clear_is_cancelled_after_mutation(
        self,
    ) -> None:
        """CancelledError after a mutating clear must restore without a second destructive clear."""
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "original"}),
        ]
        compacted_items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "compacted"},
            )
        ]

        class CancelAfterMutatingClearSession(SimpleListSession):
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
                raise asyncio.CancelledError()

        failing_session = CancelAfterMutatingClearSession(history=history)

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

        with pytest.raises(asyncio.CancelledError):
            await session.run_compaction({"force": True})

        assert await failing_session.get_items() == history
        assert failing_session.clear_calls == 1
        assert failing_session.add_calls == 1

    @pytest.mark.asyncio
    async def test_run_compaction_restores_history_when_cancelled_again_during_restore(
        self,
    ) -> None:
        """A second cancel during restore must still finish rewriting previous history."""
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "original"}),
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": "reply"}),
        ]
        compacted_items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "compacted"},
            )
        ]

        class CancelThenGateRestoreSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.add_calls = 0
                self.clear_calls = 0
                self.restore_add_started = asyncio.Event()
                self.allow_restore_add = asyncio.Event()

            async def add_items(self, items: list[TResponseInputItem]) -> None:
                self.add_calls += 1
                if self.add_calls == 1:
                    raise asyncio.CancelledError()
                # Second add is the restore rewrite after clear.
                self.restore_add_started.set()
                await self.allow_restore_add.wait()
                await super().add_items(items)

            async def clear_session(self) -> None:
                self.clear_calls += 1
                await super().clear_session()

            def snapshot(self) -> list[TResponseInputItem]:
                return list(self._items)

        failing_session = CancelThenGateRestoreSession(history=history)

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

        compaction_task = asyncio.create_task(session.run_compaction({"force": True}))
        await failing_session.restore_add_started.wait()
        # Deliver a second cancel while restore add is still blocked on the gate.
        compaction_task.cancel()
        await asyncio.sleep(0)
        assert not failing_session.allow_restore_add.is_set()
        assert failing_session.snapshot() == []

        failing_session.allow_restore_add.set()

        with pytest.raises(asyncio.CancelledError):
            await compaction_task

        # History must already be restored when CancelledError surfaces — not later
        # via an orphaned background rewrite after the await returns.
        assert failing_session.snapshot() == history
        assert failing_session.clear_calls == 2
        assert failing_session.add_calls == 2

    @pytest.mark.asyncio
    async def test_cancel_restore_waits_for_mutation_lock_before_newer_writes(
        self, tmp_path
    ) -> None:
        """Newer wrapper writes must wait out cancel-restore and survive chronologically."""
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "original"}),
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "reply"},
            ),
        ]
        newer_item: TResponseInputItem = cast(
            TResponseInputItem,
            {"type": "message", "role": "user", "content": "newer-after-cancel"},
        )
        compacted_items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "compacted"},
            )
        ]

        class GatedSQLiteSession(SQLiteSession):
            def __init__(self, session_id: str, db_path: str) -> None:
                super().__init__(session_id, db_path)
                self.add_calls = 0
                self.clear_calls = 0
                self.cancel_replacement_add = False
                self.restore_clear_started = asyncio.Event()
                self.allow_restore_clear = asyncio.Event()

            async def add_items(self, items: list[TResponseInputItem]) -> None:
                self.add_calls += 1
                if self.cancel_replacement_add and self.add_calls == 1:
                    raise asyncio.CancelledError()
                await super().add_items(items)

            async def clear_session(self) -> None:
                self.clear_calls += 1
                if self.clear_calls == 2:
                    self.restore_clear_started.set()
                    await self.allow_restore_clear.wait()
                await super().clear_session()

        underlying = GatedSQLiteSession("lock-test", str(tmp_path / "compaction_lock.db"))
        await underlying.add_items(history)
        underlying.add_calls = 0
        underlying.clear_calls = 0
        underlying.cancel_replacement_add = True

        mock_compact_response = MagicMock()
        mock_compact_response.output = compacted_items
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="lock-test",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="input",
        )
        # Warm wrapper caches so later add_items updates _session_items in place.
        await session._ensure_compaction_candidates()

        compaction_task = asyncio.create_task(session.run_compaction({"force": True}))
        await underlying.restore_clear_started.wait()

        newer_write = asyncio.create_task(session.add_items([newer_item]))
        await asyncio.sleep(0)
        assert not newer_write.done()
        assert underlying.clear_calls == 2
        assert not underlying.allow_restore_clear.is_set()

        underlying.allow_restore_clear.set()

        with pytest.raises(asyncio.CancelledError):
            await compaction_task
        await newer_write

        stored = await session.get_items()
        assert stored == [*history, newer_item]
        assert session._session_items == [*history, newer_item]
        assert underlying.clear_calls == 2
        # 1) cancelled replacement add, 2) restore rewrite, 3) newer wrapper write.
        assert underlying.add_calls == 3

    @pytest.mark.asyncio
    async def test_exception_restore_drains_when_compaction_is_cancelled(self, tmp_path) -> None:
        """Cancel during Exception-path restore must still finish rewriting history."""
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "user", "content": "original"}),
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "reply"},
            ),
        ]
        compacted_items: list[TResponseInputItem] = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": "compacted"},
            )
        ]

        class ExceptionThenGateRestoreSQLiteSession(SQLiteSession):
            def __init__(self, session_id: str, db_path: str) -> None:
                super().__init__(session_id, db_path)
                self.add_calls = 0
                self.clear_calls = 0
                self.fail_replacement_add = False
                self.restore_add_started = asyncio.Event()
                self.allow_restore_add = asyncio.Event()
                self.restore_tasks_seen: list[asyncio.Task[Any]] = []

            async def add_items(self, items: list[TResponseInputItem]) -> None:
                self.add_calls += 1
                if self.fail_replacement_add and self.add_calls == 1:
                    raise RuntimeError("replacement failed")
                if self.fail_replacement_add and self.add_calls == 2:
                    current = asyncio.current_task()
                    if current is not None:
                        self.restore_tasks_seen.append(current)
                    self.restore_add_started.set()
                    await self.allow_restore_add.wait()
                await super().add_items(items)

            async def clear_session(self) -> None:
                self.clear_calls += 1
                await super().clear_session()

        underlying = ExceptionThenGateRestoreSQLiteSession(
            "exception-cancel-restore", str(tmp_path / "exception_cancel_restore.db")
        )
        await underlying.add_items(history)
        underlying.add_calls = 0
        underlying.clear_calls = 0
        underlying.fail_replacement_add = True

        mock_compact_response = MagicMock()
        mock_compact_response.output = compacted_items
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)

        session = OpenAIResponsesCompactionSession(
            session_id="exception-cancel-restore",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="input",
        )
        await session._ensure_compaction_candidates()
        warmed_items = list(session._session_items or [])

        compaction_task = asyncio.create_task(session.run_compaction({"force": True}))
        await underlying.restore_add_started.wait()

        compaction_task.cancel()
        await asyncio.sleep(0)
        assert not compaction_task.done()
        assert not underlying.allow_restore_add.is_set()
        assert await underlying.get_items() == []

        underlying.allow_restore_add.set()

        with pytest.raises(asyncio.CancelledError):
            await compaction_task

        stored = await session.get_items()
        assert stored == history
        assert session._session_items == warmed_items
        assert not session._mutation_lock.locked()
        assert underlying.clear_calls == 2
        assert underlying.add_calls == 2
        assert all(task.done() for task in underlying.restore_tasks_seen)

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
            usage=ResponseUsage(
                input_tokens=150_000,
                input_tokens_details=InputTokensDetails(
                    cached_tokens=50_000,
                    cache_write_tokens=0,
                ),
                output_tokens=42_000,
                output_tokens_details=OutputTokensDetails(reasoning_tokens=10_000),
                total_tokens=192_000,
            ),
        )
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=compacted)

        session = OpenAIResponsesCompactionSession(
            session_id="demo",
            underlying_session=underlying,
            client=mock_client,
            should_trigger_compaction=lambda ctx: True,
        )

        model = ScriptedModel(steps=[[get_text_message("ok")]])
        agent = Agent(name="assistant", model=model)

        result = await Runner.run(agent, "hello", session=session)

        mock_client.responses.compact.assert_awaited_once()
        assert result.context_wrapper.usage.requests == 2
        assert result.context_wrapper.usage.input_tokens == 150_000
        assert result.context_wrapper.usage.output_tokens == 42_000
        assert result.context_wrapper.usage.total_tokens == 192_000
        assert result.context_wrapper.usage.input_tokens_details.cached_tokens == 50_000
        assert result.context_wrapper.usage.output_tokens_details.reasoning_tokens == 10_000
        assert len(result.context_wrapper.usage.request_usage_entries) == 1
        assert result.context_wrapper.usage.request_usage_entries[0].total_tokens == 192_000
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
        model = ScriptedModel(steps=[[get_function_tool_call("do_thing")]])
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
        model = ScriptedModel(steps=[[get_function_tool_call("do_thing")]])
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
        model = ScriptedModel()
        model.extend(
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
        model = ScriptedModel()
        model.extend(
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


class TestCompactionConcurrentMutations:
    """Wrapper mutations racing the in-flight responses.compact call."""

    def create_gated_compact_client(
        self,
        output_items: list[TResponseInputItem],
        compact_entered: asyncio.Event,
        release_compact: asyncio.Event,
    ) -> MagicMock:
        """Build a client whose compact call blocks until the test releases it."""
        mock_compact_response = MagicMock()
        mock_compact_response.output = output_items

        async def gated_compact(**kwargs: Any) -> MagicMock:
            compact_entered.set()
            await release_compact.wait()
            return mock_compact_response

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(side_effect=gated_compact)
        return mock_client

    @pytest.mark.asyncio
    async def test_concurrent_add_items_during_forced_compaction_survives(self) -> None:
        """Items added while responses.compact is in flight must survive replacement."""
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": f"msg{i}"})
            for i in range(3)
        ]
        compacted_item = cast(TResponseInputItem, {"type": "compaction", "summary": "compacted"})
        concurrent_user_item = cast(
            TResponseInputItem,
            {"type": "message", "role": "user", "content": "written mid-flight"},
        )
        concurrent_assistant_item = cast(
            TResponseInputItem,
            {"type": "message", "role": "assistant", "content": "reply mid-flight"},
        )

        underlying = SimpleListSession(history=history)
        compact_entered = asyncio.Event()
        release_compact = asyncio.Event()
        session = OpenAIResponsesCompactionSession(
            session_id="concurrent-add",
            underlying_session=underlying,
            client=self.create_gated_compact_client(
                [compacted_item], compact_entered, release_compact
            ),
            compaction_mode="input",
        )

        compaction_task = asyncio.create_task(session.run_compaction({"force": True}))
        try:
            await compact_entered.wait()
            await session.add_items([concurrent_user_item, concurrent_assistant_item])
            assert await underlying.get_items() == [
                *history,
                concurrent_user_item,
                concurrent_assistant_item,
            ]
        finally:
            release_compact.set()
        await compaction_task

        expected = [compacted_item, concurrent_user_item, concurrent_assistant_item]
        assert await session.get_items() == expected
        assert session._session_items == expected
        assert session._compaction_candidate_items == [concurrent_assistant_item]

    @pytest.mark.asyncio
    async def test_concurrent_add_items_during_threshold_compaction_survives(self) -> None:
        """The default threshold trigger path must also preserve mid-flight writes."""
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": f"msg{i}"})
            for i in range(DEFAULT_COMPACTION_THRESHOLD)
        ]
        compacted_item = cast(TResponseInputItem, {"type": "compaction", "summary": "compacted"})
        concurrent_item = cast(
            TResponseInputItem,
            {"type": "message", "role": "user", "content": "written mid-flight"},
        )

        underlying = SimpleListSession(history=history)
        compact_entered = asyncio.Event()
        release_compact = asyncio.Event()
        mock_client = self.create_gated_compact_client(
            [compacted_item], compact_entered, release_compact
        )
        session = OpenAIResponsesCompactionSession(
            session_id="concurrent-threshold",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="input",
        )

        compaction_task = asyncio.create_task(session.run_compaction())
        try:
            await compact_entered.wait()
            await session.add_items([concurrent_item])
        finally:
            release_compact.set()
        await compaction_task

        mock_client.responses.compact.assert_called_once()
        assert await session.get_items() == [compacted_item, concurrent_item]
        assert session._session_items == [compacted_item, concurrent_item]

    @pytest.mark.asyncio
    async def test_clear_session_during_compaction_is_not_resurrected(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A clear_session issued mid-flight must win over the stale snapshot."""
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": f"msg{i}"})
            for i in range(3)
        ]
        compacted_item = cast(TResponseInputItem, {"type": "compaction", "summary": "compacted"})

        underlying = SimpleListSession(history=history)
        compact_entered = asyncio.Event()
        release_compact = asyncio.Event()
        session = OpenAIResponsesCompactionSession(
            session_id="concurrent-clear",
            underlying_session=underlying,
            client=self.create_gated_compact_client(
                [compacted_item], compact_entered, release_compact
            ),
            compaction_mode="input",
        )

        with caplog.at_level(logging.WARNING, logger="openai-agents.openai.compaction"):
            compaction_task = asyncio.create_task(session.run_compaction({"force": True}))
            try:
                await compact_entered.wait()
                await session.clear_session()
                assert await underlying.get_items() == []
            finally:
                release_compact.set()
            await compaction_task

        assert await session.get_items() == []
        candidates, session_items = await session._ensure_compaction_candidates()
        assert candidates == []
        assert session_items == []
        assert "Skipped compaction replacement" in caplog.text

    @pytest.mark.asyncio
    async def test_replacement_preserves_metadata_tail_across_storage_round_trip(
        self, tmp_path
    ) -> None:
        """The snapshot prefix check must match items that round-tripped through storage."""
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": "reply"}),
            cast(
                TResponseInputItem,
                {
                    "type": "function_call",
                    "call_id": "call_history",
                    "name": "lookup",
                    "arguments": "{}",
                    TOOL_CALL_SESSION_DESCRIPTION_KEY: "Lookup private records.",
                },
            ),
        ]
        concurrent_item = cast(
            TResponseInputItem,
            {
                "type": "function_call",
                "call_id": "call_tail",
                "name": "lookup",
                "arguments": "{}",
                TOOL_CALL_SESSION_TITLE_KEY: "Lookup",
            },
        )
        compacted_item = cast(TResponseInputItem, {"type": "compaction", "summary": "compacted"})

        underlying = SQLiteSession("round-trip", str(tmp_path / "compaction_round_trip.db"))
        compact_entered = asyncio.Event()
        release_compact = asyncio.Event()
        session = OpenAIResponsesCompactionSession(
            session_id="round-trip",
            underlying_session=underlying,
            client=self.create_gated_compact_client(
                [compacted_item], compact_entered, release_compact
            ),
            compaction_mode="input",
        )

        # Warm the caches, then add through the wrapper so the cached items hold the
        # normalized shapes while the underlying store keeps the raw metadata keys.
        await session._ensure_compaction_candidates()
        await session.add_items(history)
        stored_history = [cast(dict, item) for item in await underlying.get_items()]
        assert any(TOOL_CALL_SESSION_DESCRIPTION_KEY in item for item in stored_history)
        assert all(
            TOOL_CALL_SESSION_DESCRIPTION_KEY not in cast(dict, item)
            for item in session._session_items or []
        )

        compaction_task = asyncio.create_task(session.run_compaction({"force": True}))
        try:
            await compact_entered.wait()
            await session.add_items([concurrent_item])
        finally:
            release_compact.set()
        await compaction_task

        assert await underlying.get_items() == [compacted_item, concurrent_item]
        assert session._session_items is not None
        cached_tail = cast(dict, session._session_items[1])
        assert cached_tail.get("call_id") == "call_tail"
        assert TOOL_CALL_SESSION_TITLE_KEY not in cached_tail

    @pytest.mark.asyncio
    async def test_clear_session_with_empty_snapshot_is_not_repopulated(self) -> None:
        """A clear during flight must hold even when the snapshot itself was empty.

        With previous_response_id compaction the local session can be empty when
        the request starts, so the post flight prefix check compares two empty
        lists and cannot see the clear. The destructive generation counter can.
        """
        compacted_item = cast(TResponseInputItem, {"type": "compaction", "summary": "compacted"})
        underlying = SimpleListSession(history=[])
        compact_entered = asyncio.Event()
        release_compact = asyncio.Event()
        session = OpenAIResponsesCompactionSession(
            session_id="clear-empty-baseline",
            underlying_session=underlying,
            client=self.create_gated_compact_client(
                [compacted_item], compact_entered, release_compact
            ),
            compaction_mode="previous_response_id",
        )

        compaction_task = asyncio.create_task(
            session.run_compaction({"force": True, "response_id": "resp_baseline"})
        )
        try:
            await compact_entered.wait()
            await session.clear_session()
        finally:
            release_compact.set()
        await compaction_task

        assert await underlying.get_items() == []
        assert await session.get_items() == []

    @pytest.mark.asyncio
    async def test_compact_uses_response_id_captured_before_lock_wait(self) -> None:
        """The compact call must use the response id its mode was resolved from.

        While one run_compaction waits on the mutation lock, a second call can
        overwrite the shared response id at entry. The waiter must not pick up
        the newer id after resuming.
        """
        compacted_item = cast(TResponseInputItem, {"type": "compaction", "summary": "compacted"})
        underlying = SimpleListSession(history=[])
        mock_compact_response = MagicMock()
        mock_compact_response.output = [compacted_item]
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)
        session = OpenAIResponsesCompactionSession(
            session_id="captured-response-id",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        await session._mutation_lock.acquire()
        try:
            first = asyncio.create_task(
                session.run_compaction({"force": True, "response_id": "resp_first"})
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            second = asyncio.create_task(
                session.run_compaction({"force": True, "response_id": "resp_second"})
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # The second call already overwrote the shared id while the first waits.
            assert session._response_id == "resp_second"
        finally:
            session._mutation_lock.release()
        await first
        await second

        sent_ids = [
            call.kwargs["previous_response_id"]
            for call in mock_client.responses.compact.call_args_list
        ]
        assert sent_ids == ["resp_first", "resp_second"]

    @pytest.mark.asyncio
    async def test_overlapping_compactions_with_empty_snapshots_keep_one_output(self) -> None:
        """The second of two overlapping compactions must not absorb the first.

        Both calls snapshot an empty session, so the prefix check alone cannot
        tell the first replacement from a concurrent append. Without the
        generation bump on replacement, the second call persists both outputs
        concatenated.
        """
        first_output = cast(TResponseInputItem, {"type": "compaction", "summary": "first"})
        second_output = cast(TResponseInputItem, {"type": "compaction", "summary": "second"})
        underlying = SimpleListSession(history=[])

        entered: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]
        release: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]
        outputs = [[first_output], [second_output]]
        call_index = 0

        async def gated_compact(**kwargs: Any) -> MagicMock:
            nonlocal call_index
            index = call_index
            call_index += 1
            entered[index].set()
            await release[index].wait()
            response = MagicMock()
            response.output = outputs[index]
            return response

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(side_effect=gated_compact)
        session = OpenAIResponsesCompactionSession(
            session_id="overlapping-empty",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        first = asyncio.create_task(
            session.run_compaction({"force": True, "response_id": "resp_overlap"})
        )
        await entered[0].wait()
        second = asyncio.create_task(
            session.run_compaction({"force": True, "response_id": "resp_overlap"})
        )
        await entered[1].wait()

        release[0].set()
        await first
        release[1].set()
        await second

        # The first replacement landed; the second detected the rewrite and skipped.
        assert await underlying.get_items() == [first_output]
        assert await session.get_items() == [first_output]

    @pytest.mark.asyncio
    async def test_turn_persisted_before_late_snapshot_survives_replacement(self) -> None:
        """A turn landing between a response's persisted batch and its snapshot survives.

        Ordering under test: run A's response batch is persisted, run B persists
        its own turn, and only then does run A snapshot and compact. The compact
        call with previous_response_id covers history through A's batch only, so
        the replacement must preserve B's turn even though A's late snapshot
        already contains it and the prefix and generation checks see no rewrite.
        """
        compacted_item = cast(TResponseInputItem, {"type": "compaction", "summary": "compacted"})
        a_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn a"}
        )
        b_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn b"}
        )

        underlying = SimpleListSession(history=[])
        compact_entered = asyncio.Event()
        release_compact = asyncio.Event()
        mock_client = self.create_gated_compact_client(
            [compacted_item], compact_entered, release_compact
        )
        session = OpenAIResponsesCompactionSession(
            session_id="persist-boundary",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
            should_trigger_compaction=lambda context: context["response_id"] == "resp_a",
        )

        class StubMessageRunItem:
            def __init__(self, payload: TResponseInputItem) -> None:
                self.raw_item = payload
                self.type = "message_output_item"

            def to_input_item(self) -> TResponseInputItem:
                return self.raw_item

        real_run_compaction = session.run_compaction
        run_a_compaction_requested = asyncio.Event()
        release_run_a_compaction = asyncio.Event()

        async def paused_run_compaction(args: OpenAIResponsesCompactionArgs | None = None) -> None:
            # Hold back only the first call, which belongs to run A. Run B's call
            # passes straight through and is declined by the decision hook.
            if not run_a_compaction_requested.is_set():
                run_a_compaction_requested.set()
                await release_run_a_compaction.wait()
            await real_run_compaction(args)

        session.run_compaction = paused_run_compaction  # type: ignore[method-assign]

        save_a = asyncio.create_task(
            save_result_to_session(
                session,
                [],
                [cast(RunItem, StubMessageRunItem(a_turn))],
                None,
                response_id="resp_a",
            )
        )
        await run_a_compaction_requested.wait()
        # Run A's batch is persisted and paired with its boundary, but its
        # compaction has not snapshotted yet. Run B's turn lands now.
        await save_result_to_session(
            session,
            [],
            [cast(RunItem, StubMessageRunItem(b_turn))],
            None,
            response_id="resp_b",
        )
        assert await underlying.get_items() == [a_turn, b_turn]

        release_run_a_compaction.set()
        await compact_entered.wait()
        release_compact.set()
        await save_a

        mock_client.responses.compact.assert_awaited_once()
        assert mock_client.responses.compact.call_args.kwargs["previous_response_id"] == "resp_a"
        assert await underlying.get_items() == [compacted_item, b_turn]
        assert await session.get_items() == [compacted_item, b_turn]

    @pytest.mark.asyncio
    async def test_overlapping_compaction_lands_on_translated_boundary(self) -> None:
        """A second compaction must preserve turns past its translated boundary.

        Ordering under test: response A's batch is persisted, response B's batch
        follows, and A's replacement lands while run C's turn is appended mid
        flight. A's replacement rewrites the prefix that B's recorded boundary
        counted into A's summary, so the boundary must be translated onto the
        rewritten history. B's later replacement then preserves C's turn.
        Dropping the boundary instead would make B skip its compaction, and
        keeping the raw count would misplace the boundary and drop the turn.
        """
        a_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn a"}
        )
        b_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn b"}
        )
        c_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn c"}
        )
        a_summary = cast(TResponseInputItem, {"type": "compaction", "summary": "through a"})
        b_summary = cast(TResponseInputItem, {"type": "compaction", "summary": "through b"})

        underlying = SimpleListSession(history=[])
        first_compact_entered = asyncio.Event()
        release_first_compact = asyncio.Event()
        outputs = [[a_summary], [b_summary]]
        call_index = 0

        async def gated_compact(**kwargs: Any) -> MagicMock:
            nonlocal call_index
            index = call_index
            call_index += 1
            if index == 0:
                first_compact_entered.set()
                await release_first_compact.wait()
            response = MagicMock()
            response.output = outputs[index]
            return response

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(side_effect=gated_compact)
        session = OpenAIResponsesCompactionSession(
            session_id="translated-boundary",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        await session._add_items_for_response([a_turn], response_id="resp_a")
        await session._add_items_for_response([b_turn], response_id="resp_b")

        compaction_a = asyncio.create_task(
            session.run_compaction({"force": True, "response_id": "resp_a"})
        )
        try:
            await first_compact_entered.wait()
            # A's request is in flight with its snapshot taken; C's turn lands now.
            await session.add_items([c_turn])
        finally:
            release_first_compact.set()
        await compaction_a
        assert await underlying.get_items() == [a_summary, b_turn, c_turn]

        await session.run_compaction({"force": True, "response_id": "resp_b"})

        sent_ids = [
            call.kwargs["previous_response_id"]
            for call in mock_client.responses.compact.call_args_list
        ]
        assert sent_ids == ["resp_a", "resp_b"]
        # B's boundary now sits past A's summary and B's own batch, so C's turn
        # is the only tail B's replacement must keep.
        assert await underlying.get_items() == [b_summary, c_turn]
        assert await session.get_items() == [b_summary, c_turn]

    @pytest.mark.asyncio
    async def test_compaction_skips_when_replacement_rewrote_its_recorded_baseline(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A compaction whose recorded prefix was rewritten away must skip.

        Ordering under test: response B's batch is persisted with a boundary,
        response A's batch follows, run C's turn lands while A's compaction is
        in flight, and A's replacement rewrites both batches into A's summary
        before B takes its snapshot. B's recorded prefix ended inside the
        rewritten region, so the replacement drops B's entry: no boundary on
        the new history describes what B's compaction covers. B must skip
        through the absent entry path without a billed call; falling back to
        its snapshot would classify C's turn and A's summary into B's baseline
        and drop both.
        """
        b_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn b"}
        )
        a_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn a"}
        )
        c_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn c"}
        )
        a_summary = cast(TResponseInputItem, {"type": "compaction", "summary": "through a"})
        stale_b_summary = cast(TResponseInputItem, {"type": "compaction", "summary": "through b"})

        underlying = SimpleListSession(history=[])
        compact_entered = asyncio.Event()
        release_compact = asyncio.Event()
        outputs = [[a_summary], [stale_b_summary]]
        call_index = 0

        async def gated_compact(**kwargs: Any) -> MagicMock:
            nonlocal call_index
            index = call_index
            call_index += 1
            if index == 0:
                compact_entered.set()
                await release_compact.wait()
            response = MagicMock()
            response.output = outputs[index]
            return response

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(side_effect=gated_compact)
        session = OpenAIResponsesCompactionSession(
            session_id="rewritten-baseline",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        await session._add_items_for_response([b_turn], response_id="resp_b")
        await session._add_items_for_response([a_turn], response_id="resp_a")

        compaction_a = asyncio.create_task(
            session.run_compaction({"force": True, "response_id": "resp_a"})
        )
        try:
            await compact_entered.wait()
            # A's request is in flight; C's turn lands before A's replacement.
            await session.add_items([c_turn])
        finally:
            release_compact.set()
        await compaction_a
        # A's replacement covered both batches and preserved C's turn, and it
        # dropped B's entry because B's recorded prefix was rewritten.
        assert await underlying.get_items() == [a_summary, c_turn]
        assert "resp_b" not in session._response_boundaries

        with caplog.at_level(logging.WARNING, logger="openai-agents.openai.compaction"):
            await session.run_compaction({"force": True, "response_id": "resp_b"})

        # B's compaction skipped without calling the API and replaced nothing,
        # so C's turn and A's summary both survive.
        assert await underlying.get_items() == [a_summary, c_turn]
        assert await session.get_items() == [a_summary, c_turn]
        assert mock_client.responses.compact.await_count == 1
        assert "Skipped compaction for resp_b" in caplog.text

    @pytest.mark.asyncio
    async def test_compaction_skips_when_recorded_boundary_was_evicted(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A delayed compaction whose recorded boundary was evicted must skip.

        Ordering under test: an old response's batch is persisted with a
        boundary, enough newer response batches follow to push that entry past
        the recording cap, and only then does the compaction keyed on the old
        response run. Its entry is gone, so no boundary on the current history
        describes what the compaction covers. Falling back to the snapshot
        would classify every newer turn into the old response's baseline and
        replace them all with a summary that covers the old response alone.
        """
        monkeypatch.setattr(
            "agents.memory.openai_responses_compaction_session._MAX_RECORDED_RESPONSE_BOUNDARIES",
            2,
        )
        old_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn old"}
        )
        newer_turns = [
            cast(
                TResponseInputItem,
                {"type": "message", "role": "assistant", "content": f"turn new {index}"},
            )
            for index in range(2)
        ]
        stale_summary = cast(TResponseInputItem, {"type": "compaction", "summary": "through old"})

        underlying = SimpleListSession(history=[])
        mock_compact_response = MagicMock()
        mock_compact_response.output = [stale_summary]
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)
        session = OpenAIResponsesCompactionSession(
            session_id="evicted-boundary",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        await session._add_items_for_response([old_turn], response_id="resp_old")
        for index, turn in enumerate(newer_turns):
            await session._add_items_for_response([turn], response_id=f"resp_new_{index}")
        assert "resp_old" not in session._response_boundaries

        with caplog.at_level(logging.WARNING, logger="openai-agents.openai.compaction"):
            await session.run_compaction({"force": True, "response_id": "resp_old"})

        # The compaction skipped before the billed call and replaced nothing,
        # so every newer turn survives.
        assert await underlying.get_items() == [old_turn, *newer_turns]
        assert await session.get_items() == [old_turn, *newer_turns]
        mock_client.responses.compact.assert_not_awaited()
        assert "Skipped compaction for resp_old" in caplog.text

    @pytest.mark.asyncio
    async def test_compaction_skips_for_recorded_response_after_clear_session(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A compaction keyed on a response recorded before clear_session must skip.

        Ordering under test: a response's batch is persisted with a boundary,
        clear_session wipes the history and every recorded boundary, and a
        fresh turn lands afterwards. The delayed compaction keyed on the
        cleared response then runs. Its entry is gone, so falling back to the
        snapshot would replace the fresh turn with a summary of history the
        clear already discarded.
        """
        recorded_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "recorded"}
        )
        later_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "later"}
        )
        stale_summary = cast(
            TResponseInputItem, {"type": "compaction", "summary": "cleared history"}
        )

        underlying = SimpleListSession(history=[])
        mock_compact_response = MagicMock()
        mock_compact_response.output = [stale_summary]
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)
        session = OpenAIResponsesCompactionSession(
            session_id="cleared-boundary",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        await session._add_items_for_response([recorded_turn], response_id="resp_recorded")
        await session.clear_session()
        await session.add_items([later_turn])
        assert "resp_recorded" not in session._response_boundaries

        with caplog.at_level(logging.WARNING, logger="openai-agents.openai.compaction"):
            await session.run_compaction({"force": True, "response_id": "resp_recorded"})

        # The compaction skipped instead of repopulating cleared history, so
        # the turn persisted after the clear survives untouched.
        assert await underlying.get_items() == [later_turn]
        assert await session.get_items() == [later_turn]
        mock_client.responses.compact.assert_not_awaited()
        assert "Skipped compaction for resp_recorded" in caplog.text

    @pytest.mark.asyncio
    async def test_persisted_batch_survives_read_failure_after_append(self) -> None:
        """A failing history read must not fail a batch the backend already holds.

        Ordering under test: a response's batch is persisted through the
        boundary hook while the underlying store fails every read issued
        after an append. The count that seeds the boundary is read before
        the append, so the hook records the boundary without touching the
        failing read and the caller never sees an error for a turn that is
        already stored; a read after the append would raise here, the run
        would treat the persisted turn as failed, and a retry would append
        it again. The compaction keyed on the response then proves the
        recorded boundary equals the count a read after the append would
        have produced: the turn persisted after the batch is preserved as
        the tail.
        """
        turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn a"}
        )
        later_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn later"}
        )
        summary = cast(TResponseInputItem, {"type": "compaction", "summary": "through a"})

        class ReadFailsAfterAppendSession(SimpleListSession):
            def __init__(self) -> None:
                super().__init__()
                self.add_calls = 0
                self.failing = True

            async def add_items(self, items: list[TResponseInputItem]) -> None:
                self.add_calls += 1
                await super().add_items(items)

            async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
                if self.failing and self.add_calls:
                    raise RuntimeError("history read failed after the batch was appended")
                return await super().get_items(limit)

        underlying = ReadFailsAfterAppendSession()
        mock_compact_response = MagicMock()
        mock_compact_response.output = [summary]
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)
        session = OpenAIResponsesCompactionSession(
            session_id="read-failure-boundary",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        await session._add_items_for_response([turn], response_id="resp_a")

        # The append landed exactly once and the boundary bookkeeping finished
        # even though every read after the append would have failed.
        assert underlying.add_calls == 1
        assert session._response_boundaries["resp_a"] == 1
        assert session._response_boundaries_ever_recorded is True

        underlying.failing = False
        assert await underlying.get_items() == [turn]

        await session.add_items([later_turn])
        await session.run_compaction({"force": True, "response_id": "resp_a"})

        # The recorded boundary matches the count a read after the append
        # would have produced, so the turn persisted after the batch is the
        # tail the replacement preserves.
        assert await underlying.get_items() == [summary, later_turn]
        assert await session.get_items() == [summary, later_turn]

    @pytest.mark.asyncio
    async def test_replacement_translates_boundaries_at_nonzero_shift(self) -> None:
        """Boundary translation must apply the exact shift of the rewrite.

        Ordering under test: response A persists a two item batch, response B
        persists one item, and A's replacement rewrites A's batch into a one
        item summary. The rewrite shrinks the prefix by one, so B's recorded
        boundary must shift from three to two. B's compaction then preserves
        exactly the turn appended past the translated boundary. Keeping B's
        raw count instead would place the boundary past that turn and drop
        it from the replaced history.
        """
        a_turn_one = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn a1"}
        )
        a_turn_two = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn a2"}
        )
        b_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn b"}
        )
        later_turn = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn later"}
        )
        a_summary = cast(TResponseInputItem, {"type": "compaction", "summary": "through a"})
        b_summary = cast(TResponseInputItem, {"type": "compaction", "summary": "through b"})

        underlying = SimpleListSession(history=[])
        outputs = [[a_summary], [b_summary]]
        call_index = 0

        async def sequenced_compact(**kwargs: Any) -> MagicMock:
            nonlocal call_index
            response = MagicMock()
            response.output = outputs[call_index]
            call_index += 1
            return response

        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(side_effect=sequenced_compact)
        session = OpenAIResponsesCompactionSession(
            session_id="translated-shift",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        await session._add_items_for_response([a_turn_one, a_turn_two], response_id="resp_a")
        await session._add_items_for_response([b_turn], response_id="resp_b")
        assert session._response_boundaries == {"resp_a": 2, "resp_b": 3}

        await session.run_compaction({"force": True, "response_id": "resp_a"})

        # The replacement rewrote two items into one, so every surviving
        # boundary shifts back by one.
        assert await underlying.get_items() == [a_summary, b_turn]
        assert session._response_boundaries["resp_b"] == 2

        await session.add_items([later_turn])
        await session.run_compaction({"force": True, "response_id": "resp_b"})

        # B's replacement preserved exactly the turn past the translated
        # boundary.
        assert await underlying.get_items() == [b_summary, later_turn]
        assert await session.get_items() == [b_summary, later_turn]

    @pytest.mark.asyncio
    async def test_defer_compaction_waits_for_mutation_lock(self) -> None:
        """The deferral decision must not run while another task holds the lock.

        Ordering under test: a writer holds the mutation lock, the way the
        replacement phase does, while _defer_compaction is called with cold
        caches. The deferral must park at the lock instead of reading the
        store and installing caches mid replacement; an unserialized fill
        here could overwrite the caches a replacement just installed with a
        torn read of the store.
        """
        turn = cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn"})
        underlying = SimpleListSession(history=[turn])
        session = OpenAIResponsesCompactionSession(
            session_id="defer-lock",
            underlying_session=underlying,
            client=MagicMock(),
            should_trigger_compaction=lambda context: True,
        )

        async with session._mutation_lock:
            defer_task = asyncio.create_task(session._defer_compaction("resp_a"))
            for _ in range(5):
                await asyncio.sleep(0)
            # Parked at the lock: no cache fill and no deferred id yet.
            assert not defer_task.done()
            assert session._compaction_candidate_items is None
            assert session._session_items is None
            assert session._get_deferred_compaction_response_id() is None
        await defer_task

        assert session._get_deferred_compaction_response_id() == "resp_a"
        assert session._session_items == [turn]

    @pytest.mark.asyncio
    async def test_deferred_id_survives_concurrent_run_compaction(self) -> None:
        """A deferral landing after the decision phase must not be wiped.

        Ordering under test: run A passes the compaction decision, and run B
        defers its own response while A still holds the decision phase lock.
        A subsumes only deferrals made before its decision, so B's deferral
        must survive A and keep the forced follow up compaction for B's
        response armed.
        """
        history: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": f"m{i}"})
            for i in range(3)
        ]
        summary = cast(TResponseInputItem, {"type": "compaction", "summary": "compacted"})

        class SnapshotPausingSession(SimpleListSession):
            def __init__(self, history: list[TResponseInputItem]) -> None:
                super().__init__(history=history)
                self.snapshot_entered = asyncio.Event()
                self.release_snapshot = asyncio.Event()
                self.full_reads = 0

            async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
                if limit is not None and limit > 1_000_000:
                    self.full_reads += 1
                    if self.full_reads == 1:
                        self.snapshot_entered.set()
                        await self.release_snapshot.wait()
                return await super().get_items(limit)

        underlying = SnapshotPausingSession(history=history)
        mock_compact_response = MagicMock()
        mock_compact_response.output = [summary]
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)
        session = OpenAIResponsesCompactionSession(
            session_id="deferral-survives",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="input",
            should_trigger_compaction=lambda context: True,
        )

        compaction_task = asyncio.create_task(session.run_compaction({"force": True}))
        try:
            await underlying.snapshot_entered.wait()
            # A holds the decision phase lock at its snapshot read; B's
            # deferral arrives now and parks at the lock.
            defer_task = asyncio.create_task(session._defer_compaction("resp_b"))
            for _ in range(5):
                await asyncio.sleep(0)
            assert not defer_task.done()
        finally:
            underlying.release_snapshot.set()
        await compaction_task
        await defer_task

        # B's deferral survived A's compaction instead of being wiped.
        assert session._get_deferred_compaction_response_id() == "resp_b"
        assert await underlying.get_items() == [summary]

    @pytest.mark.asyncio
    async def test_failed_replacement_drops_boundary_state(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed replacement transaction must invalidate the boundary records.

        Ordering under test: a response's batch is persisted with a boundary,
        its replacement fails, and the restore fails too, leaving the store
        empty while the boundary still counts the old history. The failure
        must drop every recorded boundary and count as a rewrite, matching
        pop_item and clear_session; a retried compaction keyed on the same
        response then skips. Keeping the boundary instead would let the retry
        slice a shorter store past its end and silently delete a batch
        persisted while the retry was in flight.
        """
        turns: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": f"t{i}"})
            for i in range(3)
        ]
        batch_turns: list[TResponseInputItem] = [
            cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": f"b{i}"})
            for i in range(2)
        ]
        stale_summary = cast(TResponseInputItem, {"type": "compaction", "summary": "stale"})

        class DoubleFailureSession(SimpleListSession):
            def __init__(self) -> None:
                super().__init__()
                self.failing_adds = 0

            async def add_items(self, items: list[TResponseInputItem]) -> None:
                if self.failing_adds > 0:
                    self.failing_adds -= 1
                    raise RuntimeError("backend write failed")
                await super().add_items(items)

        underlying = DoubleFailureSession()
        mock_compact_response = MagicMock()
        mock_compact_response.output = [stale_summary]
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)
        session = OpenAIResponsesCompactionSession(
            session_id="failed-replacement",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        await session._add_items_for_response(turns, response_id="resp_a")
        generation_before = session._destructive_generation

        # The replacement add and the restore add both fail in one outage.
        underlying.failing_adds = 2
        with pytest.raises(RuntimeError, match="backend write failed"):
            await session.run_compaction({"force": True, "response_id": "resp_a"})

        assert await underlying.get_items() == []
        assert session._response_boundaries == {}
        assert session._destructive_generation == generation_before + 1
        assert session._response_boundaries_ever_recorded is True

        # A batch persisted after the failure records its boundary cleanly.
        await session._add_items_for_response(batch_turns, response_id="resp_b")

        with caplog.at_level(logging.WARNING, logger="openai-agents.openai.compaction"):
            await session.run_compaction({"force": True, "response_id": "resp_a"})

        # The retry skipped before the billed call instead of trusting a
        # boundary the store no longer backs, so the newer batch survives.
        assert "Skipped compaction for resp_a" in caplog.text
        assert await underlying.get_items() == batch_turns
        assert mock_client.responses.compact.await_count == 1

    @pytest.mark.asyncio
    async def test_compaction_skips_when_recorded_boundary_exceeds_history(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A boundary past the stored history must skip the replacement.

        No healthy path records such a boundary: appends record the post
        append count and every rewrite drops or translates entries, so the
        state here is planted directly to stand in for corrupted
        bookkeeping. The replacement must treat it as invalid instead of
        slicing past the end, which would classify the whole history as
        covered and replace it outright.
        """
        turn = cast(TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn"})
        stale_summary = cast(TResponseInputItem, {"type": "compaction", "summary": "stale"})

        underlying = SimpleListSession(history=[])
        mock_compact_response = MagicMock()
        mock_compact_response.output = [stale_summary]
        mock_client = MagicMock()
        mock_client.responses.compact = AsyncMock(return_value=mock_compact_response)
        session = OpenAIResponsesCompactionSession(
            session_id="oversized-boundary",
            underlying_session=underlying,
            client=mock_client,
            compaction_mode="previous_response_id",
        )

        await session._add_items_for_response([turn], response_id="resp_a")
        session._response_boundaries["resp_a"] = 99

        with caplog.at_level(logging.WARNING, logger="openai-agents.openai.compaction"):
            await session.run_compaction({"force": True, "response_id": "resp_a"})

        # The replacement was skipped and the corrupt records were dropped.
        assert "Skipped compaction replacement for resp_a" in caplog.text
        assert await underlying.get_items() == [turn]
        assert session._response_boundaries == {}
        assert mock_client.responses.compact.await_count == 1

    @pytest.mark.asyncio
    async def test_failed_tail_normalization_keeps_store_untouched(self) -> None:
        """A cache build failure must surface before the store is rewritten.

        Ordering under test: a response's batch is persisted with a boundary,
        and while the compaction request is in flight a writer sharing the
        underlying store appends an item whose serialization hook raises.
        Building the refreshed caches from that tail fails; the failure must
        surface before the replacement, leaving the store, the generation
        counter, and the recorded boundaries all describing the same history.
        A replacement that lands before the failure would strand a rewritten
        store behind bookkeeping that still describes the old one.
        """

        class UnserializableItem:
            @property
            def model_dump(self) -> Any:
                raise RuntimeError("serialization failed")

        a_turn_one = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn a1"}
        )
        a_turn_two = cast(
            TResponseInputItem, {"type": "message", "role": "assistant", "content": "turn a2"}
        )
        poisoned_item = cast(TResponseInputItem, UnserializableItem())
        summary = cast(TResponseInputItem, {"type": "compaction", "summary": "through a"})

        underlying = SimpleListSession(history=[])
        compact_entered = asyncio.Event()
        release_compact = asyncio.Event()
        session = OpenAIResponsesCompactionSession(
            session_id="poisoned-tail",
            underlying_session=underlying,
            client=self.create_gated_compact_client([summary], compact_entered, release_compact),
            compaction_mode="previous_response_id",
        )

        await session._add_items_for_response([a_turn_one, a_turn_two], response_id="resp_a")
        generation_before = session._destructive_generation

        compaction_task = asyncio.create_task(
            session.run_compaction({"force": True, "response_id": "resp_a"})
        )
        try:
            await compact_entered.wait()
            # A writer sharing the store appends the item that cannot be
            # serialized while the request is in flight.
            await underlying.add_items([poisoned_item])
        finally:
            release_compact.set()
        with pytest.raises(RuntimeError, match="serialization failed"):
            await compaction_task

        # The store was not rewritten, and the bookkeeping still describes it.
        assert await underlying.get_items() == [a_turn_one, a_turn_two, poisoned_item]
        assert session._response_boundaries == {"resp_a": 2}
        assert session._destructive_generation == generation_before


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
