from __future__ import annotations

import pytest
from openai.types.responses.response_create_params import ContextManagement

from agents import Agent, ModelSettings, RunConfig, Runner

from .fake_model import FakeModel, PromptCacheFakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message
from .utils.simple_session import SimpleListSession


def _sent_prompt_cache_key(model: FakeModel, *, first_turn: bool = False) -> str | None:
    model_settings = _sent_model_settings(model, first_turn=first_turn)
    extra_args = model_settings.extra_args or {}
    value = extra_args.get("prompt_cache_key")
    assert value is None or isinstance(value, str)
    return value


def _sent_model_settings(model: FakeModel, *, first_turn: bool = False) -> ModelSettings:
    args = model.first_turn_args if first_turn else model.last_turn_args
    assert args is not None
    model_settings = args["model_settings"]
    assert isinstance(model_settings, ModelSettings)
    return model_settings


class DefaultPromptCacheDisabledFakeModel(FakeModel):
    def _supports_default_prompt_cache_key(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_runner_generates_prompt_cache_key_by_default() -> None:
    model = PromptCacheFakeModel()
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="test", model=model)

    await Runner.run(agent, "hi")

    prompt_cache_key = _sent_prompt_cache_key(model)
    assert prompt_cache_key is not None
    assert prompt_cache_key.startswith("agents-sdk:run:")


@pytest.mark.asyncio
async def test_runner_adds_prompt_cache_key_without_adding_model_call_keyword() -> None:
    model = PromptCacheFakeModel()
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="test", model=model)

    await Runner.run(agent, "hi")

    # PromptCacheFakeModel uses the public Model.get_response() signature. If the runner added
    # prompt_cache_key as a direct model-call keyword, this run would fail before this assertion.
    assert _sent_prompt_cache_key(model) is not None


@pytest.mark.asyncio
async def test_runner_reuses_generated_prompt_cache_key_across_turns() -> None:
    model = PromptCacheFakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("lookup", "{}")],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="test", model=model, tools=[get_function_tool(name="lookup")])

    await Runner.run(agent, "hi")

    first_key = _sent_prompt_cache_key(model, first_turn=True)
    second_key = _sent_prompt_cache_key(model)
    assert first_key is not None
    assert second_key == first_key


@pytest.mark.asyncio
async def test_runner_skips_generated_prompt_cache_key_when_model_disables_default() -> None:
    model = DefaultPromptCacheDisabledFakeModel()
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="test", model=model)

    await Runner.run(agent, "hi")

    assert _sent_prompt_cache_key(model) is None


@pytest.mark.asyncio
async def test_runner_respects_existing_extra_args_prompt_cache_key() -> None:
    model = PromptCacheFakeModel()
    model.set_next_output([get_text_message("done")])
    agent = Agent(
        name="test",
        model=model,
        model_settings=ModelSettings(extra_args={"prompt_cache_key": "existing-key"}),
    )

    await Runner.run(agent, "hi")

    assert _sent_prompt_cache_key(model) == "existing-key"
    model_settings = _sent_model_settings(model)
    assert model_settings.extra_args == {"prompt_cache_key": "existing-key"}


@pytest.mark.asyncio
async def test_runner_respects_existing_extra_body_prompt_cache_key() -> None:
    model = PromptCacheFakeModel()
    model.set_next_output([get_text_message("done")])
    agent = Agent(
        name="test",
        model=model,
        model_settings=ModelSettings(extra_body={"prompt_cache_key": "existing-key"}),
    )

    await Runner.run(agent, "hi")

    assert _sent_prompt_cache_key(model) is None
    model_settings = _sent_model_settings(model)
    assert model_settings.extra_args is None
    assert model_settings.extra_body == {"prompt_cache_key": "existing-key"}


