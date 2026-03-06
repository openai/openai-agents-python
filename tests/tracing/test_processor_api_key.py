from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Union, cast

import pytest

from agents.tracing.processors import BackendSpanExporter
from agents.tracing.spans import Span
from agents.tracing.traces import Trace


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


def test_exporter_uses_item_api_keys(monkeypatch):
    class DummyItem:
        def __init__(self, key: str | None, payload: dict[str, str]):
            self.tracing_api_key = key
            self._payload = payload

        def export(self) -> dict[str, str]:
            return self._payload

    calls: list[dict[str, Any]] = []

    def fake_post(*, url, headers, json):
        calls.append({"url": url, "headers": headers, "json": json})
        return SimpleNamespace(status_code=200, text="ok")

    exporter = BackendSpanExporter()
    exporter.set_api_key("global-key")
    monkeypatch.setattr(exporter, "_client", SimpleNamespace(post=fake_post))

    exporter.export(
        cast(
            list[Union[Trace, Span[Any]]],
            [
                DummyItem("key-a", {"id": "a"}),
                DummyItem(None, {"id": "b"}),
                DummyItem("key-b", {"id": "c"}),
            ],
        )
    )

    assert len(calls) == 3
    auth_by_first_item = {
        tuple(entry["id"] for entry in call["json"]["data"]): call["headers"]["Authorization"]
        for call in calls
    }
    assert ("a",) in auth_by_first_item
    assert ("b",) in auth_by_first_item
    assert ("c",) in auth_by_first_item
    assert auth_by_first_item[("a",)] == "Bearer key-a"
    assert auth_by_first_item[("c",)] == "Bearer key-b"
    assert auth_by_first_item[("b",)] == "Bearer global-key"
