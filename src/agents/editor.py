from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from .run_context import RunContextWrapper
from .util._types import MaybeAwaitable

ApplyPatchOperationType = Literal["create_file", "update_file", "delete_file"]

_DATACLASS_KWARGS = {"slots": True} if sys.version_info >= (3, 10) else {}


@dataclass(**_DATACLASS_KWARGS)
class ApplyPatchOperation:
    """Represents a single apply_patch editor operation requested by the model."""

    type: ApplyPatchOperationType
    path: str
    diff: str | None = None
    ctx_wrapper: RunContextWrapper | None = None


@dataclass(**_DATACLASS_KWARGS)
class ApplyPatchResult:
    """Optional metadata returned by editor operations."""

    status: Literal["completed", "failed"] | None = None
    output: str | None = None


@runtime_checkable
class ApplyPatchEditor(Protocol):
    """Host-defined editor that applies diffs on disk."""

    def create_file(
        self, operation: ApplyPatchOperation
    ) -> MaybeAwaitable[ApplyPatchResult | str | None]: ...

    def update_file(
        self, operation: ApplyPatchOperation
    ) -> MaybeAwaitable[ApplyPatchResult | str | None]: ...

    def delete_file(
        self, operation: ApplyPatchOperation
    ) -> MaybeAwaitable[ApplyPatchResult | str | None]: ...
