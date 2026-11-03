"""Public descriptor declaration stays pure in a fresh interpreter."""

import os
import subprocess
import sys
import textwrap


def test_provider_dataclass_declaration_does_not_load_native():
    code = """
    import sys
    from copy import copy, deepcopy
    from dataclasses import asdict, fields, replace
    from agents import Agent, NativeProvider
    provider = NativeProvider(
        base_url="https://example.invalid/v1", api_key="synthetic", supports_websockets=False
    )
    expected = {
        "base_url": "https://example.invalid/v1",
        "api_key": "synthetic",
        "supports_websockets": False,
    }
    assert [field.name for field in fields(provider)] == list(expected)
    assert asdict(provider) == expected
    assert repr(provider) == "NativeProvider(supports_websockets=False)"
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
    subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=dict(os.environ), check=True)
