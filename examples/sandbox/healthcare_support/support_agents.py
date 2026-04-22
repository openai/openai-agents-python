from __future__ import annotations

from pathlib import Path

from openai.types.shared import Reasoning

from agents import Agent, AgentOutputSchema, ModelSettings, Tool
from agents.sandbox import SandboxAgent
from agents.sandbox.capabilities import Filesystem, LocalDirLazySkillSource, Shell, Skills
from agents.sandbox.entries import LocalDir
from examples.sandbox.healthcare_support.models import (
    BenefitReview,
    CaseResolution,
    MemoryRecap,
    SandboxPolicyPacket,
)
from examples.sandbox.healthcare_support.tools import (
    HealthcareSupportContext,
    lookup_insurance_eligibility,
    lookup_patient,
    lookup_referral_status,
    route_to_human_queue,
)

BENEFITS_PROMPT = """
You are a healthcare benefits specialist in a synthetic support workflow.

Use the available lookup tools to verify patient, eligibility, and referral details, then return a
structured benefits review.

Rules:
1. Call `patient_info_lookup` first when you have a patient ID, phone number, or patient name.
2. Call `insurance_eligibility_lookup` when payer, member ID, or date of birth is available.
3. Call `appointment_referral_status_lookup` when referral ID or patient ID is available.
4. Recommend prior-auth review only when the case involves imaging, surgery, a pending referral, or
   policy-specific authorization language.
5. Set `recommended_queue` to one of `care-team-intake-queue`, `auth-review-queue`, or
   `billing-review-queue`.
6. Keep the summary concise and grounded in tool output.
""".strip()


POLICY_SANDBOX_PROMPT = """
You are a policy packet specialist running inside a sandbox workspace.

Inspect the case files and local policy library, generate concise markdown artifacts in `output/`,
and return a structured packet summary.

You must:
1. Load and use the `prior-auth-packet-builder` skill.
2. Inspect the workspace with shell commands before writing anything.
3. Use `rg` against `policies/` for prior-auth, imaging, referral, billing, PPO, and Blue Cross
   policy guidance.
4. Create `output/policy_findings.md` with the most relevant policy guidance.
5. Create `output/human_review_checklist.md` with a short checklist for a human reviewer.
6. Set `human_review_recommended=true` only when the policy search or case input shows missing
   authorization/referral details that should be reviewed by a human before responding.
7. Include the exact shell commands you ran in `shell_commands`.
8. Return only facts grounded in the files you inspected.
""".strip()


ORCHESTRATOR_PROMPT = """
You are a healthcare support orchestrator.

Coordinate a synthetic support case by combining a benefits review, a sandbox policy packet review,
and a human handoff only when the case genuinely needs it.

Rules:
1. Always call `benefits_review` first.
2. Always call `sandbox_policy_packet` second.
3. For this demo, call `route_to_human_queue` only for the
   `messy_ambiguous_knee_case` scenario when the sandbox packet recommends human review.
4. Do not escalate the other four scenarios; answer those directly from the benefits and sandbox
   outputs.
5. If you call `route_to_human_queue`, include the returned `handoff_id` and set
   `route_to_human=true`.
6. Produce a clear patient-facing response, a short internal summary, and a concrete next step.
7. Use only facts from the tool outputs and the supplied scenario payload.
""".strip()


MEMORY_PROMPT = """
Summarize what you remember from this SQLite-backed session about the prior patient support cases.

Include the most recently remembered patient, intent, handoff status, generated files, and next
step. Do not call tools.
""".strip()


benefits_agent = Agent[HealthcareSupportContext](
    name="HealthcareBenefitsAgent",
    model="gpt-5.4",
    instructions=BENEFITS_PROMPT,
    model_settings=ModelSettings(reasoning=Reasoning(effort="low"), verbosity="low"),
    tools=[
        lookup_patient,
        lookup_insurance_eligibility,
        lookup_referral_status,
    ],
    output_type=AgentOutputSchema(BenefitReview, strict_json_schema=False),
)


def build_policy_sandbox_agent(*, skills_root: Path) -> SandboxAgent[HealthcareSupportContext]:
    return SandboxAgent[HealthcareSupportContext](
        name="HealthcarePolicySandboxAgent",
        model="gpt-5.4",
        instructions=(
            POLICY_SANDBOX_PROMPT + "\n\n"
            "Use `load_skill` before reading the skill file. Use `exec_command` with `pwd`, "
            "`ls`, `cat`, and `rg` to inspect the sandbox workspace. Use `apply_patch` to create "
            "`output/policy_findings.md` and `output/human_review_checklist.md`."
        ),
        capabilities=[
            Shell(),
            Filesystem(),
            Skills(
                lazy_from=LocalDirLazySkillSource(
                    # This is a host path read by the SDK process.
                    # Requested skills are copied into `skills_path` in the sandbox.
                    source=LocalDir(src=skills_root),
                )
            ),
        ],
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="low"),
            verbosity="low",
            tool_choice="required",
        ),
        output_type=AgentOutputSchema(SandboxPolicyPacket, strict_json_schema=False),
    )


def build_orchestrator(*, sandbox_policy_tool: Tool) -> Agent[HealthcareSupportContext]:
    return Agent[HealthcareSupportContext](
        name="HealthcareSupportOrchestrator",
        model="gpt-5.4",
        instructions=ORCHESTRATOR_PROMPT,
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="low"),
            verbosity="low",
        ),
        tools=[
            benefits_agent.as_tool(
                tool_name="benefits_review",
                tool_description="Review patient eligibility, benefits, and referral status.",
            ),
            sandbox_policy_tool,
            route_to_human_queue,
        ],
        output_type=AgentOutputSchema(CaseResolution, strict_json_schema=False),
    )


memory_recap_agent = Agent[HealthcareSupportContext](
    name="HealthcareSupportMemoryAgent",
    model="gpt-5.4",
    instructions=MEMORY_PROMPT,
    model_settings=ModelSettings(reasoning=Reasoning(effort="low"), verbosity="low"),
    output_type=AgentOutputSchema(MemoryRecap, strict_json_schema=False),
)
