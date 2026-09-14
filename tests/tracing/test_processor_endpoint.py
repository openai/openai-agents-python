from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast

import agents.tracing.processors as processors
from agents.tracing.processors import BackendSpanExporter, _redact_url_for_log
from agents.tracing.spans import Span
from agents.tracing.traces import Trace

DEFAULT_ENDPOINT = BackendSpanExporter._OPENAI_TRACING_INGEST_ENDPOINT
CUSTOM_ENDPOINT = "https://traces.example.test/v1/traces/ingest"
MODEL_BASE = "https://gateway.example.test/v1"


def _reset_warning(monkeypatch) -> None:
    monkeypatch.setattr(processors, "_warned_default_trace_endpoint_with_custom_model_base", False)


def _export_once(monkeypatch, exporter: BackendSpanExporter | None = None) -> BackendSpanExporter:
    class DummyItem:
        tracing_api_key = None

        def export(self) -> dict[str, str]:
            return {"id": "span-1"}

    def fake_post(*, url, headers, json):
        return SimpleNamespace(status_code=200, text="ok")

    exporter = exporter or BackendSpanExporter()
    exporter.set_api_key("test-key")
    monkeypatch.setattr(exporter, "_client", SimpleNamespace(post=fake_post))
    exporter.export(cast(list[Trace | Span[Any]], [DummyItem()]))
    return exporter


def test_endpoint_defaults_to_openai_ingest(monkeypatch):
    monkeypatch.delenv("OPENAI_TRACING_INGEST_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _reset_warning(monkeypatch)

    exporter = BackendSpanExporter()

    assert exporter.endpoint == DEFAULT_ENDPOINT


def test_endpoint_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_TRACING_INGEST_ENDPOINT", CUSTOM_ENDPOINT)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _reset_warning(monkeypatch)

    exporter = BackendSpanExporter()

    assert exporter.endpoint == CUSTOM_ENDPOINT


def test_constructor_endpoint_wins_over_env(monkeypatch):
    monkeypatch.setenv("OPENAI_TRACING_INGEST_ENDPOINT", CUSTOM_ENDPOINT)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _reset_warning(monkeypatch)

    exporter = BackendSpanExporter(endpoint="https://explicit.example.test/ingest")

    assert exporter.endpoint == "https://explicit.example.test/ingest"


def test_export_posts_to_env_endpoint(monkeypatch):
    monkeypatch.setenv("OPENAI_TRACING_INGEST_ENDPOINT", CUSTOM_ENDPOINT)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _reset_warning(monkeypatch)

    class DummyItem:
        tracing_api_key = None

        def export(self) -> dict[str, str]:
            return {"id": "span-1"}

    calls: list[dict[str, Any]] = []

    def fake_post(*, url, headers, json):
        calls.append({"url": url, "headers": headers, "json": json})
        return SimpleNamespace(status_code=200, text="ok")

    exporter = BackendSpanExporter()
    exporter.set_api_key("test-key")
    monkeypatch.setattr(exporter, "_client", SimpleNamespace(post=fake_post))
    exporter.export(cast(list[Trace | Span[Any]], [DummyItem()]))

    assert len(calls) == 1
    assert calls[0]["url"] == CUSTOM_ENDPOINT


def test_constructor_does_not_warn(monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_BASE_URL", MODEL_BASE)
    monkeypatch.delenv("OPENAI_TRACING_INGEST_ENDPOINT", raising=False)
    _reset_warning(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        BackendSpanExporter()
        BackendSpanExporter()

    assert not [record for record in caplog.records if "Tracing still exports" in record.message]


def test_warns_once_when_model_base_url_diverges(monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_BASE_URL", MODEL_BASE)
    monkeypatch.delenv("OPENAI_TRACING_INGEST_ENDPOINT", raising=False)
    _reset_warning(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        _export_once(monkeypatch)
        _export_once(monkeypatch)

    warnings = [
        record.message for record in caplog.records if "Tracing still exports" in record.message
    ]
    assert len(warnings) == 1
    assert DEFAULT_ENDPOINT in warnings[0]
    assert MODEL_BASE in warnings[0]
    assert "OPENAI_TRACING_INGEST_ENDPOINT" in warnings[0]


def test_no_warning_until_a_trace_can_be_sent(monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_BASE_URL", MODEL_BASE)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_TRACING_INGEST_ENDPOINT", raising=False)
    _reset_warning(monkeypatch)

    class DummyItem:
        tracing_api_key = None

        def export(self) -> dict[str, str]:
            return {"id": "span-1"}

    def fake_post(*, url, headers, json):
        return SimpleNamespace(status_code=200, text="ok")

    exporter = BackendSpanExporter()
    monkeypatch.setattr(exporter, "_client", SimpleNamespace(post=fake_post))

    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        exporter.export(cast(list[Trace | Span[Any]], [DummyItem()]))

    assert not [record for record in caplog.records if "Tracing still exports" in record.message]

    exporter.set_api_key("test-key")
    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        exporter.export(cast(list[Trace | Span[Any]], [DummyItem()]))

    warnings = [
        record.message for record in caplog.records if "Tracing still exports" in record.message
    ]
    assert len(warnings) == 1
    assert MODEL_BASE in warnings[0]


def test_no_warning_when_only_openai_api_base_is_set(monkeypatch, caplog):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_BASE", MODEL_BASE)
    monkeypatch.delenv("OPENAI_TRACING_INGEST_ENDPOINT", raising=False)
    _reset_warning(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        _export_once(monkeypatch)

    assert not [record for record in caplog.records if "Tracing still exports" in record.message]


def test_warning_redacts_credentials_in_logged_urls(monkeypatch, caplog):
    secret_base = "https://user:s3cret@gateway.example.test/v1?token=signed"
    monkeypatch.setenv("OPENAI_BASE_URL", secret_base)
    monkeypatch.delenv("OPENAI_TRACING_INGEST_ENDPOINT", raising=False)
    _reset_warning(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        _export_once(monkeypatch)

    warnings = [
        record.message for record in caplog.records if "Tracing still exports" in record.message
    ]
    assert len(warnings) == 1
    assert "user:s3cret" not in warnings[0]
    assert "token=signed" not in warnings[0]
    assert "https://gateway.example.test/v1" in warnings[0]
    assert _redact_url_for_log(secret_base) in warnings[0]


def test_no_warning_when_tracing_endpoint_is_custom(monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_BASE_URL", MODEL_BASE)
    monkeypatch.setenv("OPENAI_TRACING_INGEST_ENDPOINT", CUSTOM_ENDPOINT)
    _reset_warning(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        _export_once(monkeypatch)

    assert not [record for record in caplog.records if "Tracing still exports" in record.message]


def test_no_warning_when_tracing_is_disabled(monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_BASE_URL", MODEL_BASE)
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    monkeypatch.delenv("OPENAI_TRACING_INGEST_ENDPOINT", raising=False)
    _reset_warning(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="openai.agents"):
        BackendSpanExporter()

    assert not [record for record in caplog.records if "Tracing still exports" in record.message]


def test_redact_url_for_log_strips_userinfo_query_and_fragment():
    assert (
        _redact_url_for_log("https://user:pass@api.example.test:8443/v1/traces?sig=abc#frag")
        == "https://api.example.test:8443/v1/traces"
    )
