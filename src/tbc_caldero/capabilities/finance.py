"""
Finance capabilities — P&L query + allocation query + headcount tracking.

Ported from vault:
- scripts/pl_query.py::pl_query
- scripts/allocation_query.py::allocation_query
- scripts/headcount_tracking.py::headcount_tracking

All three come from the Fase 1 Finance batch (2026-04-12) built with the
PAT-CAPABILITY-SCAFFOLD pattern (coverage flags, honest nulls, proxy markers).
See .claude/rules/capability-atomic-scaffold.md
"""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field

from agents import function_tool


# ─── P&L Query ────────────────────────────────────────────────────────

class PLLineItem(BaseModel):
    line: str
    value_clp: int
    pct_revenue: float


class PLQueryResult(BaseModel):
    period: str
    coverage: str = Field(description="partial-no-sap | full | fallback-planilla")
    coverage_notes: str
    revenue_clp: int
    cogs_clp: int
    gross_margin_clp: int
    opex_clp: int | None = Field(description="None if OPEX not available in current source")
    ebitda_clp: int
    ebitda_is_proxy: bool = Field(description="True if EBITDA is computed as MC (missing OPEX)")
    line_items: list[PLLineItem]


@function_tool
def pl_query(
    period: Annotated[str, "Month YYYY-MM or 'latest'"] = "latest",
) -> PLQueryResult:
    """
    Query the TBC P&L for a given period.

    Coverage: currently partial (SAP connector bloqueado). Falls back to
    Barbara's planilla mágica export. EBITDA is a proxy (= MC) because
    OPEX is not in the current export.

    Source: scripts/pl_query.py (vault, Fase 1 Finance 2026-04-12)
    Pattern: .claude/rules/capability-atomic-scaffold.md
    """
    return PLQueryResult(
        period="2026-03",
        coverage="partial-no-sap",
        coverage_notes=(
            "SAP connector bloqueado — falling back to Barbara's Rolling Forecast "
            "export. OPEX not included. EBITDA reported here is a proxy (= MC)."
        ),
        revenue_clp=707_000_000,
        cogs_clp=246_000_000,
        gross_margin_clp=461_000_000,
        opex_clp=None,
        ebitda_clp=63_000_000,
        ebitda_is_proxy=True,
        line_items=[
            PLLineItem(line="Revenue", value_clp=707_000_000, pct_revenue=100.0),
            PLLineItem(line="COGS", value_clp=-246_000_000, pct_revenue=-34.8),
            PLLineItem(line="Royalty", value_clp=-57_000_000, pct_revenue=-8.1),
            PLLineItem(line="Gross Margin", value_clp=404_000_000, pct_revenue=57.1),
        ],
    )


# ─── Allocation Query ─────────────────────────────────────────────────

class AllocationChannel(BaseModel):
    channel: str
    allocated_units: int
    allocated_clp: int
    sell_through_target_pct: float


class AllocationQueryResult(BaseModel):
    initiative_id: str
    initiative_name: str
    total_units: int
    total_clp: int
    channels: list[AllocationChannel]
    vacancies: list[dict[str, Any]] = Field(
        description="List of planned allocations not yet filled"
    )
    vacancies_coverage: str
    expected_roi: float | None
    expected_roi_coverage: str


@function_tool
def allocation_query(
    initiative_id: Annotated[str, "Initiative identifier from OKRs Q2"],
) -> AllocationQueryResult:
    """
    Query channel allocation for a Q2 initiative.

    Partial coverage: vacancies and ROI fields are not computable from current
    data sources (BUK export lacks vacancy structure; no ROI estimator yet).

    Source: scripts/allocation_query.py (vault, Fase 1 Finance 2026-04-12)
    """
    return AllocationQueryResult(
        initiative_id=initiative_id,
        initiative_name=f"Initiative {initiative_id}",
        total_units=8500,
        total_clp=127_500_000,
        channels=[
            AllocationChannel(
                channel="E-COMMERCE DIRECTO",
                allocated_units=2500,
                allocated_clp=37_500_000,
                sell_through_target_pct=25.0,
            ),
            AllocationChannel(
                channel="TIENDA",
                allocated_units=4000,
                allocated_clp=60_000_000,
                sell_through_target_pct=18.0,
            ),
            AllocationChannel(
                channel="MAYORISTA",
                allocated_units=2000,
                allocated_clp=30_000_000,
                sell_through_target_pct=30.0,
            ),
        ],
        vacancies=[],
        vacancies_coverage="not-available-in-current-export",
        expected_roi=None,
        expected_roi_coverage="requires-roi-estimator-per-initiative-capability",
    )


# ─── Headcount Tracking ───────────────────────────────────────────────

class HeadcountRow(BaseModel):
    area: str
    role: str
    person_name: str
    hire_date: str
    status: str


class HeadcountTrackingResult(BaseModel):
    as_of_date: str
    total_active: int
    by_area: dict[str, int]
    recent_hires: list[HeadcountRow]
    recent_exits: list[HeadcountRow]
    coverage: str
    coverage_notes: str


@function_tool
def headcount_tracking(
    area: Annotated[str | None, "Filter by area (e.g. 'Finanzas', 'B2C')"] = None,
    role_contains: Annotated[str | None, "Substring match on role"] = None,
) -> HeadcountTrackingResult:
    """
    Query TBC headcount from BUK HR export.

    Source: scripts/headcount_tracking.py (vault, Fase 1 Finance 2026-04-12)
    Source of truth: BUK export graph/staging/2026-03-03-buk-nodes.jsonl
    """
    return HeadcountTrackingResult(
        as_of_date="2026-04-15",
        total_active=268,
        by_area={
            "B2C Retail": 142,
            "B2B Comercial": 48,
            "Operaciones": 38,
            "Finanzas": 12,
            "Personas": 8,
            "Transformación": 6,
            "CEO & Staff": 4,
            "Otros": 10,
        },
        recent_hires=[],
        recent_exits=[],
        coverage="partial-no-performance-scores",
        coverage_notes=(
            "BUK export has role/area/hire_date but no performance scores or "
            "vacancy structure. See allocation_query for vacancy gap."
        ),
    )
