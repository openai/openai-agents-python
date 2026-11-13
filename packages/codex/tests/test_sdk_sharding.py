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


@pytest.mark.parametrize("count", [2, 3])
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


def test_platform_shards_keep_qualification_and_required_result_gates() -> None:
    root = Path(__file__).resolve().parents[3]
    jobs = yaml.load((root / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader)[
        "jobs"
    ]
    for name, qualifier, count in (
        ("native-macos-sandbox", "native-macos", 3),
        ("tests-windows", "native-windows", 2),
    ):
        source, gate = jobs[name + "-shards"], jobs[name]
        assert source["needs"] == jobs[qualifier]["needs"]
        assert qualifier not in source["needs"]
        assert source["if"] == "!inputs.native_only && " + jobs[qualifier]["if"]
        assert source["strategy"]["matrix"]["shard"] == list(map(str, range(count)))
        assert gate["needs"] == [qualifier, name + "-shards"]
        assert "always()" in gate["if"] and "!inputs.native_only" in gate["if"]
        assert "name" not in gate  # Preserve the original required check identities.
        assert gate["steps"][0]["run"] == 'test "$RESULT" = success && test "$QUALIFIED" = success'
        serial = next(
            s for s in source["steps"] if s.get("name") == "Run serial tests after all workers exit"
        )
        assert "matrix.shard == 0" in serial["if"]
        assert not any(k.startswith("SDK_TEST_SHARD") for k in serial.get("env", {}))
        parallel = next(s for s in source["steps"] if "SDK_TEST_SHARD_INDEX" in s.get("env", {}))
        assert source["steps"].index(parallel) < source["steps"].index(serial)
    assert jobs["tests-windows"]["strategy"]["matrix"]["python-version"] == ["3.10", "3.14"]


def test_actual_collection_hook_applies_the_partition(monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("SDK_TEST_SHARD_INDEX", "0")
    monkeypatch.setenv("SDK_TEST_SHARD_COUNT", "2")
    nodes = [_Node(f"example::case[{i}]") for i in range(80)]
    expected, _ = _collect(nodes)
    assert 0 < len(expected) < len(nodes)
    config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda items: None))
    pytest_collection_modifyitems(config, nodes)
    assert nodes == expected
