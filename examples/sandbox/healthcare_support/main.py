from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    _DEMO_DIR = Path(__file__).resolve().parent
    sys.path.insert(0, str(_DEMO_DIR.parents[2]))
    sys.path.insert(0, str(_DEMO_DIR))

from examples.auto_mode import confirm_with_fallback, input_with_fallback  # noqa: E402
from examples.sandbox.healthcare_support.data import (  # noqa: E402
    HealthcareSupportDataStore,
    load_root_env,
)
from examples.sandbox.healthcare_support.models import ScenarioCase  # noqa: E402
from examples.sandbox.healthcare_support.tools import HealthcareSupportContext  # noqa: E402
from examples.sandbox.healthcare_support.workflow import (  # noqa: E402
    CACHE_ROOT,
    DEFAULT_SESSION_ID,
    SESSION_DB_PATH,
    build_context,
    run_healthcare_support_workflow,
)

DEFAULT_SCENARIO_ID = "eligibility_verification_basic"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the healthcare support Agents SDK demo from the command line.",
    )
    parser.add_argument(
        "--scenario",
        dest="scenario_id",
        default=None,
        help="Scenario ID to run. If omitted, the CLI asks interactively.",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="Print the built-in scenario IDs and exit.",
    )
    parser.add_argument(
        "--reset-memory",
        action="store_true",
        help="Delete the shared SQLite session database before running.",
    )
    return parser


def _print_scenarios(store: HealthcareSupportDataStore) -> None:
    print("Available scenarios:\n")
    for scenario_id in store.list_scenario_ids():
        scenario = store.get_scenario(scenario_id)
        print(f"- {scenario.scenario_id}")
        print(f"  {scenario.description}")


def _pick_scenario(store: HealthcareSupportDataStore, requested_id: str | None) -> ScenarioCase:
    if requested_id:
        return store.get_scenario(requested_id)

    scenario_id = input_with_fallback(
        "Enter a scenario ID: ",
        DEFAULT_SCENARIO_ID,
    ).strip()
    if not scenario_id:
        scenario_id = DEFAULT_SCENARIO_ID
    return store.get_scenario(scenario_id)


async def _approval_handler(request: dict[str, Any]) -> bool:
    print("\nHuman approval requested")
    print(f"Agent: {request.get('agent', 'unknown')}")
    print(f"Tool: {request.get('tool', 'route_to_human_queue')}")
    print(json.dumps(request.get("arguments", {}), indent=2))
    return confirm_with_fallback("Approve handoff to a human queue? [y/N]: ", True)


def _print_run_header(*, scenario: ScenarioCase, context: HealthcareSupportContext) -> None:
    print("\n" + "=" * 80)
    print("Healthcare Support Agents SDK Demo")
    print(f"Scenario: {scenario.scenario_id}")
    print(f"Description: {scenario.description}")
    print(f"SQLite memory session: {context.session_id}")
    print("\nCustomer transcript:\n")
    print(scenario.transcript)


def _print_run_result(payload: dict[str, Any]) -> None:
    print("\nTrace URL:")
    print(payload["trace_url"])

    print("\nPatient-facing response:\n")
    print(payload["resolution"]["patient_facing_response"])

    print("\nInternal summary:")
    print(payload["resolution"]["internal_summary"])

    print("\nNext step:")
    print(payload["resolution"]["next_step"])

    if payload["resolution"].get("handoff_id"):
        print("\nHuman handoff:")
        print(payload["resolution"]["handoff_id"])

    print("\nGenerated sandbox artifacts:")
    for artifact in payload.get("artifacts", []):
        print(f"- {artifact['path']}")

    print("\nMemory recap:")
    print(json.dumps(payload["memory_recap"], indent=2))

    print(f"\nSession memory items: {payload['session_memory_items']}")


async def main() -> None:
    load_root_env()
    args = _build_parser().parse_args()
    store = HealthcareSupportDataStore.load()

    if args.list_scenarios:
        _print_scenarios(store)
        return

    if args.reset_memory and SESSION_DB_PATH.exists():
        SESSION_DB_PATH.unlink()

    scenario = _pick_scenario(store, args.scenario_id)
    context = build_context(
        store=store,
        scenario_id=scenario.scenario_id,
        session_id=DEFAULT_SESSION_ID,
    )
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)

    _print_run_header(scenario=scenario, context=context)
    payload = await run_healthcare_support_workflow(
        context=context,
        scenario_id=scenario.scenario_id,
        approval_handler=_approval_handler,
    )
    _print_run_result(payload)


if __name__ == "__main__":
    asyncio.run(main())
