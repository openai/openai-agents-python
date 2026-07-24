from __future__ import annotations

import pytest

from .conftest import (
    _PROXY_ENVIRONMENT_VARIABLES,
    _PROXY_OPT_IN_ENVIRONMENT_VARIABLE,
    _remove_ambient_proxy_environment,
)


def test_remove_ambient_proxy_environment_clears_proxy_variables() -> None:
    environment = {
        variable: "socks5h://127.0.0.1:1234" for variable in _PROXY_ENVIRONMENT_VARIABLES
    }
    environment["UNRELATED"] = "preserved"

    _remove_ambient_proxy_environment(environment)

    assert all(variable not in environment for variable in _PROXY_ENVIRONMENT_VARIABLES)
    assert environment["UNRELATED"] == "preserved"


@pytest.mark.parametrize("opt_in", ["1", "true", "TRUE", "yes", "YES"])
def test_remove_ambient_proxy_environment_preserves_proxy_variables_when_opted_in(
    opt_in: str,
) -> None:
    environment = {
        _PROXY_OPT_IN_ENVIRONMENT_VARIABLE: opt_in,
        "ALL_PROXY": "socks5h://127.0.0.1:1234",
    }

    _remove_ambient_proxy_environment(environment)

    assert environment["ALL_PROXY"] == "socks5h://127.0.0.1:1234"
