from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import cast

from typing_extensions import Self

DependencyKey = str


class DependenciesError(RuntimeError):
    pass


class DependenciesBindingError(DependenciesError, ValueError):
    pass


class DependenciesMissingDependencyError(DependenciesError, LookupError):
    pass


FactoryFn = Callable[["Dependencies"], object | Awaitable[object]]


@dataclass(slots=True)
class _ValueBinding:
    value: object


@dataclass(slots=True)
class _FactoryBinding:
    factory: FactoryFn
    cache: bool
    owns_result: bool


_Binding = _ValueBinding | _FactoryBinding


async def _close_best_effort(value: object) -> None:
    close = getattr(value, "aclose", None)
    if close is not None:
        try:
            result = close()
            if inspect.isawaitable(result):
                await cast(Awaitable[object], result)
            return
        except Exception:
            return

    close = getattr(value, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await cast(Awaitable[object], result)
    except Exception:
        return


class Dependencies:
    """Session-scoped dependency container for manifest entry materialization.

    Sandbox clients hold a configured template of bindings and clone it for each created or resumed
    session. That gives each session its own cache and owned-resource lifecycle while still letting
    callers register shared runtime-only objects such as service clients or lazy factories.
    """

    def __init__(self) -> None:
        self._bindings: dict[DependencyKey, _Binding] = {}
        self._cache: dict[DependencyKey, object] = {}
        self._pending: dict[DependencyKey, asyncio.Task[object]] = {}
        self._active_tasks: set[asyncio.Task[object]] = set()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._owned_results: list[object] = []
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False

    @classmethod
    def with_values(
        cls,
        values: Mapping[DependencyKey, object],
    ) -> Dependencies:
        dependencies = cls()
        for key, value in values.items():
            dependencies.bind_value(key, value)
        return dependencies

    def bind_value(
        self,
        key: DependencyKey,
        value: object,
        *,
        overwrite: bool = False,
    ) -> Self:
        if not key:
            raise ValueError("Dependency key must be non-empty")
        self._bind(key, _ValueBinding(value=value), overwrite=overwrite)
        return self

    def clone(self) -> Dependencies:
        cloned = Dependencies()
        for key, binding in self._bindings.items():
            if isinstance(binding, _ValueBinding):
                cloned._bindings[key] = _ValueBinding(value=binding.value)
            else:
                cloned._bindings[key] = _FactoryBinding(
                    factory=binding.factory,
                    cache=binding.cache,
                    owns_result=binding.owns_result,
                )
        return cloned

    def bind_factory(
        self,
        key: DependencyKey,
        factory: FactoryFn,
        *,
        cache: bool = True,
        overwrite: bool = False,
        owns_result: bool = False,
    ) -> Self:
        if not key:
            raise ValueError("Dependency key must be non-empty")
        self._bind(
            key,
            _FactoryBinding(
                factory=factory,
                cache=cache,
                owns_result=owns_result,
            ),
            overwrite=overwrite,
        )
        return self

    def _bind(
        self,
        key: DependencyKey,
        binding: _Binding,
        *,
        overwrite: bool,
    ) -> None:
        if not overwrite and key in self._bindings:
            raise DependenciesBindingError(f"Dependency `{key}` is already bound")
        self._bindings[key] = binding
        self._cache.pop(key, None)
        pending = self._pending.pop(key, None)
        if pending is not None:
            pending.cancel()

    async def get(self, key: DependencyKey) -> object | None:
        binding = self._bindings.get(key)
        if binding is None:
            return None
        return await self._resolve(key, binding)

    async def require(
        self,
        key: DependencyKey,
        *,
        consumer: str | None = None,
    ) -> object:
        value = await self.get(key)
        if value is not None:
            return value

        consumer_part = f" for {consumer}" if consumer else ""
        raise DependenciesMissingDependencyError(
            f"Missing dependency `{key}`{consumer_part}. "
            "Bind it on a Dependencies instance and pass it as "
            "`dependencies=` when constructing the sandbox client."
        )

    async def _resolve(self, key: DependencyKey, binding: _Binding) -> object:
        if isinstance(binding, _ValueBinding):
            return binding.value

        assert isinstance(binding, _FactoryBinding)
        if self._closed:
            raise DependenciesError(f"Dependencies container is closed; cannot resolve `{key}`")
        if binding.cache and key in self._cache:
            return self._cache[key]

        if binding.cache:
            task = self._pending.get(key)
            if task is None:
                task = self._create_factory_task(key, binding)
                self._pending[key] = task
            return await asyncio.shield(task)

        task = self._create_factory_task(key, binding)
        return await task

    def _create_factory_task(
        self, key: DependencyKey, binding: _FactoryBinding
    ) -> asyncio.Task[object]:
        task = asyncio.create_task(self._run_factory(key, binding))
        self._active_tasks.add(task)
        task.add_done_callback(_consume_task_exception)
        return task

    async def _run_factory(self, key: DependencyKey, binding: _FactoryBinding) -> object:
        try:
            produced = binding.factory(self)
            value = (
                await cast(Awaitable[object], produced)
                if inspect.isawaitable(produced)
                else produced
            )

            if self._closed:
                if binding.owns_result:
                    await self._discard_factory_result(value)
                raise DependenciesError(f"Dependencies container closed while resolving `{key}`")

            if self._bindings.get(key) is not binding:
                if binding.owns_result:
                    await self._discard_factory_result(value)
                raise DependenciesBindingError(
                    f"Dependency `{key}` was rebound while its factory was resolving"
                )

            if binding.cache:
                self._cache[key] = value
            if binding.owns_result:
                self._owned_results.append(value)
            return value
        except asyncio.CancelledError:
            if self._closed:
                raise DependenciesError(
                    f"Dependencies container closed while resolving `{key}`"
                ) from None
            if self._bindings.get(key) is not binding:
                raise DependenciesBindingError(
                    f"Dependency `{key}` was rebound while its factory was resolving"
                ) from None
            raise
        finally:
            task = asyncio.current_task()
            if task is not None:
                self._active_tasks.discard(task)
                if self._pending.get(key) is task:
                    self._pending.pop(key, None)

    async def _discard_factory_result(self, value: object) -> None:
        task = asyncio.create_task(_close_best_effort(value))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        await asyncio.shield(task)

    async def aclose(self) -> None:
        task = self._close_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(self._close())
            self._close_task = task
        await asyncio.shield(task)

    async def _close(self) -> None:
        active_tasks = tuple(self._active_tasks)
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

        while self._cleanup_tasks:
            cleanup_tasks = tuple(self._cleanup_tasks)
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

        seen_ids: set[int] = set()
        for value in reversed(self._owned_results):
            value_id = id(value)
            if value_id in seen_ids:
                continue
            seen_ids.add(value_id)
            await _close_best_effort(value)

        self._pending.clear()
        self._cache.clear()
        self._owned_results.clear()


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()
