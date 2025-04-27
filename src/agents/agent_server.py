# agents/agent_server.py

from __future__ import annotations
import os, sys, json, httpx
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# ── SDK setup ───────────────────────────────────────────────────────────────
load_dotenv()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents import Agent, Runner
from agents.extensions.handoff_prompt import prompt_with_handoff_instructions

# ── Specialist agents ──────────────────────────────────────────────────────
strategy  = Agent("StrategyAgent",  instructions="You create 7-day social strategies. Respond ONLY in structured JSON.")
content   = Agent("ContentAgent",   instructions="You write brand-aligned social posts. Respond ONLY in structured JSON.")
repurpose = Agent("RepurposeAgent", instructions="You repurpose content. Respond ONLY in structured JSON.")
feedback  = Agent("FeedbackAgent",  instructions="You critique content. Respond ONLY in structured JSON.")

AGENTS = {
    "strategy":  strategy,
    "content":   content,
    "repurpose": repurpose,
    "feedback":  feedback,
}

# ── Manager agent ──────────────────────────────────────────────────────────
MANAGER_TXT = """
You are the Manager. Look at the user's request and either:
  1) return JSON: { "handoff_to": "<one of: strategy, content, repurpose, feedback>",
                   "clarify":  "…optional follow-up question…",
                   "payload":  { /* any override or trimmed inputs */ } }
  2) or return plain-text if you need general clarification.
"""
manager = Agent(
    "Manager",
    instructions=prompt_with_handoff_instructions(MANAGER_TXT),
    handoffs=list(AGENTS.values()),
)

# ── Mappings ────────────────────────────────────────────────────────────────
ALL_AGENTS   = {"manager": manager, **AGENTS}
AGENT_TO_KEY = {agent: key for key, agent in ALL_AGENTS.items()}

# ── Helpers ────────────────────────────────────────────────────────────────
def build_payload(task_id, user_id, agent_type, message, reason, trace):
    return {
        "task_id":   task_id,
        "user_id":   user_id,
        "agent_type": agent_type,
        "message":    {"type": message.get("type"), "content": message.get("content")},
        "metadata":   {"reason": reason},
        "trace":      trace,
        "created_at": datetime.utcnow().isoformat(),
    }

def flatten_payload(p: dict) -> dict:
    """
    Flatten one level of nested dicts so Bubble sees:
      task_id, user_id, agent_type,
      message_type, message_content,
      metadata_reason, created_at
    """
    return {
        "task_id":         p["task_id"],
        "user_id":         p["user_id"],
        "agent_type":      p["agent_type"],
        "message_type":    p["message"]["type"],
        "message_content": p["message"]["content"],
        "metadata_reason": p["metadata"].get("reason", ""),
        "created_at":      p["created_at"],
    }

# ── FastAPI app ────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

@app.post("/agent")
async def run_agent(req: Request):
    data      = await req.json()
    incoming  = data.get("agent_type", "manager")
    agent     = ALL_AGENTS[incoming]

    # 1) Run whichever agent
    result = await Runner(...).run(agent, data["prompt"], …)
    raw    = result.final_output.strip()
    trace  = result.to_debug_dict()
    reason = result.metadata.get("reason", "")

    # Build a flat payload and send
    async def send_flat(key, msg, why):
        p = build_payload(data["task_id"], data["user_id"], key, {"type": "text", "content": msg}, why, trace)
        await send_webhook(flatten_payload(p))

    # 2) If it’s the manager, try to unpack a handoff envelope
    if incoming == "manager":
        try:
            env      = json.loads(raw)
            clarify  = env.get("clarify", "")
            target   = env["handoff_to"]
            payload  = env.get("payload", data)

            # (a) Manager’s “please clarify” or routing message
            await send_flat("manager", clarify, "handoff")

            # (b) Immediately call the specialist
            if target in AGENTS:
                spec_res = await Runner(...).run(AGENTS[target], payload["prompt"], …)
                spec_raw = spec_res.final_output.strip()
                spec_tr  = spec_res.to_debug_dict()
                spec_rs  = spec_res.metadata.get("reason", "")
                p2       = build_payload(data["task_id"], data["user_id"], target,
                                         {"type":"text","content": spec_raw},
                                         spec_rs, spec_tr)
                await send_webhook(flatten_payload(p2))

            return {"ok": True}

        except (json.JSONDecodeError, KeyError):
            # Not a JSON envelope → pure manager clarification
            await send_flat("manager", raw, reason)
            return {"ok": True}

    # 3) Else: a specialist’s direct run → one webhook only
    await send_flat(incoming, raw, reason)
    return {"ok": True}
