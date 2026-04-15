"""
TBC Caldero — TBC's agentic harness, forked from openai/openai-agents-python.

Principle: thin at the core, fat at the edges (Garry Tan).
The kernel knows nothing about TBC. TBC logic lives in:
- capabilities/  — atomic Python functions as @function_tool
- guardrails/    — gates (blocking) for confidentiality, financial, mode
- hooks/         — enrichers + observers (non-blocking) for context + telemetry
- sessions/      — memory backends that close loops to the World Model

Canonical: docs/strategy/2026-04-12-tbc-operating-frame.md section 3a
Rule: .claude/rules/thin-harness-fat-skills.md
"""

__version__ = "0.0.1-spike"
