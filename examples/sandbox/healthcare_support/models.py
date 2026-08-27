from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

IntentName = Literal[
    "eligibility_verification",
    "prior_auth_confusion",
    "referral_status_question",
    "billing_coverage_clarification",
    "general_intake",
]


class ScenarioExpectation(BaseModel):
    intent: IntentName
    required_entities: dict[str, str] = Field(default_factory=dict)
    required_tool_calls: list[str] = Field(default_factory=list)
    required_resolution_elements: list[str] = Field(default_factory=list)
    expected_payer: str | None = None


class ScenarioCase(BaseModel):
    scenario_id: str
    description: str
    transcript: str
    patient_metadata: dict[str, Any] = Field(default_factory=dict)
    followup_qa: dict[str, str] = Field(default_factory=dict)
    expected: ScenarioExpectation
    gold: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSnippet(BaseModel):
    document_id: str
    title: str
    chunk_id: str
    score: float
    snippet: str
    matched_terms: list[str] = Field(default_factory=list)


class BenefitReview(BaseModel):
    patient_name: str
    patient_id: str
    payer: str
    member_id: str
    eligibility_status: str
    plan_summary: str
    referral_status: str
    prior_auth_recommended: bool
    recommended_queue: str
    summary: str


class SandboxPolicyPacket(BaseModel):
    matched_policy_files: list[str] = Field(default_factory=list)
    generated_files: list[str] = Field(default_factory=list)
    shell_commands: list[str] = Field(default_factory=list)
    policy_summary: str
    human_review_recommended: bool


class CaseResolution(BaseModel):
    scenario_id: str
    intent: IntentName
    patient_name: str
    benefits_summary: str
    policy_summary: str
    next_step: str
    route_to_human: bool
    handoff_id: str | None = None
    generated_files: list[str] = Field(default_factory=list)
    internal_summary: str
    patient_facing_response: str


class MemoryRecap(BaseModel):
    remembered_patient: str | None = None
    remembered_intent: IntentName | None = None
    remembered_next_step: str
    remembered_handoff: str | None = None
    remembered_files: list[str] = Field(default_factory=list)
