from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .tool_output_trimmer import ToolOutputTrimmer

if TYPE_CHECKING:
    from .camb import CambAITools

__all__ = ["CambAITools", "ToolOutputTrimmer"]


def __getattr__(name: str) -> Any:
    if name == "CambAITools":
        try:
            from .camb import CambAITools  # noqa: F401

            return CambAITools
        except ModuleNotFoundError as e:
            raise ImportError(
                "CambAITools requires the 'camb' extra. "
                "Install it with: pip install 'openai-agents[camb]'"
            ) from e

    raise AttributeError(f"module {__name__} has no attribute {name}")