@pytest.mark.asyncio
async def test_runner_generates_prompt_cache_key_with_unrelated_extra_args() -> None:
    model = PromptCacheFakeModel()
    model.set_next_output([get_text_message("done")])
    model_settings = ModelSettings(extra_args={"service_tier": "flex"})
    agent = Agent(
        name="test",
        model=model,
        model_settings=model_settings,
    )

    await Runner.run(agent, "hi")

    assert _sent_prompt_cache_key(model) is not None
    sent_model_settings = _sent_model_settings(model)
    assert sent_model_settings.extra_args == {
        "service_tier": "flex",
        "prompt_cache_key": _sent_prompt_cache_key(model),
    }
    assert model_settings.extra_args == {"service_tier": "flex"}


@pytest.mark.asyncio
async def test_runner_preserves_context_management_when_adding_prompt_cache_key() -> None:
    model = PromptCacheFakeModel()
    model.set_next_output([get_text_message("done")])
    context_management: list[ContextManagement] = [
        {"type": "compaction", "compact_threshold": 200000}
    ]
    model_settings = ModelSettings(context_management=context_management)
    agent = Agent(
        name="test",
        model=model,
        model_settings=model_settings,
    )

    await Runner.run(agent, "hi")

    assert _sent_prompt_cache_key(model) is not None
    sent_model_settings = _sent_model_settings(model)
    assert sent_model_settings.context_management == context_management
    assert sent_model_settings.extra_args == {"prompt_cache_key": _sent_prompt_cache_key(model)}
    assert model_settings.context_management == context_management
    assert model_settings.extra_args is None


@pytest.mark.asyncio
async def test_runner_skips_generated_key_when_model_settings_has_prompt_cache_keys() -> None:
    model = PromptCacheFakeModel()
    model.set_next_output([get_text_message("done")])
    agent = Agent(
        name="test",
        model=model,
        model_settings=ModelSettings(
            extra_args={"prompt_cache_key": "extra-args-key"},
            extra_body={"prompt_cache_key": "extra-body-key"},
        ),
    )

    await Runner.run(agent, "hi")

    assert _sent_prompt_cache_key(model) == "extra-args-key"


@pytest.mark.asyncio
async def test_runner_uses_group_id_as_stable_prompt_cache_key_boundary() -> None:
    model = PromptCacheFakeModel()
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="test", model=model)

    await Runner.run(agent, "hi", run_config=RunConfig(group_id="thread-123"))

    prompt_cache_key = _sent_prompt_cache_key(model)
    assert prompt_cache_key is not None
    assert prompt_cache_key.startswith("agents-sdk:group:")


@pytest.mark.asyncio
async def test_runner_uses_session_id_as_stable_prompt_cache_key_boundary() -> None:
    model = PromptCacheFakeModel()
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="test", model=model)
    session = SimpleListSession(session_id="session-123")

    await Runner.run(agent, "hi", session=session)

    prompt_cache_key = _sent_prompt_cache_key(model)
    assert prompt_cache_key is not None
    assert prompt_cache_key.startswith("agents-sdk:session:")


@pytest.mark.asyncio
async def test_streamed_runner_generates_prompt_cache_key_by_default() -> None:
    model = PromptCacheFakeModel()
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="test", model=model)

    result = Runner.run_streamed(agent, "hi")
    async for _ in result.stream_events():
        pass

    prompt_cache_key = _sent_prompt_cache_key(model)
    assert prompt_cache_key is not None
    assert prompt_cache_key.startswith("agents-sdk:run:")


@pytest.mark.asyncio
async def test_run_state_preserves_generated_prompt_cache_key_on_resume() -> None:
    model = PromptCacheFakeModel()
    model.set_next_output([get_text_message("first")])
    agent = Agent(name="test", model=model)

    first_result = await Runner.run(agent, "hi")
    first_key = _sent_prompt_cache_key(model)
    state = first_result.to_state()
    restored_state = await type(state).from_string(agent, state.to_string())

    model.set_next_output([get_text_message("second")])
    await Runner.run(agent, restored_state)

    assert first_key is not None
    assert restored_state._generated_prompt_cache_key == first_key
    assert _sent_prompt_cache_key(model) == first_key
