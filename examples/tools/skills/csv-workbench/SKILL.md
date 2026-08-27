---
name: csv-workbench
description: Analyze CSV files in /mnt/data and return concise numeric summaries.
---

# CSV Workbench

Use this skill when the user asks for quick analysis of tabular data.

## Workflow

1. Inspect the CSV schema first (`head`, `python csv.DictReader`, or both).
2. Compute requested aggregates with a short Python script.
3. Return concise results with concrete numbers and units when available.

## Constraints

- Prefer Python stdlib for portability.
- If data is missing or malformed, state assumptions clearly.
- Keep the final answer short and actionable.
