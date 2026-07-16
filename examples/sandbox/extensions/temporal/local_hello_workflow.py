"""Minimal local Temporal SandboxAgent workflow example.

This example is intentionally smaller than ``temporal_sandbox_agent.py``. It starts a local
Temporal test server through the Temporal Python SDK, runs a ``SandboxAgent`` workflow against
the local Unix sandbox backend, and then shuts everything down.

It does not require the Temporal CLI, a long-running Temporal server, or cloud sandbox backend
credentials. It does require ``OPENAI_API_KEY`` because the model call runs through the Temporal
OpenAI Agents plugin as an activity.

Usage:
    uv run --extra temporal python -m examples.sandbox.extensions.temporal.local_hello_workflow
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
    SandboxClientProvider,
)
from temporalio.contrib.openai_agents.workflow import temporal_sandbox_client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File
from agents.sandbox.sandboxes import UnixLocalSandboxClient, UnixLocalSandboxClientOptions

TASK_QUEUE = "local-temporal-sandbox-agent"
WORKFLOW_ID = "local-temporal-sandbox-agent-workflow"
DEFAULT_MODEL = "gpt-5.4-mini"
EXPECTED_GREETING = "Temporal sandbox says hello from a local file"
TRACE_MODE_NONE = "none"
TRACE_MODE_OPENAI = "openai"
TRACE_MODE_OPENAI_WITH_TEMPORAL_SPANS = "openai_with_temporal_spans"
TRACE_MODES = {
    TRACE_MODE_NONE,
    TRACE_MODE_OPENAI,
    TRACE_MODE_OPENAI_WITH_TEMPORAL_SPANS,
}


@workflow.defn
class LocalSandboxAgentWorkflow:
    @workflow.run
    async def run(self, model: str, trace_mode: str) -> str:
        agent = SandboxAgent(
            name="Local Temporal Sandbox Agent",
            model=model,
            instructions=(
                "Inspect the sandbox workspace with the shell tool before answering. "
                "Report the greeting from README.md exactly."
            ),
            default_manifest=Manifest(
                entries={
                    "README.md": File(content=b"Temporal sandbox says hello from a local file.\n"),
                }
            ),
            capabilities=[Shell()],
            model_settings=ModelSettings(tool_choice="required"),
        )

        result = await Runner.run(
            agent,
            "Read README.md and report its greeting.",
            run_config=RunConfig(
                sandbox=SandboxRunConfig(
                    client=temporal_sandbox_client("local"),
                    options=UnixLocalSandboxClientOptions(),
                ),
                workflow_name="Local Temporal SandboxAgent workflow",
                tracing_disabled=trace_mode == TRACE_MODE_NONE,
            ),
        )
        return str(result.final_output)


def _client_with_plugin(client: Client, trace_mode: str) -> Client:
    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(start_to_close_timeout=timedelta(seconds=120)),
        sandbox_clients=[SandboxClientProvider("local", UnixLocalSandboxClient())],
        add_temporal_spans=trace_mode == TRACE_MODE_OPENAI_WITH_TEMPORAL_SPANS,
    )
    config = client.config()
    config["plugins"] = [*config.get("plugins", []), plugin]
    return Client(**config)


def _require_env(name: str) -> None:
    if not os.environ.get(name):
        raise SystemExit(f"{name} must be set before running this example.")


def _trace_mode_from_env() -> str:
    trace_mode = os.getenv("EXAMPLES_TEMPORAL_TRACE", TRACE_MODE_OPENAI).strip().lower()
    if trace_mode not in TRACE_MODES:
        supported = ", ".join(sorted(TRACE_MODES))
        raise SystemExit(
            f"EXAMPLES_TEMPORAL_TRACE must be one of: {supported}. Got {trace_mode!r}."
        )
    return trace_mode


async def main() -> None:
    _require_env("OPENAI_API_KEY")
    model = os.getenv("EXAMPLES_TEMPORAL_MODEL", DEFAULT_MODEL)
    trace_mode = _trace_mode_from_env()
    print(f"Using model: {model}")
    print(f"Using trace mode: {trace_mode}")
    print("Starting local Temporal test server...")
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = _client_with_plugin(env.client, trace_mode)
        print("Starting local Temporal worker...")
        async with Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[LocalSandboxAgentWorkflow],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "annotated_types",
                    "pydantic_core",
                ),
            ),
        ):
            result = await client.execute_workflow(
                LocalSandboxAgentWorkflow.run,
                args=[model, trace_mode],
                id=WORKFLOW_ID,
                task_queue=TASK_QUEUE,
            )

    print(f"Workflow result: {result}")
    if EXPECTED_GREETING not in result:
        raise RuntimeError(f"Expected workflow result to contain {EXPECTED_GREETING!r}.")
    print("Local Temporal SandboxAgent workflow completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
