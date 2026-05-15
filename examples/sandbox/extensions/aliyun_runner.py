"""
Minimal Aliyun-backed sandbox example for manual validation.

This mirrors the other cloud extension examples: it creates a tiny workspace,
verifies stop/resume persistence, then asks a sandboxed agent to inspect the
workspace through one shell tool.

AgentRun (Alibaba Cloud) does not currently expose tunneled ports or
hosted-specific mount strategies, so this runner stays on the non-PTY,
non-mount path.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path
from typing import cast

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, Runner
from agents.models.openai_provider import OpenAIProvider
from agents.run import RunConfig
from agents.sandbox import LocalSnapshotSpec, Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.session import BaseSandboxSession

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox import AliyunSandboxClient, AliyunSandboxClientOptions
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Aliyun sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra aliyun"
    ) from exc


DEFAULT_QUESTION = "Summarize this cloud sandbox workspace in 2 sentences."
SNAPSHOT_CHECK_PATH = Path("snapshot-check.txt")
SNAPSHOT_CHECK_CONTENT = "aliyun snapshot round-trip ok\n"
LIVE_RESUME_CHECK_PATH = Path("live-resume-check.txt")
LIVE_RESUME_CHECK_CONTENT = "aliyun live resume ok\n"


def _build_manifest() -> Manifest:
    return text_manifest(
        {
            "README.md": (
                "# Aliyun Demo Workspace\n\n"
                "This workspace exists to validate the Aliyun AgentRun sandbox backend manually.\n"
            ),
            "handoff.md": (
                "# Handoff\n\n"
                "- Customer: Northwind Traders.\n"
                "- Goal: validate Aliyun sandbox exec and persistence flows.\n"
                "- Current status: non-PTY backend slice is wired and under test.\n"
            ),
            "todo.md": (
                "# Todo\n\n"
                "1. Inspect the workspace files.\n"
                "2. Summarize the current status in two sentences.\n"
            ),
        }
    )


async def _read_text(session: BaseSandboxSession, path: Path) -> str:
    data = await session.read(path)
    text = cast(str | bytes, data.read())
    if isinstance(text, bytes):
        return text.decode("utf-8")
    return text


def _require_env(name: str) -> None:
    if os.environ.get(name):
        return
    raise SystemExit(f"{name} must be set before running this example.")


def _require_aliyun_credentials(
    *,
    access_key_id: str | None,
    access_key_secret: str | None,
    account_id: str | None,
    api_key: str | None,
) -> None:
    if access_key_id and access_key_secret:
        return
    if account_id and api_key:
        return
    if os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID") and os.environ.get(
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET"
    ):
        return
    raise SystemExit(
        "Aliyun credentials are required. Set ALIBABA_CLOUD_ACCESS_KEY_ID and "
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET, or pass --access-key-id / --access-key-secret "
        "(optionally with --account-id / --api-key)."
    )


def _make_client(
    *,
    access_key_id: str | None,
    access_key_secret: str | None,
    account_id: str | None,
    api_key: str | None,
    region: str | None,
) -> AliyunSandboxClient:
    return AliyunSandboxClient(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        account_id=account_id,
        api_key=api_key,
        region=region,
    )


def _make_options(
    *,
    region: str,
    template_name: str,
    sandbox_idle_timeout_seconds: int,
) -> AliyunSandboxClientOptions:
    return AliyunSandboxClientOptions(
        region=region,
        template_name=template_name,
        sandbox_idle_timeout_seconds=sandbox_idle_timeout_seconds,
    )


async def _verify_stop_resume(
    *,
    manifest: Manifest,
    client: AliyunSandboxClient,
    options: AliyunSandboxClientOptions,
) -> None:
    with tempfile.TemporaryDirectory(prefix="aliyun-snapshot-example-") as snapshot_dir:
        sandbox = await client.create(
            manifest=manifest,
            snapshot=LocalSnapshotSpec(base_path=Path(snapshot_dir)),
            options=options,
        )

        try:
            await sandbox.start()
            await sandbox.write(
                SNAPSHOT_CHECK_PATH,
                io.BytesIO(SNAPSHOT_CHECK_CONTENT.encode("utf-8")),
            )
            await sandbox.stop()
        finally:
            await sandbox.shutdown()

        resumed_sandbox = await client.resume(sandbox.state)
        try:
            await resumed_sandbox.start()
            restored_text = await _read_text(resumed_sandbox, SNAPSHOT_CHECK_PATH)
            if restored_text != SNAPSHOT_CHECK_CONTENT:
                raise RuntimeError(
                    "Snapshot resume verification failed: "
                    f"expected {SNAPSHOT_CHECK_CONTENT!r}, got {restored_text!r}"
                )
        finally:
            await resumed_sandbox.aclose()

    print("snapshot round-trip ok")


async def _verify_resume_running_sandbox(
    *,
    manifest: Manifest,
    client: AliyunSandboxClient,
    options: AliyunSandboxClientOptions,
) -> None:
    sandbox = await client.create(manifest=manifest, options=options)

    try:
        await sandbox.start()
        await sandbox.write(
            LIVE_RESUME_CHECK_PATH,
            io.BytesIO(LIVE_RESUME_CHECK_CONTENT.encode("utf-8")),
        )
        serialized = client.serialize_session_state(sandbox.state)
        resumed_sandbox = await client.resume(client.deserialize_session_state(serialized))
        try:
            restored_text = await _read_text(resumed_sandbox, LIVE_RESUME_CHECK_PATH)
            if restored_text != LIVE_RESUME_CHECK_CONTENT:
                raise RuntimeError(
                    "Running sandbox resume verification failed: "
                    f"expected {LIVE_RESUME_CHECK_CONTENT!r}, got {restored_text!r}"
                )
        finally:
            await resumed_sandbox.aclose()
    finally:
        await sandbox.shutdown()

    print("running sandbox resume ok")


async def main(
    *,
    model: str,
    question: str,
    region: str,
    template_name: str,
    sandbox_idle_timeout_seconds: int,
    access_key_id: str | None,
    access_key_secret: str | None,
    account_id: str | None,
    api_key: str | None,
    stream: bool,
) -> None:
    _require_env("OPENAI_API_KEY")
    _require_aliyun_credentials(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        account_id=account_id,
        api_key=api_key,
    )

    manifest = _build_manifest()
    client = _make_client(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        account_id=account_id,
        api_key=api_key,
        region=region,
    )
    options = _make_options(
        region=region,
        template_name=template_name,
        sandbox_idle_timeout_seconds=sandbox_idle_timeout_seconds,
    )

    await _verify_stop_resume(manifest=manifest, client=client, options=options)
    await _verify_resume_running_sandbox(manifest=manifest, client=client, options=options)

    agent = SandboxAgent(
        name="Aliyun Sandbox Assistant",
        model=model,
        instructions=(
            "Answer questions about the sandbox workspace. Inspect the files before answering "
            "and keep the response concise. "
            "Do not invent files or statuses that are not present in the workspace. Cite the "
            "file names you inspected."
        ),
        default_manifest=manifest,
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
    )

    sandbox = await client.create(manifest=manifest, options=options)

    run_config = RunConfig(
        model_provider=OpenAIProvider(),
        sandbox=SandboxRunConfig(session=sandbox),
        # Disable tracing because it does not currently work reliably with alternate
        # upstreams such as AI Gateway, and provider config already comes from env.
        tracing_disabled=True,
        workflow_name="Aliyun sandbox example",
    )

    try:
        async with sandbox:
            if not stream:
                result = await Runner.run(agent, question, run_config=run_config)
                print(result.final_output)
                return

            stream_result = Runner.run_streamed(agent, question, run_config=run_config)
            saw_text_delta = False
            async for event in stream_result.stream_events():
                if event.type == "raw_response_event" and isinstance(
                    event.data, ResponseTextDeltaEvent
                ):
                    if not saw_text_delta:
                        print("assistant> ", end="", flush=True)
                        saw_text_delta = True
                    print(event.data.delta, end="", flush=True)

            if saw_text_delta:
                print()
    finally:
        await client.delete(sandbox)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.5", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--region",
        default="cn-hangzhou",
        help="AgentRun region id, for example `cn-hangzhou`.",
    )
    parser.add_argument(
        "--template-name",
        default="code-interpreter",
        help="AgentRun sandbox template name.",
    )
    parser.add_argument(
        "--sandbox-idle-timeout-seconds",
        type=int,
        default=3600,
        help="Idle timeout before AgentRun reclaims the remote sandbox.",
    )
    parser.add_argument(
        "--access-key-id",
        default=None,
        help="Alibaba Cloud access key id (overrides ALIBABA_CLOUD_ACCESS_KEY_ID).",
    )
    parser.add_argument(
        "--access-key-secret",
        default=None,
        help="Alibaba Cloud access key secret (overrides ALIBABA_CLOUD_ACCESS_KEY_SECRET).",
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help="Alibaba Cloud account id, if your AgentRun deployment requires it.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="AgentRun X-API-Key value, if your deployment uses API-key auth.",
    )
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            question=args.question,
            region=args.region,
            template_name=args.template_name,
            sandbox_idle_timeout_seconds=args.sandbox_idle_timeout_seconds,
            access_key_id=args.access_key_id,
            access_key_secret=args.access_key_secret,
            account_id=args.account_id,
            api_key=args.api_key,
            stream=args.stream,
        )
    )
