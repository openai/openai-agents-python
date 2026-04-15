"""
Assortment capabilities — OTB Scorecard + Berkhout classification.

Ported from vault:
- scripts/otb_scorecard.py::build_scorecard
- scripts/berkhout_classify.py::classify_assortment_role

Both are stubs returning mock-shaped outputs. Ola 2 Step 5 wires real
DuckDB queries via an adapter to the vault.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from agents import function_tool


# ─── OTB Scorecard ────────────────────────────────────────────────────

class OTBCell(BaseModel):
    propiedad: str
    clase: str
    sell_thru_pct: float
    plan_units: int
    actual_units: int
    cell_class: str = Field(description="TRAMPA | GEM | STEADY | DOG — Berkhout 4-quadrant")
    mc_clp: int = Field(description="Margin contribution in CLP")


class OTBScorecardResult(BaseModel):
    period: str
    total_cells: int
    total_trampas: int
    total_trampas_mc_clp: int
    top_trampas: list[OTBCell]
    methodology: str


@function_tool
def otb_scorecard(
    period: Annotated[str, "Buy-book period identifier (e.g. 'Q127', 'PV26-27')"] = "Q127",
) -> OTBScorecardResult:
    """
    Build the Open-To-Buy scorecard for a buy-book period.

    Classifies each Propiedad × Clase cell into TRAMPA / GEM / STEADY / DOG
    using the Berkhout 4-quadrant method (velocity × elasticity). TRAMPA cells
    are the loss-makers — slow-moving inventory that doesn't respond to
    discount.

    Source: scripts/otb_scorecard.py::build_scorecard (vault)
    Framework: memory/frameworks/berkhout-assortment-merchandising.md
    """
    return OTBScorecardResult(
        period=period,
        total_cells=139,
        total_trampas=58,
        total_trampas_mc_clp=433_000_000,
        top_trampas=[
            OTBCell(
                propiedad="MINNIE",
                clase="ZAPATILLA",
                sell_thru_pct=14.2,
                plan_units=3200,
                actual_units=455,
                cell_class="TRAMPA",
                mc_clp=-28_500_000,
            ),
            OTBCell(
                propiedad="SPIDERMAN",
                clase="POLERON",
                sell_thru_pct=22.8,
                plan_units=1800,
                actual_units=410,
                cell_class="TRAMPA",
                mc_clp=-15_200_000,
            ),
        ],
        methodology="Berkhout 4-quadrant: velocity × elasticity → TRAMPA/GEM/STEADY/DOG",
    )


# ─── Berkhout classification ──────────────────────────────────────────

class BerkhoutClassification(BaseModel):
    propiedad: str
    role: str = Field(description="One of: HERO, DRIVER, FILL, FLANKER, REDUNDANT")
    style_count: int
    contribution_pct: float
    rationale: str


class BerkhoutClassifyResult(BaseModel):
    total_properties: int
    by_role: dict[str, int]
    top_heroes: list[BerkhoutClassification]
    choice_overload_flags: list[str]


@function_tool
def berkhout_classify(
    min_contribution_pct: Annotated[
        float,
        "Minimum revenue contribution % for a property to be classified as HERO",
    ] = 5.0,
) -> BerkhoutClassifyResult:
    """
    Classify assortment into Berkhout assortment roles (HERO/DRIVER/FILL/FLANKER/REDUNDANT).

    Flags properties with too many styles (choice overload — SDT violation).

    Source: scripts/berkhout_classify.py (vault)
    Framework: memory/frameworks/berkhout-assortment-merchandising.md
    """
    return BerkhoutClassifyResult(
        total_properties=48,
        by_role={"HERO": 4, "DRIVER": 12, "FILL": 18, "FLANKER": 10, "REDUNDANT": 4},
        top_heroes=[
            BerkhoutClassification(
                propiedad="MINNIE",
                role="HERO",
                style_count=22,
                contribution_pct=14.8,
                rationale="Top contributor + high sell-through",
            ),
            BerkhoutClassification(
                propiedad="SPIDERMAN",
                role="HERO",
                style_count=18,
                contribution_pct=11.2,
                rationale="Consistent velocity across seasons",
            ),
        ],
        choice_overload_flags=[
            "DISNEY PRINCESS: 34 styles (SDT threshold: 20) — consider pruning",
            "MARVEL: 28 styles (SDT threshold: 20) — consider pruning",
        ],
    )
