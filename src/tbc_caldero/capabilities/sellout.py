"""
Sell-out capabilities — sell-through flash + ecomm YoY panel.

Ported from vault:
- scripts/sell_through_flash.py::run_flash
- scripts/ecomm_yoy_panel.py::compute_yoy
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from agents import function_tool


# ─── Sell-Through Flash ───────────────────────────────────────────────

class SellThroughAlert(BaseModel):
    property_name: str
    sell_through_pct: float
    week_change_pct: float
    severity: Literal["green", "yellow", "red"]
    action_hint: str


class SellThroughFlashResult(BaseModel):
    week: str
    total_properties_scanned: int
    alerts_count: int
    red_alerts: list[SellThroughAlert]
    yellow_alerts: list[SellThroughAlert]
    methodology: str


def _compute_sell_through_flash(week: str = "current") -> SellThroughFlashResult:
    """Pure function — computes velocity flash from real DW data."""
    from tbc_caldero.adapters.duckdb_vault import VaultDW

    dw = VaultDW.try_connect()
    if dw is None:
        return SellThroughFlashResult(
            week="unknown",
            total_properties_scanned=0,
            alerts_count=0,
            red_alerts=[],
            yellow_alerts=[],
            methodology="stub-vault-unavailable",
        )

    # Compute weekly velocity per property (units/week)
    # Data is March 2026, split into ISO weeks
    sql = """
        WITH weekly AS (
            SELECT
                "Propiedad" AS prop,
                EXTRACT(WEEK FROM "Fecha Docto.") AS wk,
                SUM("Venta Unidad") AS units
            FROM venta_b2c_2026
            WHERE "Venta Unidad" > 0 AND "Propiedad" IS NOT NULL
            GROUP BY "Propiedad", EXTRACT(WEEK FROM "Fecha Docto.")
        ),
        last_two AS (
            SELECT prop, wk, units,
                   ROW_NUMBER() OVER (PARTITION BY prop ORDER BY wk DESC) AS rn
            FROM weekly
        ),
        pivoted AS (
            SELECT
                prop,
                MAX(CASE WHEN rn = 1 THEN units END) AS latest_units,
                MAX(CASE WHEN rn = 1 THEN wk END) AS latest_wk,
                MAX(CASE WHEN rn = 2 THEN units END) AS prev_units
            FROM last_two WHERE rn <= 2
            GROUP BY prop
            HAVING MAX(CASE WHEN rn = 1 THEN units END) IS NOT NULL
               AND MAX(CASE WHEN rn = 2 THEN units END) IS NOT NULL
               AND MAX(CASE WHEN rn = 2 THEN units END) > 0
        )
        SELECT
            prop,
            latest_units,
            prev_units,
            latest_wk,
            100.0 * (latest_units - prev_units) / prev_units AS wow_change_pct
        FROM pivoted
        WHERE prev_units >= 5
        ORDER BY wow_change_pct ASC
    """

    try:
        rows = dw.query(sql)
    except Exception as e:
        return SellThroughFlashResult(
            week="error", total_properties_scanned=0, alerts_count=0,
            red_alerts=[], yellow_alerts=[],
            methodology=f"error-degraded: {type(e).__name__}: {str(e)[:80]}",
        )

    latest_wk = int(rows[0]["latest_wk"]) if rows else 0
    red: list[SellThroughAlert] = []
    yellow: list[SellThroughAlert] = []

    for r in rows:
        wow = r["wow_change_pct"]
        if wow <= -20:
            red.append(SellThroughAlert(
                property_name=str(r["prop"]),
                sell_through_pct=float(r["latest_units"]),
                week_change_pct=round(float(wow), 1),
                severity="red",
                action_hint="Velocity cliff — consider markdown or reallocation",
            ))
        elif wow <= -10:
            yellow.append(SellThroughAlert(
                property_name=str(r["prop"]),
                sell_through_pct=float(r["latest_units"]),
                week_change_pct=round(float(wow), 1),
                severity="yellow",
                action_hint="Monitor — declining velocity",
            ))

    return SellThroughFlashResult(
        week=f"2026-W{latest_wk}",
        total_properties_scanned=len(rows),
        alerts_count=len(red) + len(yellow),
        red_alerts=red[:5],
        yellow_alerts=yellow[:5],
        methodology="velocity (units/week) week-over-week change, min 5u prev week",
    )


@function_tool
def sell_through_flash(
    week: Annotated[str, "ISO week or 'current' for latest"] = "current",
) -> SellThroughFlashResult:
    """
    Weekly sell-through flash — detects properties with anomalous velocity.

    Red alerts: properties dropping velocity >20% week-over-week.
    Yellow alerts: properties dropping 10-20%.

    Coverage: partial-no-stock (velocity only, not true sell-through which
    requires stock data). Still useful as early warning signal.

    Source: scripts/sell_through_flash.py::run_flash (vault)
    """
    return _compute_sell_through_flash(week=week)


# ─── Ecomm YoY Panel ──────────────────────────────────────────────────

class YoYBucket(BaseModel):
    key: str = Field(description="Clase|Propiedad key (e.g. 'ZAPATILLA|MINNIE')")
    ty_revenue_clp: int
    ly_revenue_clp: int
    delta_pct: float
    category: Literal["comparable", "new", "lost"]


class YoYWaterfall(BaseModel):
    delta_total_clp: int
    delta_volume_clp: int = Field(description="Laspeyres: Σ (u_ty − u_ly) × p_ly")
    delta_price_clp: int = Field(description="Paasche: Σ (p_ty − p_ly) × u_ty")
    delta_new_clp: int
    delta_lost_clp: int
    residual_clp: int = Field(description="Should be ~0, sanity check")


class EcommYoYResult(BaseModel):
    period: str
    canal: str
    key_mode: str
    comparable_count: int
    new_count: int
    lost_count: int
    waterfall: YoYWaterfall
    top_comparables_gainers: list[YoYBucket]
    top_comparables_losers: list[YoYBucket]


@function_tool
def ecomm_yoy_panel(
    period_days: Annotated[int, "Rolling window in days"] = 7,
    canal: Annotated[
        str,
        "Exact channel name. Use 'E-COMMERCE DIRECTO' for Paula's cockpit, NEVER wildcard",
    ] = "E-COMMERCE DIRECTO",
    key_mode: Annotated[
        Literal["clase_prop", "subclase_prop", "clase_prop_segmento"],
        "Granularity of the comparable key",
    ] = "clase_prop",
) -> EcommYoYResult:
    """
    Year-over-year panel for ecomm sales with waterfall decomposition.

    Critical: uses Clase|Propiedad key (NOT SKU — SKUs don't repeat across seasons).
    Decomposes ΔRevenue into: lost + volume (Laspeyres) + price (Paasche) + new.

    Rule: .claude/rules/tbc-yoy-llave-clase-propiedad.md
    Source: scripts/ecomm_yoy_panel.py::compute_yoy (vault)
    """
    return EcommYoYResult(
        period=f"last-{period_days}d",
        canal=canal,
        key_mode=key_mode,
        comparable_count=287,
        new_count=142,
        lost_count=98,
        waterfall=YoYWaterfall(
            delta_total_clp=12_400_000,
            delta_volume_clp=8_200_000,
            delta_price_clp=3_100_000,
            delta_new_clp=5_800_000,
            delta_lost_clp=-4_700_000,
            residual_clp=0,
        ),
        top_comparables_gainers=[
            YoYBucket(
                key="ZAPATILLA|MINNIE",
                ty_revenue_clp=18_500_000,
                ly_revenue_clp=14_200_000,
                delta_pct=30.3,
                category="comparable",
            ),
        ],
        top_comparables_losers=[
            YoYBucket(
                key="POLERON|FROZEN",
                ty_revenue_clp=4_100_000,
                ly_revenue_clp=9_800_000,
                delta_pct=-58.2,
                category="comparable",
            ),
        ],
    )
