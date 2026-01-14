from __future__ import annotations

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
    is_openai_responses_compaction_aware_session,
)
from agents.memory.openai_responses_compaction_session import (
    DEFAULT_COMPACTION_THRESHOLD,
    is_openai_model_name,
    select_compaction_candidate_items,
)
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message
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
        )

        with pytest.raises(ValueError, match="requires a response_id"):
            await session.run_compaction()

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
