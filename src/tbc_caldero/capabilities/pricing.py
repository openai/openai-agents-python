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


ALLOWED_GROUPINGS = {"Propiedad", "Clase", "CANAL", "Descripcion Bodega"}

# FP threshold: <=1% real discount counts as full price (matches POM methodology)
FP_THRESHOLD_PCT = 1.0


def _compute_full_price_sell_through(by: str = "Propiedad") -> FullPriceSellThroughResult:
    """Pure function — directly callable, testable, and used by the tool wrapper."""
    """
    Compute % of units sold without discount (≤1% REAL discount = full price).

    Uses REAL discount (1 - Precio Vta / Precio Blanco), NOT the unreliable
    '% Descto.' column which is always 0 for e-commerce channels.

    Reference: POM §1 diagnostic + Simon [Ch 10] Praktiker bankruptcy case.

    Wires real DuckDB if TBC_VAULT_PATH is available; falls back to stub with
    `coverage="stub-vault-unavailable"` otherwise (degraded-mode pattern per
    .claude/rules/capability-atomic-scaffold.md).
    """
    from tbc_caldero.adapters.duckdb_vault import VaultDW

    # Validate grouping against allowlist (prevent SQL injection via column name)
    if by not in ALLOWED_GROUPINGS:
        by = "Propiedad"

    dw = VaultDW.try_connect()
    if dw is None:
        # Degraded mode — return stub, mark methodology
        return FullPriceSellThroughResult(
            grouping=by,
            total_rows=0,
            avg_full_price_pct=0.0,
            top_3_full_price=[],
            methodology="stub-vault-unavailable — TBC_VAULT_PATH not set or DW missing",
        )

    # Real DuckDB query — computes real discount + full-price pct by group
    # Only rows with non-null prices contribute to the calc
    sql = f"""
        WITH priced AS (
            SELECT
                "{by}" AS group_col,
                "Venta Unidad" AS qty,
                CASE
                    WHEN "Precio Blanco" > 0 THEN
                        100.0 * (1.0 - ("Precio Vta." / NULLIF("Precio Blanco", 0)))
                    ELSE NULL
                END AS real_discount_pct
            FROM venta_b2c_2026
            WHERE "Precio Blanco" > 0
              AND "Precio Vta." > 0
              AND "Venta Unidad" > 0  -- Exclude returns/refunds (negative qty)
        ),
        grouped AS (
            SELECT
                group_col,
                SUM(qty) AS total_units,
                SUM(CASE WHEN real_discount_pct <= ? THEN qty ELSE 0 END) AS fp_units
            FROM priced
            WHERE group_col IS NOT NULL
            GROUP BY group_col
            HAVING SUM(qty) >= 10  -- Filter noise: require meaningful volume
        )
        SELECT
            group_col,
            total_units,
            fp_units,
            (100.0 * fp_units / total_units) AS fp_pct
        FROM grouped
        ORDER BY fp_pct DESC
    """

    try:
        rows = dw.query(sql, [FP_THRESHOLD_PCT])
    except Exception as e:
        return FullPriceSellThroughResult(
            grouping=by,
            total_rows=0,
            avg_full_price_pct=0.0,
            top_3_full_price=[],
            methodology=f"error-degraded: {type(e).__name__}: {str(e)[:80]}",
        )

    if not rows:
        return FullPriceSellThroughResult(
            grouping=by,
            total_rows=0,
            avg_full_price_pct=0.0,
            top_3_full_price=[],
            methodology="no-rows — check venta_b2c_2026 data freshness",
        )

    # Weighted avg FP% across all groups (weighted by total_units)
    total_units_all = sum(r["total_units"] for r in rows)
    total_fp_all = sum(r["fp_units"] for r in rows)
    avg_fp_pct = (100.0 * total_fp_all / total_units_all) if total_units_all else 0.0

    top_3 = [
        {
            "group_name": str(r["group_col"]),
            "fp_pct": round(r["fp_pct"], 2),
            "fp_units": int(r["fp_units"]),
        }
        for r in rows[:3]
    ]

    return FullPriceSellThroughResult(
        grouping=by,
        total_rows=len(rows),
        avg_full_price_pct=round(avg_fp_pct, 2),
        top_3_full_price=top_3,
        methodology=f"real_discount = 1 - (Precio Vta / Precio Blanco), FP threshold <= {FP_THRESHOLD_PCT}%",
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

    Reference: POM §1 diagnostic + Simon [Ch 10] Praktiker bankruptcy case.

    Wires real DuckDB if TBC_VAULT_PATH is available; falls back to stub with
    `coverage="stub-vault-unavailable"` otherwise (degraded-mode pattern per
    .claude/rules/capability-atomic-scaffold.md).
    """
    return _compute_full_price_sell_through(by=by)
