"""
Minimal E2B-backed sandbox example for manual validation.

This example is intentionally small: it creates a tiny workspace, lets the
agent inspect it through one shell tool, and prints a short answer.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Literal

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox import (
        E2BSandboxClient,
        E2BSandboxClientOptions,
        E2BSandboxType,
    )
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "E2B sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra e2b"
    ) from exc


DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."
DEFAULT_SANDBOX_TYPE = E2BSandboxType.E2B_ASYNC.value


def _build_manifest() -> Manifest:
    return text_manifest(
        {
            "README.md": (
                "# Renewal Notes\n\n"
                "This workspace contains a tiny account review packet for manual sandbox testing.\n"
            ),
            "customer.md": (
                "# Customer\n\n"
                "- Name: Northwind Health.\n"
                "- Renewal date: 2026-04-15.\n"
                "- Risk: unresolved SSO setup.\n"
            ),
            "next_steps.md": (
                "# Next steps\n\n"
                "1. Finish the SSO fix.\n"
                "2. Confirm legal language before procurement review.\n"
            ),
        }
    )


def _require_env(name: str) -> None:
    if os.environ.get(name):
        return
    raise SystemExit(f"{name} must be set before running this example.")


def _rewrite_template_resolution_error(exc: Exception) -> None:
    message = str(exc)
    marker = "error resolving template '"
    if marker not in message:
        return
    template = message.split(marker, 1)[1].split("'", 1)[0]
    raise SystemExit(
        f"E2B could not resolve template `{template}`.\n"
        "Pass `--template <your-template>` with a template that exists for this E2B account/team. "
        "If you were relying on the example default, the SDK default template for this backend is "
        "not available in your current E2B environment."
    ) from exc


async def main(
    *,
    model: str,
    question: str,
    sandbox_type: Literal[
        "e2b_code_interpreter_async",
        "e2b_code_interpreter",
        "e2b_async",
        "e2b",
    ],
    template: str | None,
    timeout: int | None,
    pause_on_exit: bool,
    stream: bool,
) -> None:
    _require_env("OPENAI_API_KEY")
    _require_env("E2B_API_KEY")

    manifest = _build_manifest()
    agent = SandboxAgent(
        name="E2B Sandbox Assistant",
        model=model,
        # `instructions` is the base agent instructions for this sandbox task.
        instructions=(
            "Answer questions about the sandbox workspace. Inspect the files before answering "
            "and keep the response concise."
        ),
        # `developer_instructions` is appended after that as additional deterministic instructions.
        # Here, the grounding constraints are kept in `developer_instructions`.
        developer_instructions=(
            "Do not invent files or statuses that are not present in the workspace. Cite the "
            "file names you inspected."
        ),
        default_manifest=manifest,
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    run_config = RunConfig(
        sandbox=SandboxRunConfig(
            client=E2BSandboxClient(),
            options=E2BSandboxClientOptions(
                sandbox_type=E2BSandboxType(sandbox_type),
                template=template,
                timeout=timeout,
                pause_on_exit=pause_on_exit,
            ),
        ),
        workflow_name="E2B sandbox example",
    )

    if not stream:
        try:
            result = await Runner.run(agent, question, run_config=run_config)
        except Exception as exc:
            _rewrite_template_resolution_error(exc)
            raise
        print(result.final_output)
        return

    try:
        stream_result = Runner.run_streamed(agent, question, run_config=run_config)
    except Exception as exc:
        _rewrite_template_resolution_error(exc)
        raise
    saw_text_delta = False
    try:
        async for event in stream_result.stream_events():
            if event.type == "raw_response_event" and isinstance(
                event.data, ResponseTextDeltaEvent
            ):
                if not saw_text_delta:
                    print("assistant> ", end="", flush=True)
                    saw_text_delta = True
                print(event.data.delta, end="", flush=True)
    except Exception as exc:
        _rewrite_template_resolution_error(exc)
        raise

    if saw_text_delta:
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.4", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--sandbox-type",
        default=DEFAULT_SANDBOX_TYPE,
        choices=[member.value for member in E2BSandboxType],
        help=(
            "E2B sandbox implementation to create. Defaults to the generic async sandbox because "
            "some installed code-interpreter SDK versions still request the legacy "
            "`code-interpreter-v1` template."
        ),
    )
    parser.add_argument("--template", default=None, help="Optional E2B template name.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Optional E2B sandbox timeout in seconds.",
    )
    parser.add_argument(
        "--pause-on-exit",
        action="store_true",
        default=False,
        help="Pause the sandbox on shutdown instead of killing it.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            sandbox_type=args.sandbox_type,
            template=args.template,
            timeout=args.timeout,
            pause_on_exit=args.pause_on_exit,
            stream=args.stream,
        )
    )
