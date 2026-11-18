# Write a financial report with source checks

Run this example to see how an agent plans research, consults financial and risk analysts, and checks a draft before showing it to the reader. You can switch the agents to stock Codex without changing the search or the evidence rules.

From the repository root, set `OPENAI_API_KEY` in your environment. **Both modes use the Responses API for web search and incur search and model charges.** For Codex mode, also install the stock `codex` CLI and make sure it's on your `PATH`.

```bash
# Default provider
uv run python -m examples.financial_research_agent.main

# Stock Codex for planning, analysis, writing, and verification
uv run python -m examples.financial_research_agent.main --codex
```

Enter the same question at either prompt, for example: "What drives Apple's long-term revenue, and what risks does Apple identify in its latest annual report?" Set `EXAMPLES_INTERACTIVE_MODE=auto` to try a supplied question without typing one.

## What counts as evidence

Some model replies contain links that look like citations. The example doesn't treat those links as evidence on their own. In both modes, the search agent uses the Responses API's `WebSearchTool`: the SDK can read citation annotations and the search result's source URLs from that response. Stock Codex doesn't expose the same source metadata, so the `--codex` flag deliberately leaves **only search** on Responses.

The verifier checks the report against the collected evidence. If it finds an issue, the writer gets one revision. If no search returns sources, or if the revised report still fails, the example raises an error instead of printing the report. A pass means the verifier accepted the report against the retrieved material. Check the sources yourself before relying on its conclusions.
