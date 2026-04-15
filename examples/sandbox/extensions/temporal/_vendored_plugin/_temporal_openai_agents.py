# vendored pre-release code; type errors are misreported due to patching
# mypy: ignore-errors
"""Initialize Temporal OpenAI Agents overrides."""

import dataclasses
import typing
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta

from temporalio.contrib.openai_agents._invoke_model_activity import ModelActivity
from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._openai_runner import (
    TemporalOpenAIRunner,
)
from temporalio.contrib.openai_agents._temporal_trace_provider import (
    TemporalTraceProvider,
)
from temporalio.contrib.openai_agents._trace_interceptor import (
    OpenAIAgentsContextPropagationInterceptor,
)
from temporalio.contrib.openai_agents.workflow import AgentsWorkflowError
from temporalio.contrib.opentelemetry._tracer_provider import ReplaySafeTracerProvider
from temporalio.contrib.pydantic import (
    PydanticPayloadConverter,
    ToJsonOptions,
)
from temporalio.converter import (
    DataConverter,
    DefaultPayloadConverter,
)
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from agents import ModelProvider, Trace, set_trace_provider
from agents.run import get_default_agent_runner, set_default_agent_runner
from agents.tracing import get_trace_provider
from agents.tracing.provider import DefaultTraceProvider

if typing.TYPE_CHECKING:
    from temporalio.contrib.openai_agents import (
        SandboxClientProvider,
        StatefulMCPServerProvider,
        StatelessMCPServerProvider,
    )


@contextmanager
def _set_open_ai_agent_temporal_overrides(
    model_params: ModelActivityParameters,
    start_spans_in_replay: bool = False,
):
    previous_runner = get_default_agent_runner()
    previous_trace_provider = get_trace_provider()
    provider = TemporalTraceProvider(
        start_spans_in_replay=start_spans_in_replay,
    )

    try:
        set_default_agent_runner(TemporalOpenAIRunner(model_params))
        set_trace_provider(provider)
        yield provider
    finally:
        set_default_agent_runner(previous_runner)
        set_trace_provider(previous_trace_provider or DefaultTraceProvider())


class OpenAIPayloadConverter(PydanticPayloadConverter):
    """PayloadConverter for OpenAI agents."""

    def __init__(self) -> None:
        """Initialize a payload converter."""
        super().__init__(ToJsonOptions(exclude_unset=True))


def _data_converter(converter: DataConverter | None) -> DataConverter:
    if converter is None:
        return DataConverter(payload_converter_class=OpenAIPayloadConverter)
    elif converter.payload_converter_class is DefaultPayloadConverter:
        return dataclasses.replace(converter, payload_converter_class=OpenAIPayloadConverter)
    elif not isinstance(converter.payload_converter, OpenAIPayloadConverter):
        raise ValueError("The payload converter must be of type OpenAIPayloadConverter.")
    return converter


