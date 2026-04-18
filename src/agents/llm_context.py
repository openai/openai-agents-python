from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .items import ModelResponse, TResponseInputItem
    from .run_context import RunContextWrapper
    from .usage import Usage


@dataclass(eq=False)
class LLMContext:
    """Context for LLM lifecycle hooks (on_llm_start, on_llm_end)."""

    wrapper: RunContextWrapper
    """The underlying run context wrapper."""

    system_prompt: str | None = None
    """The system prompt (instructions) passed to the LLM."""

    input_items: list[TResponseInputItem] | None = None
    """The input items (messages) passed to the LLM."""

    response: ModelResponse | None = None
    """The model response, available in on_llm_end."""

    @property
    def context(self) -> Any:
        """Access the user-provided context."""
        return self.wrapper.context

    @property
    def usage(self) -> Usage:
        """Access the run usage."""
        return self.wrapper.usage

    @classmethod
    def from_run_context(
        cls,
        wrapper: RunContextWrapper,
        *,
        system_prompt: str | None = None,
        input_items: list[TResponseInputItem] | None = None,
        response: ModelResponse | None = None,
    ) -> LLMContext:
        """Create an LLMContext by wrapping a RunContextWrapper."""
        return cls(
            wrapper=wrapper,
            system_prompt=system_prompt,
            input_items=input_items,
            response=response,
        )
