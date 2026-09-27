from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import unquote

import pytest

from examples.memory import redis_session_example as example

URL = "rediss://synthetic-user:synthetic%2Fpassword@localhost:6380/2?password=query-secret"


@pytest.fixture
def redis_demo(monkeypatch):
    monkeypatch.setenv("REDIS_URL", URL)
    session = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        clear_session=AsyncMock(),
        get_items=AsyncMock(return_value=[]),
        close=AsyncMock(),
    )
    factory = Mock(return_value=session)
    monkeypatch.setattr(example.RedisSession, "from_url", factory)
    monkeypatch.setattr(
        example.Runner, "run", AsyncMock(return_value=SimpleNamespace(final_output="Demo response"))
    )
    return session, factory


def assert_safe_output(capsys, caplog):
    captured = capsys.readouterr()
    output = captured.out + captured.err + caplog.text
    for value in (URL, unquote(URL), "synthetic-user", "synthetic%2Fpassword", "query-secret"):
        assert value not in output
    return output


@pytest.mark.asyncio
async def test_demos_preserve_connection_url_without_printing_it(redis_demo, capsys, caplog):
    session, factory = redis_demo
    await example.main()
    await example.demonstrate_advanced_features()

    output = assert_safe_output(capsys, caplog)
    assert "Conversation Complete" in output
    assert "custom key prefix created successfully" in output
    assert factory.call_count == 4
    assert all(call.kwargs["url"] == URL for call in factory.call_args_list)
    assert factory.call_args_list[2].kwargs["ttl"] == 3600
    assert factory.call_args_list[3].kwargs["key_prefix"] == "tenant_abc:sessions"
    assert session.close.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("demo", [example.main, example.demonstrate_advanced_features])
@pytest.mark.parametrize("failure", ["construct", "operation", "unreachable"])
async def test_demo_failures_do_not_render_connection_details(
    redis_demo, capsys, caplog, demo, failure
):
    session, factory = redis_demo
    # Include decoded data and a chained backend error, not just the complete URL.
    error = ValueError(f"Could not connect: {unquote(URL)}")
    error.__cause__ = RuntimeError("query-secret")
    if failure == "construct":
        factory.side_effect = error
    elif failure == "operation":
        session.ping.side_effect = error
    else:
        session.ping.return_value = False

    await demo()

    output = assert_safe_output(capsys, caplog)
    factory.assert_called_once()
    assert factory.call_args.kwargs["url"] == URL
    if failure != "unreachable":
        assert "Check the Redis configuration and connection." in output
    assert "Traceback" not in output


@pytest.mark.asyncio
async def test_invalid_query_configuration_is_reported_safely(monkeypatch, capsys, caplog):
    # The real client parser includes invalid option values in its exception message.
    monkeypatch.setenv("REDIS_URL", "redis://localhost/0?socket_timeout=query-secret")
    await example.main()
    await example.demonstrate_advanced_features()
    output = assert_safe_output(capsys, caplog)
    assert output.count("Check the Redis configuration and connection.") == 2
