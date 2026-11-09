"""Public descriptor declaration stays pure in a fresh interpreter."""

import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("response_mode", ["streaming", "buffered"])
def test_provider_dataclass_declaration_does_not_load_native(response_mode):
    code = """
    import sys
    from copy import copy, deepcopy
    from dataclasses import asdict, fields, replace
    from agents import Agent, NativeProvider
    provider = NativeProvider(
        base_url="https://example.invalid/v1", api_key="synthetic", supports_websockets=False,
        response_mode=sys.argv[1],
    )
    expected = {
        "base_url": "https://example.invalid/v1",
        "api_key": "synthetic",
        "supports_websockets": False,
        "auth": None,
        "response_mode": sys.argv[1],
        "query_params": None,
        "headers": None,
        "env_headers": None,
        "organization": None,
        "project": None,
        "model_backend": None,
        "request_max_retries": None,
        "stream_max_retries": None,
        "stream_idle_timeout_ms": None,
        "websocket_connect_timeout_ms": None,
    }
    assert [field.name for field in fields(provider)] == list(expected)
    assert asdict(provider) == expected
    assert list(asdict(provider)) == list(expected)
    assert repr(provider) == (
        f"NativeProvider(supports_websockets=False, response_mode={provider.response_mode!r})"
    )
    for copied in (copy(provider), deepcopy(provider), replace(provider)):
        assert copied is not provider
        assert copied == provider
        assert hash(copied) == hash(provider)
        assert asdict(copied) == expected
        assert Agent(instructions="unchanged", provider=copied).provider is copied
        assert vars(copied).get("_transport") is None
        assert "_owner_lock" not in vars(copied)

    assert "agents._native" not in sys.modules
    """
    subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), response_mode],
        env=dict(os.environ),
        check=True,
    )


@pytest.mark.parametrize(
    "name",
    [
        "request_max_retries",
        "stream_max_retries",
        "stream_idle_timeout_ms",
        "websocket_connect_timeout_ms",
    ],
)
@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_provider_retry_controls_reject_invalid_values(name, value):
    from agents import NativeProvider

    with pytest.raises(ValueError, match="unsigned 64-bit"):
        NativeProvider(**{name: value})
