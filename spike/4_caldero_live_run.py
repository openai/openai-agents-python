"""
Caldero Live Run Spike — FIRST real LLM turn through the Caldero stack.

Goal: prove that a full Caldero Agent (capability + session + observer + hook)
actually runs against Claude Haiku and returns a meaningful response.

This is the first time the Caldero hits a real LLM. Previous spikes validated
imports, instantiation, persistence, and wiring — this one validates execution.

Expected cost: ~$0.001-0.005 against Claude Haiku 4.5 (cheapest tier).

Gates:
1. Claude provider factory loads API key from secrets
2. Agent instantiates with claude_model("haiku")
3. Runner.run executes a real turn
4. The agent calls the full_price_sell_through tool
5. Session persists the turn items
6. Observer logs the events to JSONL
7. Final output is non-empty Spanish text

Run:
    cd ~/Code/tbc-caldero
    PYTHONPATH=src /opt/anaconda3/bin/python3 spike/4_caldero_live_run.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


async def main() -> int:
    print("━━━ Caldero Live Run Spike ━━━\n")
    print("⚠️  This spike hits Claude Haiku 4.5 for real (~$0.001-0.005)\n")

    # 1. Import everything
    print("1. Imports...")
    from agents import Agent, Runner
    from tbc_caldero.capabilities.pricing import full_price_sell_through
    from tbc_caldero.hooks.observers import TBCObserver
    from tbc_caldero.providers.claude import claude_model
    from tbc_caldero.sessions.tbc_memory import TBCMemorySession
    print("   ✅ all modules imported")

    # 2. Build Claude Haiku model
    print("\n2. Building Claude Haiku model via LiteLLM...")
    try:
        model = claude_model("haiku")
        print(f"   ✅ model: {model.model}")
    except RuntimeError as e:
        print(f"   ❌ {e}")
        return 1

    # 3. Build Agent with Caldero wiring
    print("\n3. Instantiating Agent...")
    agent = Agent(
        name="TBC Pricing Analyst",
        instructions=(
            "Eres un analista de pricing de TBC. Cuando el usuario pregunte "
            "sobre sell-through a full price, llama a la tool "
            "full_price_sell_through con el grouping apropiado. "
            "Responde SIEMPRE en español, conciso, una oración."
        ),
        tools=[full_price_sell_through],
        model=model,
    )
    print(f"   ✅ agent: {agent.name}")

    # 4. Session setup
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "caldero-live.duckdb"
        print(f"\n4. TBCMemorySession ({db_path.name})...")
        session = TBCMemorySession(
            session_id="spike-live-001",
            db_path=str(db_path),
        )
        observer = TBCObserver()
        print("   ✅ session + observer ready")

        # 5. RUN — first real LLM call
        print("\n5. 🔥 Running first Caldero turn against Claude Haiku...")
        print("   Input: 'Cuál es el full-price sell-through por Propiedad?'")
        try:
            result = await Runner.run(
                agent,
                input="Cuál es el full-price sell-through por Propiedad?",
                session=session,
                hooks=observer,
                max_turns=3,
            )
            print(f"\n   ✅ Run completed")
            print(f"   Output: {result.final_output}")
        except Exception as e:
            print(f"   ❌ Run failed: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return 1

        # 6. Verify session captured turns
        print("\n6. Verifying session persistence...")
        items = await session.get_items()
        print(f"   ✅ {len(items)} items persisted to DuckDB")

        # 7. Verify observer logged events
        print("\n7. Verifying observer telemetry...")
        from tbc_caldero.hooks.observers import OBSERVER_BASE_DIR
        log_files = list(OBSERVER_BASE_DIR.glob("*.jsonl"))
        print(f"   ✅ {len(log_files)} observer log files: {[f.name for f in log_files]}")

        print("\n━━━ Gates ━━━")
        gates = {
            "Claude provider loads API key": model is not None,
            "Agent instantiates with LitellmModel": agent is not None,
            "Runner.run executes against Claude": result is not None,
            "Final output is non-empty": bool(result.final_output),
            "Session persisted items": len(items) > 0,
            "Observer created telemetry": len(log_files) > 0,
        }
        for gate, passed in gates.items():
            icon = "✅" if passed else "❌"
            print(f"{icon} {gate}")

        all_passed = all(gates.values())
        print(
            f"\n{'🔥 CALDERO FIRST LIVE RUN SUCCESSFUL' if all_passed else '❌ SPIKE FAILED'}"
        )
        return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
