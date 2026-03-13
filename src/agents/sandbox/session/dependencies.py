from __future__ import annotations

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
        self._owned_results: list[object] = []
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
        if binding.cache and key in self._cache:
            return self._cache[key]

        produced = binding.factory(self)
        value = (
            await cast(Awaitable[object], produced) if inspect.isawaitable(produced) else produced
        )

        if binding.cache:
            self._cache[key] = value
        if binding.owns_result:
            self._owned_results.append(value)
        return value

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        seen_ids: set[int] = set()
        for value in reversed(self._owned_results):
            value_id = id(value)
            if value_id in seen_ids:
                continue
            seen_ids.add(value_id)
            await _close_best_effort(value)
