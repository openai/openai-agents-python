from __future__ import annotations

import asyncio

import pytest

from agents.sandbox.session import (
    Dependencies,
    DependenciesBindingError,
    DependenciesError,
    DependenciesMissingDependencyError,
)

_EAGER_TASK_FACTORY = getattr(asyncio, "eager_task_factory", None)


class _AsyncClosable:
    def __init__(self) -> None:
        self.calls = 0

    async def aclose(self) -> None:
        self.calls += 1


class _BlockingAsyncClosable:
    def __init__(self) -> None:
        self.calls = 0
        self.completed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def aclose(self) -> None:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        self.completed = True


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
async def test_dependencies_cached_factory_resolves_once_concurrently() -> None:
    dependencies = Dependencies()
    key = "tests.concurrent_cached_factory"
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _factory(_dependencies: Dependencies) -> _AsyncClosable:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _AsyncClosable()

    dependencies.bind_factory(key, _factory, cache=True, owns_result=True)
    tasks = [asyncio.create_task(dependencies.require(key)) for _ in range(3)]

    await started.wait()
    release.set()
    values = await asyncio.gather(*tasks)

    assert calls == 1
    assert values[0] is values[1] is values[2]

    await dependencies.aclose()
    assert isinstance(values[0], _AsyncClosable)
    assert values[0].calls == 1


@pytest.mark.asyncio
async def test_dependencies_cached_factory_survives_waiter_cancellation() -> None:
    dependencies = Dependencies()
    key = "tests.cancelled_waiter"
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _factory(_dependencies: Dependencies) -> object:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return object()

    dependencies.bind_factory(key, _factory, cache=True)
    cancelled_waiter = asyncio.create_task(dependencies.require(key))
    surviving_waiter = asyncio.create_task(dependencies.require(key))

    await started.wait()
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    release.set()
    value = await surviving_waiter

    assert calls == 1
    assert await dependencies.require(key) is value


@pytest.mark.asyncio
async def test_dependencies_cached_factory_failure_allows_retry() -> None:
    dependencies = Dependencies()
    key = "tests.failed_factory_retry"
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _factory(_dependencies: Dependencies) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            raise RuntimeError("factory failed")
        return "recovered"

    dependencies.bind_factory(key, _factory, cache=True)
    first = asyncio.create_task(dependencies.require(key))
    second = asyncio.create_task(dependencies.require(key))

    await started.wait()
    release.set()

    for task in (first, second):
        with pytest.raises(RuntimeError, match="factory failed"):
            await task

    assert await dependencies.require(key) == "recovered"
    assert calls == 2


@pytest.mark.asyncio
async def test_dependencies_rebind_before_factory_starts_cleans_up_task() -> None:
    dependencies = Dependencies()
    key = "tests.rebind_before_start"
    factory_started = asyncio.Event()

    async def _factory(_dependencies: Dependencies) -> object:
        factory_started.set()
        return object()

    dependencies.bind_factory(key, _factory, cache=True)
    stale_resolve = asyncio.create_task(dependencies.require(key))

    def _rebind() -> None:
        dependencies.bind_factory(
            key, lambda _dependencies: "replacement", cache=True, overwrite=True
        )

    asyncio.get_running_loop().call_soon(_rebind)
    await asyncio.sleep(0)

    with pytest.raises(DependenciesBindingError, match="rebound"):
        await stale_resolve
    assert not factory_started.is_set()
    assert await dependencies.require(key) == "replacement"

    await asyncio.sleep(0)
    assert not dependencies._pending
    assert not dependencies._active_tasks


