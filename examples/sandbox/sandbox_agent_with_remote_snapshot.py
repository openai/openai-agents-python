"""
Sandbox agent example using a dependency-injected remote snapshot client.

This demonstrates persisting a Unix-local sandbox workspace to S3 with `RemoteSnapshotSpec`,
then resuming the session from the downloaded snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
from pathlib import Path

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, RemoteSnapshotSpec, SandboxAgent, SandboxRunConfig
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from agents.sandbox.session import Dependencies

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

S3_BUCKET_ENV_VAR = "S3_MOUNT_BUCKET"
SNAPSHOT_OBJECT_PREFIX = "openai-agents-python/sandbox-snapshots"
SNAPSHOT_CLIENT_DEPENDENCY_KEY = "examples.remote_snapshot.s3_client"
SNAPSHOT_CHECK_PATH = Path("snapshot-check.txt")
SNAPSHOT_CHECK_CONTENT = "remote snapshot round-trip ok\n"


class S3SnapshotClient:
    """Minimal S3 client adapter for `RemoteSnapshot`."""

    def __init__(self, *, bucket: str, prefix: str) -> None:
        try:
            import boto3  # type: ignore[import-untyped]
        except Exception as exc:  # pragma: no cover - optional local dependency
            raise SystemExit(
                "This example requires boto3 for S3 snapshot storage.\n"
                "Install it with: uv sync --extra s3"
            ) from exc

        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._s3 = boto3.client("s3")

    def upload(self, snapshot_id: str, data: io.IOBase) -> None:
        self._s3.upload_fileobj(data, self._bucket, self._object_key(snapshot_id))

    def download(self, snapshot_id: str) -> io.IOBase:
        buffer = io.BytesIO()
        self._s3.download_fileobj(self._bucket, self._object_key(snapshot_id), buffer)
        buffer.seek(0)
        return buffer

    def exists(self, snapshot_id: str) -> bool:
        from botocore.exceptions import ClientError  # type: ignore[import-untyped]

        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._object_key(snapshot_id))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def _object_key(self, snapshot_id: str) -> str:
        return f"{self._prefix}/{snapshot_id}.tar"


def _build_manifest() -> Manifest:
    return text_manifest(
        {
            "README.md": (
                "# Remote Snapshot Demo\n\n"
                "This workspace exists to show a sandbox session persisting its snapshot to S3.\n"
            ),
            "status.md": (
                "# Status\n\n"
                "- The first run writes a snapshot check file into the workspace.\n"
                "- The resumed run verifies that the file came back from remote storage.\n"
            ),
        }
    )


def _build_agent(*, model: str, manifest: Manifest) -> SandboxAgent:
    return SandboxAgent(
        name="Remote Snapshot Assistant",
        model=model,
        instructions=(
            "Inspect the sandbox workspace before answering. Keep the response concise and "
            "mention the file names you used. "
            "Do not invent files or state. Only describe what is present in the workspace."
        ),
        default_manifest=manifest,
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
    )


def _require_s3_bucket() -> str:
    bucket = os.environ.get(S3_BUCKET_ENV_VAR)
    if not bucket:
        raise SystemExit(f"{S3_BUCKET_ENV_VAR} must be set before running this example.")
    return bucket


async def _verify_remote_snapshot_round_trip(*, model: str) -> None:
    manifest = _build_manifest()
    dependencies = Dependencies().bind_value(
        SNAPSHOT_CLIENT_DEPENDENCY_KEY,
        S3SnapshotClient(bucket=_require_s3_bucket(), prefix=SNAPSHOT_OBJECT_PREFIX),
    )
    client = UnixLocalSandboxClient(dependencies=dependencies)

    sandbox = await client.create(
        manifest=manifest,
        snapshot=RemoteSnapshotSpec(client_dependency_key=SNAPSHOT_CLIENT_DEPENDENCY_KEY),
        options=None,
    )

    try:
        await sandbox.start()
        await sandbox.write(SNAPSHOT_CHECK_PATH, io.BytesIO(SNAPSHOT_CHECK_CONTENT.encode("utf-8")))
        await sandbox.stop()
    finally:
        await sandbox.shutdown()

    resumed_sandbox = await client.resume(sandbox.state)
    try:
        await resumed_sandbox.start()
        restored = await resumed_sandbox.read(SNAPSHOT_CHECK_PATH)
        restored_text = restored.read()
        if isinstance(restored_text, bytes):
            restored_text = restored_text.decode("utf-8")
        if restored_text != SNAPSHOT_CHECK_CONTENT:
            raise RuntimeError(
                "Remote snapshot resume verification failed: "
                f"expected {SNAPSHOT_CHECK_CONTENT!r}, got {restored_text!r}"
            )
    finally:
        await resumed_sandbox.aclose()

    agent = _build_agent(model=model, manifest=manifest)
    result = await Runner.run(
        agent,
        "Summarize this workspace in one sentence.",
        run_config=RunConfig(
            sandbox=SandboxRunConfig(client=client),
            workflow_name="Remote snapshot sandbox example",
        ),
    )

    print("snapshot round-trip ok (s3)")
    print(result.final_output)


async def main(model: str) -> None:
    await _verify_remote_snapshot_round_trip(model=model)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.4", help="Model name to use.")
    args = parser.parse_args()

    asyncio.run(main(args.model))
