from __future__ import annotations

from unittest.mock import patch

import pytest

from agents._httpx_compat import (
    _load_legacy_httpx,
    is_legacy_httpx_instance,
    legacy_httpx_types,
    require_legacy_httpx,
)


def test_is_legacy_httpx_instance_is_false_before_httpx_import() -> None:
    import sys

    with patch.dict(sys.modules):
        sys.modules.pop("httpx", None)
        assert is_legacy_httpx_instance(object(), "Client") is False
        assert "httpx" not in sys.modules


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ModuleNotFoundError("No module named 'httpx'", name="httpx"), None),
        (ModuleNotFoundError("No module named 'other'", name="other"), "other"),
        (ImportError("httpx is broken"), "httpx is broken"),
    ],
    ids=["httpx-missing", "other-module-missing", "httpx-import-broken"],
)
def test_load_legacy_httpx_degrades_only_for_missing_httpx(
    error: Exception, expected: str | None
) -> None:
    _load_legacy_httpx.cache_clear()
    try:
        if expected is None:
            with patch("agents._httpx_compat.import_module", side_effect=error):
                assert _load_legacy_httpx() is None
            assert legacy_httpx_types("Client") == ()
        else:
            with (
                patch("agents._httpx_compat.import_module", side_effect=error),
                pytest.raises(Exception, match=expected),
            ):
                _load_legacy_httpx()
    finally:
        _load_legacy_httpx.cache_clear()


def test_require_legacy_httpx_raises_when_httpx_is_missing() -> None:
    _load_legacy_httpx.cache_clear()
    try:
        with patch(
            "agents._httpx_compat.import_module",
            side_effect=ModuleNotFoundError("No module named 'httpx'", name="httpx"),
        ):
            with pytest.raises(
                ImportError, match="installed integration requires the legacy httpx"
            ):
                require_legacy_httpx()
    finally:
        _load_legacy_httpx.cache_clear()


def test_require_legacy_httpx_returns_httpx_module() -> None:
    _load_legacy_httpx.cache_clear()
    try:
        httpx = require_legacy_httpx()
        assert httpx.__name__ == "httpx"
        client_types = legacy_httpx_types("Client")
        assert len(client_types) == 1
        assert client_types[0].__name__ == "Client"
        assert is_legacy_httpx_instance(httpx.Client(), "Client") is True
        assert is_legacy_httpx_instance(object(), "Client") is False
    finally:
        _load_legacy_httpx.cache_clear()
