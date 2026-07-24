from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import APIConnectionError, AsyncOpenAI

from agents import (
    Agent,
    ModelRetrySettings,
    ModelSettings,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
    SQLiteSession,
    retry_policies,
)

pytestmark = pytest.mark.core


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_retry_reaches_real_api_without_rewinding_session_input(
    integration_model: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    model = OpenAIResponsesModel(model=integration_model, openai_client=AsyncOpenAI())
    original_fetch = model._fetch_response
    attempts = 0

    async def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise APIConnectionError(
                message="Controlled integration-test transport failure.",
                request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
            )
        return await original_fetch(*args, **kwargs)

    monkeypatch.setattr(model, "_fetch_response", fail_once)
    agent = Agent(
        name="Packaged real retry agent",
        model=model,
        instructions="Reply with exactly RETRY_RECOVERED.",
        model_settings=ModelSettings(
            max_tokens=256,
            retry=ModelRetrySettings(
                max_retries=1,
                backoff={"initial_delay": 0.0},
                policy=retry_policies.network_error(),
            ),
        ),
    )
    session = SQLiteSession("packaged-retry", tmp_path / "retry.sqlite3")
    config = RunConfig(tracing_disabled=True)
    result: RunResult | RunResultStreaming

    try:
        if streaming:
            streamed = Runner.run_streamed(
                agent, "Recover exactly once.", session=session, run_config=config
            )
            async for _event in streamed.stream_events():
                pass
            result = streamed
        else:
            result = await Runner.run(
                agent, "Recover exactly once.", session=session, run_config=config
            )
        session_items = await session.get_items()
    finally:
        session.close()

    assert attempts == 2
    assert result.final_output == "RETRY_RECOVERED"
    assert [item.get("role") for item in session_items] == ["user", "assistant"]
