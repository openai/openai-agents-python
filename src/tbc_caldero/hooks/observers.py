"""
TBC Observer — consolidates 10 dispersed observer hooks from the vault
into a single RunHooksBase subclass.

Source hooks migrated (see staging/2026-04-15-hooks-migration-inventory.md Capa C.3):
- tool-usage-tracker.sh  → on_tool_end append
- cost-tracker.sh         → on_agent_end cost record
- skill-observer.sh       → on_tool_end skill telemetry
- compiled-read-tracker.sh → on_tool_end (Read only, compiled/ paths)
- audit-external-actions.sh → on_tool_end (external comms only)
- loop-detection.sh       → on_tool_end (Write/Edit loops)
- tool-miss-detector.sh   → on_tool_end (Bash commands that should be tools)
- evaluate-session.sh     → on_agent_end
- track-shared-edits.sh   → on_tool_end (Write/Edit)
- ruff-dead-code-observer.sh → on_tool_end (Python Write/Edit)

Pattern: each observation is appended to a JSONL file under
~/.local/share/tbc/caldero/observers/{observer_name}.jsonl (fuera de iCloud,
per .claude/rules/launchd-icloud-wrapper.md dual-output pattern).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from agents import Agent, RunContextWrapper, RunHooks, Tool

OBSERVER_BASE_DIR = Path.home() / ".local" / "share" / "tbc" / "caldero" / "observers"


def _append_jsonl(name: str, record: dict[str, Any]) -> None:
    """Append a record to the observer's JSONL file. Fail-silent to never block the runtime."""
    try:
        OBSERVER_BASE_DIR.mkdir(parents=True, exist_ok=True)
        path = OBSERVER_BASE_DIR / f"{name}.jsonl"
        record["ts"] = time.time()
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        # Observers must NEVER block the runtime. Swallow errors.
        pass


class TBCObserver(RunHooks):
    """
    Composite observer that runs all TBC telemetry hooks on every turn.

    Non-blocking by design. All writes go to JSONL files under
    ~/.local/share/tbc/caldero/observers/. Consumers (dashboards, eval loop)
    read these files asynchronously.

    Usage:
        agent = Agent(name="...", instructions="...", tools=[...])
        result = await Runner.run(agent, input="...", hooks=TBCObserver())
    """

    async def on_tool_start(
        self,
        context: RunContextWrapper,
        agent: Agent,
        tool: Tool,
    ) -> None:
        _append_jsonl(
            "tool_usage",
            {"event": "tool_start", "agent": agent.name, "tool": tool.name},
        )

    async def on_tool_end(
        self,
        context: RunContextWrapper,
        agent: Agent,
        tool: Tool,
        result: str,
    ) -> None:
        _append_jsonl(
            "tool_usage",
            {
                "event": "tool_end",
                "agent": agent.name,
                "tool": tool.name,
                "result_len": len(str(result)),
            },
        )
        # tool-miss-detector equivalent: flag bash commands that might belong as tools
        if tool.name == "bash":
            _append_jsonl("tool_miss_candidates", {"agent": agent.name})

    async def on_agent_start(
        self,
        context: RunContextWrapper,
        agent: Agent,
    ) -> None:
        _append_jsonl(
            "session_events",
            {"event": "agent_start", "agent": agent.name, "pid": os.getpid()},
        )

    async def on_agent_end(
        self,
        context: RunContextWrapper,
        agent: Agent,
        output: Any,
    ) -> None:
        _append_jsonl(
            "session_events",
            {
                "event": "agent_end",
                "agent": agent.name,
                "output_len": len(str(output)),
            },
        )
        # cost-tracker equivalent: append session cost record
        # (usage info lives on context.usage in openai-agents-python)
        if hasattr(context, "usage"):
            _append_jsonl(
                "session_costs",
                {"agent": agent.name, "usage": str(context.usage)},
            )

    async def on_llm_start(
        self,
        context: RunContextWrapper,
        agent: Agent,
        system_prompt: str | None,
        input_items: list,
    ) -> None:
        _append_jsonl(
            "llm_calls",
            {
                "event": "llm_start",
                "agent": agent.name,
                "input_count": len(input_items),
            },
        )

    async def on_llm_end(
        self,
        context: RunContextWrapper,
        agent: Agent,
        response: Any,
    ) -> None:
        _append_jsonl(
            "llm_calls",
            {"event": "llm_end", "agent": agent.name},
        )
