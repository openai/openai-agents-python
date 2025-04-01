import pytest

from agents.tracing.processors import BackendSpanExporter


@pytest.mark.asyncio
async def test_processor_api_key(monkeypatch):
    # If the API key is not set, it should be None
    monkeypatch.delenv("OPENAI_API_KEY", None)
    processor = BackendSpanExporter()
    assert processor.api_key is None

    # If we set it afterwards, it should be the new value
    processor.set_api_key("test_api_key")
    assert processor.api_key == "test_api_key"


@pytest.mark.asyncio
async def test_processor_api_key_from_env(monkeypatch):
    # If the API key is not set at creation time but set before access time, it should be the new
    # value
    monkeypatch.delenv("OPENAI_API_KEY", None)
    processor = BackendSpanExporter()

    # If we set it afterwards, it should be the new value
    monkeypatch.setenv("OPENAI_API_KEY", "foo_bar_123")
    assert processor.api_key == "foo_bar_123"
