from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents import Agent, ModelSettings, RunConfig, Runner
from agents.models.codex import CodexProvider
from agents.retry import ModelRetrySettings, RetryDecision
from tests.models.test_codex_provider import FakeThread, OperationThread, threads as threads
from tests.testing_processor import fetch_ordered_spans


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["same_attempt", "multiple_before_retry", "single_before_retry"]
)
async def test_distinct_codex_inferences_do_not_merge_across_provider_or_runner_attempts(
    tmp_path: Path, threads: Any, scenario: str
) -> None:
    # Native observations preserve inference boundaries that ScriptedModel cannot emit.
    class InferenceThread(OperationThread):
        def __init__(self, outcomes: list[str], complete: bool) -> None:
            super().__init__(capture="content")
            self.outcomes = outcomes
            self.complete = complete

        async def events(self):
            for index, outcome in enumerate(self.outcomes):
                operation = {
                    "threadId": "thread_1",
                    "turnId": "turn_1",
                    "operationId": f"inference-{index}",
                    "kind": "model",
                    "requestedModel": "test-model",
                    "startedAt": f"2026-11-20T00:00:0{index}.000Z",
                    "input": {"value": {"input": [{"role": "user", "content": f"input-{index}"}]}},
                }
                yield {"method": "turn/operation/started", "params": operation}
                terminal = {
                    **operation,
                    "completedAt": f"2026-11-20T00:00:0{index}.250Z",
                    "outcome": outcome,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
                if not self.complete:
                    terminal.update(
                        observedModel="stale-model",
                        responseId="stale-response",
                        output={"value": [{"role": "assistant", "content": "stale-output"}]},
                    )
                yield {"method": "turn/operation/completed", "params": terminal}
            if self.complete:
                async for event in FakeThread.events(self):
                    yield event

    queue, _ = threads
    if scenario == "single_before_retry":
        queue.extend([InferenceThread(["error"], False), InferenceThread(["success"], True)])
    elif scenario == "multiple_before_retry":
        queue.extend(
            [InferenceThread(["success", "error"], False), InferenceThread(["success"], True)]
        )
    else:
        queue.append(InferenceThread(["error", "success"], True))
    agent = Agent(
        name="boundaries",
        model_settings=ModelSettings(
            retry=ModelRetrySettings(
                max_retries=1,
                policy=lambda _: RetryDecision(retry=True, approve_unsafe_replay=True),
                backoff={"initial_delay": 0},
            )
        ),
    )
    result = await Runner.run(
        agent,
        "initial input",
        run_config=RunConfig(
            model_provider=CodexProvider(cwd=tmp_path), tracing={"operation_detail": "content"}
        ),
    )
    assert result.final_output == "A Codex response"
    models = [span for span in fetch_ordered_spans() if span.span_data.type == "generation"]
    assert (
        len(models)
        == {"same_attempt": 2, "multiple_before_retry": 3, "single_before_retry": 1}[scenario]
    )
    assert len({span.span_id for span in models}) == len(models)
    assert [span.span_data.operation.outcome for span in models].count("failed") == (
        0 if scenario == "single_before_retry" else 1
    )
    assert all(span.ended_at is not None for span in models)
    assert all(
        span.span_data.usage["input_tokens"] == (20 if scenario == "single_before_retry" else 10)
        for span in models
    )
    if scenario == "single_before_retry":
        assert models[0].error is None
        assert models[0].span_data.output is None
        assert models[0].span_data.operation.response_model is None
        assert models[0].span_data.operation.response_id is None
