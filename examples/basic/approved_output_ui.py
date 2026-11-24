"""Offline, final-only UI. Never display raw stream deltas or exception messages."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agents import (
    Agent,
    FunctionTool,
    ModelRetrySettings,
    ModelSettings,
    NativeTool,
    RunConfig,
    RunHooks,
    Runner,
    RunResultStreaming,
)
from agents.decorators import tool
from agents.model_preflight import tool_ownership
from agents.stream_events import RunCompleted
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tracing import TracingProcessor, metadata_only_run_tracing, set_trace_processors
from agents.tracing.span_data import FunctionSpanData, GenerationSpanData, ResponseSpanData


class MetadataCounter(TracingProcessor):
    """A fixed-label receiver; no payload, names, IDs or exception details leave it."""

    def __init__(self) -> None:
        self.finished_spans = 0
        self.child_spans = 0
        self.observations: list[dict[str, Any]] = []
        self._span_ids: dict[str, str] = {}

    def on_trace_start(self, trace: Any) -> None:
        pass

    def on_trace_end(self, trace: Any) -> None:
        pass

    def on_span_start(self, span: Any) -> None:
        self._span_ids.setdefault(span.span_id, f"span-{len(self._span_ids) + 1}")

    def on_span_end(self, span: Any) -> None:
        self.finished_spans += 1
        self.child_spans += int(span.parent_id is not None)
        data = span.span_data
        if isinstance(data, (GenerationSpanData, ResponseSpanData, FunctionSpanData)):
            is_model = isinstance(data, (GenerationSpanData, ResponseSpanData))
            # Spans alone do not prove which executor ran a same-named tool.
            owner = "unknown"
            outcome = data.operation.outcome
            status = (
                outcome
                if outcome in {"completed", "failed", "interrupted", "rejected"}
                else "unknown"
            )
            self.observations.append(
                {
                    "id": self._span_ids.get(span.span_id),
                    "parent": self._span_ids.get(span.parent_id),
                    "kind": "model" if is_model else "tool",
                    "status": status,
                    "logical_requests": None,
                    "observed_adapter_attempts": 1 + len(data.operation.retry_timestamps)
                    if is_model and data.operation.api_type in {"responses", "chat_completions"}
                    else None,
                    "transport_attempts": None,
                    "tool_owner": None if is_model else owner,
                }
            )

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


@dataclass
class View:
    states: list[str] = field(default_factory=lambda: ["running"])
    output: str | None = None
    # A span is not a network attempt. Native internal activity stays unknown.
    native_transport_attempts: None = None
    outer_requests: int = 0
    tool_owners: list[str] = field(default_factory=list)


class RequestCounter(RunHooks[Any]):
    def __init__(self, view: View) -> None:
        self.view = view

    async def on_llm_start(
        self, context: Any, agent: Any, system_prompt: Any, input_items: Any
    ) -> None:
        # Runner invokes this once per outer request, outside its retry loop.
        self.view.outer_requests += 1

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        # Use the execution hook's actual object, never an inferred tool name.
        if isinstance(tool, NativeTool):
            self.view.tool_owners.append("native")
        elif isinstance(tool, FunctionTool):
            self.view.tool_owners.append(tool_ownership([tool])[0].executor)
        else:
            self.view.tool_owners.append("unknown")


async def _cancel_unsubscribed_run(run: RunResultStreaming) -> None:
    """Stop a run whose approved reader never took ownership."""
    run.cancel()

    async def drain() -> None:
        async for _ in run.stream_events():
            pass

    # Keep cleanup independent of caller cancellation. Subscription already failed.
    cleanup = asyncio.gather(drain(), return_exceptions=True)
    cancellation = None
    while not cleanup.done():
        try:
            await asyncio.wait((cleanup,))
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
    if cancellation is not None:
        raise cancellation


async def display(
    agent: Agent[Any],
    prompt: str,
    *,
    allow_final: Callable[[str], bool],
    view: View | None = None,
    run_config: RunConfig | None = None,
) -> View:
    """Drain the producer and approve complete final text after successful cleanup.

    The reader skips previews, so it never buffers partial text for approval.
    The application supplies allow_final using its disclosure policy. Completion
    itself grants no permission to display output. Exceptions remain available
    to the caller's private diagnostics; this UI emits only fixed status labels.
    """
    view = view if view is not None else View()
    with metadata_only_run_tracing():
        try:
            run = Runner.run_streamed(
                agent,
                prompt,
                run_config=run_config,
                hooks=RequestCounter(view),
            )
            subscribed = False
            try:
                async with run.stream_approved_output() as events:
                    subscribed = True
                    async for event in events:
                        if isinstance(event, RunCompleted):
                            view.states.append("producer_complete")
            except RuntimeError:
                if not subscribed:
                    # An eager task factory can start the producer before we subscribe.
                    await _cancel_unsubscribed_run(run)
                raise
            if run.interruptions:
                view.states.append("paused")
            elif isinstance(run.final_output, str) and allow_final(run.final_output):
                view.output = run.final_output
                view.states.append("complete")
            else:
                view.states.append("rejected")
        except asyncio.CancelledError:
            view.states.append("cancelled")
            raise
        except Exception:
            # Do not copy provider/tool errors, tracebacks or rejected answers to UI.
            view.states.append("failed")
    return view


async def main() -> None:
    counter = MetadataCounter()
    # Configure once at standalone app startup; do not replace active processors.
    set_trace_processors([counter])

    @tool
    def lookup() -> str:
        """Return a synthetic public result from this SDK executor."""
        return "Public tool result"

    model = ScriptedModel(
        [
            RuntimeError("Synthetic private retry detail"),
            [function_call("lookup", {}, call_id="public-call")],
            [assistant_message("Public result")],
        ],
        emit_traces=True,
    )
    agent = Agent(
        name="demo",
        model=model,
        tools=[lookup],
        model_settings=ModelSettings(
            retry=ModelRetrySettings(
                max_retries=1,
                backoff={"initial_delay": 0},
                policy=lambda ctx: True,
            )
        ),
    )
    view = await display(
        agent,
        "Return a synthetic public answer.",
        allow_final=lambda text: text == "Public result",
    )
    print(view)
    for observation in counter.observations:
        print(observation)


if __name__ == "__main__":
    asyncio.run(main())
