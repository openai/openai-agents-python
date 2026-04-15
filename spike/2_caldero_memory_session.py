"""
Caldero Memory Session Spike — validates TBCMemorySession end-to-end.

Goal: prove that conversation history persists across instance boundaries
(analogous to process boundaries) via DuckDB-backed Session protocol.

Gates:
1. Session instantiates against a fresh DuckDB file
2. add_items persists items (writes to caldero_session_items table)
3. get_items returns the items in chronological order
4. NEW session instance with SAME session_id reads the same items (persistence)
5. pop_item removes the last item + returns it
6. limit parameter returns latest N in chronological order
7. clear_session removes all items but keeps session_summaries audit row
8. Two different session_ids are isolated (no cross-talk)

Run:
    cd ~/Code/tbc-caldero
    PYTHONPATH=src /opt/anaconda3/bin/python3 spike/2_caldero_memory_session.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


async def main() -> int:
    print("━━━ Caldero Memory Session Spike ━━━\n")

    from tbc_caldero.sessions.tbc_memory import TBCMemorySession

    # Use a temp DuckDB file so the spike doesn't touch real warehouse
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "caldero-spike.duckdb"

        print(f"1. Instantiating session with fresh DB: {db_path}")
        session = TBCMemorySession(
            session_id="spike-session-001",
            db_path=str(db_path),
        )
        print("   ✅ session created")

        print("\n2. Adding 3 items...")
        items = [
            {"role": "user", "content": "Cual es el full-price sell-through de marzo?"},
            {"role": "assistant", "content": "Déjame consultar. [tool: full_price_sell_through]"},
            {"role": "tool", "content": "Average FP: 12.2%, top: MINNIE 34.1%"},
        ]
        await session.add_items(items)
        print(f"   ✅ {len(items)} items added")

        print("\n3. Retrieving all items via get_items()...")
        retrieved = await session.get_items()
        assert len(retrieved) == 3, f"Expected 3 items, got {len(retrieved)}"
        assert retrieved[0]["role"] == "user"
        assert retrieved[2]["role"] == "tool"
        print(f"   ✅ {len(retrieved)} items retrieved in order")

        print("\n4. Creating NEW session instance with SAME session_id...")
        session2 = TBCMemorySession(
            session_id="spike-session-001",
            db_path=str(db_path),
        )
        retrieved2 = await session2.get_items()
        assert len(retrieved2) == 3, f"Persistence broken: got {len(retrieved2)} items on fresh instance"
        assert retrieved2[0]["content"] == retrieved[0]["content"]
        print(f"   ✅ {len(retrieved2)} items read from fresh instance (persistence works)")

        print("\n5. Adding a 4th item via session2, reading via session...")
        await session2.add_items([{"role": "user", "content": "Sigamos"}])
        retrieved3 = await session.get_items()
        assert len(retrieved3) == 4, f"Cross-instance write not visible: got {len(retrieved3)}"
        print(f"   ✅ cross-instance write visible ({len(retrieved3)} items)")

        print("\n6. pop_item removes and returns the latest item...")
        popped = await session.pop_item()
        assert popped is not None
        assert popped["content"] == "Sigamos", f"Wrong item popped: {popped}"
        remaining = await session.get_items()
        assert len(remaining) == 3, f"Expected 3 after pop, got {len(remaining)}"
        print(f"   ✅ popped {popped['role']!r} item, {len(remaining)} remain")

        print("\n7. limit parameter returns latest N in chronological order...")
        latest_2 = await session.get_items(limit=2)
        assert len(latest_2) == 2
        assert latest_2[0]["role"] == "assistant"
        assert latest_2[1]["role"] == "tool"
        print(f"   ✅ latest 2 items: {[i['role'] for i in latest_2]}")

        print("\n8. Isolated session_id (different session doesn't see items)...")
        other_session = TBCMemorySession(
            session_id="spike-session-002",
            db_path=str(db_path),
        )
        other_items = await other_session.get_items()
        assert len(other_items) == 0, f"Isolation broken: got {len(other_items)} items"
        print("   ✅ isolated session has 0 items (no cross-talk)")

        print("\n9. clear_session removes items but keeps session_summaries...")
        await session.clear_session()
        cleared = await session.get_items()
        assert len(cleared) == 0
        # Verify session_summaries still has the row
        import duckdb

        with duckdb.connect(str(db_path)) as con:
            rows = con.execute(
                "SELECT COUNT(*) FROM session_summaries WHERE session_id = ?",
                ["spike-session-001"],
            ).fetchall()
            assert rows[0][0] == 1, "session_summaries audit row should remain"
        print("   ✅ items cleared, audit row preserved")

        print("\n━━━ Gates ━━━")
        gates = {
            "Session instantiates against fresh DuckDB": True,
            "add_items persists to caldero_session_items": len(retrieved) == 3,
            "get_items returns items in order": retrieved[0]["role"] == "user",
            "Persistence across instances": len(retrieved2) == 3,
            "Cross-instance writes visible": len(retrieved3) == 4,
            "pop_item removes + returns latest": popped is not None,
            "limit returns latest N chronologically": len(latest_2) == 2,
            "Isolated session_ids": len(other_items) == 0,
            "clear_session preserves audit row": True,
        }
        for gate, passed in gates.items():
            icon = "✅" if passed else "❌"
            print(f"{icon} {gate}")

        all_passed = all(gates.values())
        print(f"\n{'✅ SPIKE PASSED' if all_passed else '❌ SPIKE FAILED'}")
        return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
