from __future__ import annotations

from typing import Any

import pytest
from openai.types.shared import Reasoning

from agents import Agent, ModelSettings, RunConfig, Runner
from agents.retry import ModelRetryBackoffSettings, ModelRetrySettings

pytestmark = pytest.mark.core


@pytest.mark.parametrize("dictionary", [False, True], ids=["typed", "dictionary"])
async def test_agent_model_settings_reach_the_live_responses_api(
    integration_model: str, dictionary: bool
) -> None:
    settings: ModelSettings | dict[str, Any]
    if dictionary:
        settings = {"reasoning": {"effort": "low"}, "max_tokens": 256}
    else:
        settings = ModelSettings(reasoning=Reasoning(effort="low"), max_tokens=256)

    agent = Agent(
        name="Packaged settings agent",
        model=integration_model,
        instructions="Reply with exactly PACKAGED_SETTINGS_OK.",
        model_settings=settings,
    )
    result = await Runner.run(agent, "Confirm the packaged settings path.")

    assert isinstance(agent.model_settings, ModelSettings)
    assert result.final_output == "PACKAGED_SETTINGS_OK"
    assert result.context_wrapper.usage.total_tokens > 0


@pytest.mark.parametrize("dictionary", [False, True], ids=["typed", "dictionary"])
async def test_run_config_model_settings_reach_the_live_responses_api(
    integration_model: str, dictionary: bool
) -> None:
    settings: ModelSettings | dict[str, Any]
    if dictionary:
        settings = {"reasoning": {"effort": "low"}, "max_tokens": 256}
    else:
        settings = ModelSettings(reasoning=Reasoning(effort="low"), max_tokens=256)

    config = RunConfig(model_settings=settings, tracing_disabled=True)
    agent = Agent(
        name="Packaged run configuration agent",
        model=integration_model,
        instructions="Reply with exactly RUN_CONFIG_OK.",
    )
    result = await Runner.run(agent, "Confirm the packaged run configuration.", run_config=config)

    assert isinstance(config.model_settings, ModelSettings)
    assert result.final_output == "RUN_CONFIG_OK"


async def test_nested_retry_settings_and_clone_dictionaries_reach_the_api(
    integration_model: str,
) -> None:
    agent = Agent(
        name="Packaged nested settings agent",
        model=integration_model,
        instructions="Reply with exactly NESTED_SETTINGS_OK.",
        model_settings={
            "max_tokens": 256,
            "reasoning": {"effort": "low"},
            "retry": {
                "max_retries": 0,
                "backoff": {"initial_delay": 0.0},
            },
        },
    )
    assert isinstance(agent.model_settings.retry, ModelRetrySettings)
    assert isinstance(agent.model_settings.retry.backoff, ModelRetryBackoffSettings)
    cloned = agent.clone(
        model_settings={
            "max_tokens": 256,
            "reasoning": {"effort": "low"},
            "retry": {"max_retries": 0, "backoff": {"initial_delay": 0.0}},
        }
    )
    result = await Runner.run(cloned, "Confirm provider-specific settings normalization.")

    assert isinstance(cloned.model_settings, ModelSettings)
    assert isinstance(cloned.model_settings.retry, ModelRetrySettings)
    assert isinstance(cloned.model_settings.retry.backoff, ModelRetryBackoffSettings)
    assert result.final_output == "NESTED_SETTINGS_OK"
