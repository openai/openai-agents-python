"""
Pricing capabilities — Spike port of scripts/pricing_engine.py

This is the FIRST capability port from the TBC vault. Goal: prove end-to-end
that an existing sync Python function can be wrapped as @function_tool without
friction.

Source capability: scripts/pricing_engine.py::full_price_sell_through
POM section: §1 diagnostic, §7 P6 Anti-Praktiker
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from agents import function_tool


class FullPriceSellThroughResult(BaseModel):
    """Structured output for full_price_sell_through capability."""

    grouping: str = Field(description="Column used for grouping (Propiedad, Clase, CANAL)")
    total_rows: int = Field(description="Number of rows returned")
    avg_full_price_pct: float = Field(
        description="Average % of units sold at full price across the grouping (0-100)"
    )
    top_3_full_price: list[dict] = Field(
        description="Top 3 groups by full-price %, each with keys: group_name, fp_pct, fp_units"
    )
    methodology: str = Field(
        description="Discount calculation method — real discount = 1 - (Precio Vta / Precio Blanco)"
    )


@function_tool
def full_price_sell_through(
    by: Annotated[
        str,
        "Column to group by. Must be one of: Propiedad, Clase, CANAL, Descripcion Bodega",
    ] = "Propiedad",
) -> FullPriceSellThroughResult:
    """
    Compute % of units sold without discount (≤1% REAL discount = full price).

    Uses REAL discount (1 - Precio Vta / Precio Blanco), NOT the unreliable
    '% Descto.' column which is always 0 for e-commerce channels.

    Critical reference: POM §1 diagnostic + Simon [Ch 10] Praktiker bankruptcy case.

    SPIKE NOTE: This is a stub returning mock data. In production port, this
    wrapper will call scripts/pricing_engine.full_price_sell_through() and
    serialize the DataFrame result. Requires DuckDB adapter (Ola 2 Step 2).
    """
    # TODO (Ola 2): wire real DuckDB query via adapter pattern:
    # from tbc_caldero.adapters.vault_capability import call_vault
    # df = call_vault("pricing_engine", "full_price_sell_through", by=by)
    # return FullPriceSellThroughResult.from_dataframe(df)

    # Spike stub — proves the wiring end-to-end without DuckDB dependency
    return FullPriceSellThroughResult(
        grouping=by,
        total_rows=12,
        avg_full_price_pct=12.2,
        top_3_full_price=[
            {"group_name": "MINNIE", "fp_pct": 34.1, "fp_units": 245},
            {"group_name": "SPIDERMAN", "fp_pct": 28.7, "fp_units": 189},
            {"group_name": "PAW PATROL", "fp_pct": 22.3, "fp_units": 156},
        ],
        methodology="real_discount = 1 - (Precio Vta / Precio Blanco)",
    )
