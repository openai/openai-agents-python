# agents/agent_server.py — single webhook with full trace
from __future__ import annotations
import os, sys, json, httpx
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ── SDK setup ───────────────────────────────────────────────────────────────
load_dotenv()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents import Agent, Runner

# helper to prepend handoff instructions
from agents.extensions.handoff_prompt import prompt_with_handoff_instructions

# ── common helpers ─────────────────────────────────────────────────────────
CHAT_URL = os.getenv("BUBBLE_CHAT_URL")          # one endpoint is enough
_now     = lambda: datetime.utcnow().isoformat()

async def send_webhook(payload: dict):
    async with httpx.AsyncClient() as c:
        print("=== Webhook Dispatch ===\n", json.dumps(payload, indent=2))
        await c.post(CHAT_URL, json=payload)     # always same URL
        print("========================")

def build_payload(task_id, user_id, agent_type, message, reason, trace):
    return {
        "task_id":   task_id,
        "user_id":   user_id,
        "agent_type": agent_type,
        "message":    message,                   # text | structured JSON
        "metadata":  {"reason": reason},
        "trace":      trace,                     # full execution chain
        "created_at": _now(),
    }

# ── specialist agents (instructions unchanged) ────────────────────────────
strategy  = Agent("StrategyAgent",  instructions="You create 7-day social strategies. Respond ONLY in structured JSON.")
content   = Agent("ContentAgent",   instructions="You write brand-aligned social posts. Respond ONLY in structured JSON.")
repurpose = Agent("RepurposeAgent", instructions="You repurpose content. Respond ONLY in structured JSON.")
feedback  = Agent("FeedbackAgent",  instructions="You critique content. Respond ONLY in structured JSON.")

AGENTS = { "strategy": strategy, "content": content, "repurpose": repurpose, "feedback": feedback }

# ── Manager with native handoffs ───────────────────────────────────────────
MANAGER_TXT = """
You are an intelligent router for user requests.
If you need clarification, ask a question (requires_user_input).
Otherwise delegate via a handoff to the correct agent.
When delegating, emit: {"handoff":"<agent_key>","reason":"..."} using one of: strategy, content, repurpose, feedback.
Never output the final plan yourself.
Never wrap JSON in code fences.
"""
manager = Agent("Manager",
                instructions=prompt_with_handoff_instructions(MANAGER_TXT),
                handoffs=list(AGENTS.values()))

# ── FastAPI boilerplate ────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

# ── main endpoint ─────────────────────────────────────────────────────────
@app.post("/agent")
async def run_agent(req: Request):
    data   = await req.json()
    action = data.get("action")
    if action not in ("new_task", "new_message"):
        raise HTTPException(400, "Unknown action")

    task_id, user_id = data["task_id"], data["user_id"]
    text_in          = data.get("user_prompt") or data.get("message")
    if not text_in:
        raise HTTPException(422, "Missing user_prompt or message")

    # decide which agent continues this thread
    session_key = data.get("agent_session_id") or "manager"
    current     = manager if session_key == "manager" else AGENTS.get(session_key, manager)

    # let Runner drive full loop (handoffs + tools) until completion / clarification
    result = await Runner.run(current, input=text_in, max_turns=12)

    # message to Bubble
    if getattr(result, "requires_user_input", None):
        msg   = {"type":"text","content": result.requires_user_input}
        cause = "Agent requested clarification"
    else:
        # try parse structured JSON; fallback to plain text
        try:
            parsed = json.loads(result.final_output)
            if "output_type" in parsed:
                msg, cause = parsed, "Agent returned structured output"
            else:
                raise ValueError
        except Exception:
            msg   = {"type":"text","content": result.final_output.strip()}
            cause = "Agent returned unstructured output"

    # build trace (list of dicts with role, content, tool_calls, etc.)
    try:
        trace = result.to_debug_dict()  # new SDK helper
    except Exception:
        trace = []

    payload = build_payload(task_id, user_id,
                            result.agent_type or "manager",
                            msg, cause, trace)
    await send_webhook(payload)
    return {"ok": True}
