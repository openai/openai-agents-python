from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .._redaction import current_redactor
from ..exceptions import (
    _is_error_data_redacted,
    _prepare_data_redacted_error,
    _raise_data_redacted_error,
)

if TYPE_CHECKING:
    from ..agent import Agent
    from ..lifecycle import RunHooks
    from ..run_config import ToolExecutionConfig
    from ..run_context import RunContextWrapper


@dataclass(frozen=True)
class ModelToolHooks:
    agent: Agent[Any]
    context: RunContextWrapper[Any]
    lifecycle: RunHooks[Any]
    settings: ToolExecutionConfig | None
    resumable: bool = False


_MODEL_RUN_CONTEXT: ContextVar[tuple[object | None, ModelToolHooks | None]] = ContextVar(
    "model_run_context", default=(None, None)
)


@contextmanager
def model_run_context(owner: object, hooks: ModelToolHooks | None = None) -> Iterator[None]:
    token = _MODEL_RUN_CONTEXT.set((owner, hooks))
    try:
        yield
    except Exception as error:
        if current_redactor() is None or _is_error_data_redacted(error):
            raise
        # Retry decisions need the original provider error. Once they finish,
        # discard any reflected request, response body and exception chain.
        _raise_data_redacted_error(_prepare_data_redacted_error(error))
    finally:
        _MODEL_RUN_CONTEXT.reset(token)


def get_model_run_owner() -> object | None:
    owner, _ = _MODEL_RUN_CONTEXT.get()
    return owner


def get_model_tool_hooks() -> ModelToolHooks | None:
    return _MODEL_RUN_CONTEXT.get()[1]
