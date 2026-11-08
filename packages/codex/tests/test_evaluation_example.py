"""The evaluation harness scores real callbacks and rejects incompatible baselines."""

import json
from pathlib import Path

import pytest

from examples.codex.evaluation.run import Case, Configuration, compare, evaluate, score

FOLDER = Path(__file__).parents[3] / "examples" / "codex" / "evaluation"


async def test_scripted_trials_record_real_tool_behavior_and_configuration(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = Configuration.model_validate_json((FOLDER / "config.json").read_text())
    cases = [
        Case.model_validate(value) for value in json.loads((FOLDER / "cases.json").read_text())
    ]
    artifact = await evaluate(config, cases, "scripted", 2)
    assert artifact["evidence"] == "harness_only"
    assert artifact["config"] == config.model_dump()
    assert len(artifact["trials"]) == 4
    assert [row["tool_calls"] for row in artifact["trials"]] == [["A"], ["A"], ["Z"], ["Z"]]
    assert all(row["passed"] for row in artifact["trials"])
    assert compare(artifact, artifact)["regressions"] == []


def test_wrong_answers_extra_calls_and_errors_fail_independently():
    case = Case(id="known", prompt="A", expected_answer="violet", expected_lookups=["A"])
    assert score("violet", ["A", "A"], case, None) == {
        "completed": True,
        "answer_exact": True,
        "tool_behavior": False,
    }
    assert not score("amber", ["A"], case, None)["answer_exact"]
    assert not any(score("violet", ["A"], case, "TimeoutError").values())


def test_baseline_reports_tool_regression_and_rejects_mixed_evidence():
    baseline = {
        "cases_sha256": "same",
        "catalog_sha256": "catalog",
        "repeats": 1,
        "mode": "live",
        "scoring_version": 1,
        "config_sha256": "old",
        "trials": [
            {
                "case_id": "A",
                "repeat": 1,
                "scores": {"answer_exact": True, "tool_behavior": True},
                "passed": True,
            }
        ],
    }
    current = {
        **baseline,
        "config_sha256": "new",
        "trials": [
            {
                "case_id": "A",
                "repeat": 1,
                "scores": {"answer_exact": True, "tool_behavior": False},
                "passed": False,
            }
        ],
    }
    assert compare(current, baseline) == {
        "baseline_config_sha256": "old",
        "regressions": [{"case_id": "A", "repeat": 1, "score": "tool_behavior"}],
        "passed_trial_delta": -1,
    }
    with pytest.raises(ValueError, match="mode"):
        compare({**current, "mode": "scripted"}, baseline)


async def test_live_mode_never_silently_falls_back_without_credentials(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = Configuration(model="example", instructions="Answer")
    case = Case(id="A", prompt="A", expected_answer="violet", expected_lookups=["A"])
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        await evaluate(config, [case], "live", 1)
    with pytest.raises(ValueError, match="unique"):
        await evaluate(config, [case, case], "scripted", 1)


@pytest.mark.parametrize("invalid", ["repeats", "identities", "duplicate", "missing-file"])
def test_cli_preflights_baseline_before_trials(monkeypatch, tmp_path, invalid):
    from examples.codex.evaluation import run

    cases = [
        Case.model_validate(value) for value in json.loads((FOLDER / "cases.json").read_text())
    ]
    baseline = {
        "cases_sha256": run.fingerprint([case.model_dump() for case in cases]),
        "catalog_sha256": run.fingerprint(run.CATALOG),
        "repeats": 1,
        "mode": "scripted",
        "scoring_version": run.SCORING_VERSION,
        "trials": [{"case_id": case.id, "repeat": 1} for case in cases],
    }
    if invalid == "repeats":
        baseline["repeats"] = 2
    elif invalid == "identities":
        baseline["trials"][0]["case_id"] = "unknown"
    elif invalid == "duplicate":
        baseline["trials"].append(baseline["trials"][0])
    path = tmp_path / "baseline.json"
    if invalid != "missing-file":
        path.write_text(json.dumps(baseline))
    output = tmp_path / "candidate.json"
    monkeypatch.setattr(
        "sys.argv", ["evaluation", "--baseline", str(path), "--output", str(output)]
    )

    async def unexpected_trial(*args):
        pytest.fail("Baseline was rejected after candidate work started")

    monkeypatch.setattr(run, "trial", unexpected_trial)
    with pytest.raises(FileNotFoundError if invalid == "missing-file" else ValueError):
        run.main()
    assert not output.exists()


def test_cli_retains_completed_candidate_when_comparison_fails(monkeypatch, tmp_path):
    from examples.codex.evaluation import run

    config = Configuration.model_validate_json((FOLDER / "config.json").read_text())
    cases = [
        Case.model_validate(value) for value in json.loads((FOLDER / "cases.json").read_text())
    ]
    baseline = {
        "cases_sha256": run.fingerprint([case.model_dump() for case in cases]),
        "catalog_sha256": run.fingerprint(run.CATALOG),
        "repeats": 1,
        "mode": "scripted",
        "scoring_version": run.SCORING_VERSION,
        "trials": [{"case_id": case.id, "repeat": 1, "scores": {}} for case in cases],
    }
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline))
    output = tmp_path / "candidate.json"
    monkeypatch.setattr(
        "sys.argv", ["evaluation", "--baseline", str(path), "--output", str(output)]
    )
    completed = []

    async def completed_trial(config, case, mode, repeat):
        completed.append(case.id)
        return {"case_id": case.id, "repeat": repeat, "scores": {"completed": True}, "passed": True}

    monkeypatch.setattr(run, "trial", completed_trial)
    with pytest.raises(KeyError, match="completed"):
        run.main()
    artifact = json.loads(output.read_text())
    assert completed == [case.id for case in cases]
    assert artifact["config"] == config.model_dump()
    assert len(artifact["trials"]) == len(cases)
    assert all(row["passed"] for row in artifact["trials"])
    assert "comparison" not in artifact
