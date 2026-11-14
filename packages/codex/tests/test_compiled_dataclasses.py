import dataclasses
import inspect
import py_compile
import sys
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import pytest

from _agents_codex import _compiled_dataclasses as compiled


@pytest.mark.skipif(
    compiled.driver_fingerprint() is None, reason="compiled factories require supported CPython"
)
def test_compiled_method_factory_uses_current_fields_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factories = {}

    def record(source, namespace, result):
        digest = sha256(source.encode()).digest()
        exec(source, namespace, result)
        factories[digest] = result["__create_fn__"]

    class First:
        values: list[int] = dataclasses.field(default_factory=lambda: [1])

    with monkeypatch.context() as capture:
        capture.setattr(dataclasses, "exec", record, raising=False)
        stock = dataclasses.dataclass(First, frozen=True, slots=True)
    assert stock().values == [1]

    class Second:
        values: list[int] = dataclasses.field(default_factory=lambda: [2])

    class FactoryHits(dict):
        hits = 0

        def get(self, key, default=None):
            result = super().get(key, default)
            if result is not None:
                self.hits += 1
            return result

    recorded = FactoryHits(factories)
    candidate = compiled._scoped_dataclass(recorded)(Second, frozen=True, slots=True)
    assert recorded.hits > 0
    first, second = candidate(), candidate()
    assert first.values == second.values == [2]
    assert first.values is not second.values
    assert dataclasses.replace(first, values=[3]).values == [3]
    assert inspect.signature(candidate).parameters["values"].default is (
        inspect.signature(stock).parameters["values"].default
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.values = [4]
    assert "exec" not in vars(dataclasses)


def test_unrecognized_factory_archive_keeps_stock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fingerprint = compiled.driver_fingerprint()
    if fingerprint is None or sys.flags.optimize or sys._xoptions:
        pytest.skip("requires an ordinary supported CPython process")
    name = f"_dataclass_factories_3{sys.version_info.minor}"
    monkeypatch.setattr(compiled, "__file__", str(tmp_path / "_compiled_dataclasses.py"))
    assert compiled._load_factories() == {}

    source = tmp_path / "_dataclass_factories.py"
    key = b"x" * 32
    source.write_text(f"FACTORIES = {{{key!r}: lambda: ()}}\n")
    cached = py_compile.compile(
        str(source),
        dfile="_agents_codex/_dataclass_factories.py",
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    )
    with ZipFile(tmp_path / f"{name}-mismatch.zip", "w") as archive:
        archive.write(source, source.name)
        archive.write(cached, "_dataclass_factories.pyc")
    assert compiled._load_factories() == {}

    (tmp_path / f"{name}-mismatch.zip").rename(tmp_path / f"{name}-{fingerprint}.zip")
    assert compiled._load_factories()[key]() == ()
