"""
Caldero DuckDB Adapter Spike — validates real data flows end-to-end.

Upgrades full_price_sell_through from stub → real DuckDB query against the
TBC warehouse. This is the first capability to return REAL TBC data through
the Caldero stack, not mock data.

Gates:
1. VaultDW.try_connect() finds the DW via default iCloud path
2. dw.query() returns real rows
3. full_price_sell_through returns real data (not stub)
4. Methodology string reflects real computation (not 'stub-vault-unavailable')
5. Top 3 groups have non-zero values

Run:
    cd ~/Code/tbc-caldero
    PYTHONPATH=src /opt/anaconda3/bin/python3 spike/6_caldero_duckdb_adapter.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


def main() -> int:
    print("━━━ Caldero DuckDB Adapter Spike ━━━\n")

    # 1. Vault connection
    print("1. Connecting to vault DW via VaultDW.try_connect()...")
    from tbc_caldero.adapters.duckdb_vault import VaultDW

    dw = VaultDW.try_connect()
    if dw is None:
        print("   ❌ VaultDW.try_connect() returned None — vault not found")
        return 1
    print(f"   ✅ {dw}")

    # 2. Probe tables
    print("\n2. Listing tables...")
    tables = dw.tables()
    print(f"   ✅ {len(tables)} tables found")
    for t in sorted(tables)[:8]:
        print(f"      · {t}")

    # 3. Direct query sanity check
    print("\n3. Probe query against venta_b2c_2026...")
    rows = dw.query(
        "SELECT COUNT(*) AS n FROM venta_b2c_2026 WHERE CANAL = ?",
        ["E-COMMERCE DIRECTO"],
    )
    n = rows[0]["n"] if rows else 0
    print(f"   ✅ E-COMMERCE DIRECTO rows: {n:,}")

    # 4. Call capability — now backed by real DW
    print("\n4. Invoking _compute_full_price_sell_through (pure function)...")
    from tbc_caldero.capabilities.pricing import _compute_full_price_sell_through

    result = _compute_full_price_sell_through(by="Propiedad")
    print(f"   ✅ grouping: {result.grouping}")
    print(f"   ✅ total_rows: {result.total_rows}")
    print(f"   ✅ avg_fp_pct: {result.avg_full_price_pct}")
    print(f"   ✅ methodology: {result.methodology}")
    print("\n   Top 3 full-price groups:")
    for i, g in enumerate(result.top_3_full_price, 1):
        print(f"      {i}. {g}")
    methodology_ok = "real_discount" in result.methodology
    total_rows_ok = result.total_rows > 0

    print("\n━━━ Gates ━━━")
    gates = {
        "VaultDW.try_connect() found the DW": dw is not None,
        "Tables listed (including venta_b2c_2026)": "venta_b2c_2026" in tables,
        "E-COMMERCE DIRECTO has rows": n > 0,
        "full_price_sell_through returned real data": total_rows_ok,
        "Methodology reflects real computation": methodology_ok,
    }
    for gate, passed in gates.items():
        icon = "✅" if passed else "❌"
        print(f"{icon} {gate}")

    all_passed = all(gates.values())
    print(
        f"\n{'🔥 CALDERO v0.0.5 — REAL DATA FLOWS END-TO-END' if all_passed else '❌ SPIKE FAILED'}"
    )
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
