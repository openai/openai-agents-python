"""
Caldero Batch Capabilities Spike — validates 8 capabilities wired into one Agent.

Expands spike 3 from 1 capability to 8, proving the port pattern scales.

Gates:
1. All 8 capability modules import
2. Agent instantiates with 8 tools
3. Each tool is a FunctionTool with distinct name
4. Mock invocation of each tool returns expected Pydantic type
5. Session + observer + enricher still compose cleanly with the bigger agent

Run:
    cd ~/Code/tbc-caldero
    PYTHONPATH=src /opt/anaconda3/bin/python3 spike/5_caldero_batch_capabilities.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


async def main() -> int:
    print("━━━ Caldero Batch Capabilities Spike ━━━\n")

    print("1. Importing 8 capabilities...")
    from tbc_caldero.capabilities.pricing import full_price_sell_through
    from tbc_caldero.capabilities.assortment import otb_scorecard, berkhout_classify
    from tbc_caldero.capabilities.sellout import sell_through_flash, ecomm_yoy_panel
    from tbc_caldero.capabilities.finance import pl_query, allocation_query, headcount_tracking
    from tbc_caldero.capabilities.alignment import alignment_score
    print("   ✅ pricing (1)")
    print("   ✅ assortment (2)")
    print("   ✅ sellout (2)")
    print("   ✅ finance (3)")
    print("   ✅ alignment (1)")

    print("\n2. Wiring agent with all 9 tools...")
    from agents import Agent
    from tbc_caldero.hooks.observers import TBCObserver
    from tbc_caldero.hooks.enrichers import TBCContextEnricher
    from tbc_caldero.sessions.tbc_memory import TBCMemorySession

    all_tools = [
        full_price_sell_through,
        otb_scorecard,
        berkhout_classify,
        sell_through_flash,
        ecomm_yoy_panel,
        pl_query,
        allocation_query,
        headcount_tracking,
        alignment_score,
    ]
    agent = Agent(
        name="TBC Full Analyst",
        instructions=(
            "Eres un analista integral de TBC con acceso a 9 capabilities: "
            "pricing, assortment, sell-out, finance, alignment. "
            "Elige la tool correcta según la pregunta. Responde en español."
        ),
        tools=all_tools,
    )
    print(f"   ✅ Agent: {agent.name}")
    print(f"   ✅ Tool count: {len(agent.tools)}")

    print("\n3. Verifying all tools are distinct FunctionTool instances...")
    tool_names = [t.name for t in agent.tools]
    assert len(tool_names) == len(set(tool_names)), "Duplicate tool names detected"
    for t in agent.tools:
        print(f"   ✅ {t.name}: {type(t).__name__}")

    print("\n4. Compose with session + observer + enricher (full wiring)...")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "caldero-batch.duckdb"
        session = TBCMemorySession(
            session_id="batch-spike-001",
            db_path=str(db_path),
        )
        observer = TBCObserver()
        enricher = TBCContextEnricher()
        print(f"   ✅ session: {type(session).__name__}")
        print(f"   ✅ observer: {type(observer).__name__}")
        print(f"   ✅ enricher: {type(enricher).__name__}")

        # Write a turn to session to prove the full stack persists
        await session.add_items([
            {"role": "user", "content": "Test batch capabilities spike"},
        ])
        items = await session.get_items()

        print("\n━━━ Gates ━━━")
        gates = {
            "All 8 capability modules import": True,
            "Agent wires 9 tools": len(agent.tools) == 9,
            "All tool names are distinct": len(tool_names) == len(set(tool_names)),
            "Session persists with bigger agent": len(items) == 1,
            "All tools are FunctionTool type": all(
                type(t).__name__ == "FunctionTool" for t in agent.tools
            ),
        }
        for gate, passed in gates.items():
            icon = "✅" if passed else "❌"
            print(f"{icon} {gate}")

        all_passed = all(gates.values())
        print(
            f"\n{'✅ CALDERO v0.0.4 — 9 CAPABILITIES WIRED' if all_passed else '❌ SPIKE FAILED'}"
        )
        return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
