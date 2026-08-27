from __future__ import annotations

import importlib
import ssl
import sys
import types
import urllib.request
from pathlib import Path
from typing import Any

from examples.sandbox.extensions.daytona.usaspending_text2sql import setup_db


def test_paths_use_examples_artifacts_dir_when_set(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("EXAMPLES_ARTIFACTS_DIR", str(tmp_path))
    reloaded = importlib.reload(setup_db)

    try:
        assert reloaded.DB_PATH == tmp_path / "data" / "usaspending.db"
        assert reloaded.GLOSSARY_PATH == tmp_path / "schema" / "glossary.md"
    finally:
        monkeypatch.delenv("EXAMPLES_ARTIFACTS_DIR", raising=False)
        importlib.reload(setup_db)


def test_urlopen_ssl_context_uses_certifi_when_available(monkeypatch: Any) -> None:
    setup_db._urlopen_ssl_context.cache_clear()
    ssl_context = object()
    certifi = types.SimpleNamespace(where=lambda: "/tmp/certifi.pem")
    monkeypatch.setitem(sys.modules, "certifi", certifi)

    def fake_create_default_context(*, cafile: str) -> object:
        assert cafile == "/tmp/certifi.pem"
        return ssl_context

    monkeypatch.setattr(ssl, "create_default_context", fake_create_default_context)

    try:
        assert setup_db._urlopen_ssl_context() is ssl_context
    finally:
        setup_db._urlopen_ssl_context.cache_clear()


def test_urlopen_ssl_context_falls_back_without_certifi(monkeypatch: Any) -> None:
    setup_db._urlopen_ssl_context.cache_clear()
    monkeypatch.setitem(sys.modules, "certifi", None)

    def fail_create_default_context(**kwargs: object) -> object:
        raise AssertionError("stdlib-only fallback should not create a certifi SSL context")

    monkeypatch.setattr(ssl, "create_default_context", fail_create_default_context)

    try:
        assert setup_db._urlopen_ssl_context() is None
    finally:
        setup_db._urlopen_ssl_context.cache_clear()


def test_urlopen_with_retry_passes_optional_ssl_context(monkeypatch: Any) -> None:
    ssl_context = object()
    captured: dict[str, object] = {}

    class DummyResponse:
        def __enter__(self) -> DummyResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"ok"

    def fake_urlopen(
        req: urllib.request.Request, *, timeout: int, context: object | None
    ) -> DummyResponse:
        captured["req"] = req
        captured["timeout"] = timeout
        captured["context"] = context
        return DummyResponse()

    monkeypatch.setattr(setup_db, "_urlopen_ssl_context", lambda: ssl_context)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    req = urllib.request.Request("https://api.usaspending.gov")

    assert setup_db._urlopen_with_retry(req, timeout=12, retries=1) == b"ok"
    assert captured == {"req": req, "timeout": 12, "context": ssl_context}
