from __future__ import annotations

import pytest
from openai.types.responses.response_create_params import ContextManagement, PromptCacheOptions

from agents import Agent, ModelSettings, RunConfig, Runner
from agents.testing import ScriptedModel

from .test_responses import get_function_tool, get_function_tool_call, get_text_message
from .utils.simple_session import SimpleListSession


def _sent_prompt_cache_key(model: ScriptedModel, *, first_turn: bool = False) -> str | None:
    model_settings = _sent_model_settings(model, first_turn=first_turn)
    extra_args = model_settings.extra_args or {}
    value = extra_args.get("prompt_cache_key")
    assert value is None or isinstance(value, str)
    return value


def _sent_model_settings(model: ScriptedModel, *, first_turn: bool = False) -> ModelSettings:
    call = model.calls[0] if first_turn else model.calls[-1]
    model_settings = call.model_settings
    assert isinstance(model_settings, ModelSettings)
    return model_settings


class PromptCacheScriptedModel(ScriptedModel):
    def _supports_default_prompt_cache_key(self) -> bool:
        return True


class DefaultPromptCacheDisabledScriptedModel(ScriptedModel):
    def _supports_default_prompt_cache_key(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_runner_generates_prompt_cache_key_by_default() -> None:
    model = PromptCacheScriptedModel()
    model.enqueue([get_text_message("done")])
    agent = Agent(name="test", model=model)

    await Runner.run(agent, "hi")

    prompt_cache_key = _sent_prompt_cache_key(model)
    assert prompt_cache_key is not None
    assert prompt_cache_key.startswith("agents-sdk:run:")


@pytest.mark.asyncio
async def test_runner_adds_prompt_cache_key_without_adding_model_call_keyword() -> None:
    model = PromptCacheScriptedModel()
    model.enqueue([get_text_message("done")])
    agent = Agent(name="test", model=model)

    await Runner.run(agent, "hi")

    # PromptCacheScriptedModel uses the public Model.get_response() signature. If the runner added
    # prompt_cache_key as a direct model-call keyword, this run would fail before this assertion.
    assert _sent_prompt_cache_key(model) is not None


@pytest.mark.asyncio
async def test_runner_reuses_generated_prompt_cache_key_across_turns() -> None:
    model = PromptCacheScriptedModel()
    model.extend(
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
    model = DefaultPromptCacheDisabledScriptedModel()
    model.enqueue([get_text_message("done")])
    agent = Agent(name="test", model=model)

    await Runner.run(agent, "hi")

    assert _sent_prompt_cache_key(model) is None


@pytest.mark.asyncio
async def test_runner_respects_existing_extra_args_prompt_cache_key() -> None:
    model = PromptCacheScriptedModel()
    model.enqueue([get_text_message("done")])
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
    model = PromptCacheScriptedModel()
    model.enqueue([get_text_message("done")])
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
    model = PromptCacheScriptedModel()
    model.enqueue([get_text_message("done")])
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
    model = PromptCacheScriptedModel()
    model.enqueue([get_text_message("done")])
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
async def test_runner_preserves_prompt_cache_options_when_adding_prompt_cache_key() -> None:
    model = PromptCacheScriptedModel()
    model.enqueue([get_text_message("done")])
    prompt_cache_options: PromptCacheOptions = {"mode": "explicit", "ttl": "30m"}
    model_settings = ModelSettings(prompt_cache_options=prompt_cache_options)
    agent = Agent(name="test", model=model, model_settings=model_settings)

    await Runner.run(agent, "hi")

    assert _sent_prompt_cache_key(model) is not None
    sent_model_settings = _sent_model_settings(model)
    assert sent_model_settings.prompt_cache_options == prompt_cache_options
    assert sent_model_settings.extra_args == {"prompt_cache_key": _sent_prompt_cache_key(model)}
    assert model_settings.prompt_cache_options == prompt_cache_options
    assert model_settings.extra_args is None


@pytest.mark.asyncio
async def test_runner_skips_generated_key_when_model_settings_has_prompt_cache_keys() -> None:
    model = PromptCacheScriptedModel()
    model.enqueue([get_text_message("done")])
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
    model = PromptCacheScriptedModel()
    model.enqueue([get_text_message("done")])
    agent = Agent(name="test", model=model)

    await Runner.run(agent, "hi", run_config=RunConfig(group_id="thread-123"))

    prompt_cache_key = _sent_prompt_cache_key(model)
    assert prompt_cache_key is not None
    assert prompt_cache_key.startswith("agents-sdk:group:")


@pytest.mark.asyncio
async def test_runner_uses_session_id_as_stable_prompt_cache_key_boundary() -> None:
    model = PromptCacheScriptedModel()
    model.enqueue([get_text_message("done")])
    agent = Agent(name="test", model=model)
    session = SimpleListSession(session_id="session-123")

    await Runner.run(agent, "hi", session=session)

    prompt_cache_key = _sent_prompt_cache_key(model)
    assert prompt_cache_key is not None
    assert prompt_cache_key.startswith("agents-sdk:session:")


@pytest.mark.asyncio
async def test_streamed_runner_generates_prompt_cache_key_by_default() -> None:
    model = PromptCacheScriptedModel()
    model.enqueue([get_text_message("done")])
    agent = Agent(name="test", model=model)

    result = Runner.run_streamed(agent, "hi")
    async for _ in result.stream_events():
        pass

    prompt_cache_key = _sent_prompt_cache_key(model)
    assert prompt_cache_key is not None
    assert prompt_cache_key.startswith("agents-sdk:run:")


@pytest.mark.asyncio
async def test_run_state_preserves_generated_prompt_cache_key_on_resume() -> None:
    model = PromptCacheScriptedModel()
    model.enqueue([get_text_message("first")])
    agent = Agent(name="test", model=model)

    first_result = await Runner.run(agent, "hi")
    first_key = _sent_prompt_cache_key(model)
    state = first_result.to_state()
    restored_state = await type(state).from_string(agent, state.to_string())

    model.enqueue([get_text_message("second")])
    await Runner.run(agent, restored_state)

    assert first_key is not None
    assert restored_state._generated_prompt_cache_key == first_key
    assert _sent_prompt_cache_key(model) == first_key


@pytest.mark.asyncio
async def test_as_tool_nested_runs_share_stable_prompt_cache_key_without_session() -> None:
    """Consecutive identical as_tool() calls must share a cache key like a normal runner."""
    from agents.tool_context import ToolContext

    nested_model = PromptCacheScriptedModel()
    nested_model.enqueue([get_text_message("n1")])
    nested_model.enqueue([get_text_message("n2")])
    nested = Agent(name="nested-worker", model=nested_model, instructions="stable instructions")
    tool = nested.as_tool(tool_name="run_nested", tool_description="Run nested worker")

    await tool.on_invoke_tool(
        ToolContext(context=None, tool_name="run_nested", tool_call_id="c1", tool_arguments="{}"),
        '{"input":"first"}',
    )
    await tool.on_invoke_tool(
        ToolContext(context=None, tool_name="run_nested", tool_call_id="c2", tool_arguments="{}"),
        '{"input":"second"}',
    )

    first_key = _sent_prompt_cache_key(nested_model, first_turn=True)
    second_key = _sent_prompt_cache_key(nested_model)
    assert first_key is not None
    assert second_key == first_key
    assert first_key.startswith("agents-sdk:group:")


@pytest.mark.asyncio
async def test_as_tool_nested_run_namespaces_parent_group_id() -> None:
    """Nested as_tool() must not share the parent's raw group_id cache partition."""
    from agents.tool_context import ToolContext

    nested_model = PromptCacheScriptedModel()
    nested_model.enqueue([get_text_message("n1")])
    nested = Agent(name="nested-worker", model=nested_model)
    tool = nested.as_tool(tool_name="run_nested", tool_description="Run nested worker")

    parent_rc = RunConfig(group_id="parent-thread")
    await tool.on_invoke_tool(
        ToolContext(
            context=None,
            tool_name="run_nested",
            tool_call_id="c1",
            tool_arguments="{}",
            run_config=parent_rc,
        ),
        '{"input":"first"}',
    )

    key = _sent_prompt_cache_key(nested_model)
    assert key is not None
    assert key.startswith("agents-sdk:group:")
    # Hash of namespaced group, not the bare parent group value alone.
    direct_model = PromptCacheScriptedModel()
    direct_model.enqueue([get_text_message("d")])
    await Runner.run(
        Agent(name="direct", model=direct_model),
        "hi",
        run_config=RunConfig(group_id="parent-thread"),
    )
    parent_key = _sent_prompt_cache_key(direct_model)
    assert key != parent_key


@pytest.mark.asyncio
async def test_as_tool_with_session_matches_normal_runner_prompt_cache_key() -> None:
    """With a session, as_tool() must use the same session cache key as Runner.run()."""
    from agents.tool_context import ToolContext

    session = SimpleListSession(session_id="shared-session")
    nested_model = PromptCacheScriptedModel()
    nested_model.enqueue([get_text_message("n1")])
    nested_model.enqueue([get_text_message("n2")])
    nested = Agent(name="nested-worker", model=nested_model)
    tool = nested.as_tool(
        tool_name="run_nested",
        tool_description="Run nested worker",
        session=session,
    )

    await tool.on_invoke_tool(
        ToolContext(context=None, tool_name="run_nested", tool_call_id="c1", tool_arguments="{}"),
        '{"input":"first"}',
    )
    await tool.on_invoke_tool(
        ToolContext(context=None, tool_name="run_nested", tool_call_id="c2", tool_arguments="{}"),
        '{"input":"second"}',
    )

    as_tool_key = _sent_prompt_cache_key(nested_model, first_turn=True)
    assert as_tool_key == _sent_prompt_cache_key(nested_model)
    assert as_tool_key is not None
    assert as_tool_key.startswith("agents-sdk:session:")

    direct_model = PromptCacheScriptedModel()
    direct_model.enqueue([get_text_message("d")])
    await Runner.run(Agent(name="direct", model=direct_model), "hi", session=session)
    assert _sent_prompt_cache_key(direct_model) == as_tool_key
