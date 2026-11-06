from typing import Literal

from pydantic import BaseModel, Field


class FinancialMetric(BaseModel):
    source_file: str = Field(
        description="Workspace-relative source path under data/, such as data/10-k-mdna-overview.txt."
    )
    filing_section: Literal[
        "Part II, Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations",
        "Part II, Item 8. Financial Statements and Supplementary Data",
    ] = Field(description="Normalized 10-K filing section for the source document.")
    metric_name: str = Field(
        description="Metric label exactly as written in the source document or table."
    )
    fiscal_period: Literal["FY2025", "FY2024", "2025-12-31", "2024-12-31"] = Field(
        description="Annual period label for statement rows, or balance-sheet date for point-in-time rows."
    )
    value: float = Field(description="Numeric value from the source row.")
    unit: Literal["USD millions", "percent"] = Field(
        description="Unit for `value`; use USD millions for dollar amounts and percent for margins."
    )
    segment: str | None = Field(
        default=None,
        description="Reportable segment or geography when the row is segment-specific, otherwise null.",
    )


class FinancialMetricBatch(BaseModel):
    metrics: list[FinancialMetric] = Field(
        description="One row per metric-period pair extracted from each source document."
    )
