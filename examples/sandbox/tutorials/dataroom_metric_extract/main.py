"""
Extract structured financial metrics from a synthetic 10-K dataroom and write a
JSONL or CSV artifact.
"""

import argparse
import asyncio
import csv
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING, Literal, cast

from openai.types.shared.reasoning import Reasoning
from pydantic import BaseModel

from agents import ModelSettings, Runner, RunResultStreaming, TResponseInputItem
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import File, LocalDir

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

if TYPE_CHECKING or __package__:
    from .schemas import FinancialMetric, FinancialMetricBatch
else:
    from schemas import FinancialMetric, FinancialMetricBatch

from examples.sandbox.tutorials.misc import (
    DEFAULT_SANDBOX_IMAGE,
    console,
    create_sandbox_client_and_session,
    load_env_defaults,
    print_event,
    run_interactive_loop,
)

DEMO_DIR = Path(__file__).resolve().parent
DATAROOM_DATA_DIR = DEMO_DIR.parent / "data" / "dataroom"
DEFAULT_QUESTION = (
    "Extract revenue, gross margin, operating income, cash flow, balance-sheet, segment, "
    "and geography metrics from the 10-K packet into one row per metric-period-source. "
    "For each table, include every explicit line item in the source, even when it is "
    "similar to a line item in another source."
)
AGENTS_MD = dedent(
    """\
    # AGENTS.md

    Extract structured financial metrics from the synthetic 10-K packet under `data/`.

    ## Output (one row per metric-value occurrence)

    Required fields: `source_file`, `filing_section`, `metric_name`, `fiscal_period`, `value`,
    `unit` (`USD millions` or `percent`).
    Optional field: `segment` (segment/geography if explicitly stated, else null).

    ## Rules

    - Review all `.txt` and `.pdf` under `data/` (these PDFs contain searchable text).
    - Use shell tools (`rg`, `sed`) for discovery/inspection; do not run Python from the sandbox shell.
    - Do not read `data/setup.py`.
    - Emit a separate row for each metric-period pair in each source file (do not dedupe across files).
    - For tables, include every explicit table line item in that source. For example, the
      statements-of-operations PDF has separate Net revenue, Gross profit, and Operating income rows.
    - Only extract explicit source line items / table rows. Do not invent rollups or “cleaned up” metrics.
    - Do not treat Gross profit and Gross margin as duplicates; they are distinct source metrics.
    - Preserve labels as written (e.g., `Revenue` vs `Net revenue`).

    ## Completeness checklist

    Before final output, verify the batch has exactly 41 rows from these source-level line items:

    - `data/10-k-mdna-overview.txt`: Revenue, Gross margin, and Operating income for FY2025 and FY2024.
    - `data/10-k-mdna-liquidity.txt`: Net cash provided by operating activities, Capital expenditures,
      and Free cash flow for FY2025 and FY2024.
    - `data/10-k-note-segments.txt`: Platform segment revenue and Services segment revenue for FY2025
      and FY2024, with the matching segment names.
    - `data/10-k-note-geography.txt`: Americas revenue, EMEA revenue, and APAC revenue for FY2025, with
      the matching geography names as segments.
    - `data/10-k-note-balance-sheet.txt`: Cash and cash equivalents and Deferred revenue for 2025-12-31
      and 2024-12-31.
    - `data/10-k-statements-of-operations.pdf`: Net revenue, Gross profit, and Operating income for
      FY2025 and FY2024.
    - `data/10-k-balance-sheets.pdf`: Cash and cash equivalents, Accounts receivable, and Deferred revenue
      for 2025-12-31 and 2024-12-31.
    - `data/10-k-statements-of-cash-flows.pdf`: Net cash provided by operating activities, Capital
      expenditures, and Free cash flow for FY2025 and FY2024.

    Return the structured rows directly in your final output.
    """
)


async def print_streamed_result(result: RunResultStreaming) -> BaseModel:
    async for event in result.stream_events():
        print_event(event)
    if result.final_output is None:
        raise RuntimeError("10-K Metric Extractor returned no structured metric output.")
    print_event(str(result.final_output).strip())
    return cast(BaseModel, result.final_output)


