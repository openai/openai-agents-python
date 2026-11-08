"""Compare synthetic task trials; scripted mode checks the harness only."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agents import Agent, NativeError, function_tool
from agents.testing import ScriptedResponses, text, tool_call
from agents.version import __version__

SCORING_VERSION = 1
CATALOG = {"A": "violet", "B": "amber"}


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    timeout_seconds: float = Field(default=60, gt=0, le=600)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    expected_answer: str
    expected_lookups: list[str]


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def score(answer: str | None, calls: list[str], case: Case, error: str | None) -> dict[str, bool]:
    return {
        "completed": error is None,
        "answer_exact": error is None
        and answer is not None
        and answer.strip() == case.expected_answer,
        # Order and multiplicity matter: an extra or repeated lookup fails.
        "tool_behavior": error is None and calls == case.expected_lookups,
    }


async def trial(config: Configuration, case: Case, mode: str, repeat: int) -> dict[str, Any]:
    calls: list[str] = []

    @function_tool
    async def lookup(code: str) -> str:
        """Read the synthetic catalog. Missing codes return UNKNOWN."""
        calls.append(code)
        return CATALOG.get(code, "UNKNOWN")

    async def execute(provider: Any = None) -> str:
        agent = Agent(
            instructions=config.instructions,
            model=config.model,
            model_settings={"reasoning_effort": config.reasoning_effort}
            if config.reasoning_effort
            else {},
            tools=[lookup],
            provider=provider,
        )
        result = await asyncio.wait_for(agent.run(case.prompt), timeout=config.timeout_seconds)
        return result.final_output

    started = perf_counter()
    answer: str | None = None
    error: str | None = None
    provider_category: str | None = None
    try:
        if mode == "scripted":
            responses = [
                [tool_call("lookup", {"code": code}, call_id=f"lookup-{i}")]
                for i, code in enumerate(case.expected_lookups)
            ]
            responses.append([text(case.expected_answer)])
            async with ScriptedResponses(*responses) as peer:
                answer = await execute(peer.provider)
                # Receipt proves real callbacks submitted their results to Codex.
                for i, code in enumerate(case.expected_lookups):
                    if peer.tool_output(f"lookup-{i}") != CATALOG.get(code, "UNKNOWN"):
                        raise RuntimeError("Tool result did not reach the synthetic peer")
        else:
            answer = await execute()
    except NativeError as exc:
        error = type(exc).__name__
        provider_category = exc.category
    except Exception as exc:
        # Do not copy exception messages or traceback locals into result artifacts.
        error = type(exc).__name__
    scores = score(answer, calls, case, error)
    return {
        "case_id": case.id,
        "repeat": repeat,
        "answer": answer,
        "tool_calls": calls,
        "error_type": error,
        "provider_category": provider_category,
        "elapsed_seconds": perf_counter() - started,
        "scores": scores,
        "passed": all(scores.values()),
    }


def validate_baseline(current: dict[str, Any], baseline: dict[str, Any]) -> None:
    for field in ("cases_sha256", "catalog_sha256", "repeats", "mode", "scoring_version"):
        if current[field] != baseline[field]:
            raise ValueError(
                f"Baseline {field} differs; use the same cases, repeats, mode and scorer"
            )
    keys = [(row["case_id"], row["repeat"]) for row in current["trials"]]
    prior = {(row["case_id"], row["repeat"]): row for row in baseline["trials"]}
    if len(prior) != len(baseline["trials"]) or set(prior) != set(keys):
        raise ValueError("Baseline trial identities differ")


def compare(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    validate_baseline(current, baseline)
    prior = {(row["case_id"], row["repeat"]): row for row in baseline["trials"]}
    regressions = [
        {"case_id": row["case_id"], "repeat": row["repeat"], "score": name}
        for row in current["trials"]
        for name, passed in row["scores"].items()
        if prior[(row["case_id"], row["repeat"])]["scores"][name] and not passed
    ]
    return {
        "baseline_config_sha256": baseline["config_sha256"],
        "regressions": regressions,
        "passed_trial_delta": sum(row["passed"] for row in current["trials"])
        - sum(row["passed"] for row in baseline["trials"]),
    }


async def evaluate(
    config: Configuration,
    cases: list[Case],
    mode: str,
    repeats: int,
    *,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mode not in {"scripted", "live"} or repeats < 1:
        raise ValueError("Select scripted/live mode and at least one repeat")
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("Cases must be nonempty with unique IDs")
    if mode == "live" and not os.environ.get("OPENAI_API_KEY"):
        raise ValueError(
            "Live mode requires OPENAI_API_KEY; use scripted mode to check the harness"
        )
    config_data = config.model_dump()
    artifact = {
        "scoring_version": SCORING_VERSION,
        "mode": mode,
        "evidence": "harness_only" if mode == "scripted" else "live_model_trials",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sdk_version": __version__,
        "python_version": platform.python_version(),
        "config": config_data,
        "config_sha256": fingerprint(config_data),
        "catalog_sha256": fingerprint(CATALOG),
        "catalog": CATALOG,
        "cases": [case.model_dump() for case in cases],
        "cases_sha256": fingerprint([case.model_dump() for case in cases]),
        "repeats": repeats,
        "trials": [
            {"case_id": case.id, "repeat": repeat}
            for case in cases
            for repeat in range(1, repeats + 1)
        ],
    }
    if baseline is not None:
        validate_baseline(artifact, baseline)
    artifact["trials"] = [
        await trial(config, case, mode, repeat)
        for case in cases
        for repeat in range(1, repeats + 1)
    ]
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    folder = Path(__file__).parent
    parser.add_argument("--config", type=Path, default=folder / "config.json")
    parser.add_argument("--cases", type=Path, default=folder / "cases.json")
    parser.add_argument("--mode", choices=["scripted", "live"], default="scripted")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    config = Configuration.model_validate_json(args.config.read_text())
    cases = [Case.model_validate(case) for case in json.loads(args.cases.read_text())]
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    artifact = asyncio.run(evaluate(config, cases, args.mode, args.repeats, baseline=baseline))
    # Keep completed trial evidence even if comparison later rejects score data.
    args.output.write_text(json.dumps(artifact, indent=2) + "\n")
    if baseline is not None:
        artifact["comparison"] = compare(artifact, baseline)
        args.output.write_text(json.dumps(artifact, indent=2) + "\n")
    print(
        json.dumps(
            {
                "evidence": artifact["evidence"],
                "trials": len(artifact["trials"]),
                "passed": sum(row["passed"] for row in artifact["trials"]),
                "comparison": artifact.get("comparison"),
            }
        )
    )
    if any(not row["passed"] for row in artifact["trials"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
