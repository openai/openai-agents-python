from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .tool_output_trimmer import ToolOutputTrimmer

if TYPE_CHECKING:
    from .agentspan import AgentspanRunResult, AgentspanRunner

__all__ = ["ToolOutputTrimmer", "AgentspanRunner", "AgentspanRunResult"]


def __getattr__(name: str) -> Any:
    if name in ("AgentspanRunner", "AgentspanRunResult"):
        try:
            from .agentspan import AgentspanRunResult, AgentspanRunner  # noqa: F401

            return AgentspanRunner if name == "AgentspanRunner" else AgentspanRunResult
        except ImportError as e:
            raise ImportError(
                f"{name} requires the 'agentspan' package. "
                "Install it with: pip install openai-agents[agentspan]"
            ) from e

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
