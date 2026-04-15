from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import LocalSnapshotSpec, Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, Memory, Shell
from agents.sandbox.entries import File
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DEFAULT_MODEL = "gpt-5.4"
FIRST_PROMPT = "Inspect workspace and fix invoice total bug in src/acme_metrics/report.py."
SECOND_PROMPT = "Add a regression test for the previous bug you fixed."


def _build_manifest() -> Manifest:
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
        }
    )


def _build_agent(*, model: str, manifest: Manifest) -> SandboxAgent:
    # This one user-facing agent can read existing memory, update stale memory in place, and
    # generate new background memories when the sandbox session closes.
    return SandboxAgent(
        name="Sandbox Memory Demo",
        model=model,
        instructions=(
            "Answer questions about the sandbox workspace. Inspect files before answering, make "
            "minimal edits, and keep the response concise. "
            "Use the shell tool to inspect and validate the workspace. Use apply_patch for text "
            "edits when it is the clearest option. Do not invent files you did not read."
        ),
        default_manifest=manifest,
        capabilities=[
            # `Memory()` enables both read and generate behavior with live updates on by default.
            Memory(),
            Filesystem(),
            Shell(),
        ],
        # `Memory()` is the recommended default. If you need to tune the behavior, you can switch
        # to an explicit config such as:
        #
        # Memory(
        #     layout=MemoryLayoutConfig(memories_dir="agent_memory", sessions_dir="agent_sessions"),
        #     read=MemoryReadConfig(live_update=False),
        #     generate=MemoryGenerateConfig(max_raw_memories_for_consolidation=128),
        # )
        #
        # `generate.max_raw_memories_for_consolidation`: cap how many recent raw memories are
        # considered during consolidation. Older conversation-specific guidance may be removed from
        # consolidated memory when the cap is exceeded.
        #
        # Multi-turn conversations work best when all turns share the same live sandbox session and
        # an SDK Session. The SDK session_id groups those runs into one memory conversation. Without
        # an SDK session, sandbox memory falls back to OpenAI conversation_id, then RunConfig
        # group_id, then one generated memory conversation for each Runner.run().
        #
        # `read.live_update=False`: use this when the agent should not repair stale memory during
        # the run. That can save a few seconds, but stale memory debt can accumulate until a later
        # consolidation, which may or may not catch the staleness. It also prevents the agent from
        # updating memory immediately during the run, including when the user explicitly asks it to
        # remember something new or revise existing memory.
        #
        # If you need additional memory-generation guidance, `generate.extra_prompt` is appended to the
        # built-in memory prompt. Keep it short, ideally a few focused bullets and well under ~5k
        # tokens, so the model still pays attention to the conversation evidence.
        #
        # Memory(
        #     generate=MemoryGenerateConfig(
        #         extra_prompt="Pay extra attention to documenting what bug was fixed and why it happened."
        #     )
        # )
    )


def _artifact_paths(
    *, memories_dir: str = "memories", sessions_dir: str = "sessions"
) -> tuple[Path, ...]:
    return (
        Path(sessions_dir),
        Path(memories_dir) / "MEMORY.md",
        Path(memories_dir) / "memory_summary.md",
        Path(memories_dir) / "raw_memories.md",
        Path(memories_dir) / "raw_memories",
        Path(memories_dir) / "rollout_summaries",
    )


def _print_memory_tree(workspace_root: Path) -> None:
    print("\nGenerated memory artifacts:")
    for relative_path in _artifact_paths():
        full_path = workspace_root / relative_path
        if not full_path.exists():
            print(f"- {relative_path} (missing)")
            continue

        if full_path.is_dir():
            print(f"- {relative_path}/")
            for child in sorted(full_path.iterdir()):
                print(f"  - {relative_path / child.name}")
                if relative_path == Path("sessions"):
                    contents = child.read_text().rstrip()
                    if not contents:
                        print("    (empty)")
                    else:
                        for line in contents.splitlines():
                            print(f"    {line}")
            continue

        print(f"- {relative_path}")
        print(full_path.read_text().rstrip() or "(empty)")


def _run_config(*, sandbox: BaseSandboxSession, workflow_name: str) -> RunConfig:
    return RunConfig(
        sandbox=SandboxRunConfig(session=sandbox),
        workflow_name=workflow_name,
        tracing_disabled=True,
    )


async def main(*, model: str) -> None:
    manifest = _build_manifest()
    agent = _build_agent(model=model, manifest=manifest)
    client = UnixLocalSandboxClient()

    with tempfile.TemporaryDirectory(prefix="sandbox-memory-example-") as snapshot_dir:
        # Use a local snapshot so the second run resumes the same workspace in a new sandbox
        # session. That makes the second prompt rely on memory instead of in-process agent state.
        sandbox = await client.create(
            manifest=manifest,
            snapshot=LocalSnapshotSpec(base_path=Path(snapshot_dir)),
        )
        workspace_root = Path(sandbox.state.manifest.root)

        try:
            async with sandbox:
                # Run 1 fixes the bug and generates memory artifacts when the session closes.
                first = await Runner.run(
                    agent,
                    FIRST_PROMPT,
                    run_config=_run_config(
                        sandbox=sandbox,
                        workflow_name="Sandbox memory example: initial fix",
                    ),
                )
                print("\n[first run]")
                print(first.final_output)

            resumed_sandbox = await client.resume(sandbox.state)
            async with resumed_sandbox:
                # Run 2 starts from the resumed snapshot and reads the memory generated by run 1
                # before answering the follow-up prompt.
                second = await Runner.run(
                    agent,
                    SECOND_PROMPT,
                    run_config=_run_config(
                        sandbox=resumed_sandbox,
                        workflow_name="Sandbox memory example: follow-up",
                    ),
                )
                print("\n[second run]")
                print(second.final_output)

            _print_memory_tree(workspace_root)
        finally:
            await client.delete(sandbox)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run one sandbox agent twice across a snapshot resume with shared memory."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    args = parser.parse_args()
    asyncio.run(main(model=args.model))
