# MCP Sylex Search Example

This example connects an agent to [Sylex Search](https://sylex.ai) — a curated
catalog of 11,000+ AI tools, libraries, and SaaS products — via its public MCP
SSE endpoint.

The agent uses the Sylex Search tools to answer questions about AI products
without making web requests or calling external search APIs. All results come
from a deterministic full-text index, so responses are fast and reproducible.

## What it demonstrates

- Connecting to a **remote SSE MCP server** using `MCPServerSse`
- Writing agent **instructions** that guide tool selection across multiple MCP
  tools
- Running the agent against several research queries in sequence

## Sylex Search tools used

| Tool | Purpose |
|---|---|
| `search.discover` | Keyword search across the product catalog |
| `search.details` | Full details for a specific product |
| `search.compare` | Side-by-side comparison of multiple products |
| `search.alternatives` | Find similar products to a known one |

The server also exposes `search.categories`, `search.feedback`,
`manage.register`, `manage.claim`, `manage.update`, and `manage.list_mcp` — see
the [Sylex Search docs](https://sylex.ai) for the full tool reference.

## Prerequisites

- `OPENAI_API_KEY` set for the model calls.
- No Sylex API key required — the endpoint is public.

## Run it

```bash
uv run python examples/mcp/sylex_search_example/main.py
```

## Example output

```
Query: What are some good vector database options for a Python project?
======================================================================
Based on the Sylex catalog, here are strong vector database options for Python:

1. **Chroma** — Open-source embedding database built for LLM apps, with a
   native Python client and in-process or server mode.
2. **Qdrant** — High-performance vector search engine with a Python SDK,
   supports filtering and payload indexing.
3. **Weaviate** — Open-source vector database with GraphQL and REST APIs,
   strong Python integration.
...
```
