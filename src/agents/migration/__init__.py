"""Opt-in Classic-to-native runner migration; requires the classic extra."""

# Run Classic's optional-dependency check before importing the adapter's dependencies.
from .. import classic as _classic  # noqa: F401
from ._result import SDKRunResult, SDKRunResultStreaming
from ._runner import SDKRunner

__all__ = ("SDKRunner", "SDKRunResult", "SDKRunResultStreaming")
