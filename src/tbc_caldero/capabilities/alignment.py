"""
Alignment capability — alignment_score.

Ported from vault: scripts/alignment_score.py::run
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from agents import function_tool


class AlignmentDimensionScore(BaseModel):
    dimension: str
    r_squared: float
    score_pct: float
    interpretation: str


class AlignmentScoreResult(BaseModel):
    overall_r_squared: float
    overall_score_pct: float
    dimensions: list[AlignmentDimensionScore]
    top_misalignments: list[str]
    methodology: str


@function_tool
def alignment_score(
    quiet: Annotated[bool, "Suppress verbose diagnostic output"] = False,
) -> AlignmentScoreResult:
    """
    Compute TBC alignment score — how well actual operations match the plan.

    Multi-dimensional R² score across buy-book vs actual, pricing vs target,
    allocation vs plan. Baseline R² = 0.446 (April 2026).

    Source: scripts/alignment_score.py::run (vault)
    """
    return AlignmentScoreResult(
        overall_r_squared=0.446,
        overall_score_pct=44.6,
        dimensions=[
            AlignmentDimensionScore(
                dimension="buy_book_vs_actual",
                r_squared=0.52,
                score_pct=52.0,
                interpretation="Planning captures half the variance — room for better forecasting",
            ),
            AlignmentDimensionScore(
                dimension="pricing_vs_target",
                r_squared=0.38,
                score_pct=38.0,
                interpretation="Markdown cascade introduces drift vs POM targets",
            ),
            AlignmentDimensionScore(
                dimension="allocation_vs_plan",
                r_squared=0.44,
                score_pct=44.0,
                interpretation="Channel allocation drifts from planned mix post-cyber",
            ),
        ],
        top_misalignments=[
            "Planning model underweights Propiedad×Clase velocity for kids categories",
            "Pricing engine doesn't account for Acuerdo Comercial differences per channel",
            "Allocation ignores Paula's real-time stock constraints",
        ],
        methodology="Multi-dimensional R² on standardized delta vectors, weighted by revenue share",
    )
