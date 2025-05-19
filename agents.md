# Agents.md – Bootstrapping OpenAI Agents SDK in Codex

## 1 · Purpose  
Establish a reproducible scaffold that enables Codex to build, run, and test multi‑agent workflows using **OpenAI Agents SDK v0.0.15** in your forked repository. The guide respects project constraints: outbound network only in `setup.sh`, pinned versions, GitHub Actions CI/CD, Netlify deploy, and a trunk‑based merge queue.

## 2 · Prerequisites  

| Layer | Pin / Requirement | Notes |
|-------|-------------------|-------|
| **Python** | 3.11.x | Matches SDK classifiers |
| **OpenAI Agents SDK** | `openai-agents==0.0.15` (PyPI 15 May 2025) |
| **Node** | 20 LTS | Needed only if Codex builds front‑end assets |
| **OpenAI API key** | `OPENAI_API_KEY` env var | Read by SDK on import |
| **Git** | trunk + short‑lived feature branches | Auto‑merge via queue |

*Why venv?* Isolates SDK dependencies and avoids system‑wide conflicts.

## 3 · Directory map (created by `setup.sh`)

/
├── .github/
│   └── workflows/
│       ├── test.yml
│       ├── build.yml
│       └── deploy.yml
├── agents/
│   ├── __init__.py
│   ├── base_agent.py
│   └── handoffs.py
├── tools/
│   └── __init__.py
├── guardrails/
│   └── __init__.py
├── setup.sh
├── .env.example
└── README.md

## 4 · First agent example  

```python
from agents import Agent

hello_agent = Agent(
    name="HelloAgent",
    instructions="You are a concise, friendly helper."
)
```

`run.py`

```python
import asyncio
from agents import Runner
from agents.base_agent import hello_agent

async def main():
    result = await Runner.run(hello_agent, "Say hi in 8 words")
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

Run:

```bash
source .venv/bin/activate
python run.py
```

## 5 · Extending with Tools & Handoffs  

1. **Structured tool**

```python
from agents import function_tool

@function_tool
def get_weather(city: str) -> str:
    return f"The weather in {city} is sunny."
```

2. **Language router**

```python
spanish = Agent(name="ES", instructions="Reply only in Spanish.")
english = Agent(name="EN", instructions="Reply only in English.")

router = Agent(
    name="Router",
    instructions="Detect language and hand off accordingly.",
    handoffs=[spanish, english],
)
```

## 6 · Guardrails (optional)

Attach custom checks:

```python
guardrails=[validate_length, block_pii]
```

If any guardrail returns `False`, the loop aborts.

## 7 · Tracing & Observability  

*Default*: console.  
Add sinks (Logfire, AgentOps, etc.) via env vars:

| Variable | Example |
|----------|---------|
| `AGENTS_TRACING` | `logfire` |

`openai‑agents trace view` opens local UI.

## 8 · GitHub Actions workflow skeletons  

`.github/workflows/test.yml`

```yaml
name: Test
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: |
          python -m pip install -r requirements.txt
          pytest
```

`build.yml` → Netlify preview  
`deploy.yml` → Netlify production on merge to `main`

## 9 · Branch & merge‑queue strategy  

* **main** – always deployable  
* **feat/** – short‑lived feature branches  
* PR label `automerge` triggers merge queue; `rebase.yml` refreshes stale PRs.

## 10 · Environment variables  

| Var | Purpose |
|-----|---------|
| `OPENAI_API_KEY` | LLM calls & tracing |
| `AGENTS_TRACING` | `console` (default) or external sink |

```bash
cp .env.example .env
```
Populate locally; inject secrets via GitHub Settings → **Secrets & variables**.

## 11 · Next steps  

1. **Specialise agents** for HR automation, PDF extraction, or RAG.  
2. **Write integration tests** mocking tool output.  
3. **Add compliance guardrails** (PII redaction, toxicity filters).  
4. **Instrument tracing sinks**; tune loops for latency & cost.