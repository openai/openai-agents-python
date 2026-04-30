from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from agents import (
    Agent,
    AgentHookContext,
    RunContextWrapper,
    RunHooks,
    Runner,
    SQLiteSession,
    Tool,
    gen_trace_id,
    trace,
)
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxRunConfig
from agents.sandbox.entries import Dir, File, LocalDir
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from agents.tool_context import ToolContext
from examples.sandbox.healthcare_support.data import HealthcareSupportDataStore
from examples.sandbox.healthcare_support.models import (
    CaseResolution,
    MemoryRecap,
    ScenarioCase,
)
from examples.sandbox.healthcare_support.support_agents import (
    build_orchestrator,
    build_policy_sandbox_agent,
    memory_recap_agent,
)
from examples.sandbox.healthcare_support.tools import HealthcareSupportContext

EXAMPLE_ROOT = Path(__file__).resolve().parent
POLICIES_ROOT = EXAMPLE_ROOT / "policies"
SKILLS_ROOT = EXAMPLE_ROOT / "skills"
SDK_ROOT = EXAMPLE_ROOT.parents[2]
CACHE_ROOT = SDK_ROOT / ".cache" / "healthcare_support"
SESSION_DB_PATH = CACHE_ROOT / "sessions.db"
DEFAULT_SESSION_ID = "healthcare-support-demo-memory"

ApprovalHandler = Callable[[dict[str, Any]], Awaitable[bool]]


class WorkflowHooks(RunHooks[HealthcareSupportContext]):
    async def on_agent_start(
        self,
        context: AgentHookContext[HealthcareSupportContext],
        agent: Agent[HealthcareSupportContext],
    ) -> None:
        await context.context.emit("agent_start", agent=agent.name)

    async def on_agent_end(
        self,
        context: RunContextWrapper[HealthcareSupportContext],
        agent: Agent[HealthcareSupportContext],
        output: Any,
    ) -> None:
        await context.context.emit(
            "agent_end",
            agent=agent.name,
            output=_to_jsonable(output),
        )

    async def on_tool_start(
        self,
        context: RunContextWrapper[HealthcareSupportContext],
        agent: Agent[HealthcareSupportContext],
        tool: Tool,
    ) -> None:
        tool_context = cast(ToolContext[HealthcareSupportContext], context)
        await context.context.emit(
            "tool_start",
            agent=agent.name,
            tool=tool.name,
            call_id=tool_context.tool_call_id,
            arguments=tool_context.tool_arguments,
        )

    async def on_tool_end(
        self,
        context: RunContextWrapper[HealthcareSupportContext],
        agent: Agent[HealthcareSupportContext],
        tool: Tool,
        result: str,
    ) -> None:
        tool_context = cast(ToolContext[HealthcareSupportContext], context)
        await context.context.emit(
            "tool_end",
            agent=agent.name,
            tool=tool.name,
            call_id=tool_context.tool_call_id,
            output=_to_jsonable(result),
        )


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict | list | str | int | float | bool) or value is None:
        return value
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return str(value)


