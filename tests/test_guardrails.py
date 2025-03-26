from __future__ import annotations

from typing import Any

import pytest

from agents import (
    Agent,
    FactCheckingGuardrail,
    GuardrailFunctionOutput,
    InputGuardrail,
    OutputGuardrail,
    RunContextWrapper,
    TResponseInputItem,
    UserError,
)
from agents.guardrail import fact_checking_guardrail, input_guardrail, output_guardrail


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


def get_sync_fact_checking_guardrail(triggers: bool, output_info: Any | None = None):
    def sync_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any, agent_input: Any
    ):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return sync_guardrail


@pytest.mark.asyncio
async def test_sync_fact_checking_guardrail():
    guardrail = FactCheckingGuardrail(
        guardrail_function=get_sync_fact_checking_guardrail(triggers=False)
    )
    result = await guardrail.run(
        agent=Agent(name="test"),
        agent_input="test",
        agent_output="test",
        context=RunContextWrapper(context=None),
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = FactCheckingGuardrail(
        guardrail_function=get_sync_fact_checking_guardrail(triggers=True)
    )
    result = await guardrail.run(
        agent=Agent(name="test"),
        agent_input="test",
        agent_output="test",
        context=RunContextWrapper(context=None),
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = FactCheckingGuardrail(
        guardrail_function=get_sync_fact_checking_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"),
        agent_input="test",
        agent_output="test",
        context=RunContextWrapper(context=None),
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


def get_async_fact_checking_guardrail(triggers: bool, output_info: Any | None = None):
    async def async_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any, agent_input: Any
    ):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return async_guardrail


@pytest.mark.asyncio
async def test_async_fact_checking_guardrail():
    guardrail = FactCheckingGuardrail(
        guardrail_function=get_async_fact_checking_guardrail(triggers=False)
    )
    result = await guardrail.run(
        agent=Agent(name="test"),
        agent_input="test",
        agent_output="test",
        context=RunContextWrapper(context=None),
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = FactCheckingGuardrail(
        guardrail_function=get_async_fact_checking_guardrail(triggers=True)
    )
    result = await guardrail.run(
        agent=Agent(name="test"),
        agent_input="test",
        agent_output="test",
        context=RunContextWrapper(context=None),
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = FactCheckingGuardrail(
        guardrail_function=get_async_fact_checking_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"),
        agent_input="test",
        agent_output="test",
        context=RunContextWrapper(context=None),
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


@pytest.mark.asyncio
async def test_invalid_fact_checking_guardrail_raises_user_error():
    with pytest.raises(UserError):
        # Purposely ignoring type error
        guardrail = FactCheckingGuardrail(guardrail_function="foo")  # type: ignore
        await guardrail.run(
            agent=Agent(name="test"),
            agent_input="test",
            agent_output="test",
            context=RunContextWrapper(context=None),
        )


@fact_checking_guardrail
def decorated_fact_checking_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any, agent_input: Any
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_5",
        tripwire_triggered=False,
    )


@fact_checking_guardrail(name="Custom name")
def decorated_named_fact_checking_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any, agent_input: Any
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_6",
        tripwire_triggered=False,
    )


@pytest.mark.asyncio
async def test_fact_checking_guardrail_decorators():
    guardrail = decorated_fact_checking_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"),
        agent_input="test",
        agent_output="test",
        context=RunContextWrapper(context=None),
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_5"

    guardrail = decorated_named_fact_checking_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"),
        agent_input="test",
        agent_output="test",
        context=RunContextWrapper(context=None),
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_6"
    assert guardrail.get_name() == "Custom name"
