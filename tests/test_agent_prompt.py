import pytest

from agents import Agent, Prompt, RunContextWrapper, Runner

from .fake_model import FakeModel
from .test_responses import get_text_message


class PromptCaptureFakeModel(FakeModel):
    """Subclass of FakeModel that records the prompt passed to the model."""

    def __init__(self):
        super().__init__()
        self.last_prompt = None

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id,
        conversation_id,
        prompt,
    ):
        # Record the prompt that the agent resolved and passed in.
        self.last_prompt = prompt
        return await super().get_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )


@pytest.mark.asyncio
async def test_static_prompt_is_resolved_correctly():
    static_prompt: Prompt = {
        "id": "my_prompt",
        "version": "1",
        "variables": {"some_var": "some_value"},
    }

    agent = Agent(name="test", prompt=static_prompt)
    context_wrapper = RunContextWrapper(context=None)

    resolved = await agent.get_prompt(context_wrapper)

    assert resolved == {
        "id": "my_prompt",
        "version": "1",
        "variables": {"some_var": "some_value"},
    }


@pytest.mark.asyncio
async def test_dynamic_prompt_is_resolved_correctly():
    dynamic_prompt_value: Prompt = {"id": "dyn_prompt", "version": "2"}

    def dynamic_prompt_fn(_data):
        return dynamic_prompt_value

    agent = Agent(name="test", prompt=dynamic_prompt_fn)
    context_wrapper = RunContextWrapper(context=None)

    resolved = await agent.get_prompt(context_wrapper)

    assert resolved == {"id": "dyn_prompt", "version": "2", "variables": None}


@pytest.mark.asyncio
async def test_prompt_is_passed_to_model():
    static_prompt: Prompt = {"id": "model_prompt"}

    model = PromptCaptureFakeModel()
    agent = Agent(name="test", model=model, prompt=static_prompt)

    # Ensure the model returns a simple message so the run completes in one turn.
    model.set_next_output([get_text_message("done")])

    await Runner.run(agent, input="hello")

    # The model should have received the prompt resolved by the agent.
    expected_prompt = {
        "id": "model_prompt",
        "version": None,
        "variables": None,
    }
    assert model.last_prompt == expected_prompt
