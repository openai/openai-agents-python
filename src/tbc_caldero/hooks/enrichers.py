"""
TBCContextEnricher — composite enricher that injects context at lifecycle points.

Consolidates 4 enricher hooks from the vault into a single RunHooks subclass
(see staging/2026-04-15-hooks-migration-inventory.md Capa C.2):

Source hooks migrated:
- session-context-loader.sh  → on_agent_start (memory/projects/status injection)
- pre-read-context-injector.py → on_tool_start (observations from DuckDB by file)
- tool-focus-injector.sh     → on_llm_start (tool usage hints)
- auto-compact-check.sh      → on_llm_start (context limit warning)

These are NON-blocking by design. They inject context into the agent's run
via log messages or by mutating context attributes — they never halt execution.
For blocking behavior, use Guardrails.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from agents import Agent, RunContextWrapper, RunHooks, Tool

ENRICHER_BASE_DIR = Path.home() / ".local" / "share" / "tbc" / "caldero" / "enrichers"

# Context window warning threshold (rough heuristic — 0.6 = warn at 60% full)
CONTEXT_WARN_THRESHOLD = 0.6


def _append_enricher_log(name: str, record: dict[str, Any]) -> None:
    """Append enricher event to telemetry. Fail-silent."""
    try:
        ENRICHER_BASE_DIR.mkdir(parents=True, exist_ok=True)
        path = ENRICHER_BASE_DIR / f"{name}.jsonl"
        record["ts"] = time.time()
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


def _load_session_context() -> str:
    """
    Build a minimal context snippet for agent_start injection.

    Mirrors what session-context-loader.sh does in the vault, but simplified:
    - current date
    - active project count (read from env var if set)
    - any known active focus

    Full WM integration (memory/, projects/, compiled/) comes in Ola 2 Step 4
    when we wire the vault path as a Caldero config param.
    """
    lines: list[str] = []
    lines.append(f"[Caldero session · {time.strftime('%Y-%m-%d %H:%M')}]")

    active_focus = os.environ.get("TBC_ACTIVE_FOCUS")
    if active_focus:
        lines.append(f"Active focus: {active_focus}")

    vault_path = os.environ.get("TBC_VAULT_PATH")
    if vault_path and Path(vault_path).exists():
        lines.append(f"Vault: {vault_path}")

    return "\n".join(lines)


class TBCContextEnricher(RunHooks):
    """
    Composite enricher that runs all TBC context-injection hooks.

    Non-blocking. All operations are either:
    - Append to telemetry JSONL (observability)
    - Return context strings that the runtime can append to the prompt
      (via additionalContext / system prompt extension — see on_agent_start)

    Usage:
        enricher = TBCContextEnricher(vault_path="/path/to/vault")
        result = await Runner.run(agent, input="...", hooks=enricher)
    """

    def __init__(self, vault_path: str | Path | None = None):
        self.vault_path = Path(vault_path).expanduser() if vault_path else None
        self._start_ts: float | None = None

    async def on_agent_start(
        self,
        context: RunContextWrapper,
        agent: Agent,
    ) -> None:
        """
        Inject TBC session context. Equivalent to session-context-loader.sh.

        Target effect: extend the system prompt with current date, active
        focus, vault path. The upstream Runner reads extra context from
        context.additional_context if set.
        """
        self._start_ts = time.time()
        ctx_snippet = _load_session_context()

        # Store in context for downstream use. The specific attr depends on
        # how openai-agents-python exposes additional context injection.
        # For now, log it — Phase 2 wires it into the actual prompt.
        _append_enricher_log(
            "session_context",
            {
                "event": "agent_start",
                "agent": agent.name,
                "context_chars": len(ctx_snippet),
                "snippet_preview": ctx_snippet[:200],
            },
        )

    async def on_tool_start(
        self,
        context: RunContextWrapper,
        agent: Agent,
        tool: Tool,
    ) -> None:
        """
        Observations recall for Read tool calls.
        Equivalent to pre-read-context-injector.py in the vault.

        Phase 1: log which files are being read.
        Phase 2: query observations DuckDB for prior context on the file.
        """
        if tool.name == "read_file" or tool.name == "Read":
            _append_enricher_log(
                "observations_recall",
                {
                    "event": "read_attempted",
                    "agent": agent.name,
                    "tool": tool.name,
                },
            )

    async def on_llm_start(
        self,
        context: RunContextWrapper,
        agent: Agent,
        system_prompt: str | None,
        input_items: list,
    ) -> None:
        """
        Context window warning.
        Equivalent to auto-compact-check.sh in the vault.
        """
        item_count = len(input_items)
        # Heuristic: if too many items, suggest compaction
        # (real impl would check actual token count)
        if item_count > 50:
            _append_enricher_log(
                "context_warnings",
                {
                    "event": "large_context",
                    "agent": agent.name,
                    "item_count": item_count,
                    "threshold": 50,
                    "suggestion": "consider compaction",
                },
            )

    async def on_agent_end(
        self,
        context: RunContextWrapper,
        agent: Agent,
        output: Any,
    ) -> None:
        """Telemetry only — record agent duration."""
        duration = time.time() - self._start_ts if self._start_ts else 0.0
        _append_enricher_log(
            "session_context",
            {"event": "agent_end", "agent": agent.name, "duration_s": round(duration, 2)},
        )
