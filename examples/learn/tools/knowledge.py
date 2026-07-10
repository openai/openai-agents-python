"""Knowledge query tool - demonstrates simulating a knowledge base with mock data."""

from typing import Any

from agents import function_tool

_KB = {
    "openai-agents": "OpenAI official multi-agent SDK, supports tools/handoffs/guardrails/session/tracing. pip install openai-agents",
    "agent": "In openai-agents, Agent is the core object containing name/instructions/model/tools/handoffs/output_type etc.",
    "function_tool": "Python functions decorated with @function_tool are automatically converted to LLM-callable tools; docstring and type hints generate JSON schema.",
    "handoff": "Let one Agent pass control to another, often used for intent routing (triage agent + multiple specialists).",
    "guardrail": "input_guardrails check before user input enters Agent, output_guardrails check before Agent output returns.",
    "session": "openai-agents provides SQLiteSession (local file by default) and other abstractions to automatically maintain multi-turn chat history.",
    "runner": "Runner.run / run_sync / run_streamed are three entrypoints to execute an Agent (async/sync/streaming).",
    "tracing": "Trace is enabled by default, recording the span tree for each run, can connect to OpenAI platform / OTel / Console.",
}


@function_tool
def lookup(term: str) -> dict[str, Any]:
    """Query openai-agents related terms in the local mini knowledge base.

    Args:
        term: Term string, e.g., 'handoff' / 'guardrail' / 'session'.
    """
    term_l = term.strip().lower()
    if term_l in _KB:
        return {"term": term, "found": True, "definition": _KB[term_l]}
    # Fuzzy matching
    for k, v in _KB.items():
        if term_l in k or k in term_l:
            return {"term": term, "found": True, "matched": k, "definition": v}
    return {"term": term, "found": False, "definition": "Term not found in knowledge base"}


KNOWLEDGE_TOOLS = [lookup]
