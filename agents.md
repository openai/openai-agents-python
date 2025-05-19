# agents.md – OpenAI Agents SDK Bootstrap Guide

Welcome! This guide explains **how to initialise and integrate the OpenAI Agents SDK ≥ 0.0.15** into your Codex project fork.  
It assumes you have already cloned the repository and run `setup.sh`.

---

## 1 Prerequisites

| Tool | Locked Version | Notes |
|------|----------------|-------|
| Python | 3.12.10 | Installed via `pyenv` |
| Node JS | 22.15.1 | Required for tooling / CI |
| OpenAI Agents SDK | 0.0.15 | Installed into `.venv` |
| uv | 0.7.5 | Ultra‑fast resolver used by `setup.sh` |

In addition, ensure *git*, *curl* and *GNU Make* are on your `PATH`.

---

## 2 Repository Layout (after bootstrap)

    .
    ├── .github/            ← CI workflows
    ├── agents/             ← Your custom agents live here
    ├── src/                ← Application code
    ├── tests/              ← Pytest suite
    ├── setup.sh            ← Environment bootstrap (run once)
    ├── requirements.lock   ← Pinned dependency hashes (uv‑generated)
    └── agents.md           ← **You are here**

---

## 3 First‑time Setup (quick recap)

1.   `./setup.sh` – installs Python 3.12.10 with *pyenv*, creates `.venv`, installs uv, OpenAI Agents SDK 0.0.15, pre‑commit hooks, and Node 22.15.1.  
2.   `source .venv/bin/activate` – activate the virtual‑env.  
3.   `make verify` – run formatters, linters, type‑check and tests.

---

## 4 Scaffolding a New Agent

1. Generate a skeleton:

        python -m agents.cli new --name "MyToolAgent" --task "Summarise pull‑requests"

   This creates:

        agents/my_tool_agent/
        ├── __init__.py
        ├── schema.py          ← Pydantic schemas for tool I/O
        ├── tools.py           ← Tool implementations
        ├── plan.yaml          ← High‑level chain‑of‑thought recipe
        └── tests/

2. Implement business logic in `tools.py`.  
3. Describe the task graph in `plan.yaml`.  
4. Add unit tests in `tests/`.  
5. Register the agent in `src/agents_registry.py`:

        from agents.my_tool_agent import MyToolAgent
        AGENT_REGISTRY.register(MyToolAgent)

---

## 5 Local Invocation

Activate the environment and run:

        agents run my-tool-agent --input /path/to/file.md

Logs stream in **structured JSON**; pretty‑printing is provided by `jq` or the bundled `scripts/format‑logs.py`.

---

## 6 CI / CD Workflow Skeleton

Workflow file: `.github/workflows/agents.yml`

    name: agents
    on:
      pull_request:
        paths: ['agents/**', 'src/**', 'tests/**']
    jobs:
      test:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - uses: actions/setup-python@v5
            with:
              python-version: '3.12'
          - run: pip install uv==0.7.5 && uv pip install --system -r requirements.lock
          - run: make verify

A separate `deploy.yml` promotes passing revisions to your chosen runtime (e.g. Netlify Functions or AWS Lambda).

---

## 7 Security & Compliance Guidelines

* **Never** store API keys in git; use GitHub Actions secrets or `.env`.  
* Tools must validate and sanitise all external input before execution.  
* Follow the principle of least privilege when invoking network or file‑system resources.  
* All code **must** pass `ruff`, `mypy` and `pytest -q`.

---

## 8 Contributor Workflow

1.  Fork → create feature branch.  
2.  `make dev` – watch mode with hot‑reloading.  
3.  Ensure `make verify` passes before committing.  
4.  Push and open a PR; CI must be green.  
5.  At least **one reviewer** from `CODEOWNERS` approves & merges.

_Style guideline_: write comments as full sentences and end them with a period.

---

## 9 Next Steps

* Read the OpenAI Agents SDK docs.  
* Explore `examples/` shipped with the SDK.  
* Extend `setup.sh` to install GPU‑dependent libraries if required.  
* Optional: enable *OpenTelemetry* tracing via `export AGENTS_TRACE=1`.
