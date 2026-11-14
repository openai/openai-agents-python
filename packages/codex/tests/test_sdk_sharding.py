from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from .conftest import _shard_collection, pytest_collection_modifyitems


@dataclass(frozen=True)
class _Node:
    nodeid: str
    serial: bool = False

    def get_closest_marker(self, name: str):
        return self if name == "serial" and self.serial else None


def _collect(nodes: list[_Node]) -> tuple[list[_Node], list[_Node]]:
    selected, deselected = list(nodes), []
    config = SimpleNamespace(
        hook=SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items))
    )
    _shard_collection(config, selected)
    return selected, deselected


@pytest.mark.parametrize("count", [2, 4])
def test_shards_partition_parallel_nodes_and_retain_serial_nodes(monkeypatch, count: int) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    parallel = [_Node(f"test_example.py::test_case[{i}]") for i in range(80)]
    serial = [_Node(f"test_example.py::test_serial[{i}]", serial=True) for i in range(5)]
    partitions = []
    for index in range(count):
        monkeypatch.setenv("SDK_TEST_SHARD_INDEX", str(index))
        monkeypatch.setenv("SDK_TEST_SHARD_COUNT", str(count))
        selected, deselected = _collect(parallel + serial)
        assert set(serial) <= set(selected)
        assert set(selected).isdisjoint(deselected)
        assert set(selected + deselected) == set(parallel + serial)
        partitions.append(set(selected) - set(serial))
        assert set(_collect(list(reversed(parallel + serial)))[0]) == set(selected)
    assert set().union(*partitions) == set(parallel)
    assert sum(map(len, partitions)) == len(parallel)


@pytest.mark.parametrize("nested", [False, True])
def test_sharding_is_inactive_without_controls_or_in_nested_pytest(
    monkeypatch, nested: bool
) -> None:
    for name in ("PYTEST_CURRENT_TEST", "SDK_TEST_SHARD_INDEX", "SDK_TEST_SHARD_COUNT"):
        monkeypatch.delenv(name, raising=False)
    if nested:
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "outer.py::test_case (call)")
        monkeypatch.setenv("SDK_TEST_SHARD_INDEX", "0")
        monkeypatch.setenv("SDK_TEST_SHARD_COUNT", "3")
    nodes = [_Node(f"example::{i}") for i in range(80)]
    assert _collect(nodes) == (nodes, [])


@pytest.mark.parametrize(
    ("index", "count"),
    [(None, "2"), ("0", None), ("0", "0"), ("2", "2"), ("-1", "2"), ("０", "2"), ("0", "bad")],
)
def test_invalid_shard_configuration_fails_before_collection(monkeypatch, index, count) -> None:
    for name, value in (("SDK_TEST_SHARD_INDEX", index), ("SDK_TEST_SHARD_COUNT", count)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    with pytest.raises(pytest.UsageError, match="Set valid SDK_TEST_SHARD"):
        _collect([_Node("example::case")])


@pytest.mark.parametrize("count", [2, 4])
def test_actual_collection_hook_applies_the_partition(monkeypatch, count: int) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("SDK_TEST_SHARD_INDEX", "0")
    monkeypatch.setenv("SDK_TEST_SHARD_COUNT", str(count))
    nodes = [_Node(f"example::case[{i}]") for i in range(80)]
    expected, _ = _collect(nodes)
    assert 0 < len(expected) < len(nodes)
    config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda items: None))
    pytest_collection_modifyitems(config, nodes)
    assert nodes == expected


def test_macos_shards_preserve_qualification_serial_scope_and_required_check() -> None:
    root = Path(__file__).resolve().parents[3]
    jobs = yaml.load((root / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]
    source, gate = jobs["native-macos-sandbox-shards"], jobs["native-macos-sandbox"]
    assert source["needs"] == "native-macos"
    assert source["if"] == "${{ !inputs.native_only }}"
    shards = ["0", "1", "2", "3"]
    assert source["strategy"] == {"fail-fast": "false", "matrix": {"shard": shards}}
    assert "'macos-15'" in source["runs-on"]
    assert gate["needs"] == ["native-macos", "native-macos-sandbox-shards"]
    assert "name" not in gate
    assert gate["if"] == (
        "${{ !inputs.native_only && always() && needs.native-macos.result == 'success' }}"
    )
    assert gate["steps"][0]["env"] == {"RESULT": "${{ needs.native-macos-sandbox-shards.result }}"}
    phase = next(
        step for step in source["steps"] if step["name"] == "Run native SDK tests on macOS"
    )
    assert phase["timeout-minutes"] == "35"
    assert phase["shell"] == "bash"
    assert phase["env"]["MACOS_TEST_SHARD"] == "${{ matrix.shard }}"
    assert not any(key.startswith("SDK_TEST_SHARD") for key in source["env"] | phase["env"])
    assert phase["run"] == (
        'SDK_TEST_SHARD_INDEX="$MACOS_TEST_SHARD" '
        f"SDK_TEST_SHARD_COUNT={len(shards)} make tests-parallel\n"
        'if [[ "$MACOS_TEST_SHARD" == 0 ]]; then\n'
        "  make tests-serial\n"
        "fi\n"
    )
    assert gate["steps"][0]["run"] == 'test "$RESULT" = success'
