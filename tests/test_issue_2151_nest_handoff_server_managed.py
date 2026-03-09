"""Tests for issue #2151: nest_handoff_history incompatibility with server-managed conversations."""

import pytest

from agents.run_config import RunConfig
from agents.run_internal.agent_runner_helpers import validate_nest_handoff_history_settings
from agents.exceptions import UserError


class TestValidateNestHandoffHistorySettings:
    """Tests for validate_nest_handoff_history_settings function."""

    def test_no_error_when_nest_handoff_history_disabled(self):
        """No error when nest_handoff_history is False."""
        run_config = RunConfig(nest_handoff_history=False)
        # Should not raise
        validate_nest_handoff_history_settings(
            run_config,
            conversation_id="conv_123",
            previous_response_id=None,
            auto_previous_response_id=False,
        )

    def test_no_error_when_no_server_managed_conversation(self):
        """No error when not using server-managed conversation."""
        run_config = RunConfig(nest_handoff_history=True)
        # Should not raise
        validate_nest_handoff_history_settings(
            run_config,
            conversation_id=None,
            previous_response_id=None,
            auto_previous_response_id=False,
        )

    def test_no_error_when_run_config_is_none(self):
        """No error when run_config is None."""
        # Should not raise
        validate_nest_handoff_history_settings(
            None,
            conversation_id="conv_123",
            previous_response_id=None,
            auto_previous_response_id=False,
        )

    def test_raises_error_with_conversation_id(self):
        """Error when nest_handoff_history is True with conversation_id."""
        run_config = RunConfig(nest_handoff_history=True)
        with pytest.raises(UserError) as exc_info:
            validate_nest_handoff_history_settings(
                run_config,
                conversation_id="conv_123",
                previous_response_id=None,
                auto_previous_response_id=False,
            )
        assert "nest_handoff_history is incompatible" in str(exc_info.value)
        assert "conversation_id" in str(exc_info.value)

    def test_raises_error_with_previous_response_id(self):
        """Error when nest_handoff_history is True with previous_response_id."""
        run_config = RunConfig(nest_handoff_history=True)
        with pytest.raises(UserError) as exc_info:
            validate_nest_handoff_history_settings(
                run_config,
                conversation_id=None,
                previous_response_id="resp_123",
                auto_previous_response_id=False,
            )
        assert "nest_handoff_history is incompatible" in str(exc_info.value)
        assert "previous_response_id" in str(exc_info.value)

    def test_raises_error_with_auto_previous_response_id(self):
        """Error when nest_handoff_history is True with auto_previous_response_id."""
        run_config = RunConfig(nest_handoff_history=True)
        with pytest.raises(UserError) as exc_info:
            validate_nest_handoff_history_settings(
                run_config,
                conversation_id=None,
                previous_response_id=None,
                auto_previous_response_id=True,
            )
        assert "nest_handoff_history is incompatible" in str(exc_info.value)
        assert "auto_previous_response_id" in str(exc_info.value)

    def test_error_includes_github_issue_link(self):
        """Error message should include link to GitHub issue for context."""
        run_config = RunConfig(nest_handoff_history=True)
        with pytest.raises(UserError) as exc_info:
            validate_nest_handoff_history_settings(
                run_config,
                conversation_id="conv_123",
                previous_response_id=None,
                auto_previous_response_id=False,
            )
        assert "issues/2151" in str(exc_info.value)
