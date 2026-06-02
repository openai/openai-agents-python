from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING or __package__:
    from .schemas import FinancialMetric, FinancialMetricBatch
else:
    from schemas import FinancialMetric, FinancialMetricBatch

MetricKey: TypeAlias = tuple[str, str, str, str | None]

EXPECTED_SOURCE_METADATA: dict[str, str] = {
    "data/10-k-mdna-overview.txt": (
        "Part II, Item 7. Management's Discussion and Analysis of Financial Condition and "
        "Results of Operations"
    ),
    "data/10-k-mdna-liquidity.txt": (
        "Part II, Item 7. Management's Discussion and Analysis of Financial Condition and "
        "Results of Operations"
    ),
    "data/10-k-note-segments.txt": ("Part II, Item 8. Financial Statements and Supplementary Data"),
    "data/10-k-note-geography.txt": (
        "Part II, Item 8. Financial Statements and Supplementary Data"
    ),
    "data/10-k-note-balance-sheet.txt": (
        "Part II, Item 8. Financial Statements and Supplementary Data"
    ),
    "data/10-k-statements-of-operations.pdf": (
        "Part II, Item 8. Financial Statements and Supplementary Data"
    ),
    "data/10-k-balance-sheets.pdf": (
        "Part II, Item 8. Financial Statements and Supplementary Data"
    ),
    "data/10-k-statements-of-cash-flows.pdf": (
        "Part II, Item 8. Financial Statements and Supplementary Data"
    ),
}

EXPECTED_ROWS: dict[MetricKey, tuple[float, str]] = {
    ("data/10-k-mdna-overview.txt", "Revenue", "FY2025", None): (1284.0, "USD millions"),
    ("data/10-k-mdna-overview.txt", "Revenue", "FY2024", None): (1008.0, "USD millions"),
    ("data/10-k-mdna-overview.txt", "Gross margin", "FY2025", None): (71.4, "percent"),
    ("data/10-k-mdna-overview.txt", "Gross margin", "FY2024", None): (68.2, "percent"),
    ("data/10-k-mdna-overview.txt", "Operating income", "FY2025", None): (186.0, "USD millions"),
    ("data/10-k-mdna-overview.txt", "Operating income", "FY2024", None): (118.0, "USD millions"),
    (
        "data/10-k-mdna-liquidity.txt",
        "Net cash provided by operating activities",
        "FY2025",
        None,
    ): (248.0, "USD millions"),
    (
        "data/10-k-mdna-liquidity.txt",
        "Net cash provided by operating activities",
        "FY2024",
        None,
    ): (192.0, "USD millions"),
    ("data/10-k-mdna-liquidity.txt", "Capital expenditures", "FY2025", None): (
        86.0,
        "USD millions",
    ),
    ("data/10-k-mdna-liquidity.txt", "Capital expenditures", "FY2024", None): (
        73.0,
        "USD millions",
    ),
    ("data/10-k-mdna-liquidity.txt", "Free cash flow", "FY2025", None): (
        162.0,
        "USD millions",
    ),
    ("data/10-k-mdna-liquidity.txt", "Free cash flow", "FY2024", None): (
        119.0,
        "USD millions",
    ),
    ("data/10-k-note-segments.txt", "Platform segment revenue", "FY2025", "Platform"): (
        942.0,
        "USD millions",
    ),
    ("data/10-k-note-segments.txt", "Platform segment revenue", "FY2024", "Platform"): (
        711.0,
        "USD millions",
    ),
    ("data/10-k-note-segments.txt", "Services segment revenue", "FY2025", "Services"): (
        342.0,
        "USD millions",
    ),
    ("data/10-k-note-segments.txt", "Services segment revenue", "FY2024", "Services"): (
        297.0,
        "USD millions",
    ),
    ("data/10-k-note-geography.txt", "Americas revenue", "FY2025", "Americas"): (
        764.0,
        "USD millions",
    ),
    ("data/10-k-note-geography.txt", "EMEA revenue", "FY2025", "EMEA"): (
        343.0,
        "USD millions",
    ),
    ("data/10-k-note-geography.txt", "APAC revenue", "FY2025", "APAC"): (
        177.0,
        "USD millions",
    ),
    (
        "data/10-k-note-balance-sheet.txt",
        "Cash and cash equivalents",
        "2025-12-31",
        None,
    ): (422.0, "USD millions"),
    (
        "data/10-k-note-balance-sheet.txt",
        "Cash and cash equivalents",
        "2024-12-31",
        None,
    ): (351.0, "USD millions"),
    ("data/10-k-note-balance-sheet.txt", "Deferred revenue", "2025-12-31", None): (
        402.0,
        "USD millions",
    ),
    ("data/10-k-note-balance-sheet.txt", "Deferred revenue", "2024-12-31", None): (
        337.0,
        "USD millions",
    ),
    ("data/10-k-statements-of-operations.pdf", "Net revenue", "FY2025", None): (
        1284.0,
        "USD millions",
    ),
    ("data/10-k-statements-of-operations.pdf", "Net revenue", "FY2024", None): (
        1008.0,
        "USD millions",
    ),
    ("data/10-k-statements-of-operations.pdf", "Gross profit", "FY2025", None): (
        917.0,
        "USD millions",
    ),
    ("data/10-k-statements-of-operations.pdf", "Gross profit", "FY2024", None): (
        687.0,
        "USD millions",
    ),
    ("data/10-k-statements-of-operations.pdf", "Operating income", "FY2025", None): (
        186.0,
        "USD millions",
    ),
    ("data/10-k-statements-of-operations.pdf", "Operating income", "FY2024", None): (
        118.0,
        "USD millions",
    ),
    (
        "data/10-k-balance-sheets.pdf",
        "Cash and cash equivalents",
        "2025-12-31",
        None,
    ): (422.0, "USD millions"),
    (
        "data/10-k-balance-sheets.pdf",
        "Cash and cash equivalents",
        "2024-12-31",
        None,
    ): (351.0, "USD millions"),
    ("data/10-k-balance-sheets.pdf", "Accounts receivable", "2025-12-31", None): (
        211.0,
        "USD millions",
    ),
    ("data/10-k-balance-sheets.pdf", "Accounts receivable", "2024-12-31", None): (
        187.0,
        "USD millions",
    ),
    ("data/10-k-balance-sheets.pdf", "Deferred revenue", "2025-12-31", None): (
        402.0,
        "USD millions",
    ),
    ("data/10-k-balance-sheets.pdf", "Deferred revenue", "2024-12-31", None): (
        337.0,
        "USD millions",
    ),
    (
        "data/10-k-statements-of-cash-flows.pdf",
        "Net cash provided by operating activities",
        "FY2025",
        None,
    ): (248.0, "USD millions"),
    (
        "data/10-k-statements-of-cash-flows.pdf",
        "Net cash provided by operating activities",
        "FY2024",
        None,
    ): (192.0, "USD millions"),
    ("data/10-k-statements-of-cash-flows.pdf", "Capital expenditures", "FY2025", None): (
        86.0,
        "USD millions",
    ),
    ("data/10-k-statements-of-cash-flows.pdf", "Capital expenditures", "FY2024", None): (
        73.0,
        "USD millions",
    ),
    ("data/10-k-statements-of-cash-flows.pdf", "Free cash flow", "FY2025", None): (
        162.0,
        "USD millions",
    ),
    ("data/10-k-statements-of-cash-flows.pdf", "Free cash flow", "FY2024", None): (
        119.0,
        "USD millions",
    ),
}


