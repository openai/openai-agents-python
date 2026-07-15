"""External governance checkpoint example with tool approval.

This example demonstrates how to:
1. Build a deterministic action envelope for a sensitive tool call.
2. Review the envelope with an external governance checkpoint before execution.
3. Use the SDK's human-in-the-loop interruption/resume flow for approval-bound actions.
4. Recompute the action hash at execution time so approval is bound to the same action.
5. Persist the checkpoint decision so delayed approvals can resume in a new process.
"""

import asyncio
import hashlib
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Literal, TypedDict

from agents import Agent, ModelSettings, Runner, RunState, function_tool
from examples.auto_mode import confirm_with_fallback


class GovernanceActionEnvelope(TypedDict):
    action_hash: str
    tool_name: str
    proposed_action: str
    arguments: dict[str, Any]


class GovernanceDecision(TypedDict):
    verdict: Literal["allow", "require_approval", "deny"]
    reason: str
    action_hash: str
    decision_id: str


RESULT_PATH = Path(".cache/agent_patterns/external_governance_approval/result.json")
DECISION_PATH = RESULT_PATH.with_name("decisions.json")
EXTERNAL_GOVERNANCE_URL = os.environ.get("EXTERNAL_GOVERNANCE_URL")
decisions_by_action_hash: dict[str, GovernanceDecision] = {}


def stable_json(value: Any) -> str:
    """Serialize a value deterministically for hashing and checkpoint requests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256(value: str) -> str:
    """Return the SHA-256 hex digest for a string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_action_envelope(
    *,
    account_id: str,
    destination: str,
    record_limit: int,
    contains_pii: bool,
) -> GovernanceActionEnvelope:
    """Build a portable envelope describing the proposed tool action."""
    tool_name = "export_customer_records"
    arguments = {
        "account_id": account_id,
        "destination": destination,
        "record_limit": record_limit,
        "contains_pii": contains_pii,
    }
    proposed_action = (
        f"Export up to {record_limit} customer records for account {account_id} to {destination}."
    )
    action_hash = sha256(
        stable_json(
            {
                "tool_name": tool_name,
                "proposed_action": proposed_action,
                "arguments": arguments,
            }
        )
    )
    return {
        "action_hash": action_hash,
        "tool_name": tool_name,
        "proposed_action": proposed_action,
        "arguments": arguments,
    }


