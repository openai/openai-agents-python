# TBC Caldero

**TBC's agentic harness.** Fork de [openai/openai-agents-python](https://github.com/openai/openai-agents-python) (MIT), extended con capabilities, guardrails, hooks y sessions TBC.

## Principio rector

> **Thin at the core, fat at the edges.** — Garry Tan

El kernel upstream (`src/agents/`) no sabe de TBC. Todo lo TBC vive en `src/tbc_caldero/`.

## Estructura

```
src/tbc_caldero/
├── adapters/               ← bridges a sistemas externos (DW, Neo4j, Granola)
│   └── duckdb_vault.py     ← read-only client al TBC warehouse
├── capabilities/            ← 9 @function_tool primitivas atómicas
│   ├── pricing.py           ← 🔥 data real (full_price_sell_through)
│   ├── assortment.py        ← OTB + Berkhout (stub)
│   ├── sellout.py           ← 🔥 data real (sell_through_flash) + YoY (stub)
│   ├── finance.py           ← 🔥 data real (pl_query) + allocation + headcount (stub)
│   └── alignment.py         ← alignment_score (stub)
├── guardrails/              ← 3 InputGuardrail gates (fail-closed)
│   ├── sensitivity.py       ← P1-P6 confidentiality (unified)
│   ├── financial.py         ← financial content/sheet gate
│   └── mode.py              ← plan/execute/read-only mode enforcement
├── hooks/                   ← RunHooks subclasses
│   ├── observers.py         ← TBCObserver composite (10 event handlers)
│   └── enrichers.py         ← TBCContextEnricher composite (4 context injectors)
├── providers/               ← model factory functions
│   └── claude.py            ← claude_model("haiku"|"sonnet"|"opus") via LiteLLM
└── sessions/                ← Session backends
    └── tbc_memory.py        ← DuckDB-backed, Loop 19 WM persistence

spike/                       ← validation spikes (all passing)
```

## Spikes (status)

| # | Spike | Gates | Hito |
|---|---|---|---|
| 1 | hello | 5/5 | Imports + Agent instantiation + @function_tool |
| 2 | memory | 9/9 | TBCMemorySession persists across instances |
| 3 | wiring | 6/6 | Full 4 sub-tipos composed in one Agent |
| 4 | live | infra ✅ | Provider → Runner → Claude API (wallet blocked) |
| 5 | batch | 5/5 | 9 capabilities wired, all FunctionTool |
| 6 | adapter | 5/5 | DuckDB adapter + first REAL data through stack |

## Cómo correr

```bash
cd ~/Code/tbc-caldero
PYTHONPATH=src /opt/anaconda3/bin/python3 spike/6_caldero_duckdb_adapter.py
```

Necesita: Python 3.11+, `duckdb`, `openai`, `litellm`, `pydantic`. El DW está en `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Cowork JIC/data/tbc-warehouse.duckdb`.

## Relación con el vault

El Caldero es un **runtime separado** del vault (`Cowork JIC`). El vault contiene:
- Las capabilities originales (`scripts/*.py`) — el Caldero las portea como `@function_tool`
- Las rules (`.claude/rules/`) — el Caldero las implementa como Guardrails + RunHooks
- El DW (`data/tbc-warehouse.duckdb`) — el Caldero lo lee via `VaultDW` adapter

El Caldero **NO modifica** el vault. Flujo es vault → Caldero (read-only).

## Canonical refs

- Frame TBC: `docs/strategy/2026-04-12-tbc-operating-frame.md` sección 3a
- Decision brief: `staging/spikes/2026-04-15-goose-vs-openai-agents-decision.md`
- Hooks migration inventory: `staging/2026-04-15-hooks-migration-inventory.md`
- Temporal spike: `staging/spikes/2026-04-15-temporal-spike.py`
