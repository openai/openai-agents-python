"""
DuckDB Vault Adapter — read-only client for the TBC warehouse.

Locates the vault DuckDB via environment variable TBC_VAULT_PATH
(+ fallback to the default iCloud location) and exposes a `query()`
method that returns list[dict] for clean serialization.

Capabilities use this adapter to replace stub returns with real data.
If the adapter can't connect (vault not mounted, env var unset), capabilities
should fall back to mock data with `coverage="stub-vault-unavailable"`
instead of raising — degraded mode > crash (per rule `capability-atomic-scaffold.md`).

Usage:
    from tbc_caldero.adapters.duckdb_vault import VaultDW

    dw = VaultDW.try_connect()
    if dw is not None:
        rows = dw.query("SELECT * FROM venta_b2c_2026 WHERE CANAL = ? LIMIT 10",
                        ["E-COMMERCE DIRECTO"])
    else:
        # Degraded mode: return stub
        ...
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import duckdb

# Default iCloud path. TBC-specific, hardcoded as fallback because that's
# where 100% of TBC vault installs live (until proven otherwise).
DEFAULT_VAULT_PATH = (
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "iCloud~md~obsidian"
    / "Documents"
    / "Cowork JIC"
)

DW_RELATIVE_PATH = "data/tbc-warehouse.duckdb"


class VaultDW:
    """Read-only adapter to the TBC warehouse DuckDB file."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        if not db_path.exists():
            raise FileNotFoundError(f"TBC DW not found at {db_path}")

    @classmethod
    def try_connect(cls, vault_path: str | Path | None = None) -> VaultDW | None:
        """
        Attempt to locate + open the vault DW. Returns None on failure
        instead of raising — capabilities use this for degraded-mode fallback.

        Resolution order:
        1. Explicit vault_path param
        2. TBC_VAULT_PATH env var
        3. DEFAULT_VAULT_PATH (iCloud location)
        """
        candidate_vaults: list[Path] = []
        if vault_path:
            candidate_vaults.append(Path(vault_path).expanduser())
        env = os.environ.get("TBC_VAULT_PATH")
        if env:
            candidate_vaults.append(Path(env).expanduser())
        candidate_vaults.append(DEFAULT_VAULT_PATH)

        for vault in candidate_vaults:
            db = vault / DW_RELATIVE_PATH
            if db.exists():
                try:
                    return cls(db)
                except Exception:
                    continue
        return None

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """
        Execute a read-only query and return rows as list of dicts.

        Opens a fresh connection per query (DuckDB is fast, avoids stale handles).
        """
        with duckdb.connect(str(self.db_path), read_only=True) as con:
            cursor = con.execute(sql, params or [])
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
            return [dict(zip(columns, row)) for row in rows]

    def tables(self) -> list[str]:
        """List all tables in the DW for discovery."""
        rows = self.query("SHOW TABLES")
        return [r.get("name", "") for r in rows if r.get("name")]

    def __repr__(self) -> str:
        return f"VaultDW(db_path={self.db_path})"
