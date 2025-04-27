# agents/agent_server.py — deterministic handoffs via SDK `handoff()`

from __future__ import annotations
import os
import sys
import json
import httpx
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── SDK setup ───────────────────────────────────────────────────────────────
load_dotenv()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents import Agent, Runner, handoff, RunContextWrapper
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
strategy  = Agent(
    name="strategy",
    instructions="You create 7-day social strategies. Respond ONLY in structured JSON."
)
content   = Agent(
    name="content",
    instructions="You write brand-aligned social posts. Respond ONLY in structured JSON."
)
repurpose = Agent(
    name="repurpose",
    instructions="You repurpose content. Respond ONLY in structured JSON."
)
feedback  = Agent(
    name="feedback",
    instructions="You critique content. Respond ONLY in structured JSON."
)

AGENTS = {"strategy": strategy, "content": content,
          "repurpose": repurpose, "feedback": feedback}

# ── Pydantic model for Manager handoff payload ────────────────────────────
class HandoffData(BaseModel):
    clarify: str
    prompt: str

# ── Manager agent ──────────────────────────────────────────────────────────
MANAGER_TXT = """
You are the Manager. When routing, you MUST call exactly one of these tools:
  • transfer_to_strategy
  • transfer_to_content
  • transfer_to_repurpose
  • transfer_to_feedback

Each call must pass a JSON object matching this schema (HandoffData):
{
  "clarify": "<optional follow-up question or empty string>",
  "prompt":  "<the text to send next>"
}

Do NOT output any other JSON or wrap in Markdown. The SDK will handle the rest.
"""

async def on_handoff(ctx: RunContextWrapper[HandoffData], input_data: HandoffData):
    # Send manager clarification webhook
    task_id = ctx.context['task_id']
    user_id = ctx.context['user_id']
    payload = build_payload(
        task_id=task_id,
        user_id=user_id,
        agent_type=(result.agent.name if hasattr(result, 'agent') and result.agent else 'manager'),
        message={'type':'text','content': input_data.clarify},
        reason='handoff',
        trace=ctx.usage.to_debug_dict() if hasattr(ctx.usage, 'to_debug_dict') else []
    )
    await send_webhook(flatten_payload(payload))

manager = Agent(
    name="manager",
    instructions=prompt_with_handoff_instructions(MANAGER_TXT),
    handoffs=[
        handoff(agent=strategy,  on_handoff=on_handoff, input_type=HandoffData),
        handoff(agent=content,   on_handoff=on_handoff, input_type=HandoffData),
        handoff(agent=repurpose, on_handoff=on_handoff, input_type=HandoffData),
        handoff(agent=feedback,  on_handoff=on_handoff, input_type=HandoffData),
    ]
)

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
    Flatten one level of nested message/metadata for Bubble.
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
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.post("/agent")
async def run_agent(req: Request):
    data    = await req.json()
    # normalize prompt
    prompt = (
        data.get("prompt") or data.get("user_prompt") or data.get("message")
    )
    if not prompt:
        raise HTTPException(422, "Missing 'prompt' field")

    # mandatory IDs
    task_id = data.get("task_id")
    user_id = data.get("user_id")
    if not task_id or not user_id:
        raise HTTPException(422, "Missing 'task_id' or 'user_id'")

    # 1) Always invoke the manager; pass context for on_handoff
    result = await Runner.run(
        manager,
        input=prompt,
        context={"task_id": task_id, "user_id": user_id},
        max_turns=12,
    )

    # 2) Final output comes from the last agent in the chain
    raw    = result.final_output.strip()
    try:
        json.loads(raw)
        reason = "Agent returned structured JSON"
    except json.JSONDecodeError:
        reason = "Agent returned unstructured output"
    trace = []
    if hasattr(result, 'to_debug_dict'):
        trace = result.to_debug_dict()

    # 3) Send the final specialist webhook
    out_payload = build_payload(
        task_id=task_id,
        user_id=user_id,
        agent_type = (
            result.agent.name
            if hasattr(result, "agent") and result.agent
            else "manager"
        ),
        message={"type":"text","content": raw},
        reason=reason,
        trace=trace
    )
    await send_webhook(flatten_payload(out_payload))

    return {"ok": True}
