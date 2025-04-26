# agents/agent_server.py  —  stable tool-call router
from __future__ import annotations
import os, sys, json, httpx
from datetime import datetime
from typing import Any
from types import SimpleNamespace

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ── OpenAI Agents SDK imports ────────────────────────────────────────────────
load_dotenv()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents import Agent, Runner   # <- your existing SDK

# ----------------------------------------------------------------------------
# Helper payload builders  (unchanged)
# ----------------------------------------------------------------------------
_now = lambda: datetime.utcnow().isoformat()

def build_clarification_payload(task_id, user_id, agent_type, text, reason):
    return {
        "task_id": task_id, "user_id": user_id, "agent_type": agent_type,
        "message": {"type": "text", "content": text},
        "metadata": {"reason": reason},
        "created_at": _now(),
    }

def build_structured_payload(task_id, user_id, agent_type, obj):
    return {
        "task_id": task_id, "user_id": user_id, "agent_type": agent_type,
        "message": obj,
        "created_at": _now(),
    }

async def dispatch(url: str, payload: dict):
    async with httpx.AsyncClient() as c:
        print("=== Webhook Dispatch ===\n", json.dumps(payload, indent=2))
        await c.post(url, json=payload)
        print("========================")

CHAT_URL   = os.getenv("BUBBLE_CHAT_URL")
STRUCT_URL = os.getenv("BUBBLE_STRUCTURED_URL")

# -------------------------------------------------------------------- tools --
class ToolDict(dict):
    """
    A dict (for OpenAI API) that ALSO has `.name` (for Runner.run).
    Works on every Agents SDK version.
    """
    def __init__(self, name: str, description: str):
        schema = {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        }
        super().__init__(name=name, description=description, parameters=schema)
        self.name = name        # <-- Runner looks for this

TOOLS = [
    ToolDict("route_to_strategy",  "Send task to StrategyAgent"),
    ToolDict("route_to_content",   "Send task to ContentAgent"),
    ToolDict("route_to_repurpose", "Send task to RepurposeAgent"),
    ToolDict("route_to_feedback",  "Send task to FeedbackAgent"),
]

manager_agent = Agent(
    name="Manager",
    instructions=(
        "You are an intelligent router for user requests.\n"
        "If you need more info ask a question (requires_user_input).\n"
        "Otherwise call exactly ONE of the route_to_* tools with a reason."
    ),
    tools=TOOLS,          # ← list contains ToolDict objects
)


strategy_agent  = Agent("StrategyAgent",  instructions="You create 7-day social media strategies. Respond ONLY in structured JSON.")
content_agent   = Agent("ContentAgent",   instructions="You write brand-aligned social posts. Respond ONLY in structured JSON.")
repurpose_agent = Agent("RepurposeAgent", instructions="You repurpose content across platforms. Respond ONLY in structured JSON.")
feedback_agent  = Agent("FeedbackAgent",  instructions="You critique content and suggest edits. Respond ONLY in structured JSON.")

AGENT_MAP = {
    "strategy":  strategy_agent,
    "content":   content_agent,
    "repurpose": repurpose_agent,
    "feedback":  feedback_agent,
}

# ----------------------------------------------------------------------------
# Common dispatcher
# ----------------------------------------------------------------------------
async def _dispatch(task_id, user_id, agent_key, result):
    if getattr(result, "requires_user_input", None):
        await dispatch(CHAT_URL, build_clarification_payload(
            task_id, user_id, agent_key, result.requires_user_input, "Agent requested clarification"))
        return
    try:
        parsed = json.loads(result.final_output)
        if "output_type" in parsed:
            await dispatch(STRUCT_URL, build_structured_payload(
                task_id, user_id, agent_key, parsed))
            return
    except Exception:
        pass
    await dispatch(CHAT_URL, build_clarification_payload(
        task_id, user_id, agent_key, result.final_output.strip(), "Agent returned unstructured output"))

# ----------------------------------------------------------------------------
# FastAPI
# ----------------------------------------------------------------------------
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

@app.post("/agent")
async def endpoint(req: Request):
    data = await req.json()
    action = data.get("action")
    if action not in ("new_task", "new_message"):
        raise HTTPException(400, "Unknown action")

    task_id = data["task_id"]; user_id = data["user_id"]
    user_text = data.get("user_prompt") or data.get("message")
    if not user_text: raise HTTPException(422, "Missing user_prompt or message")

    agent_key = "manager" if action == "new_task" else data.get("agent_session_id", "manager")
    agent_obj = manager_agent if agent_key == "manager" else AGENT_MAP.get(agent_key, manager_agent)

    result = await Runner.run(agent_obj, input=user_text)

    # Manager tool-call handling
    if agent_key == "manager" and result.tool_calls:
        call = result.tool_calls[0]
        route_to = call["name"].removeprefix("route_to_")
        reason   = call["arguments"]["reason"]

        await dispatch(CHAT_URL, build_clarification_payload(
            task_id, user_id, "manager", json.dumps({"route_to": route_to, "reason": reason}),
            "Manager routing decision"))

        downstream = AGENT_MAP.get(route_to)
        if not downstream:
            return {"ok": True}

        res2 = await Runner.run(downstream, input=user_text)
        await _dispatch(task_id, user_id, route_to, res2)
        return {"ok": True}

    # Manager asked clarification OR downstream flow
    await _dispatch(task_id, user_id, agent_key, result)
    return {"ok": True}
