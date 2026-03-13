from __future__ import annotations

import pytest

from agents.sandbox.session import (
    Dependencies,
    DependenciesBindingError,
    DependenciesMissingDependencyError,
)


class _AsyncClosable:
    def __init__(self) -> None:
        self.calls = 0

    async def aclose(self) -> None:
        self.calls += 1


class _AsyncCloseMethod:
    def __init__(self) -> None:
        self.calls = 0

    async def close(self) -> None:
        self.calls += 1


class _SyncClosable:
    def __init__(self) -> None:
        self.calls = 0

    def close(self) -> None:
        self.calls += 1


@pytest.mark.asyncio
async def test_dependencies_with_values_binds_multiple_values() -> None:
    key1 = "tests.with_values.str"
    key2 = "tests.with_values.int"
    dependencies = Dependencies.with_values({key1: "hello", key2: 123})

    assert await dependencies.require(key1) == "hello"
    assert await dependencies.require(key2) == 123


@pytest.mark.asyncio
async def test_dependencies_bind_value_and_require() -> None:
    dependencies = Dependencies()
    key = "tests.value"
    dependencies.bind_value(key, "hello")

    assert await dependencies.get(key) == "hello"
    assert await dependencies.require(key, consumer="test") == "hello"


@pytest.mark.asyncio
async def test_dependencies_missing_dependency_includes_key_and_consumer() -> None:
    dependencies = Dependencies()
    key = "tests.missing"

    with pytest.raises(DependenciesMissingDependencyError, match="tests.missing"):
        await dependencies.require(key, consumer="SedimentFile")


def test_dependencies_duplicate_binding_raises() -> None:
    dependencies = Dependencies()
    key = "tests.dup"
    dependencies.bind_value(key, "a")

    with pytest.raises(DependenciesBindingError, match="already bound"):
        dependencies.bind_value(key, "b")


def test_dependencies_empty_key_raises() -> None:
    dependencies = Dependencies()

    with pytest.raises(ValueError, match="non-empty"):
        dependencies.bind_value("", "x")

    with pytest.raises(ValueError, match="non-empty"):
        dependencies.bind_factory("", lambda _dependencies: "x")


@pytest.mark.asyncio
async def test_dependencies_cached_factory_resolves_once() -> None:
    dependencies = Dependencies()
    key = "tests.cached_factory"
    calls = 0

    def _factory(_dependencies: Dependencies) -> str:
        nonlocal calls
        calls += 1
        return f"value-{calls}"

    dependencies.bind_factory(key, _factory, cache=True)

    assert await dependencies.require(key) == "value-1"
    assert await dependencies.require(key) == "value-1"
    assert calls == 1


@pytest.mark.asyncio
async def test_dependencies_uncached_factory_resolves_every_time() -> None:
    dependencies = Dependencies()
    key = "tests.uncached_factory"
    calls = 0

    def _factory(_dependencies: Dependencies) -> str:
        nonlocal calls
        calls += 1
        return f"value-{calls}"

    dependencies.bind_factory(key, _factory, cache=False)

    assert await dependencies.require(key) == "value-1"
    assert await dependencies.require(key) == "value-2"
    assert calls == 2


@pytest.mark.asyncio
async def test_dependencies_async_factory_supported() -> None:
    dependencies = Dependencies()
    key = "tests.async_factory"

    async def _factory(_dependencies: Dependencies) -> str:
        return "async-value"

    dependencies.bind_factory(key, _factory)
    assert await dependencies.require(key) == "async-value"


@pytest.mark.asyncio
async def test_dependencies_aclose_closes_owned_results_and_is_idempotent() -> None:
    dependencies = Dependencies()
    k1 = "tests.async_aclose"
    k2 = "tests.async_close"
    k3 = "tests.sync_close"

    dependencies.bind_factory(k1, lambda _deps: _AsyncClosable(), owns_result=True)
    dependencies.bind_factory(k2, lambda _deps: _AsyncCloseMethod(), owns_result=True)
    dependencies.bind_factory(k3, lambda _deps: _SyncClosable(), owns_result=True, cache=False)

    v1 = await dependencies.require(k1)
    v2 = await dependencies.require(k2)
    v3a = await dependencies.require(k3)
    v3b = await dependencies.require(k3)

    assert v3a is not v3b

    await dependencies.aclose()
    await dependencies.aclose()

    assert isinstance(v1, _AsyncClosable) and v1.calls == 1
    assert isinstance(v2, _AsyncCloseMethod) and v2.calls == 1
    assert isinstance(v3a, _SyncClosable) and v3a.calls == 1
    assert isinstance(v3b, _SyncClosable) and v3b.calls == 1


@pytest.mark.asyncio
async def test_dependencies_bound_values_are_not_closed() -> None:
    dependencies = Dependencies()
    key = "tests.bound_value"
    value = _SyncClosable()
    dependencies.bind_value(key, value)

    _ = await dependencies.require(key)
    await dependencies.aclose()

    assert value.calls == 0
