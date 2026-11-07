"""Opt-in Classic-to-native runner migration; requires the classic extra."""

# Run Classic's optional-dependency check before importing the adapter's dependencies.
from .. import classic as _classic  # noqa: F401
from ..session import get_cleanup
from ._result import SDKRunHandle, SDKRunResult
from ._runner import SDKRunner

__all__ = ("SDKRunner", "SDKRunResult", "SDKRunHandle", "get_cleanup")
