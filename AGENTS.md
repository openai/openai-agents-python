Welcome to the OpenAI Agents SDK repository. This file contains the main points for new contributors.

## Repository overview

- **Source code**: `src/agents/` contains the implementation.
- **Tests**: `tests/` with a short guide in `tests/README.md`.
- **Examples**: under `examples/`.
- **Documentation**: markdown pages live in `docs/` with `mkdocs.yml` controlling the site.
- **Utilities**: developer commands are defined in the `Makefile`.
- **PR template**: `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md` describes the information every PR must include.

## Local workflow

1. Format, lint and type‑check your changes:

   ```bash
   make format
   make lint
   make mypy
   ```

2. Run the tests:

   ```bash
   make tests
   ```

   To run a single test, use `uv run pytest -s -k <test_name>`.

3. Build the documentation (optional but recommended for docs changes):

   ```bash
   make build-docs
   ```

   Coverage can be generated with `make coverage`.

All python commands should be run via `uv run python ...`

## Snapshot tests

Some tests rely on inline snapshots. See `tests/README.md` for details on updating them:

```bash
make snapshots-fix      # update existing snapshots
make snapshots-create   # create new snapshots
```

Run `make tests` again after updating snapshots to ensure they pass.

## Style notes

- Write comments as full sentences and end them with a period.

## Pull request expectations

PRs should use the template located at `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md`. Provide a summary, test plan and issue number if applicable, then check that:

- New tests are added when needed.
- Documentation is updated.
- `make lint` and `make format` have been run.
- The full test suite passes.

Commit messages should be concise and written in the imperative mood. Small, focused commits are preferred.

## What reviewers look for

- Tests covering new behaviour.
- Consistent style: code formatted with `uv run ruff format`, imports sorted, and type hints passing `uv run mypy .`.
- Clear documentation for any public API changes.
- Clean history and a helpful PR description.

## PO Assistant (example) – Agents and MCP integration

This repository includes a reference example under `examples/po_assistant` that demonstrates:

- A FastAPI backend exposing endpoints to ingest POs, reconcile against Airtable, and build a preview commit plan.
- An agent-based summarizer using the model `gpt-5-2025-08-07` to produce human-readable commit previews.
- Optional Zapier MCP integration to call Airtable tools over the Streamable HTTP transport.

Key components:

- `examples/po_assistant/app.py`: FastAPI app with routes.
- `examples/po_assistant/routes.py`: Endpoints including `/po/sync`, `/po/plan`, `/po/plan/summary`, `/po/plan/summary/stream`, `/po/plan/summary/guarded`, `/mcp/tools`, `/airtable/schema`.
- `examples/po_assistant/agent_summary.py`: Summary agent using `gpt-5-2025-08-07` via `Runner.run`.
- `examples/po_assistant/mcp_zapier.py`: Helper to instantiate `MCPServerStreamableHttp` using `ZAPIER_MCP_URL` and `ZAPIER_MCP_KEY`.
- `examples/po_assistant/reconcile.py`: Airtable-backed reconciliation with fuzzy matching.
- `examples/po_assistant/commit_planner.py`: Preview-only plan builder (reserve vs backorder; no writes yet).

Environment variables:

- `OPENAI_API_KEY` – required to run agent summaries.
- `AIRTABLE_PAT`, `AIRTABLE_BASE_ID` – used for direct Airtable reads during sync/plan.
- `ZAPIER_MCP_URL`, `ZAPIER_MCP_KEY` – used to enable Zapier MCP endpoints and live Airtable tooling.
- `AIRTABLE_WRITES_ENABLED` – set to `true`/`1` to allow write scaffolding to attempt Airtable writes during commit; defaults to disabled.

Run commands (example):

```bash
export OPENAI_API_KEY=... \
       AIRTABLE_PAT=... \
       AIRTABLE_BASE_ID=appIQpYvYVDlVtAPS \
       ZAPIER_MCP_URL=https://mcp.zapier.com/api/mcp/mcp \
       ZAPIER_MCP_KEY=...
uv run python -m uvicorn examples.po_assistant.app:app --reload
```

Endpoints:

- `/po/sync`: PDF extract (stub) → Airtable reconciliation (companies/items).
- `/po/plan`: Computes a preview of reserve/backorder per line (no writes).
- `/po/plan/summary`: Agent-generated summary for the preview.
- `/po/plan/summary/stream`: Streamed events (NDJSON) from the summary agent for UI.
- `/po/plan/summary/guarded`: Summary with an output guardrail; returns violations on fail.
- `/mcp/tools`: Lists Zapier MCP tools.
- `POST /airtable/schema` – live schema via Zapier MCP.

Future work (tracked in TODOs):

- Implement safe, idempotent commit with inventory reservations and audit logs.
- Extend extraction with layout-aware PDF parsing and OCR fallback.

## PO Assistant runbook

### Daily dev workflow
- Pull latest `main`.
- `uv run python -m uvicorn examples.po_assistant.app:app --reload`.
- Export env: `OPENAI_API_KEY`, `AIRTABLE_PAT`, `AIRTABLE_BASE_ID`, `ZAPIER_MCP_URL`, `ZAPIER_MCP_KEY`.
- Smoke tests:
  - `GET /healthz`
  - `GET /mcp/tools`
  - `POST /airtable/schema?base_id=$AIRTABLE_BASE_ID`
  - `POST /po/sync` (empty body ok)
  - `POST /po/plan` and `/po/plan/summary`
  - `POST /po/plan/summary/stream` (observe `application/x-ndjson` lines)
  - `POST /po/plan/summary/guarded` (returns `{ ok, summary | violations }`)

### Branch & commit checklist
- Scope small, focused changes.
- Update docs if endpoints/env changed.
- Run `make format && make lint && make mypy`.
- Add/adjust tests if logic changed.
- Validate curl examples in README still work.

### Release readiness (internal)
- Preview-first flow validated (no unintended writes).
- MCP endpoints OK with valid `ZAPIER_MCP_KEY`.
- Error paths return helpful messages (missing envs, base not found).
- Idempotency keys logged in plan responses.

### Streaming traces & files

The streamed summary endpoint emits semantic events using the Agents SDK streaming model:

- `agent_updated_stream_event` when the active agent changes.
- `raw_response_event` for LLM deltas (we forward only text and reasoning deltas).
- `run_item_stream_event` for items such as messages and tool calls.

Lightweight trace logs are persisted under `logs/po_assistant_traces/{idempotency_key}.jsonl`.
Each line is a JSON object: `{ ts, type, payload }`. Likely secrets are redacted.

### Output guardrails

The summary agent is wrapped with an output guardrail that flags unsafe/invalid summaries and
returns structured violations via `/po/plan/summary/guarded` instead of raising.

## Quick reference: endpoints
- `GET /healthz` – service alive.
- `GET /schema/overview` – static overview.
- `POST /po/sync` – parse → reconcile (Airtable candidates).
- `POST /po/plan` – compute reserve/backorder (no writes).
- `POST /po/plan/summary` – agent summary (`gpt-5-2025-08-07`).
- `GET /tasks.json` – JSON variant of tasks list for frontend usage.
- `GET /mcp/tools` – list Zapier MCP tools.
- `POST /airtable/schema` – live schema via Zapier MCP.

## Troubleshooting
- 403 from Airtable: ensure `AIRTABLE_PAT` scopes and base access.
- MCP tool error `Invalid arguments`: include `instructions` and exact param names (e.g., `baseId`).
- Empty candidates: confirm `AIRTABLE_BASE_ID` and data exists in `Clients` / `Product Options`.
