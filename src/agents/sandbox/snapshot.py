import abc
import inspect
import io
import shutil
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from .errors import (
    SnapshotNotRestorableError,
    SnapshotPersistError,
    SnapshotRestoreError,
)
from .session.dependencies import Dependencies

SnapshotClass = type["SnapshotBase"]


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await cast(Awaitable[object], value)
    return value


class SnapshotBase(BaseModel, abc.ABC):
    model_config = ConfigDict(frozen=True)

    type: str
    id: str
    _subclass_registry: ClassVar[dict[str, SnapshotClass]] = {}

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        super().__pydantic_init_subclass__(**kwargs)

        type_field = cls.model_fields.get("type")
        type_default = type_field.default if type_field is not None else None
        if not isinstance(type_default, str) or type_default == "":
            raise TypeError(f"{cls.__name__} must define a non-empty string default for `type`")

        existing = SnapshotBase._subclass_registry.get(type_default)
        if existing is not None and existing is not cls:
            raise TypeError(
                f"snapshot type `{type_default}` is already registered by {existing.__name__}"
            )
        SnapshotBase._subclass_registry[type_default] = cls

    @classmethod
    def parse(cls, payload: object) -> "SnapshotBase":
        if isinstance(payload, SnapshotBase):
            return payload

        if isinstance(payload, dict):
            snapshot_type = payload.get("type")
            if isinstance(snapshot_type, str):
                snapshot_class = cls._snapshot_class_for_type(snapshot_type)
                if snapshot_class is not None:
                    return snapshot_class.model_validate(payload)

            raise ValueError(f"unknown snapshot type `{snapshot_type}`")

        raise TypeError("snapshot payload must be a SnapshotBase or object payload")

    @model_serializer(mode="wrap")
    def _serialize_always_include_type(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if isinstance(data, dict):
            data["type"] = self.type
        return cast(dict[str, Any], data)

    @classmethod
    def _snapshot_class_for_type(cls, snapshot_type: str) -> SnapshotClass | None:
        return SnapshotBase._subclass_registry.get(snapshot_type)

    @abc.abstractmethod
    async def persist(
        self, data: io.IOBase, *, dependencies: Dependencies | None = None
    ) -> None: ...

    @abc.abstractmethod
    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase: ...

    @abc.abstractmethod
    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool: ...


class LocalSnapshot(SnapshotBase):
    type: Literal["local"] = "local"

    base_path: Path

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        _ = dependencies
        path = self._path()
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("wb") as f:
                shutil.copyfileobj(data, f)
            temp_path.replace(path)
        except OSError as e:
            with suppress(OSError):
                temp_path.unlink()
            raise SnapshotPersistError(snapshot_id=self.id, path=path, cause=e) from e
        except BaseException:
            with suppress(OSError):
                temp_path.unlink()
            raise

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        path = self._path()
        try:
            return path.open("rb")
        except OSError as e:
            raise SnapshotRestoreError(snapshot_id=self.id, path=path, cause=e) from e

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return self._path().is_file()

    def _path(self) -> Path:
        return self.base_path / self._filename()

    def _filename(self) -> str:
        # Compare the raw id to both platform basenames so trailing separators are rejected.
        posix_name = PurePosixPath(self.id).name
        windows_name = PureWindowsPath(self.id).name
        if self.id in {"", ".", ".."} or self.id != posix_name or self.id != windows_name:
            raise ValueError("LocalSnapshot id must be a single path segment")
        return f"{self.id}.tar"


class NoopSnapshot(SnapshotBase):
    type: Literal["noop"] = "noop"

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        _ = (data, dependencies)
        return

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        raise SnapshotNotRestorableError(snapshot_id=self.id, path=Path("<noop>"))

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return False


class RemoteSnapshot(SnapshotBase):
    type: Literal["remote"] = "remote"

    client_dependency_key: str

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        try:
            upload = await self._require_client_method("upload", dependencies)
            await _maybe_await(upload(self.id, data))
        except Exception as e:
            raise SnapshotPersistError(
                snapshot_id=self.id,
                path=self._remote_path(),
                cause=e,
            ) from e

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        try:
            download = await self._require_client_method("download", dependencies)
            restored = await _maybe_await(download(self.id))
        except Exception as e:
            raise SnapshotRestoreError(
                snapshot_id=self.id,
                path=self._remote_path(),
                cause=e,
            ) from e

        if not isinstance(restored, io.IOBase):
            raise SnapshotRestoreError(
                snapshot_id=self.id,
                path=self._remote_path(),
                cause=TypeError("Remote snapshot client download() must return an IOBase stream"),
            )
        return restored

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        check = await self._require_client_method("exists", dependencies)
        result = await _maybe_await(check(self.id))
        return bool(result)

    async def _require_client_method(
        self, method_name: str, dependencies: Dependencies | None
    ) -> Callable[..., object]:
        if dependencies is None:
            raise RuntimeError(
                f"RemoteSnapshot(id={self.id!r}) requires session dependencies to resolve "
                f"remote client `{self.client_dependency_key}`"
            )
        client = await dependencies.require(self.client_dependency_key, consumer="RemoteSnapshot")
        method = getattr(client, method_name, None)
        if not callable(method):
            raise TypeError(
                f"Remote snapshot client must implement `{method_name}(snapshot_id, ...)`"
            )
        return cast(Callable[..., object], method)

    def _remote_path(self) -> Path:
        return Path(f"<remote:{self.client_dependency_key}>")


class SnapshotSpec(BaseModel, abc.ABC):
    type: str

    @model_serializer(mode="wrap")
    def _serialize_always_include_type(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if isinstance(data, dict):
            data["type"] = self.type
        return cast(dict[str, Any], data)

    @abc.abstractmethod
    def build(self, snapshot_id: str) -> SnapshotBase: ...


class LocalSnapshotSpec(SnapshotSpec):
    type: Literal["local"] = "local"
    base_path: Path

    def build(self, snapshot_id: str) -> SnapshotBase:
        return LocalSnapshot(id=snapshot_id, base_path=self.base_path)


class NoopSnapshotSpec(SnapshotSpec):
    type: Literal["noop"] = "noop"

    def build(self, snapshot_id: str) -> SnapshotBase:
        return NoopSnapshot(id=snapshot_id)


class RemoteSnapshotSpec(SnapshotSpec):
    type: Literal["remote"] = "remote"
    client_dependency_key: str

    def build(self, snapshot_id: str) -> SnapshotBase:
        return RemoteSnapshot(id=snapshot_id, client_dependency_key=self.client_dependency_key)


SnapshotSpecUnion = Annotated[
    LocalSnapshotSpec | NoopSnapshotSpec | RemoteSnapshotSpec,
    Field(discriminator="type"),
]


def resolve_snapshot(spec: SnapshotBase | SnapshotSpec | None, snapshot_id: str) -> SnapshotBase:
    if isinstance(spec, SnapshotBase):
        return spec
    return (spec or NoopSnapshotSpec()).build(snapshot_id)
