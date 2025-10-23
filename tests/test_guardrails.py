from __future__ import annotations

from typing import Any

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    OutputGuardrail,
    RunContextWrapper,
    TResponseInputItem,
    UserError,
)
from agents.guardrail import input_guardrail, output_guardrail


def get_sync_guardrail(triggers: bool, output_info: Any | None = None):
    def sync_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return sync_guardrail


@pytest.mark.asyncio
async def test_sync_input_guardrail():
    guardrail = InputGuardrail(guardrail_function=get_sync_guardrail(triggers=False))
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = InputGuardrail(guardrail_function=get_sync_guardrail(triggers=True))
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = InputGuardrail(
        guardrail_function=get_sync_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


def get_async_input_guardrail(triggers: bool, output_info: Any | None = None):
    async def async_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return async_guardrail


@pytest.mark.asyncio
async def test_async_input_guardrail():
    guardrail = InputGuardrail(guardrail_function=get_async_input_guardrail(triggers=False))
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = InputGuardrail(guardrail_function=get_async_input_guardrail(triggers=True))
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = InputGuardrail(
        guardrail_function=get_async_input_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


@pytest.mark.asyncio
async def test_invalid_input_guardrail_raises_user_error():
    with pytest.raises(UserError):
        # Purposely ignoring type error
        guardrail = InputGuardrail(guardrail_function="foo")  # type: ignore
        await guardrail.run(
            agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
        )


def get_sync_output_guardrail(triggers: bool, output_info: Any | None = None):
    def sync_guardrail(context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return sync_guardrail


@pytest.mark.asyncio
async def test_sync_output_guardrail():
    guardrail = OutputGuardrail(guardrail_function=get_sync_output_guardrail(triggers=False))
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = OutputGuardrail(guardrail_function=get_sync_output_guardrail(triggers=True))
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = OutputGuardrail(
        guardrail_function=get_sync_output_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


def get_async_output_guardrail(triggers: bool, output_info: Any | None = None):
    async def async_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return async_guardrail


@pytest.mark.asyncio
async def test_async_output_guardrail():
    guardrail = OutputGuardrail(guardrail_function=get_async_output_guardrail(triggers=False))
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = OutputGuardrail(guardrail_function=get_async_output_guardrail(triggers=True))
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = OutputGuardrail(
        guardrail_function=get_async_output_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


@pytest.mark.asyncio
async def test_invalid_output_guardrail_raises_user_error():
    with pytest.raises(UserError):
        # Purposely ignoring type error
        guardrail = OutputGuardrail(guardrail_function="foo")  # type: ignore
        await guardrail.run(
            agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
        )


@input_guardrail
def decorated_input_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_1",
        tripwire_triggered=False,
    )


@input_guardrail(name="Custom name")
def decorated_named_input_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_2",
        tripwire_triggered=False,
    )


@pytest.mark.asyncio
async def test_input_guardrail_decorators():
    guardrail = decorated_input_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_1"

    guardrail = decorated_named_input_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_2"
    assert guardrail.get_name() == "Custom name"


@output_guardrail
def decorated_output_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_3",
        tripwire_triggered=False,
    )


@output_guardrail(name="Custom name")
def decorated_named_output_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_4",
        tripwire_triggered=False,
    )


@pytest.mark.asyncio
async def test_output_guardrail_decorators():
    guardrail = decorated_output_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_3"

    guardrail = decorated_named_output_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_4"
    assert guardrail.get_name() == "Custom name"


@pytest.mark.asyncio
async def test_input_guardrail_run_in_parallel_default():
    guardrail = InputGuardrail(
        guardrail_function=lambda ctx, agent, input: GuardrailFunctionOutput(
            output_info=None, tripwire_triggered=False
        )
    )
    assert guardrail.run_in_parallel is True


@pytest.mark.asyncio
async def test_input_guardrail_run_in_parallel_false():
    guardrail = InputGuardrail(
        guardrail_function=lambda ctx, agent, input: GuardrailFunctionOutput(
            output_info=None, tripwire_triggered=False
        ),
        run_in_parallel=False,
    )
    assert guardrail.run_in_parallel is False


@pytest.mark.asyncio
async def test_input_guardrail_decorator_with_run_in_parallel():
    @input_guardrail(run_in_parallel=False)
    def blocking_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info="blocking",
            tripwire_triggered=False,
        )

    assert blocking_guardrail.run_in_parallel is False
    result = await blocking_guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "blocking"


@pytest.mark.asyncio
async def test_input_guardrail_decorator_with_name_and_run_in_parallel():
    @input_guardrail(name="custom_name", run_in_parallel=False)
    def named_blocking_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info="named_blocking",
            tripwire_triggered=False,
        )

    assert named_blocking_guardrail.get_name() == "custom_name"
    assert named_blocking_guardrail.run_in_parallel is False
