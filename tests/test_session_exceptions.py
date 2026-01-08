"""Tests for session exception classes."""

from __future__ import annotations

from agents.exceptions import (
    AgentsException,
    SessionError,
    SessionNotFoundError,
    SessionSerializationError,
)


class TestSessionExceptions:
    """Tests for session exception classes."""

    def test_session_error_base_class(self) -> None:
        """SessionError should be a subclass of AgentsException."""
        assert issubclass(SessionError, AgentsException)

    def test_session_error_with_session_id(self) -> None:
        """SessionError should include session_id in message when provided."""
        error = SessionError("Database connection failed", session_id="sess_123")
        assert error.session_id == "sess_123"
        assert "sess_123" in str(error)
        assert "Database connection failed" in str(error)

    def test_session_error_without_session_id(self) -> None:
        """SessionError should work without session_id."""
        error = SessionError("General session failure")
        assert error.session_id is None
        assert "General session failure" in str(error)

    def test_session_not_found_error(self) -> None:
        """SessionNotFoundError should be a subclass of SessionError."""
        assert issubclass(SessionNotFoundError, SessionError)
        error = SessionNotFoundError("sess_missing")
        assert error.session_id == "sess_missing"
        assert "sess_missing" in str(error)
        assert "not found" in str(error).lower()

    def test_session_serialization_error(self) -> None:
        """SessionSerializationError should be a subclass of SessionError."""
        assert issubclass(SessionSerializationError, SessionError)
        error = SessionSerializationError("Invalid JSON", session_id="sess_456")
        assert error.session_id == "sess_456"
        assert "Invalid JSON" in str(error)
        assert "Serialization" in str(error)

    def test_session_serialization_error_without_session_id(self) -> None:
        """SessionSerializationError should work without session_id."""
        error = SessionSerializationError("Circular reference detected")
        assert error.session_id is None
        assert "Circular reference detected" in str(error)

    def test_exceptions_have_run_data_attribute(self) -> None:
        """All session exceptions should have run_data attribute from AgentsException."""
        errors = [
            SessionError("test"),
            SessionNotFoundError("sess_test"),
            SessionSerializationError("test"),
        ]
        for error in errors:
            assert hasattr(error, "run_data")
            assert error.run_data is None
