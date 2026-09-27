from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from agents.tracing import processors
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
            list[Trace | Span[Any]],
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


class _KeylessItem:
    tracing_api_key = None

    def export(self) -> dict[str, str]:
        return {"id": "item"}


def _record_authorization_headers(monkeypatch, exporter: BackendSpanExporter) -> list[str]:
    authorization_headers: list[str] = []

    def fake_post(*, url, headers, json):
        authorization_headers.append(headers["Authorization"])
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(exporter, "_client", SimpleNamespace(post=fake_post))
    return authorization_headers


def _export_keyless_item(exporter: BackendSpanExporter) -> None:
    exporter.export(cast(list[Trace | Span[Any]], [_KeylessItem()]))


def test_exporter_uses_env_api_key_set_after_an_export_without_one(monkeypatch):
    """A key missing at the first export must not disable tracing for the rest of the process."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    exporter = BackendSpanExporter()
    authorization_headers = _record_authorization_headers(monkeypatch, exporter)

    _export_keyless_item(exporter)
    assert authorization_headers == []

    monkeypatch.setenv("OPENAI_API_KEY", "sk-set-later")
    _export_keyless_item(exporter)

    assert authorization_headers == ["Bearer sk-set-later"]
    assert exporter.api_key == "sk-set-later"


def test_exporter_keeps_env_api_key_once_resolved(monkeypatch):
    """Changing the variable later must not reroute exports that did not call `set_api_key`."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-first")
    exporter = BackendSpanExporter()
    authorization_headers = _record_authorization_headers(monkeypatch, exporter)

    _export_keyless_item(exporter)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-rotated")
    _export_keyless_item(exporter)

    assert authorization_headers == ["Bearer sk-first", "Bearer sk-first"]


def test_exporter_uses_an_assigned_api_key(monkeypatch):
    """Assigning `api_key` directly must keep working as it did with the cached property."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    exporter = BackendSpanExporter()
    authorization_headers = _record_authorization_headers(monkeypatch, exporter)

    exporter.api_key = "sk-assigned"
    _export_keyless_item(exporter)

    assert authorization_headers == ["Bearer sk-assigned"]


def test_keyless_lookup_does_not_discard_a_key_set_while_it_runs(monkeypatch):
    """`set_api_key` can run on another thread while the export worker looks up the variable."""
    exporter = BackendSpanExporter()

    class EnvironmentWithoutKey:
        def get(self, name: str) -> str | None:
            exporter.set_api_key("sk-explicit")
            return None

    monkeypatch.setattr(processors, "os", SimpleNamespace(environ=EnvironmentWithoutKey()))

    assert exporter.api_key in (None, "sk-explicit")
    assert exporter.api_key == "sk-explicit"
