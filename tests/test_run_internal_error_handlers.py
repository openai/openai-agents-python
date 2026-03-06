from __future__ import annotations

import json
from typing import Any

import pytest

from agents import Agent
from agents.agent_output import AgentOutputSchemaBase
from agents.exceptions import MaxTurnsExceeded, UserError
from agents.run_context import RunContextWrapper
from agents.run_error_handlers import RunErrorData
from agents.run_internal import error_handlers as run_error_handlers


class _CustomSchema(AgentOutputSchemaBase):
    def is_plain_text(self) -> bool:
        return False

    def name(self) -> str:
        return "CustomSchema"

    def json_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    def is_strict_json_schema(self) -> bool:
        return True

    def validate_json(self, json_str: str) -> Any:
        return json.loads(json_str)


def _make_run_data(agent: Agent[Any]) -> RunErrorData:
    return RunErrorData(
        input="hello",
        new_items=[],
        history=[],
        output=[],
        raw_responses=[],
        last_agent=agent,
    )


def test_format_final_output_text_handles_wrapped_payload() -> None:
    agent = Agent(name="wrapped-output", output_type=list[str])
    output = {"response": ["a", "b"]}

    rendered = run_error_handlers.format_final_output_text(agent, output)
    assert json.loads(rendered) == output


def test_validate_handler_final_output_accepts_wrapped_payload() -> None:
    agent = Agent(name="wrapped-validate", output_type=list[str])
    output = {"response": ["ok"]}

    validated = run_error_handlers.validate_handler_final_output(agent, output)
    assert validated == ["ok"]


def test_format_final_output_text_uses_custom_schema_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="custom-format")
    custom_schema = _CustomSchema()
    monkeypatch.setattr(run_error_handlers, "get_output_schema", lambda _agent: custom_schema)

    rendered = run_error_handlers.format_final_output_text(agent, {"ok": True})
    assert json.loads(rendered) == {"ok": True}

    value = object()
    fallback = run_error_handlers.format_final_output_text(agent, value)
    assert fallback == str(value)


def test_validate_handler_final_output_raises_for_unserializable_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="custom-validate")
    custom_schema = _CustomSchema()
    monkeypatch.setattr(run_error_handlers, "get_output_schema", lambda _agent: custom_schema)

    with pytest.raises(UserError, match="Invalid run error handler final_output"):
        run_error_handlers.validate_handler_final_output(agent, {"bad": {1, 2}})


@pytest.mark.asyncio
async def test_resolve_run_error_handler_result_covers_async_and_validation_paths() -> None:
    agent = Agent(name="max-turns")
    context_wrapper: RunContextWrapper[dict[str, Any]] = RunContextWrapper(context={})
    run_data = _make_run_data(agent)
    error = MaxTurnsExceeded("too many turns")

    no_handler = await run_error_handlers.resolve_run_error_handler_result(
        error_handlers={},
        error=error,
        context_wrapper=context_wrapper,
        run_data=run_data,
    )
    assert no_handler is None

    async def async_handler(_handler_input: Any) -> None:
        return None

    async_none = await run_error_handlers.resolve_run_error_handler_result(
        error_handlers={"max_turns": async_handler},
        error=error,
        context_wrapper=context_wrapper,
        run_data=run_data,
    )
    assert async_none is None

    with pytest.raises(UserError, match="Invalid run error handler result"):
        await run_error_handlers.resolve_run_error_handler_result(
            error_handlers={
                "max_turns": lambda _handler_input: {"final_output": "x", "extra": "y"}
            },
            error=error,
            context_wrapper=context_wrapper,
            run_data=run_data,
        )
