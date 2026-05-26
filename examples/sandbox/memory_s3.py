from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import (
    Manifest,
    MemoryGenerateConfig,
    MemoryLayoutConfig,
    SandboxAgent,
    SandboxRunConfig,
)
from agents.sandbox.capabilities import Filesystem, Memory, Shell
from agents.sandbox.entries import File, InContainerMountStrategy, RcloneMountPattern, S3Mount
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
    DockerSandboxClientOptions,
)
from agents.sandbox.session import SandboxSession

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.sandbox.basic import _import_docker_from_env
from examples.sandbox.docker.mounts.mount_smoke import IMAGE as MOUNT_IMAGE, ensure_mount_image

DEFAULT_MODEL = "gpt-5.5"
DEFAULT_MOUNT_DIR = "persistent"
FIRST_PROMPT = "Inspect workspace and fix invoice total bug in src/acme_metrics/report.py."
SECOND_PROMPT = (
    "Add a regression test for the previous bug you fixed. Put it in "
    "tests/test_invoice_regression.py."
)
MEMORY_EXTRA_PROMPT = (
    "This is an S3-backed memory demo. If a run fixes a concrete code bug, remember the "
    "specific file path, test expectation, root cause, and patch so a future fresh sandbox can "
    "reuse the fix instead of rediscovering it."
)


@dataclass(frozen=True)
class S3MemoryExampleConfig:
    bucket: str
    access_key_id: str | None
    secret_access_key: str | None
    session_token: str | None
    region: str | None
    endpoint_url: str | None
    prefix: str

    @classmethod
    def from_env(cls, *, prefix: str | None = None) -> S3MemoryExampleConfig:
        bucket = os.getenv("S3_BUCKET") or os.getenv("S3_MOUNT_BUCKET")
        if not bucket:
            raise SystemExit(
                "Missing S3 bucket name. Set S3_BUCKET or S3_MOUNT_BUCKET. "
                "This example works well with: source ~/.s3.env"
            )
        resolved_prefix = (
            prefix
            or os.getenv("S3_MOUNT_PREFIX", f"sandbox-memory-example/{uuid.uuid4().hex}")
            or f"sandbox-memory-example/{uuid.uuid4().hex}"
        )
        return cls(
            bucket=bucket,
            access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            session_token=os.getenv("AWS_SESSION_TOKEN"),
            region=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
            endpoint_url=os.getenv("S3_ENDPOINT_URL"),
            prefix=resolved_prefix.strip("/"),
        )


def _persistent_layout(*, mount_dir: str = DEFAULT_MOUNT_DIR) -> MemoryLayoutConfig:
    return MemoryLayoutConfig(
        memories_dir=f"{mount_dir}/memories",
        sessions_dir=f"{mount_dir}/sessions",
    )


def _artifact_paths(*, mount_dir: str = DEFAULT_MOUNT_DIR) -> tuple[Path, ...]:
    layout = _persistent_layout(mount_dir=mount_dir)
    return (
        Path(layout.sessions_dir),
        Path(layout.memories_dir) / "MEMORY.md",
        Path(layout.memories_dir) / "memory_summary.md",
        Path(layout.memories_dir) / "raw_memories.md",
        Path(layout.memories_dir) / "raw_memories",
        Path(layout.memories_dir) / "rollout_summaries",
    )


def _build_manifest(
    *, config: S3MemoryExampleConfig, mount_dir: str = DEFAULT_MOUNT_DIR
) -> Manifest:
    return Manifest(
        entries={
            "README.md": File(
                content=(
                    b"# Acme Metrics\n\n"
                    b"Small demo package for validating invoice total formatting.\n"
                )
            ),
            "pyproject.toml": File(
                content=(
                    b"[project]\n"
                    b'name = "acme-metrics"\n'
                    b'version = "0.1.0"\n'
                    b'requires-python = ">=3.10"\n'
                    b"\n"
                    b"[tool.pytest.ini_options]\n"
                    b'pythonpath = ["src"]\n'
                )
            ),
            "src/acme_metrics/__init__.py": File(
                content=b"from .report import format_invoice_total\n"
            ),
            "src/acme_metrics/report.py": File(
                content=(
                    b"from __future__ import annotations\n\n"
                    b"def format_invoice_total(subtotal: float, tax_rate: float) -> str:\n"
                    b"    total = subtotal + tax_rate\n"
                    b'    return f"${total:.2f}"\n'
                )
            ),
            "tests/test_report.py": File(
                content=(
                    b"from acme_metrics import format_invoice_total\n\n\n"
                    b"def test_format_invoice_total_applies_tax_rate() -> None:\n"
                    b'    assert format_invoice_total(100.0, 0.075) == "$107.50"\n'
                )
            ),
            mount_dir: S3Mount(
                bucket=config.bucket,
                access_key_id=config.access_key_id,
                secret_access_key=config.secret_access_key,
                session_token=config.session_token,
                prefix=config.prefix,
                region=config.region,
                endpoint_url=config.endpoint_url,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                read_only=False,
            ),
        }
    )


def _build_agent(
    *, model: str, manifest: Manifest, mount_dir: str = DEFAULT_MOUNT_DIR
) -> SandboxAgent:
    return SandboxAgent(
        name="Sandbox Memory S3 Demo",
        model=model,
        instructions=(
            "Answer questions about the sandbox workspace. Inspect files before answering, make "
            "minimal edits, and keep the response concise. "
            "Use the shell tool to inspect and validate the workspace. Use apply_patch for text "
            "edits when it is the clearest option. Do not invent files you did not read."
        ),
        default_manifest=manifest,
        capabilities=[
            Memory(
                layout=_persistent_layout(mount_dir=mount_dir),
                generate=MemoryGenerateConfig(extra_prompt=MEMORY_EXTRA_PROMPT),
            ),
            Filesystem(),
            Shell(),
        ],
    )


