from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from agents import Runner, SQLiteSession
from agents.run import RunConfig
from agents.sandbox import Manifest, MemoryLayoutConfig, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, Memory, Shell
from agents.sandbox.entries import Dir, File
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DEFAULT_MODEL = "gpt-5.5"
GTM_SESSION_ID = "gtm-q2-pipeline-review"
ENGINEERING_SESSION_ID = "eng-invoice-test-fix"

GTM_TURN_1 = (
    "Analyze data/leads.csv. Find one promising GTM segment, explain why, and say what "
    "follow-up data you need."
)
GTM_TURN_2 = (
    "Using your previous GTM analysis, write a short outreach hypothesis and save it to "
    "gtm_hypothesis.md."
)
ENGINEERING_TURN = (
    "Fix the invoice total bug in src/acme_metrics/report.py, then run the test suite."
)


def _build_manifest() -> Manifest:
    return Manifest(
        entries={
            "data": Dir(
                children={
                    "leads.csv": File(
                        content=(
                            b"account,segment,seats,trial_events,monthly_spend\n"
                            b"Northstar Health,healthcare,240,98,18000\n"
                            b"Beacon Retail,retail,75,18,4200\n"
                            b"Apex Fintech,financial-services,180,76,13500\n"
                            b"Summit Labs,healthcare,52,22,3900\n"
                        )
                    )
                }
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
            "src": Dir(
                children={
                    "acme_metrics": Dir(
                        children={
                            "__init__.py": File(
                                content=b"from .report import format_invoice_total\n"
                            ),
                            "report.py": File(
                                content=(
                                    b"from __future__ import annotations\n\n"
                                    b"def format_invoice_total(subtotal: float, tax_rate: float) -> str:\n"
                                    b"    total = subtotal + tax_rate\n"
                                    b'    return f"${total:.2f}"\n'
                                )
                            ),
                        }
                    )
                }
            ),
            "tests": Dir(
                children={
                    "test_report.py": File(
                        content=(
                            b"from acme_metrics import format_invoice_total\n\n\n"
                            b"def test_format_invoice_total_applies_tax_rate() -> None:\n"
                            b'    assert format_invoice_total(100.0, 0.075) == "$107.50"\n'
                        )
                    )
                }
            ),
        }
    )


def _build_gtm_agent(*, model: str, manifest: Manifest) -> SandboxAgent:
    return SandboxAgent(
        name="GTM analyst",
        model=model,
        instructions=(
            "You are a GTM analyst. Inspect the workspace data before answering. Keep analysis "
            "specific and cite file paths you used."
        ),
        default_manifest=manifest,
        capabilities=[
            # Same layout + same SDK session across turns means one memory conversation.
            Memory(
                layout=MemoryLayoutConfig(
                    memories_dir="memories/gtm",
                    sessions_dir="sessions/gtm",
                )
            ),
            Filesystem(),
            Shell(),
            Filesystem(),
        ],
    )


def _build_engineering_agent(*, model: str, manifest: Manifest) -> SandboxAgent:
    return SandboxAgent(
        name="Engineering fixer",
        model=model,
        instructions=(
            "You are an engineer. Inspect files before editing, make minimal changes, and verify "
            "with tests. Use a non-login POSIX shell for commands. Make one focused pytest attempt; "
            "if the local sandbox blocks Python or toolchain access, report that validation was "
            "blocked and finish instead of retrying repeatedly."
        ),
        default_manifest=manifest,
        capabilities=[
            # Different layout keeps engineering memory separate even in the same sandbox workspace.
            Memory(
                layout=MemoryLayoutConfig(
                    memories_dir="memories/engineering",
                    sessions_dir="sessions/engineering",
                )
            ),
            Shell(),
            Filesystem(),
        ],
    )


def _print_tree(
    root: Path, label: str, relative_path: str, *, print_file_contents: bool = False
) -> None:
    print(f"\n[{label}]")
    base = root / relative_path
    if not base.exists():
        print(f"{relative_path} (missing)")
        return
    for path in sorted(base.rglob("*")):
        if path.is_file():
            print(path.relative_to(root))
            if print_file_contents:
                contents = path.read_text().rstrip()
                if not contents:
                    print("    (empty)")
                else:
                    for line in contents.splitlines():
                        print(f"    {line}")


async def main(*, model: str) -> None:
    manifest = _build_manifest()
    gtm_agent = _build_gtm_agent(model=model, manifest=manifest)
    engineering_agent = _build_engineering_agent(model=model, manifest=manifest)
    client = UnixLocalSandboxClient()
    sandbox = await client.create(manifest=manifest)
    workspace_root = Path(sandbox.state.manifest.root)

    try:
        async with sandbox:
            gtm_conversation_session = SQLiteSession(GTM_SESSION_ID)
            gtm_config = RunConfig(
                sandbox=SandboxRunConfig(session=sandbox),
                workflow_name="GTM memory layout example",
            )
            gtm_first = await Runner.run(
                gtm_agent,
                GTM_TURN_1,
                session=gtm_conversation_session,
                run_config=gtm_config,
            )
            print("\n[gtm turn 1]")
            print(gtm_first.final_output)

            # Reuse the SDK session so the model sees prior turns and memory extracts them together.
            gtm_second = await Runner.run(
                gtm_agent,
                GTM_TURN_2,
                session=gtm_conversation_session,
                run_config=gtm_config,
            )
            print("\n[gtm turn 2]")
            print(gtm_second.final_output)

            engineering_conversation_session = SQLiteSession(ENGINEERING_SESSION_ID)
            engineering_config = RunConfig(
                sandbox=SandboxRunConfig(session=sandbox),
                workflow_name="Engineering memory layout example",
            )
            engineering = await Runner.run(
                engineering_agent,
                ENGINEERING_TURN,
                session=engineering_conversation_session,
                run_config=engineering_config,
                max_turns=20,
            )
            print("\n[engineering]")
            print(engineering.final_output)

        _print_tree(workspace_root, "gtm memory", "memories/gtm")
        _print_tree(workspace_root, "engineering memory", "memories/engineering")
        _print_tree(workspace_root, "gtm sessions", "sessions/gtm", print_file_contents=True)
        _print_tree(
            workspace_root,
            "engineering sessions",
            "sessions/engineering",
            print_file_contents=True,
        )
    finally:
        await client.delete(sandbox)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run two sandbox agents with separate memory layouts in one workspace."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    args = parser.parse_args()

    asyncio.run(main(model=args.model))
