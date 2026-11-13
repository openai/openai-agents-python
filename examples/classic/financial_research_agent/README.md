# Financial research agent

<a id="financial-research-agent-example"></a>

Complete [Classic setup](../../../CLASSIC.md), install `rich` in that environment with `python -m pip install rich`, and set `OPENAI_API_KEY`. Commands below use the repository root. The bot makes multiple model and web-search requests per query, with corresponding usage charges.

This example builds on the [research bot](../research_bot/README.md) by adding financial specialists, source evidence, and report verification with one possible revision.

The flow is:

1. **Planning**: A planner turns the request into search queries for financial analysis, such as earnings, filings, and industry news.
2. **Search**: A search agent uses `WebSearchTool` to gather summaries and source URLs for the queries.
3. **Specialist tools**: The writer can call fundamentals and risk analysts as tools and receive their summaries.
4. **Writing**: The writer combines the evidence and specialist summaries into a Markdown report, executive summary, and follow-up questions.
5. **Verification**: A verifier checks the report against the gathered evidence. If verification fails, the writer revises the report once and the verifier checks it again. A second failure raises an error instead of printing the report. Model-based verification does not establish financial correctness.

You can run the example with:

```bash
python -m examples.classic.financial_research_agent.main
```

and enter a query like:

```
Write up an analysis of Apple Inc.'s most recent quarter.
```

After a successful run, the terminal prints the report, follow-up questions, and verification result. The wording and search results vary. If evidence verification still fails after the revision, inspect the verification issues in the error before changing the prompts or source data.

<a id="starter-prompt"></a>

## Customize the workflow

[`manager.py`](manager.py) coordinates searches, specialist tools, writing, revision, and verification. [`agents/writer_agent.py`](agents/writer_agent.py) contains the writer and revision prompts; [`agents/verifier_agent.py`](agents/verifier_agent.py) defines the verification checks. Update those instructions when adapting the example to your data sources and report requirements.
