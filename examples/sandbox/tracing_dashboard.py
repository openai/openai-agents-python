from __future__ import annotations

import argparse
import asyncio
import io
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agents import Agent, ModelSettings, Runner, flush_traces, gen_trace_id, handoff, trace
from agents.items import HandoffOutputItem, ToolCallItem
from agents.run import RunConfig
from agents.sandbox import Manifest, MemoryReadConfig, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import LocalDirLazySkillSource, Memory, Shell, Skills
from agents.sandbox.entries import Dir, File, LocalDir
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DEFAULT_MODEL = "gpt-5.4"
EXAMPLE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = EXAMPLE_DIR / "tracing_dashboard_skills"

DEFAULT_PROMPT = (
    "Review the Bluebird data export case. The customer wants a production data export "
    "this week, security review is still open, and finance already mapped SSO attributes."
)


def _build_manifest() -> Manifest:
    return Manifest(
        entries={
            "data": Dir(
                children={
                    "customer_case.md": File(
                        content=(
                            b"# Bluebird Logistics case\n\n"
                            b"- Customer wants a production data export enabled this week.\n"
                            b"- Security review is still open.\n"
                            b"- Finance already mapped the required SSO attributes.\n"
                        )
                    ),
                }
            ),
            "memories": Dir(
                children={
                    "memory_summary.md": File(
                        content=(
                            b"# Memory summary\n\n"
                            b"- Bluebird should not get production exports before security review "
                            b"is complete.\n"
                        )
                    ),
                    "MEMORY.md": File(
                        content=(
                            b"# Task Group: Bluebird Logistics\n\n"
                            b"## What to remember\n\n"
                            b"- A prior export request was paused because security approval was "
                            b"missing.\n\n"
                            b"## Search keywords\n\n"
                            b"Bluebird, data export, security review\n"
                        )
                    ),
                }
            ),
        }
    )


def _build_agents(*, model: str, manifest: Manifest) -> tuple[Agent[Any], SandboxAgent[Any]]:
    evidence_reviewer = SandboxAgent(
        name="Evidence Reviewer",
        model=model,
        handoff_description="Reviews the Bluebird export case in the sandbox.",
        instructions=(
            "Use the sandbox workspace for the case review. First, call only `load_skill` for "
            "`case-note`. After the skill loads, do not call any more tools. Return the customer "
            "evidence note using the user-provided case facts and memory if relevant. Include "
            "exactly these sections: `## Facts`, `## Policy`, and `## Recommendation`. Keep it "
            "under 90 words."
        ),
        default_manifest=manifest,
        capabilities=[
            Memory(read=MemoryReadConfig(live_update=False), generate=None),
            Shell(),
            Skills(
                lazy_from=LocalDirLazySkillSource(
                    source=LocalDir(src=SKILLS_DIR),
                )
            ),
        ],
        model_settings=ModelSettings(tool_choice="load_skill"),
    )

    account_coordinator = Agent(
        name="Account Coordinator",
        model=model,
        instructions=(
            "You route customer-account review requests. For this request, hand off to the "
            "evidence reviewer."
        ),
        handoffs=[
            handoff(evidence_reviewer, tool_name_override="transfer_to_evidence_reviewer"),
        ],
        model_settings=ModelSettings(tool_choice="transfer_to_evidence_reviewer"),
    )
    return account_coordinator, evidence_reviewer


async def _prestage_case_note_skill(sandbox: BaseSandboxSession) -> None:
    skill_markdown = (SKILLS_DIR / "case-note" / "SKILL.md").read_bytes()
    await sandbox.mkdir(Path(".agents/case-note"), parents=True)
    await sandbox.write(Path(".agents/case-note/SKILL.md"), io.BytesIO(skill_markdown))


def _raw_attr(raw_item: object, name: str) -> object:
    if isinstance(raw_item, dict):
        return raw_item.get(name)
    return getattr(raw_item, name, None)


def _tool_call_name(item: ToolCallItem) -> str:
    raw_item = item.raw_item
    raw_type = _raw_attr(raw_item, "type")
    name = _raw_attr(raw_item, "name")

    if raw_type == "apply_patch_call":
        return "apply_patch"
    if raw_type == "mcp_call" and isinstance(name, str):
        return f"mcp:{name}"
    if isinstance(name, str) and name:
        return name
    if isinstance(raw_type, str) and raw_type:
        return raw_type
    return ""


def _handoff_names(items: Sequence[object]) -> list[str]:
    return [
        f"{item.source_agent.name} -> {item.target_agent.name}"
        for item in items
        if isinstance(item, HandoffOutputItem)
    ]


def _validate_trace_features(
    result_items: Sequence[object],
    final_output: object,
) -> None:
    handoff_names = _handoff_names(result_items)
    tool_calls = [item for item in result_items if isinstance(item, ToolCallItem)]
    tool_names = [_tool_call_name(item) for item in tool_calls]

    if len(handoff_names) != 1:
        raise RuntimeError(f"Expected exactly one handoff, saw: {handoff_names}")
    if "load_skill" not in tool_names:
        raise RuntimeError(f"Expected a load_skill call, saw: {tool_names}")
    extra_tool_names = [
        name for name in tool_names if name not in {"transfer_to_evidence_reviewer", "load_skill"}
    ]
    if extra_tool_names:
        raise RuntimeError(
            f"Expected no tool calls except handoff and load_skill, saw: {tool_names}"
        )
    if "## Policy" not in str(final_output):
        raise RuntimeError("Expected final output to include a Policy section.")


async def main(*, model: str, prompt: str) -> None:
    manifest = _build_manifest()
    account_coordinator, _evidence_reviewer = _build_agents(
        model=model,
        manifest=manifest,
    )
    client = UnixLocalSandboxClient()
    sandbox = await client.create(manifest=manifest)
    trace_id = gen_trace_id()
    trace_url = f"https://platform.openai.com/traces/trace?trace_id={trace_id}"

    try:
        async with sandbox:
            await _prestage_case_note_skill(sandbox)
            with trace(
                workflow_name="Customer evidence review",
                trace_id=trace_id,
                group_id="customer-evidence-review-demo",
                metadata={
                    "example": "examples/sandbox/tracing_dashboard.py",
                    "features": "regular_agent,sandbox_agent,handoff,load_skill,memory",
                },
            ):
                print(f"View trace: {trace_url}\n")
                result = await Runner.run(
                    account_coordinator,
                    prompt,
                    max_turns=4,
                    run_config=RunConfig(
                        sandbox=SandboxRunConfig(session=sandbox),
                        group_id="customer-evidence-review-demo",
                    ),
                )

            result_items = list(result.new_items)
            _validate_trace_features(result_items, result.final_output)

            print("=== Final output ===")
            print(result.final_output)
            print("\n=== Trace features observed ===")
            for handoff_name in _handoff_names(result_items):
                print(f"handoff: {handoff_name}")
            for item in result_items:
                if isinstance(item, ToolCallItem):
                    print(f"tool: {_tool_call_name(item)}")
    finally:
        try:
            await client.delete(sandbox)
        finally:
            flush_traces()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Generate a trace with one handoff from a regular agent to a sandbox agent, "
            "plus a load_skill call and sandbox memory."
        )
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt to send to the agent.")
    args = parser.parse_args()

    asyncio.run(main(model=args.model, prompt=args.prompt))