async def review_with_external_governance(
    envelope: GovernanceActionEnvelope,
) -> GovernanceDecision:
    """Review an action envelope with a real or local governance checkpoint."""
    if EXTERNAL_GOVERNANCE_URL:
        body = stable_json(envelope).encode("utf-8")

        def send_request() -> GovernanceDecision:
            request = urllib.request.Request(
                EXTERNAL_GOVERNANCE_URL,
                data=body,
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read().decode("utf-8")
                decision = json.loads(payload)
            if decision.get("action_hash") != envelope["action_hash"]:
                raise RuntimeError(
                    "External governance returned a decision for a different action."
                )
            return decision

        return await asyncio.to_thread(send_request)

    requires_review = contains_high_risk_data(envelope)
    verdict: Literal["allow", "require_approval"] = (
        "require_approval" if requires_review else "allow"
    )
    return {
        "verdict": verdict,
        "reason": (
            "Customer data export requires approval."
            if requires_review
            else "The proposed export is within the local low-risk policy."
        ),
        "action_hash": envelope["action_hash"],
        "decision_id": f"local-{envelope['action_hash'][:12]}",
    }


def load_decision_cache() -> dict[str, GovernanceDecision]:
    """Load persisted checkpoint decisions for approval resume flows."""
    if not DECISION_PATH.exists():
        return {}
    with DECISION_PATH.open() as f:
        return json.load(f)


def persist_decision(decision: GovernanceDecision) -> None:
    """Persist a decision so a resumed run can fail closed or execute with proof."""
    decisions_by_action_hash[decision["action_hash"]] = decision
    DECISION_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache = load_decision_cache()
    cache[decision["action_hash"]] = decision
    with DECISION_PATH.open("w") as f:
        json.dump(cache, f, indent=2)


def find_decision(action_hash: str) -> GovernanceDecision | None:
    """Find a decision in memory or in the persisted approval cache."""
    if action_hash in decisions_by_action_hash:
        return decisions_by_action_hash[action_hash]
    decision = load_decision_cache().get(action_hash)
    if decision is not None:
        decisions_by_action_hash[action_hash] = decision
    return decision


def contains_high_risk_data(envelope: GovernanceActionEnvelope) -> bool:
    """Classify whether a proposed export should require approval."""
    return (
        bool(envelope["arguments"]["contains_pii"])
        or int(envelope["arguments"]["record_limit"]) > 10
    )


async def needs_export_approval(_ctx: Any, params: dict[str, Any], _call_id: str) -> bool:
    """Ask the checkpoint whether this export needs approval before execution."""
    envelope = build_action_envelope(
        account_id=str(params["account_id"]),
        destination=str(params["destination"]),
        record_limit=int(params["record_limit"]),
        contains_pii=bool(params["contains_pii"]),
    )
    decision = await review_with_external_governance(envelope)
    persist_decision(decision)

    print("\nExternal governance decision")
    print(f"  verdict: {decision['verdict']}")
    print(f"  decision_id: {decision['decision_id']}")
    print(f"  action_hash: {decision['action_hash']}")
    print(f"  reason: {decision['reason']}\n")

    return decision["verdict"] != "allow"


@function_tool(needs_approval=needs_export_approval)
async def export_customer_records(
    account_id: str,
    destination: str,
    record_limit: int,
    contains_pii: bool,
) -> dict[str, str | int | bool]:
    """Export customer records after governance approval."""
    envelope = build_action_envelope(
        account_id=account_id,
        destination=destination,
        record_limit=record_limit,
        contains_pii=contains_pii,
    )
    decision = find_decision(envelope["action_hash"])

    if decision is None:
        raise RuntimeError(
            "No persisted governance decision was recorded for this action; "
            "failing closed."
        )
    if decision["verdict"] == "deny":
        raise RuntimeError(f"Blocked by external governance: {decision['reason']}")

    return {
        "status": "executed",
        "exported": record_limit,
        "account_id": account_id,
        "destination": destination,
        "governance_decision_id": decision["decision_id"],
        "action_hash": envelope["action_hash"],
    }


agent = Agent(
    name="Governed Export Assistant",
    instructions=(
        "You help operations teams export customer records. "
        "Use the export_customer_records tool when asked, and keep the final response concise."
    ),
    model_settings=ModelSettings(tool_choice="export_customer_records"),
    tools=[export_customer_records],
)


async def confirm(question: str) -> bool:
    """Prompt user for approval, with an automated fallback for examples."""
    return confirm_with_fallback(f"{question} (y/n): ", default=True)


async def main() -> None:
    """Run the external governance approval example."""
    result = await Runner.run(
        agent,
        "Export 25 customer records for account acme-123 to the internal compliance drive. The export contains PII.",
    )

    while result.interruptions:
        print("\n" + "=" * 80)
        print("Run interrupted - external governance approval required")
        print("=" * 80)

        state = result.to_state()
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RESULT_PATH.open("w") as f:
            json.dump(state.to_json(), f, indent=2)
        print(f"State saved to {RESULT_PATH}")

        with RESULT_PATH.open() as f:
            stored_state_json = json.load(f)
        state = await RunState.from_json(agent, stored_state_json)

        for interruption in result.interruptions:
            print("\nTool call details:")
            print(f"  Agent: {interruption.agent.name}")
            print(f"  Tool: {interruption.name}")
            print(f"  Arguments: {interruption.arguments}")

            approved = await confirm("\nDo you approve this governed action?")
            if approved:
                print(f"Approved: {interruption.name}")
                state.approve(interruption)
            else:
                print(f"Rejected: {interruption.name}")
                state.reject(interruption)

        print("\nResuming agent execution...")
        result = await Runner.run(agent, state)

    print("\n" + "=" * 80)
    print("Final Output:")
    print("=" * 80)
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
