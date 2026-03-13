import abc
import io
import shutil
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, Field, PrivateAttr

from .errors import (
    SnapshotNotRestorableError,
    SnapshotPersistError,
    SnapshotRestoreError,
)

SnapshotClass = type["SnapshotBase"]


class SnapshotBase(BaseModel, abc.ABC):
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

    @classmethod
    def _snapshot_class_for_type(cls, snapshot_type: str) -> SnapshotClass | None:
        return SnapshotBase._subclass_registry.get(snapshot_type)

    @abc.abstractmethod
    async def persist(self, data: io.IOBase) -> None: ...

    @abc.abstractmethod
    async def restore(self) -> io.IOBase: ...

    @abc.abstractmethod
    async def restorable(self) -> bool: ...


class LocalSnapshot(SnapshotBase):
    type: Literal["local"] = "local"

    base_path: Path
    _checksum: str | None = PrivateAttr(default=None)

    async def persist(self, data: io.IOBase) -> None:
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as f:
                shutil.copyfileobj(data, f)
        except OSError as e:
            raise SnapshotPersistError(snapshot_id=self.id, path=path, cause=e) from e

    async def restore(self) -> io.IOBase:
        path = self._path()
        try:
            return path.open("rb")
        except OSError as e:
            raise SnapshotRestoreError(snapshot_id=self.id, path=path, cause=e) from e

    async def restorable(self) -> bool:
        return self._path().exists()

    def _path(self) -> Path:
        return Path(str(self.base_path / self.id) + ".tar")


class NoopSnapshot(SnapshotBase):
    type: Literal["noop"] = "noop"

    async def persist(self, data: io.IOBase) -> None:
        _ = data
        return

    async def restore(self) -> io.IOBase:
        raise SnapshotNotRestorableError(snapshot_id=self.id, path=Path("<noop>"))

    async def restorable(self) -> bool:
        return False


class SnapshotSpec(BaseModel, abc.ABC):
    type: str

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


SnapshotSpecUnion = Annotated[LocalSnapshotSpec | NoopSnapshotSpec, Field(discriminator="type")]


def resolve_snapshot(spec: SnapshotSpec | None, snapshot_id: str) -> SnapshotBase:
    return (spec or NoopSnapshotSpec()).build(snapshot_id)
