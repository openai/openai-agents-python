from __future__ import annotations

import abc
from typing import Any, ClassVar, Generic, TypeVar, cast

from pydantic import BaseModel, ConfigDict, model_serializer

from ..manifest import Manifest
from ..snapshot import SnapshotBase, SnapshotSpec
from .base_sandbox_session import BaseSandboxSession
from .dependencies import Dependencies
from .manager import Instrumentation
from .sandbox_session import SandboxSession
from .sandbox_session_state import SandboxSessionState

SandboxClientOptionsClass = type["BaseSandboxClientOptions"]
ClientOptionsT = TypeVar("ClientOptionsT")


class BaseSandboxClientOptions(BaseModel):
    """Polymorphic base for sandbox client options that need JSON round-trips."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    type: str
    _subclass_registry: ClassVar[dict[str, SandboxClientOptionsClass]] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if args:
            positional_fields = [name for name in type(self).model_fields if name != "type"]
            if len(args) > len(positional_fields):
                raise TypeError(
                    f"{type(self).__name__}() takes at most {len(positional_fields)} positional "
                    f"arguments but {len(args)} were given"
                )
            for field_name, value in zip(positional_fields, args, strict=False):
                if field_name in kwargs:
                    raise TypeError(
                        f"{type(self).__name__}() got multiple values for argument {field_name!r}"
                    )
                kwargs[field_name] = value
        super().__init__(**kwargs)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        super().__pydantic_init_subclass__(**kwargs)

        type_field = cls.model_fields.get("type")
        type_default = type_field.default if type_field is not None else None
        if not isinstance(type_default, str) or type_default == "":
            raise TypeError(f"{cls.__name__} must define a non-empty string default for `type`")

        existing = BaseSandboxClientOptions._subclass_registry.get(type_default)
        if (
            existing is not None
            and existing is not cls
            and (existing.__module__, existing.__qualname__) != (cls.__module__, cls.__qualname__)
        ):
            raise TypeError(
                f"sandbox client options type `{type_default}` is already registered by "
                f"{existing.__name__}"
            )
        if existing is not None:
            return
        BaseSandboxClientOptions._subclass_registry[type_default] = cls

    @classmethod
    def parse(cls, payload: object) -> BaseSandboxClientOptions:
        if isinstance(payload, BaseSandboxClientOptions):
            return payload

        if isinstance(payload, dict):
            options_type = payload.get("type")
            if isinstance(options_type, str):
                options_class = cls._options_class_for_type(options_type)
                if options_class is not None:
                    return options_class.model_validate(payload)

            raise ValueError(f"unknown sandbox client options type `{options_type}`")

        raise TypeError(
            "sandbox client options payload must be a BaseSandboxClientOptions or object payload"
        )

    @model_serializer(mode="wrap")
    def _serialize_always_include_type(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if isinstance(data, dict):
            data["type"] = self.type
        return cast(dict[str, Any], data)

    @classmethod
    def _options_class_for_type(
        cls,
        options_type: str,
    ) -> SandboxClientOptionsClass | None:
        return BaseSandboxClientOptions._subclass_registry.get(options_type)


class BaseSandboxClient(abc.ABC, Generic[ClientOptionsT]):
    backend_id: str
    supports_default_options: bool = False
    _dependencies: Dependencies | None = None

    def _resolve_dependencies(self) -> Dependencies | None:
        if self._dependencies is None:
            return None
        # Sessions get clones instead of the shared template so per-session factory caches and
        # owned resources do not leak across unrelated sandboxes.
        return self._dependencies.clone()

    def _wrap_session(
        self,
        inner: BaseSandboxSession,
        *,
        instrumentation: Instrumentation | None = None,
    ) -> SandboxSession:
        # Always return the instrumented wrapper so callers get consistent events and dependency
        # lifecycle handling regardless of which backend created the inner session.
        return SandboxSession(
            inner,
            instrumentation=instrumentation,
            dependencies=self._resolve_dependencies(),
        )

    @abc.abstractmethod
    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: ClientOptionsT,
    ) -> SandboxSession:
        """Create a new session.

        Args:
            snapshot: Snapshot or spec used to create a snapshot instance for
                the session. If omitted, the session uses a no-op snapshot.
            manifest: Optional manifest to materialize into the workspace when
                the session starts.
            options: Sandbox-specific settings. For example, Docker expects
                ``DockerSandboxClientOptions(image="...")``.
        Returns:
            A `SandboxSession` that can be entered with `async with` or closed explicitly with
            `await session.aclose()`.
        """

    @abc.abstractmethod
    async def delete(self, session: SandboxSession) -> SandboxSession:
        """Delete a session and release sandbox resources."""

    @abc.abstractmethod
    async def resume(
        self,
        state: SandboxSessionState,
    ) -> SandboxSession:
        """Resume an owning session from a previously persisted `SandboxSessionState`.

        Providers should first try to reattach to the backend sandbox identified
        by `state`. If that resource still exists, including after unclean
        process/client shutdown where `delete()` was never called, the returned
        session should target the same backend sandbox and be able to clean it
        up later.

        If the original backend sandbox is unavailable, providers may create a
        replacement and should hydrate its workspace from `state.snapshot`
        during `SandboxSession.start()`.

        The returned session owns its provider lifecycle; pass a live
        `session=` when you want to reuse an already-running sandbox session.
        """

    def serialize_session_state(self, state: SandboxSessionState) -> dict[str, object]:
        """Serialize backend-specific sandbox state into a JSON-compatible payload."""
        return state.model_dump(mode="json")

    @abc.abstractmethod
    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        """Deserialize backend-specific sandbox state from a JSON-compatible payload."""
