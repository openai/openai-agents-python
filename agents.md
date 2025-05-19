# Agents.md – Bootstrapping OpenAI Agents SDK with `uv`

> **Note**  
> This root‑level guide complements `docs/agents.md`, which focuses on contributor workflow.  
> For API reference, see that doc. For environment setup & agent orchestration, stay here.

## 0 · Quick start

```bash
./setup.sh          # installs Python 3.11, uv, Agents SDK, pre‑commit hooks
source .venv/bin/activate
python run.py       # try the sample agent
```

## 1 · Purpose  
Provide a reproducible scaffold that enables Codex to build, run, and test multi‑agent
workflows using **OpenAI Agents SDK v0.0.15** with dependency management via **uv**.

## 2 · Prerequisites  

| Layer | Pin / Requirement | Notes |
|-------|-------------------|-------|
| **Python** | 3.11.x | Matches SDK classifiers |
| **OpenAI Agents SDK** | openai-agents==0.0.15 |
| **uv** | 0.1.36 | Fast, deterministic dependency resolver |
| **Node** | 20 LTS | Needed if Codex builds front‑end assets |
| **OpenAI API key** | OPENAI_API_KEY env var | Read by SDK on import |

[...]

*(content unchanged after section 2 – keep previous examples & workflows)*

## A · Pre‑commit

The project comes with a `.pre-commit-config.yaml` including:

* **ruff** – lint & format  
* **black** – opinionated formatter  
* Core hooks: large file detector, EOF fixer

Run once:

```bash
pre-commit run --all-files
```

or leave the Git hook to run automatically on commit.