def _run_config(*, sandbox: SandboxSession, workflow_name: str) -> RunConfig:
    return RunConfig(
        sandbox=SandboxRunConfig(session=sandbox),
        workflow_name=workflow_name,
        tracing_disabled=True,
    )


async def _read_text(session: SandboxSession, path: str) -> str:
    handle = await session.read(Path(path))
    try:
        payload = handle.read()
    finally:
        handle.close()
    if isinstance(payload, bytes):
        return payload.decode("utf-8")
    return str(payload)


async def _path_exists(session: SandboxSession, path: Path) -> bool:
    result = await session.exec("test", "-e", str(path), shell=False)
    return result.ok()


async def _path_is_dir(session: SandboxSession, path: Path) -> bool:
    result = await session.exec("test", "-d", str(path), shell=False)
    return result.ok()


async def _assert_fixed(session: SandboxSession) -> None:
    report_py = await _read_text(session, "src/acme_metrics/report.py")
    if "subtotal * (1 + tax_rate)" not in report_py:
        raise RuntimeError("Sandbox did not apply expected invoice total fix.")


async def _assert_memory_summary_generated(session: SandboxSession) -> None:
    memory_summary = await _read_text(session, f"{DEFAULT_MOUNT_DIR}/memories/memory_summary.md")
    if not memory_summary.strip():
        raise RuntimeError(
            "First sandbox session did not generate a memory summary in S3-backed storage."
        )


async def _assert_regression_test_added(session: SandboxSession) -> None:
    test_path = Path("tests/test_invoice_regression.py")
    if not await _path_exists(session, test_path):
        raise RuntimeError("Sandbox did not add the expected regression test file.")

    regression_test = await _read_text(session, str(test_path))
    if "format_invoice_total" not in regression_test:
        raise RuntimeError("Regression test does not exercise format_invoice_total.")


async def _print_tree(session: SandboxSession, *, mount_dir: str = DEFAULT_MOUNT_DIR) -> None:
    print("\nS3-backed memory artifacts:")
    for relative_path in _artifact_paths(mount_dir=mount_dir):
        if not await _path_exists(session, relative_path):
            print(f"- {relative_path} (missing)")
            continue
        if await _path_is_dir(session, relative_path):
            print(f"- {relative_path}/")
            children = await session.ls(relative_path)
            for child in sorted(children, key=lambda entry: entry.path):
                child_name = Path(child.path).name
                if child_name in {".", ".."}:
                    continue
                print(f"  - {relative_path / child_name}")
            continue
        print(f"- {relative_path}")
        print((await _read_text(session, str(relative_path))).rstrip() or "(empty)")


async def _create_session(*, manifest: Manifest) -> tuple[DockerSandboxClient, SandboxSession]:
    docker_from_env = _import_docker_from_env()
    docker_client = docker_from_env()
    sandbox_client = DockerSandboxClient(docker_client)
    sandbox = await sandbox_client.create(
        manifest=manifest,
        options=DockerSandboxClientOptions(image=MOUNT_IMAGE),
    )
    return sandbox_client, sandbox


async def _print_persisted_tree(*, manifest: Manifest) -> None:
    inspect_client, inspect_sandbox = await _create_session(manifest=manifest)
    try:
        async with inspect_sandbox:
            await _print_tree(inspect_sandbox)
    finally:
        await inspect_client.delete(inspect_sandbox)


async def main(*, model: str, prefix: str | None) -> None:
    ensure_mount_image()
    config = S3MemoryExampleConfig.from_env(prefix=prefix)
    manifest = _build_manifest(config=config)
    agent = _build_agent(model=model, manifest=manifest)

    first_client, first_sandbox = await _create_session(manifest=manifest)
    try:
        async with first_sandbox:
            first = await Runner.run(
                agent,
                FIRST_PROMPT,
                run_config=_run_config(
                    sandbox=first_sandbox,
                    workflow_name="Sandbox memory S3 example: first sandbox",
                ),
            )
            print("\n[first sandbox]")
            print(first.final_output)
            await _assert_fixed(first_sandbox)
    finally:
        await first_client.delete(first_sandbox)

    second_client, second_sandbox = await _create_session(manifest=manifest)
    try:
        async with second_sandbox:
            await _assert_memory_summary_generated(second_sandbox)

            second = await Runner.run(
                agent,
                SECOND_PROMPT,
                run_config=_run_config(
                    sandbox=second_sandbox,
                    workflow_name="Sandbox memory S3 example: second sandbox",
                ),
            )
            print("\n[second sandbox]")
            print(second.final_output)
            await _assert_regression_test_added(second_sandbox)
    finally:
        await second_client.delete(second_sandbox)

    await _print_persisted_tree(manifest=manifest)
    print(f"\nS3 prefix: {config.prefix}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run sandbox memory across two fresh Docker sandboxes with S3-backed storage."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    parser.add_argument(
        "--prefix",
        default=None,
        help="Optional S3 prefix for mounted memory artifacts. Defaults to a unique prefix.",
    )
    args = parser.parse_args()
    asyncio.run(main(model=args.model, prefix=args.prefix))
