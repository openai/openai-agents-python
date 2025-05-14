from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ..agent_output import AgentOutputSchemaBase
from ..handoffs import Handoff
from ..items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from ..tool import Tool
from .interface import ModelTracing

if TYPE_CHECKING:
    from ..model_settings import ModelSettings

class JSONModeModelMixin:
    '''
    A mixin class for JSON mode models.
    '''
    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
    ) -> ModelResponse:
        pass

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        pass
