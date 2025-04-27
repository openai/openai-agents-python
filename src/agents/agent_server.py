# agents/agent_server.py — single webhook with full trace

from __future__ import annotations
import os
import sys
import json
import httpx
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ── SDK setup ───────────────────────────────────────────────────────────────
load_dotenv()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents import Agent, Runner
from agents.extensions.handoff_prompt import prompt_with_handoff_instructions

# ── Environment variable for Bubble webhook URL
CHAT_URL = os.getenv("BUBBLE_CHAT_URL")

# ── send_webhook helper ─────────────────────────────────────────────────────
async def send_webhook(payload: dict):
    async with httpx.AsyncClient() as client:
        print("=== Webhook Dispatch ===\n", json.dumps(payload, indent=2))
        await client.post(CHAT_URL, json=payload)
        print("========================")

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
  1) return JSON: {
         "handoff_to": "<one of: strategy, content, repurpose, feedback>",
         "clarify":    "...optional follow-up question…",
         "payload":    { /* any override or trimmed inputs */ }
     }
  2) or return plain-text if you need general clarification.
"""
manager = Agent(
    "Manager",
    instructions=prompt_with_handoff_instructions(MANAGER_TXT),
    handoffs=list(AGENTS.values()),
)

# ── All agents map ───────────────────────────────────────────────────────────
ALL_AGENTS = {"manager": manager, **AGENTS}

# ── Payload builders ─────────────────────────────────────────────────────────
def build_payload(task_id, user_id, agent_type, message, reason, trace):
    return {
        "task_id":    task_id,
        "user_id":    user_id,
        "agent_type": agent_type,
        "message":    {"type": message.get("type"), "content": message.get("content")},
        "metadata":   {"reason": reason},
        "trace":      trace,
        "created_at": datetime.utcnow().isoformat(),
    }

def flatten_payload(p: dict) -> dict:
    """
    Take one level of nested message & metadata fields
    and promote to top-level keys for Bubble.
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/agent")
async def run_agent(req: Request):
    data     = await req.json()
    incoming = data.get("agent_type", "manager")
    agent    = ALL_AGENTS.get(incoming, manager)

    # ensure we have a prompt (accept new or legacy fields)
    prompt = (
        data.get("prompt")
        or data.get("user_prompt")
        or data.get("message")
    )
    if not prompt:
        raise HTTPException(422, "Missing 'prompt' field")

    # 1) run the selected agent
    result = await Runner.run(agent, input=prompt, max_turns=12)
    raw    = result.final_output.strip()
    trace  = result.to_debug_dict()
    reason = result.metadata.get("reason", "")

    async def send_flat(key: str, msg: str, why: str):
        payload = build_payload(
            data["task_id"],
            data["user_id"],
            key,
            {"type": "text", "content": msg},
            why,
            trace,
        )
        await send_webhook(flatten_payload(payload))

    # 2) Manager path: try JSON envelope
    if incoming == "manager":
        try:
            env     = json.loads(raw)
            clarify = env.get("clarify", "")
            target  = env["handoff_to"]
            payload = env.get("payload", data)

            # 2a) manager’s clarification or routing message
            await send_flat("manager", clarify, "handoff")

            # 2b) immediately run specialist
            if target in AGENTS:
                spec_prompt = payload.get("prompt", prompt)
                spec_res    = await Runner.run(AGENTS[target], input=spec_prompt, max_turns=12)
                spec_raw    = spec_res.final_output.strip()
                spec_trace  = spec_res.to_debug_dict()
                spec_reason = spec_res.metadata.get("reason", "")
                spec_payload = build_payload(
                    data["task_id"],
                    data["user_id"],
                    target,
                    {"type": "text", "content": spec_raw},
                    spec_reason,
                    spec_trace,
                )
                await send_webhook(flatten_payload(spec_payload))

            return {"ok": True}

        except (json.JSONDecodeError, KeyError):
            # pure manager clarification
            await send_flat("manager", raw, reason)
            return {"ok": True}

    # 3) Specialist path: single webhook
    await send_flat(incoming, raw, reason)
    return {"ok": True}
