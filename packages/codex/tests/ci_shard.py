"""Split macOS CI's parallel pytest cases across two runners."""

from hashlib import sha256

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--ci-shard", type=int, choices=(0, 1), required=True)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = config.getoption("--ci-shard")
    selected = []
    deselected = []
    for item in items:
        # A stable hash also distributes expensive cases from one parametrized module.
        if sha256(item.nodeid.encode("utf-8")).digest()[0] % 2 == shard:
            selected.append(item)
        else:
            deselected.append(item)
    items[:] = selected
    if deselected:
        config.hook.pytest_deselected(items=deselected)
