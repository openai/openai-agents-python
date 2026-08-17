from __future__ import annotations

from typing import Any, cast

import pytest

from agents.agent_tool_input import StructuredToolInputBuilder, resolve_agent_tool_input


@pytest.mark.asyncio
async def test_resolve_agent_tool_input_rejects_invalid_builder_result() -> None:
    def invalid_builder(_options: Any) -> Any:
        return {"input": "invalid"}

    with pytest.raises(TypeError, match="must return a string or list.*got dict"):
        await resolve_agent_tool_input(
            params={"input": "ignored"},
            input_builder=cast(StructuredToolInputBuilder, invalid_builder),
        )
