from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agents import RunContextWrapper, function_tool
from examples.sandbox.healthcare_support.data import HealthcareSupportDataStore
from examples.sandbox.healthcare_support.models import ScenarioCase


@dataclass
class HealthcareSupportContext:
    store: HealthcareSupportDataStore
    scenario: ScenarioCase
    session_id: str = ""
    human_handoffs: list[dict[str, Any]] = field(default_factory=list)
    human_handoff_approved: bool = False
    emit_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    async def emit(self, event_name: str, **payload: Any) -> None:
        if self.emit_event is None:
            return
        await self.emit_event(
            {
                "type": "workflow_event",
                "event": event_name,
                **payload,
            }
        )


@function_tool(name_override="patient_info_lookup")
def lookup_patient(
    context: RunContextWrapper[HealthcareSupportContext],
    patient_id: str | None = None,
    phone: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Look up a synthetic patient profile by patient ID, phone, or name."""
    return context.context.store.lookup_patient(
        patient_id=patient_id,
        phone=phone,
        name=name,
    )


@function_tool(name_override="insurance_eligibility_lookup")
def lookup_insurance_eligibility(
    context: RunContextWrapper[HealthcareSupportContext],
    payer: str | None = None,
    member_id: str | None = None,
    dob: str | None = None,
) -> dict[str, Any]:
    """Look up synthetic insurance eligibility by payer, member ID, and DOB."""
    return context.context.store.lookup_eligibility(
        payer=payer,
        member_id=member_id,
        dob=dob,
    )


@function_tool(name_override="appointment_referral_status_lookup")
def lookup_referral_status(
    context: RunContextWrapper[HealthcareSupportContext],
    referral_id: str | None = None,
    patient_id: str | None = None,
) -> dict[str, Any]:
    """Look up synthetic referral status by referral ID or patient ID."""
    return context.context.store.lookup_referral(
        referral_id=referral_id,
        patient_id=patient_id,
    )


async def _needs_human_approval(
    context: RunContextWrapper[HealthcareSupportContext],
    _params: dict[str, Any],
    _call_id: str,
) -> bool:
    return not context.context.human_handoff_approved


@function_tool(name_override="route_to_human_queue", needs_approval=_needs_human_approval)
def route_to_human_queue(
    context: RunContextWrapper[HealthcareSupportContext],
    queue: str,
    priority: str,
    reason: str,
    summary: str,
) -> dict[str, Any]:
    """Route a synthetic case to a human queue after explicit approval."""
    payload = {
        "queue": queue,
        "priority": priority,
        "reason": reason,
        "summary": summary,
        "scenario_id": context.context.scenario.scenario_id,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    result = {
        "status": "queued",
        "handoff_id": f"HUMAN-{digest.upper()}",
        "queue": queue,
        "priority": priority,
        "reason": reason,
        "summary": summary,
    }
    context.context.human_handoffs.append({"payload": payload, "result": result})
    return result