def build_context(
    *,
    store: HealthcareSupportDataStore,
    scenario_id: str = "eligibility_verification_basic",
    session_id: str = DEFAULT_SESSION_ID,
    emit_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> HealthcareSupportContext:
    return HealthcareSupportContext(
        store=store,
        scenario=store.get_scenario(scenario_id),
        session_id=session_id,
        emit_event=emit_event,
    )


def _build_manifest(scenario: ScenarioCase) -> Manifest:
    return Manifest(
        entries={
            "case": Dir(
                children={
                    "scenario.json": File(
                        content=json.dumps(scenario.model_dump(mode="json"), indent=2).encode(
                            "utf-8"
                        )
                    ),
                    "transcript.txt": File(content=scenario.transcript.encode("utf-8")),
                },
                description="Synthetic support request and scenario metadata.",
            ),
            "policies": LocalDir(
                src=POLICIES_ROOT,
                description="Local healthcare policy and workflow documents.",
            ),
            "output": Dir(description="Generated support artifacts for this case."),
        }
    )


async def _structured_tool_output_extractor(result: Any) -> str:
    final_output = result.final_output
    if isinstance(final_output, BaseModel):
        return json.dumps(final_output.model_dump(mode="json"), sort_keys=True)
    return str(final_output)


def _fallback_artifacts(*, scenario: ScenarioCase, resolution: CaseResolution) -> dict[str, str]:
    policy_doc = f"""# Policy Findings

## Case
{scenario.description}

## Policy summary
{resolution.policy_summary}

## Next step
{resolution.next_step}
"""
    checklist_doc = f"""# Human Review Checklist

- Confirm whether the request needs prior authorization for this service and payer.
- Verify referral state and any missing clinical or billing identifiers.
- Use this internal summary: {resolution.internal_summary}
- Patient-facing response: {resolution.patient_facing_response}
"""
    return {
        "policy_findings.md": policy_doc,
        "human_review_checklist.md": checklist_doc,
    }


async def _copy_output_files(
    *,
    sandbox: Any,
    scenario: ScenarioCase,
    resolution: CaseResolution,
) -> list[dict[str, str]]:
    scenario_id = scenario.scenario_id
    destination_root = CACHE_ROOT / "output" / scenario_id
    destination_root.mkdir(parents=True, exist_ok=True)
    copied_by_name: dict[str, dict[str, str]] = {}

    for entry in await sandbox.ls("output"):
        entry_path = Path(entry.path)
        if entry.is_dir():
            continue

        handle = await sandbox.read(entry_path)
        try:
            payload = handle.read()
        finally:
            handle.close()

        local_path = destination_root / entry_path.name
        if isinstance(payload, str):
            content = payload
            local_path.write_text(content, encoding="utf-8")
        else:
            content = bytes(payload).decode("utf-8", errors="replace")
            local_path.write_text(content, encoding="utf-8")

        copied_by_name[entry_path.name] = {
            "name": entry_path.name,
            "path": str(local_path),
            "content": content,
        }

    for filename, content in _fallback_artifacts(
        scenario=scenario,
        resolution=resolution,
    ).items():
        if filename in copied_by_name:
            continue
        local_path = destination_root / filename
        local_path.write_text(content, encoding="utf-8")
        copied_by_name[filename] = {
            "name": filename,
            "path": str(local_path),
            "content": content,
        }

    return [copied_by_name[name] for name in sorted(copied_by_name)]


async def _resolve_interruptions(
    *,
    result: Any,
    orchestrator: Agent[HealthcareSupportContext],
    context: HealthcareSupportContext,
    conversation_session: SQLiteSession,
    hooks: WorkflowHooks,
    approval_handler: ApprovalHandler | None,
) -> Any:
    approval_round = 0
    while result.interruptions:
        approval_round += 1
        if approval_round > 5:
            raise RuntimeError("Exceeded 5 approval rounds while resuming the workflow.")

        state = result.to_state()
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        state_payload = state.to_json(
            context_serializer=lambda value: {
                "scenario_id": value.scenario.scenario_id,
                "session_id": value.session_id,
                "human_handoffs": value.human_handoffs,
            }
        )
        (CACHE_ROOT / "pending_state.json").write_text(
            json.dumps(state_payload, indent=2),
            encoding="utf-8",
        )

        for interruption in result.interruptions:
            request = {
                "agent": interruption.agent.name,
                "tool": interruption.name,
                "arguments": _to_jsonable(interruption.arguments),
            }
            await context.emit("human_approval_requested", request=request)
            approved = True if approval_handler is None else await approval_handler(request)

            if approved:
                context.human_handoff_approved = True
                state.approve(interruption, always_approve=False)
                await context.emit("human_approval_resolved", approved=True, request=request)
            else:
                context.human_handoff_approved = False
                state.reject(interruption)
                await context.emit("human_approval_resolved", approved=False, request=request)

        result = await Runner.run(
            orchestrator,
            state,
            session=conversation_session,
            hooks=hooks,
        )
    return result


def _workflow_prompt(scenario: ScenarioCase) -> str:
    return json.dumps(
        {
            "scenario_id": scenario.scenario_id,
            "description": scenario.description,
            "transcript": scenario.transcript,
            "patient_metadata": scenario.patient_metadata,
            "followup_answers": scenario.followup_qa,
        },
        indent=2,
    )


async def run_healthcare_support_workflow(
    *,
    context: HealthcareSupportContext,
    scenario_id: str,
    approval_handler: ApprovalHandler | None = None,
) -> dict[str, Any]:
    scenario = context.store.get_scenario(scenario_id)
    context.scenario = scenario
    context.human_handoffs.clear()
    context.human_handoff_approved = False

    await context.emit(
        "scenario_loaded",
        scenario_id=scenario.scenario_id,
        description=scenario.description,
        transcript=scenario.transcript,
    )

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    conversation_session = SQLiteSession(
        session_id=context.session_id or DEFAULT_SESSION_ID, db_path=SESSION_DB_PATH
    )
    await context.emit("memory_ready", session_id=conversation_session.session_id)

    hooks = WorkflowHooks()
    sandbox_client = UnixLocalSandboxClient()
    sandbox = await sandbox_client.create(manifest=_build_manifest(scenario))
    await context.emit(
        "sandbox_ready",
        backend="unix_local",
        workspace=["case/scenario.json", "case/transcript.txt", "policies/", "output/"],
    )

    policy_agent = build_policy_sandbox_agent(skills_root=SKILLS_ROOT)
    sandbox_policy_tool = policy_agent.as_tool(
        tool_name="sandbox_policy_packet",
        tool_description="Inspect policy files in a sandbox and generate support artifacts.",
        custom_output_extractor=_structured_tool_output_extractor,
        run_config=RunConfig(
            sandbox=SandboxRunConfig(session=sandbox),
            workflow_name="Healthcare support sandbox packet",
        ),
        hooks=hooks,
        max_turns=20,
    )
    orchestrator = build_orchestrator(sandbox_policy_tool=sandbox_policy_tool)
    trace_id = gen_trace_id()
    trace_url = f"https://platform.openai.com/traces/trace?trace_id={trace_id}"

    try:
        async with sandbox:
            await context.emit("trace_ready", trace_id=trace_id, trace_url=trace_url)
            with trace(
                "Healthcare support workflow",
                trace_id=trace_id,
                group_id=scenario.scenario_id,
            ):
                result = await Runner.run(
                    orchestrator,
                    _workflow_prompt(scenario),
                    context=context,
                    session=conversation_session,
                    hooks=hooks,
                )
                result = await _resolve_interruptions(
                    result=result,
                    orchestrator=orchestrator,
                    context=context,
                    conversation_session=conversation_session,
                    hooks=hooks,
                    approval_handler=approval_handler,
                )
                resolution = result.final_output_as(CaseResolution)

                copied_files = await _copy_output_files(
                    sandbox=sandbox,
                    scenario=scenario,
                    resolution=resolution,
                )
                await context.emit("artifacts_ready", files=copied_files)

                memory_result = await Runner.run(
                    memory_recap_agent,
                    (
                        "Summarize what you remember from the session. Include patient, intent, "
                        "handoff state, generated files, and next step."
                    ),
                    context=context,
                    session=conversation_session,
                    hooks=hooks,
                )
                recap = memory_result.final_output_as(MemoryRecap)

        history_items = await conversation_session.get_items()
        payload = {
            "scenario_id": scenario.scenario_id,
            "description": scenario.description,
            "transcript": scenario.transcript,
            "trace_id": trace_id,
            "trace_url": trace_url,
            "resolution": resolution.model_dump(mode="json"),
            "memory_recap": recap.model_dump(mode="json"),
            "artifacts": copied_files,
            "session_id": conversation_session.session_id,
            "session_memory_items": len(history_items),
        }
        await context.emit("workflow_complete", payload=payload)
        return payload
    finally:
        await sandbox_client.delete(sandbox)
        await context.emit("sandbox_stopped", backend="unix_local")