def write_jsonl(path: Path, metrics: Sequence[BaseModel]) -> None:
    path.write_text(
        "\n".join(metric.model_dump_json() for metric in metrics) + "\n",
        encoding="utf-8",
    )


def write_csv(path: Path, metrics: list[FinancialMetric]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=[
                "source_file",
                "filing_section",
                "metric_name",
                "fiscal_period",
                "value",
                "unit",
                "segment",
            ],
        )
        writer.writeheader()
        for metric in metrics:
            writer.writerow(json.loads(metric.model_dump_json()))


def write_final_artifact(
    output_dir: Path,
    output_format: Literal["jsonl", "csv"],
    metrics: list[FinancialMetric],
) -> Path:
    output_path = output_dir / f"financial_metrics.{output_format}"
    if output_format == "jsonl":
        write_jsonl(output_path, metrics)
    else:
        write_csv(output_path, metrics)
    return output_path


async def main(
    model: str,
    question: str,
    output_format: Literal["jsonl", "csv"],
    use_docker: bool,
    image: str,
    no_interactive: bool,
) -> None:
    if not (DATAROOM_DATA_DIR / "10-k-mdna-overview.txt").exists():
        raise SystemExit(
            "Run `uv run python examples/sandbox/tutorials/data/dataroom/setup.py` "
            "before starting this demo."
        )

    manifest = Manifest(
        entries={
            "AGENTS.md": File(content=AGENTS_MD.encode("utf-8")),
            "data": LocalDir(src=DATAROOM_DATA_DIR),
        }
    )
    agent = SandboxAgent(
        name="10-K Metric Extractor",
        model=model,
        instructions=AGENTS_MD,
        capabilities=[Shell()],
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="high"),
            tool_choice="required",
        ),
        output_type=FinancialMetricBatch,
    )

    client, sandbox = await create_sandbox_client_and_session(
        manifest=manifest,
        use_docker=use_docker,
        image=image,
    )
    try:
        async with sandbox:
            extracted_metrics: FinancialMetricBatch | None = None

            async def run_turn(
                conversation: list[TResponseInputItem],
            ) -> list[TResponseInputItem]:
                nonlocal extracted_metrics

                result = Runner.run_streamed(
                    agent,
                    conversation,
                    max_turns=25,
                    run_config=RunConfig(
                        sandbox=SandboxRunConfig(session=sandbox),
                        tracing_disabled=True,
                        workflow_name="Dataroom extraction example",
                    ),
                )
                extracted_metrics = cast(FinancialMetricBatch, await print_streamed_result(result))
                return result.to_input_list()

            conversation: list[TResponseInputItem] = [{"role": "user", "content": question}]
            conversation = await run_turn(conversation)
            await run_interactive_loop(
                conversation=conversation,
                no_interactive=no_interactive,
                run_turn=run_turn,
            )
    finally:
        await client.delete(sandbox)

    if extracted_metrics is None:
        raise RuntimeError("10-K Metric Extractor returned no structured metric output.")

    output_dir = DEMO_DIR / "output"
    output_dir.mkdir(exist_ok=True)
    artifact_path = write_final_artifact(output_dir, output_format, extracted_metrics.metrics)
    console.print(
        f"[green]Wrote {len(extracted_metrics.metrics)} metric row(s) to {artifact_path}[/green]"
    )


if __name__ == "__main__":
    load_env_defaults(DEMO_DIR / ".env")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        default="gpt-5.4-mini",
        help="Model name to use.",
    )
    parser.add_argument(
        "--question",
        default=DEFAULT_QUESTION,
        help="Prompt to send to the agent.",
    )
    parser.add_argument(
        "--output-format",
        choices=("jsonl", "csv"),
        default="csv",
        help="Artifact format.",
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Run this example in Docker instead of Unix-local.",
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_SANDBOX_IMAGE,
        help="Docker image to use when --docker is set.",
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Run the scripted turn and skip follow-up terminal input.",
    )
    args = parser.parse_args()

    asyncio.run(
        main(
            args.model,
            args.question,
            args.output_format,
            args.docker,
            args.image,
            args.no_interactive,
        )
    )