@dataclass(frozen=True)
class EvalSummary:
    row_count: int


def load_metrics(artifact_path: Path) -> FinancialMetricBatch:
    if artifact_path.suffix == ".jsonl":
        metrics = [
            FinancialMetric.model_validate_json(line)
            for line in artifact_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return FinancialMetricBatch(metrics=metrics)

    if artifact_path.suffix == ".csv":
        with artifact_path.open(encoding="utf-8", newline="") as input_file:
            reader = csv.DictReader(input_file)
            metrics = []
            for row in reader:
                row["segment"] = row["segment"] or None
                row["value"] = float(row["value"])
                metrics.append(FinancialMetric.model_validate(row))
        return FinancialMetricBatch(metrics=metrics)

    raise ValueError(f"Unsupported artifact type: {artifact_path}")


def validate_outputs(metrics: FinancialMetricBatch) -> EvalSummary:
    rows = metrics.metrics
    duplicate_keys: list[MetricKey] = []
    seen_keys: set[MetricKey] = set()
    rows_by_key: dict[MetricKey, FinancialMetric] = {
        (
            row.source_file.strip(),
            row.metric_name.strip(),
            row.fiscal_period,
            row.segment.strip() if row.segment else None,
        ): row
        for row in rows
    }

    for row in rows:
        row_key = (
            row.source_file.strip(),
            row.metric_name.strip(),
            row.fiscal_period,
            row.segment.strip() if row.segment else None,
        )
        if row_key in seen_keys:
            duplicate_keys.append(row_key)
        seen_keys.add(row_key)

    if duplicate_keys:
        raise AssertionError(f"Duplicate metric rows found: {sorted(set(duplicate_keys))}.")

    if len(rows) != len(EXPECTED_ROWS):
        raise AssertionError(
            f"Expected exactly {len(EXPECTED_ROWS)} metric rows, found {len(rows)}."
        )

    for source_file, expected_section in EXPECTED_SOURCE_METADATA.items():
        source_rows = [row for row in rows if row.source_file.strip() == source_file]
        if not source_rows:
            raise AssertionError(f"Missing rows from {source_file}.")
        bad_sections = {
            row.filing_section for row in source_rows if row.filing_section != expected_section
        }
        if bad_sections:
            raise AssertionError(
                f"{source_file} filing_section mismatch. Expected {expected_section}, found {bad_sections}."
            )

    missing_rows = [
        key
        for key, (expected_value, expected_unit) in EXPECTED_ROWS.items()
        if key not in rows_by_key
        or rows_by_key[key].value != expected_value
        or rows_by_key[key].unit != expected_unit
    ]
    if missing_rows:
        observed = sorted(rows_by_key)
        raise AssertionError(
            f"Missing or mismatched expected metric rows: {missing_rows}. Observed keys: {observed}."
        )

    unexpected_rows = sorted(set(rows_by_key) - set(EXPECTED_ROWS))
    if unexpected_rows:
        raise AssertionError(f"Unexpected metric rows found: {unexpected_rows}.")

    return EvalSummary(row_count=len(rows))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact-path",
        default=str(Path(__file__).resolve().parent / "output" / "financial_metrics.jsonl"),
        help="Path to the generated JSONL or CSV artifact.",
    )
    args = parser.parse_args()

    summary = validate_outputs(load_metrics(Path(args.artifact_path)))
    print(f"Eval checks passed for {summary.row_count} metric row(s).")