@pytest.mark.asyncio
@pytest.mark.skipif(_EAGER_TASK_FACTORY is None, reason="requires Python 3.12+")
async def test_dependencies_eager_factory_failure_allows_retry() -> None:
    dependencies = Dependencies()
    key = "tests.eager_failed_factory_retry"
    calls = 0

    async def _factory(_dependencies: Dependencies) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("factory failed")
        return "recovered"

    loop = asyncio.get_running_loop()
    previous_task_factory = loop.get_task_factory()
    loop.set_task_factory(_EAGER_TASK_FACTORY)
    try:
        dependencies.bind_factory(key, _factory, cache=True)
        with pytest.raises(RuntimeError, match="factory failed"):
            await dependencies.require(key)
        assert await dependencies.require(key) == "recovered"
    finally:
        loop.set_task_factory(previous_task_factory)

    assert calls == 2


@pytest.mark.asyncio
async def test_dependencies_rebind_preserves_aliased_stale_result_until_close() -> None:
    dependencies = Dependencies()
    key = "tests.rebind_aliased_result"
    started = asyncio.Event()
    value = _AsyncClosable()

    async def _factory(_dependencies: Dependencies) -> _AsyncClosable:
        started.set()
        try:
            await asyncio.Future()
            raise AssertionError("Unreachable")
        except asyncio.CancelledError:
            return value

    dependencies.bind_factory(key, _factory, cache=True, owns_result=True)
    stale_resolve = asyncio.create_task(dependencies.require(key))

    await started.wait()
    dependencies.bind_factory(
        key,
        lambda _dependencies: value,
        cache=True,
        overwrite=True,
        owns_result=True,
    )

    with pytest.raises(DependenciesBindingError, match="rebound"):
        await stale_resolve
    assert await dependencies.require(key) is value
    assert value.calls == 0

    await dependencies.aclose()
    assert value.calls == 1


@pytest.mark.asyncio
async def test_dependencies_close_cleans_up_owned_in_flight_result() -> None:
    dependencies = Dependencies()
    key = "tests.close_in_flight"
    started = asyncio.Event()
    produced: list[_AsyncClosable] = []

    async def _factory(_dependencies: Dependencies) -> _AsyncClosable:
        started.set()
        try:
            await asyncio.Future()
            raise AssertionError("Unreachable")
        except asyncio.CancelledError:
            value = _AsyncClosable()
            produced.append(value)
            return value

    dependencies.bind_factory(key, _factory, cache=True, owns_result=True)
    resolve_task = asyncio.create_task(dependencies.require(key))

    await started.wait()
    await dependencies.aclose()

    with pytest.raises(DependenciesError, match="closed"):
        await resolve_task
    assert len(produced) == 1
    assert produced[0].calls == 1


@pytest.mark.asyncio
async def test_dependencies_close_before_waiter_resumes_rejects_closed_result() -> None:
    dependencies = Dependencies()
    key = "tests.close_before_waiter_resumes"
    produced = asyncio.Event()
    value = _AsyncClosable()

    async def _factory(_dependencies: Dependencies) -> _AsyncClosable:
        produced.set()
        return value

    dependencies.bind_factory(key, _factory, cache=True, owns_result=True)
    resolve_task = asyncio.create_task(dependencies.require(key))

    await produced.wait()
    await dependencies.aclose()

    with pytest.raises(DependenciesError, match="closed"):
        await resolve_task
    assert value.calls == 1


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
async def test_dependencies_aclose_continues_after_waiter_cancellation() -> None:
    dependencies = Dependencies()
    key = "tests.cancelled_close"
    value = _BlockingAsyncClosable()
    dependencies.bind_factory(key, lambda _dependencies: value, owns_result=True)
    _ = await dependencies.require(key)

    close_waiter = asyncio.create_task(dependencies.aclose())
    await value.started.wait()
    close_waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await close_waiter

    value.release.set()
    await dependencies.aclose()
    assert value.calls == 1
    assert value.completed


@pytest.mark.asyncio
async def test_dependencies_bound_values_are_not_closed() -> None:
    dependencies = Dependencies()
    key = "tests.bound_value"
    value = _SyncClosable()
    dependencies.bind_value(key, value)

    _ = await dependencies.require(key)
    await dependencies.aclose()

    assert value.calls == 0
