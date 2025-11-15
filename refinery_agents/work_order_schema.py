"""Structured output models for the refinery work order."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DocRef(BaseModel):
    """Reference to an authoritative engineering document."""

    doc_type: str = Field(description="Document type such as P&ID, LineList, or Standard")
    identifier: str = Field(description="Primary identifier, tag, or drawing number")
    revision: str | None = Field(default=None, description="Revision or issue identifier")
    location: str | None = Field(
        default=None, description="Optional page, sheet, or section reference"
    )


class DesignAssumption(BaseModel):
    """Assumptions that underpin the engineering design."""

    description: str
    risk_if_wrong: str | None = None


class DesignOption(BaseModel):
    """Possible design approach considered by the team."""

    name: str
    description: str
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    recommended: bool = False
    supporting_docs: list[DocRef] = Field(default_factory=list)


class WorkOrderTaskStep(BaseModel):
    """Sequenced field or engineering task for the final work order."""

    step_number: int
    description: str
    references: list[DocRef] = Field(default_factory=list)


class EngineeringWorkOrder(BaseModel):
    """Final structured artifact produced by the orchestration."""

    title: str
    scope_summary: str
    design_basis: str
    design_assumptions: list[DesignAssumption] = Field(default_factory=list)
    selected_option: DesignOption
    rejected_options: list[DesignOption] = Field(default_factory=list)
    materials_and_parts: list[str] = Field(default_factory=list)
    task_steps: list[WorkOrderTaskStep] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    required_approvals: list[str] = Field(default_factory=list)
    evidence_sources: list[DocRef] = Field(default_factory=list)

    def merge_reviewer_feedback(self, issues: list[str]) -> "EngineeringWorkOrder":
        """Return a copy of the work order with reviewer issues appended to open questions."""

        if not issues:
            return self

        updated_questions = [*self.open_questions, *issues]
        return self.model_copy(update={"open_questions": updated_questions})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EngineeringWorkOrder":
        """Utility factory to create a work order from loosely structured data."""

        return cls.model_validate(data)

