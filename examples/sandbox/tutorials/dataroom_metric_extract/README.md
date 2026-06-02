# Dataroom metric extract

## Goal

Extract financial metrics from a synthetic 10-K packet, write the resulting
table as CSV or JSONL, then validate the generated artifact with a deterministic
eval script.

The packet uses synthetic company data, but the source docs are formatted as
annual-report excerpts with 10-K `Part II, Item 7` MD&A sections and `Part II,
Item 8` financial statement sections.

## Why this is valuable

This demo shows a single-pass structured extraction pattern: a sandbox agent
reads messy filing documents and emits typed financial rows, then a separate
host-side eval script checks the artifact. The wrapper does not repair or
deduplicate model output after the fact; if the row set is wrong, `evals.py`
fails and you iterate on the prompt or fixture data instead.

## Setup

Run the fixture generator and then the Unix-local example from the repository
root. Set `OPENAI_API_KEY` in your shell environment before running the example.

```bash
uv run python examples/sandbox/tutorials/data/dataroom/setup.py
uv run python examples/sandbox/tutorials/dataroom_metric_extract/main.py --output-format csv
uv run python examples/sandbox/tutorials/dataroom_metric_extract/evals.py --artifact-path examples/sandbox/tutorials/dataroom_metric_extract/output/financial_metrics.csv
```

After the initial extraction, the demo keeps the sandbox session open for
Rich-rendered follow-up prompts before writing the final artifact. Pass
`--no-interactive` for a one-shot run.

To run extraction in Docker, build the shared tutorial image once and add `--docker`
to `main.py`:

```bash
docker build --tag sandbox-tutorials:latest examples/sandbox/tutorials
uv run python examples/sandbox/tutorials/dataroom_metric_extract/main.py --docker --output-format csv
uv run python examples/sandbox/tutorials/dataroom_metric_extract/evals.py --artifact-path examples/sandbox/tutorials/dataroom_metric_extract/output/financial_metrics.csv
```

## Expected artifacts

- `output/financial_metrics.csv`
- `output/financial_metrics.jsonl`

## Demo shape

- Inputs: the shared SEC fixture packet in `examples/sandbox/tutorials/data/dataroom/`.
- Runtime primitives: sandbox-local bash/file search plus typed agent outputs.
- Workflow: a fixed single-step pipeline where the sandbox extractor emits
  `FinancialMetricBatch`; no handoff is needed. `main.py` writes the selected
  artifact format, and `evals.py` validates that artifact in a separate step.
- Scratch space: the extractor may use `scratchpad/` for interim notes, but only
  the selected `output/financial_metrics.*` artifact is part of the final
  contract.
