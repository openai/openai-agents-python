"""Custom text patch formats used by sandbox workspace editors."""

from typing import Protocol, runtime_checkable

from .apply_diff import ApplyDiffMode


@runtime_checkable
class PatchFormat(Protocol):
    @staticmethod
    def apply_diff(input: str, diff: str, mode: ApplyDiffMode = "default") -> str: ...
