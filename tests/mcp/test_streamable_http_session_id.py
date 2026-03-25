"""Tests for MCPServerStreamableHttp.session_id property (issue #924)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.mcp import MCPServerStreamableHttp


class TestStreamableHttpSessionId:
    """Tests that the session_id property is correctly exposed."""

    def test_session_id_is_none_before_connect(self):
        """session_id should be None when the server has not been connected yet."""
        server = MCPServerStreamableHttp(params={"url": "http://localhost:9999/mcp"})
        assert server.session_id is None

    def test_session_id_returns_none_when_callback_is_none(self):
        """session_id should be None when _get_session_id callback is None."""
        server = MCPServerStreamableHttp(params={"url": "http://localhost:9999/mcp"})
        server._get_session_id = None
        assert server.session_id is None

    def test_session_id_returns_callback_value(self):
        """session_id should return the value from the get_session_id callback."""
        server = MCPServerStreamableHttp(params={"url": "http://localhost:9999/mcp"})
        mock_get_session_id = MagicMock(return_value="test-session-abc123")
        server._get_session_id = mock_get_session_id
        assert server.session_id == "test-session-abc123"
        mock_get_session_id.assert_called_once()

    def test_session_id_returns_none_when_callback_returns_none(self):
        """session_id should return None when the callback itself returns None."""
        server = MCPServerStreamableHttp(params={"url": "http://localhost:9999/mcp"})
        mock_get_session_id = MagicMock(return_value=None)
        server._get_session_id = mock_get_session_id
        assert server.session_id is None

    def test_session_id_reflects_updated_callback_value(self):
        """session_id should reflect the latest value from the callback each time."""
        server = MCPServerStreamableHttp(params={"url": "http://localhost:9999/mcp"})
        call_count = 0

        def changing_callback() -> str | None:
            nonlocal call_count
            call_count += 1
            return f"session-{call_count}"

        server._get_session_id = changing_callback
        assert server.session_id == "session-1"
        assert server.session_id == "session-2"

    @pytest.mark.asyncio
    async def test_connect_captures_get_session_id_callback(self):
        """connect() should capture the third element of the transport tuple as _get_session_id."""
        server = MCPServerStreamableHttp(params={"url": "http://localhost:9999/mcp"})

        mock_read = AsyncMock()
        mock_write = AsyncMock()
        mock_get_session_id = MagicMock(return_value="captured-session-xyz")

        mock_initialize_result = MagicMock()
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock(return_value=mock_initialize_result)

        # Simulate the full 3-tuple that streamablehttp_client returns
        transport_tuple = (mock_read, mock_write, mock_get_session_id)

        with patch("agents.mcp.server.ClientSession") as mock_client_session_cls:
            mock_client_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_client_session_cls.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch.object(
                server,
                "create_streams",
            ) as mock_create_streams:
                mock_cm = MagicMock()
                mock_cm.__aenter__ = AsyncMock(return_value=transport_tuple)
                mock_cm.__aexit__ = AsyncMock(return_value=None)
                mock_create_streams.return_value = mock_cm

                with patch.object(server.exit_stack, "enter_async_context") as mock_enter:
                    # First call returns transport, second call returns session
                    mock_enter.side_effect = [transport_tuple, mock_session]
                    mock_session.initialize.return_value = mock_initialize_result

                    await server.connect()

        # After connect, _get_session_id should be the callable from the transport
        assert server._get_session_id is mock_get_session_id
        assert server.session_id == "captured-session-xyz"


@pytest.mark.asyncio
async def test_session_id_is_none_after_cleanup():
    """session_id must return None after disconnect (cleanup clears _get_session_id)."""
    server = MCPServerStreamableHttp(params={"url": "http://localhost:8000/mcp"})

    mock_get_session_id = MagicMock(return_value="session-to-clear")
    # Manually inject a session-id callback to simulate a connected state
    server._get_session_id = mock_get_session_id
    server.session = MagicMock()  # pretend connected

    assert server.session_id == "session-to-clear"

    # Now simulate cleanup completing (exit_stack.aclose is a no-op here)
    with patch.object(server.exit_stack, "aclose", new_callable=AsyncMock):
        await server.cleanup()

    # After cleanup both session and _get_session_id must be None
    assert server.session is None
    assert server._get_session_id is None
    assert server.session_id is None
