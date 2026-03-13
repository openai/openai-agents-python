from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from ..manifest import Manifest
from ..snapshot import SnapshotBase


class SandboxSessionState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    session_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    snapshot: SnapshotBase
    manifest: Manifest

    @field_validator("snapshot", mode="before")
    @classmethod
    def _coerce_snapshot(cls, value: object) -> SnapshotBase:
        return SnapshotBase.parse(value)

    @field_serializer("snapshot", when_used="json")
    def _serialize_snapshot(self, snapshot: SnapshotBase) -> object:
        # Ensure subclass fields (e.g. LocalSnapshot.base_path) are preserved in JSON.
        return snapshot.model_dump(mode="json")
