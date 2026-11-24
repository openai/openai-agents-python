from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, overload, runtime_checkable

from .exceptions import _data_redacted_error_boundary
from .usage import Usage

DecisionInput = str | Sequence[dict[str, Any]]
_T = TypeVar("_T")


@dataclass(frozen=True, kw_only=True)
class DecisionRequest:
    """A question, explicit evidence and the JSON schema of the requested value."""

    question: str = field(repr=False)
    input: DecisionInput = field(repr=False)
    output_schema: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class DecisionResponse:
    """A JSON value and optional metadata. Missing usage is unknown."""

    value: Any = field(repr=False)
    usage: Usage | None = None
    request_id: str | None = None
    model: str | None = None


@runtime_checkable
class DecisionProvider(Protocol):
    """Answer one decision without changing the conversation or executing tools."""

    async def get_decision(self, request: DecisionRequest) -> DecisionResponse: ...


class _Decider:
    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: None = None,
        output_type: None = None,
    ) -> bool: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: Sequence[_T],
        output_type: None = None,
    ) -> _T: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: None = None,
        output_type: type[_T],
    ) -> _T: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: None = None,
        output_type: Any,
    ) -> Any: ...

    @_data_redacted_error_boundary
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: Sequence[Any] | None = None,
        output_type: Any = None,
    ) -> Any:
        """Answer from explicit input only. Omit choices and output_type for a boolean.

        A tool, request filter or final output policy may use its live context. A provider
        override applies to this call only. Decision usage is separate from run usage.
        """
        from .decisions import _decide

        return await _decide(self, question, input, provider, choices, output_type)
