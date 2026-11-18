# Write a financial report with source checks

Run this example to see how an agent plans research, consults financial and risk analysts, and checks a draft before showing it to the reader. You can switch the agents to stock Codex without changing the search or the evidence rules.

From the repository root, set `OPENAI_API_KEY` in your environment. **Both modes use hosted web search for research and for the final audit. These calls incur search and model charges.** For Codex mode, also install the stock `codex` CLI and make sure it's on your `PATH`.

```bash
# Default provider
uv run python -m examples.financial_research_agent.main

# Stock Codex for planning, analysis, writing, and draft verification
uv run python -m examples.financial_research_agent.main --codex
```

Enter the same question at either prompt, for example: "What drives Apple's long-term revenue, and what risks does Apple identify in its latest annual report?" Set `EXAMPLES_INTERACTIVE_MODE=auto` to try a supplied question without typing one.

## What counts as evidence

Some model replies contain links that look like citations. The example doesn't treat those links as evidence on their own. In both modes, the search agent uses the Responses API's `WebSearchTool`: the SDK can read citation annotations and the search result's source URLs from that response. Stock Codex doesn't expose the same source metadata, so both stages that need to read sources use hosted search, even with `--codex`.

A real link can still accompany a wrong number. A verifier checks the draft against the collected evidence. A separate auditor opens primary documents and compares the report's material claims with what those documents say. For example, a figure could match the summary but come from the wrong year in the original table. That discrepancy should send the draft back for revision, just like a missing source.

If a draft fails but the auditor has enough confirmed findings, the example uses its one revision to write a narrower report from those findings. It labels the narrower scope and checks that report again. If no search returns sources, or the revised report still fails either check, the example raises an error instead of presenting the report as verified. A passing audit is evidence you can inspect, not a guarantee: a model can miss a claim or misread a table. Check the original sources before making a decision.
