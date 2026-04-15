from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from examples.sandbox.healthcare_support.models import KnowledgeSnippet, ScenarioCase

EXAMPLE_ROOT = Path(__file__).resolve().parent
SCENARIOS_DIR = EXAMPLE_ROOT / "data" / "scenarios"
FIXTURES_DIR = EXAMPLE_ROOT / "data" / "fixtures"
POLICIES_DIR = EXAMPLE_ROOT / "policies"
ROOT_ENV_PATH = EXAMPLE_ROOT.parents[2] / ".env"
DEMO_ENV_PATH = EXAMPLE_ROOT / ".env"


def load_root_env() -> None:
    """Load environment defaults from the repository root and this demo folder."""
    for env_path in (ROOT_ENV_PATH, DEMO_ENV_PATH):
        if not env_path.exists():
            continue

        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def normalize_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def tokenize(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def normalize_date(value: str | None) -> str:
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return "".join(re.findall(r"\d+", value))


@dataclass
class PolicyDocument:
    document_id: str
    title: str
    text: str


@dataclass
class HealthcareSupportDataStore:
    scenarios: dict[str, ScenarioCase]
    patient_records: list[dict[str, Any]]
    eligibility_records: list[dict[str, Any]]
    referral_records: list[dict[str, Any]]
    policy_documents: list[PolicyDocument]

    @classmethod
    def load(cls) -> HealthcareSupportDataStore:
        scenarios = {
            path.stem: ScenarioCase.model_validate(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(SCENARIOS_DIR.glob("*.json"))
        }
        patient_records = json.loads(
            (FIXTURES_DIR / "patient_profiles.json").read_text(encoding="utf-8")
        )["records"]
        eligibility_records = json.loads(
            (FIXTURES_DIR / "insurance_eligibility.json").read_text(encoding="utf-8")
        )["records"]
        referral_records = json.loads(
            (FIXTURES_DIR / "referral_status.json").read_text(encoding="utf-8")
        )["records"]
        policy_documents = [
            PolicyDocument(
                document_id=path.stem,
                title=path.stem.replace("_", " ").title(),
                text=path.read_text(encoding="utf-8"),
            )
            for path in sorted(POLICIES_DIR.glob("*.md"))
        ]
        return cls(
            scenarios=scenarios,
            patient_records=patient_records,
            eligibility_records=eligibility_records,
            referral_records=referral_records,
            policy_documents=policy_documents,
        )

    def list_scenario_ids(self) -> list[str]:
        return sorted(self.scenarios)

    def get_scenario(self, scenario_id: str) -> ScenarioCase:
        try:
            return self.scenarios[scenario_id]
        except KeyError as exc:
            raise KeyError(f"Unknown scenario_id: {scenario_id}") from exc

    def search_policies(self, query: str, top_k: int = 4) -> list[KnowledgeSnippet]:
        query_terms = tokenize(query)
        if not query_terms:
            return []

        scored: list[KnowledgeSnippet] = []
        for document in self.policy_documents:
            matched_terms = sorted(query_terms & tokenize(document.text))
            if not matched_terms:
                continue
            score = round(len(matched_terms) / max(len(query_terms), 1), 4)
            snippet = " ".join(document.text.split())[:320]
            scored.append(
                KnowledgeSnippet(
                    document_id=document.document_id,
                    title=document.title,
                    chunk_id=f"{document.document_id}:0",
                    score=score,
                    snippet=snippet,
                    matched_terms=matched_terms,
                )
            )

        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:top_k]

    def lookup_patient(
        self,
        *,
        patient_id: str | None = None,
        phone: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        for record in self.patient_records:
            if patient_id and record.get("patient_id") == patient_id:
                return {"lookup_status": "matched", "record": record}
            if phone and record.get("phone") == phone:
                return {"lookup_status": "matched", "record": record}
            if name and normalize_text(record.get("name", "")) == normalize_text(name):
                return {"lookup_status": "matched", "record": record}
        return {"lookup_status": "not_found", "record": None}

    def lookup_eligibility(
        self,
        *,
        payer: str | None = None,
        member_id: str | None = None,
        dob: str | None = None,
    ) -> dict[str, Any]:
        payer_norm = normalize_text(payer or "")
        dob_norm = normalize_date(dob)
        fallback_match: dict[str, Any] | None = None

        for record in self.eligibility_records:
            if member_id and record.get("member_id") != member_id:
                continue
            if dob_norm and normalize_date(record.get("dob")) != dob_norm:
                continue
            if payer_norm:
                if normalize_text(record.get("payer", "")) == payer_norm:
                    return {"lookup_status": "matched", **record}
                continue
            if fallback_match is None:
                fallback_match = {"lookup_status": "matched", **record}

        if fallback_match is not None:
            return fallback_match

        return {
            "lookup_status": "not_found",
            "eligibility_status": "unknown",
            "notes": "No eligibility match. Ask for payer, member ID, and date of birth.",
        }

    def lookup_referral(
        self,
        *,
        referral_id: str | None = None,
        patient_id: str | None = None,
    ) -> dict[str, Any]:
        for record in self.referral_records:
            if referral_id and record.get("referral_id") == referral_id:
                return {"lookup_status": "matched", **record}
            if patient_id and record.get("patient_id") == patient_id:
                return {"lookup_status": "matched", **record}
        return {"lookup_status": "not_found", "status": "unknown"}