class OpenAIAgentsPlugin(SimplePlugin):
    """Temporal plugin for integrating OpenAI agents with Temporal workflows.

    This plugin provides seamless integration between the OpenAI Agents SDK and
    Temporal workflows. It automatically configures the necessary interceptors,
    activities, and data converters to enable OpenAI agents to run within
    Temporal workflows with proper tracing and model execution.

    The plugin:
    1. Configures the Pydantic data converter for type-safe serialization
    2. Sets up tracing interceptors for OpenAI agent interactions
    3. Registers model execution activities
    4. Automatically registers MCP server activities and manages their lifecycles
    5. Manages the OpenAI agent runtime overrides during worker execution

    Example:
        >>> from temporalio.client import Client
        >>> from temporalio.worker import Worker
        >>> from temporalio.contrib.openai_agents import OpenAIAgentsPlugin, ModelActivityParameters, StatelessMCPServerProvider
        >>> from agents.mcp import MCPServerStdio
        >>> from datetime import timedelta
        >>>
        >>> # Configure model parameters
        >>> model_params = ModelActivityParameters(
        ...     start_to_close_timeout=timedelta(seconds=30),
        ...     retry_policy=RetryPolicy(maximum_attempts=3)
        ... )
        >>>
        >>> # Create MCP servers
        >>> filesystem_server = StatelessMCPServerProvider(MCPServerStdio(
        ...     name="Filesystem Server",
        ...     params={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}
        ... ))
        >>>
        >>> # Create plugin with MCP servers
        >>> plugin = OpenAIAgentsPlugin(
        ...     model_params=model_params,
        ...     mcp_server_providers=[filesystem_server]
        ... )
        >>>
        >>> # Use with client and worker
        >>> client = await Client.connect(
        ...     "localhost:7233",
        ...     plugins=[plugin]
        ... )
        >>> worker = Worker(
        ...     client,
        ...     task_queue="my-task-queue",
        ...     workflows=[MyWorkflow],
        ... )
    """

    def __init__(
        self,
        model_params: ModelActivityParameters | None = None,
        model_provider: ModelProvider | None = None,
        mcp_server_providers: Sequence[
            "StatelessMCPServerProvider | StatefulMCPServerProvider"
        ] = (),
        sandbox_clients: Sequence["SandboxClientProvider"] = (),
        register_activities: bool = True,
        add_temporal_spans: bool = True,
        use_otel_instrumentation: bool = False,
    ) -> None:
        """Initialize the OpenAI agents plugin.

        Args:
            model_params: Configuration parameters for Temporal activity execution
                of model calls. If None, default parameters will be used.
            model_provider: Optional model provider for custom model implementations.
                Useful for testing or custom model integrations.
            mcp_server_providers: Sequence of MCP servers to automatically register with the worker.
                Each server will be wrapped in a TemporalMCPServer if not already wrapped,
                and their activities will be automatically registered with the worker.
                The plugin manages the connection lifecycle of these servers.
            sandbox_clients: Sequence of named sandbox client providers to register
                on the worker.  Each provider pairs a unique name with a real
                ``BaseSandboxClient`` (e.g. ``DaytonaSandboxClient``,
                ``UnixLocalSandboxClient``).  On the workflow side, use
                :func:`~temporalio.contrib.openai_agents.workflow.temporal_sandbox_client`
                with the matching name to target the correct backend.
            register_activities: Whether to register activities during the worker execution.
                This can be disabled on some workers to allow a separation of workflows and activities
                but should not be disabled on all workers, or agents will not be able to progress.
            add_temporal_spans: Whether to add temporal spans to traces
            use_otel_instrumentation: If set to true, enable open telemetry instrumentation.
                Warning: use_otel_instrumentation is experimental and behavior may change in future versions.
                Use with caution in production environments.

        """
        if model_params is None:
            model_params = ModelActivityParameters()

        # For the default provider, we provide a default start_to_close_timeout of 60 seconds.
        # Other providers will need to define their own.
        if (
            model_params.start_to_close_timeout is None
            and model_params.schedule_to_close_timeout is None
        ):
            if model_provider is None:
                model_params.start_to_close_timeout = timedelta(seconds=60)
            else:
                raise ValueError(
                    "When configuring a custom provider, the model activity must have start_to_close_timeout or schedule_to_close_timeout"
                )

        # Store OTEL configuration for later setup
        self._instrumented = False
        self._use_otel_instrumentation = use_otel_instrumentation

        # Delay activity construction until they are actually needed
        def add_activities(
            activities: Sequence[Callable] | None,
        ) -> Sequence[Callable]:
            if not register_activities:
                return activities or []

            new_activities = [ModelActivity(model_provider).invoke_model_activity]

            server_names = [server.name for server in mcp_server_providers]
            if len(server_names) != len(set(server_names)):
                raise ValueError(
                    "More than one mcp server registered with the same name. Please provide unique names."
                )

            for mcp_server in mcp_server_providers:
                new_activities.extend(mcp_server._get_activities())

            sandbox_names = [sc.name for sc in sandbox_clients]
            if len(sandbox_names) != len(set(sandbox_names)):
                raise ValueError(
                    "More than one sandbox client registered with the same name. Please provide unique names."
                )

            for sandbox_provider in sandbox_clients:
                new_activities.extend(sandbox_provider._get_activities())

            return list(activities or []) + new_activities

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if not runner:
                raise ValueError("No WorkflowRunner provided to the OpenAI plugin.")

            # If in sandbox, add additional passthrough
            if isinstance(runner, SandboxedWorkflowRunner):
                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        "openai", "agents", "mcp"
                    ),
                )
            return runner

        if not use_otel_instrumentation:
            interceptor = OpenAIAgentsContextPropagationInterceptor(
                add_temporal_spans=add_temporal_spans,
            )
        else:
            from opentelemetry import trace as otel_trace

            from ._otel_trace_interceptor import (
                OTelOpenAIAgentsContextPropagationInterceptor,
            )

            provider = otel_trace.get_tracer_provider()
            if not isinstance(provider, ReplaySafeTracerProvider):
                raise ValueError(
                    "Global tracer provider must a ReplaySafeTracerProvider. Use temporalio.contrib.opentelemtry.create_trace_provider to create one."
                )

            interceptor = OTelOpenAIAgentsContextPropagationInterceptor(
                add_temporal_spans=add_temporal_spans,
                otel_id_generator=provider.id_generator(),
            )

        @asynccontextmanager
        async def run_context() -> AsyncIterator[None]:
            with self.tracing_context():
                with _set_open_ai_agent_temporal_overrides(
                    model_params,
                    start_spans_in_replay=use_otel_instrumentation,
                ):
                    yield

        super().__init__(
            name="OpenAIAgentsPlugin",
            data_converter=_data_converter,
            interceptors=[interceptor],
            activities=add_activities,
            workflow_runner=workflow_runner,
            workflow_failure_exception_types=[AgentsWorkflowError],
            run_context=lambda: run_context(),
        )

    @contextmanager
    def tracing_context(self) -> Iterator[None]:
        """Context manager for setting up OpenAI Agents tracing instrumentation.

        This should be called if AgentsSDK traces and/or spans are started outside of the context of a worker.
        For example:

        .. code-block:: python

            with env.openai_agents_plugin.tracing_context():
                with trace("External trace"):
                    with custom_span("External span"):
                        workflow_handle = await new_client.start_workflow(
                            ...
                        )

        Yields:
            Context with tracing instrumentation enabled.
        """
        # Set up OTEL instrumentation if exporters are provided
        otel_instrumentor = None
        if self._use_otel_instrumentation and not self._instrumented:
            from openinference.instrumentation.openai_agents import (
                OpenAIAgentsInstrumentor,
            )
            from openinference.instrumentation.openai_agents._processor import (
                OpenInferenceTracingProcessor,
            )
            from opentelemetry import trace
            from opentelemetry.context import attach
            from opentelemetry.trace import set_span_in_context

            # Unfortunate monkey patching is needed to ensure the trace is set in context so we can propagate it.
            original_on_trace_start = OpenInferenceTracingProcessor.on_trace_start

            def on_trace_start(self, trace: Trace) -> None:  # type: ignore[reportMissingParameterType]
                original_on_trace_start(self, trace)
                otel_span = self._root_spans[trace.trace_id]
                attach(set_span_in_context(otel_span))

            OpenInferenceTracingProcessor.on_trace_start = on_trace_start  # type:ignore[method-assign]

            # Set up instrumentor
            otel_instrumentor = OpenAIAgentsInstrumentor()
            otel_instrumentor.instrument(tracer_provider=trace.get_tracer_provider())
            self._instrumented = True
        try:
            yield
        finally:
            # Clean up OTEL instrumentation
            if otel_instrumentor is not None:
                otel_instrumentor.uninstrument()
