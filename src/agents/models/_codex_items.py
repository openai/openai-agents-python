"""Internal output types attesting to work already completed by the Codex adapter."""

from openai.types.responses import (
    ResponseCustomToolCallOutputItem,
    ResponseFunctionToolCallOutputItem,
)


class CodexFunctionToolCallOutput(ResponseFunctionToolCallOutputItem):
    """A matching native function call has already completed in Codex."""


class CodexCustomToolCallOutput(ResponseCustomToolCallOutputItem):
    """A matching native custom call has already completed in Codex."""
