# agents/agent_server.py — handoff-based, dual-webhook

from __future__ import annotations
import os, sys, json, httpx
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# SDK
load_dotenv()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents import Agent, Runner
from agents.extensions.handoff_prompt import prompt_with_handoff_instructions

# ── helpers ────────────────────────────────────────────────────────────────
_now = lambda: datetime.utcnow().isoformat()

def clarify(task, user, agent, text, reason):
    if not agent:
        agent = "manager"
    return {
        "task_id": task, "user_id": user, "agent_type": agent,
        "message": {"type": "text", "content": text},
        "metadata": {"reason": reason},
        "created_at": _now(),
    }

def structured(task, user, agent, obj):
    if not agent:
        agent = "manager"
    return {
        "task_id": task, "user_id": user, "agent_type": agent,
        "message": obj, "created_at": _now(),
    }

async def dispatch(url: str, payload: dict):
    async with httpx.AsyncClient() as c:
        print("=== Webhook Dispatch ===\n", json.dumps(payload, indent=2))
        await c.post(url, json=payload)
        print("========================")

CHAT_URL   = os.getenv("BUBBLE_CHAT_URL")
STRUCT_URL = os.getenv("BUBBLE_STRUCTURED_URL")

# ── specialist agents ─────────────────────────────────────────────────────
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

# ── manager with handoffs ─────────────────────────────────────────────────
MANAGER_TXT = prompt_with_handoff_instructions("""
You are an intelligent router for user requests.
If you need clarification, ask a question (requires_user_input).
Otherwise delegate via a handoff to the correct agent.
If you are not asking a question, you MUST emit: {"handoff": "<agent_name>", "reason": "..."} and nothing else.
Never output the plan yourself.
Never wrap the JSON in code fences.
""")

manager_agent = Agent("Manager", instructions=MANAGER_TXT, handoffs=list(AGENT_MAP.values()))

# ── dispatch helper (robust JSON parse) ───────────────────────────────────
async def _dispatch(task_id: str, user_id: str, agent_key: str, result):
    if getattr(result, "requires_user_input", None):
        await dispatch(CHAT_URL, clarify(task_id, user_id, agent_key,
                                         result.requires_user_input, "Agent requested clarification"))
        return
    try:
        clean = result.final_output.strip()
        if clean.startswith("```"):
            clean = clean.split("```", 2)[1].strip()
        parsed = json.loads(clean)
        if "output_type" in parsed:
            await dispatch(STRUCT_URL, structured(task_id, user_id, agent_key, parsed))
            return
    except Exception:
        pass
    await dispatch(CHAT_URL, clarify(task_id, user_id, agent_key,
                                     result.final_output.strip(), "Agent returned unstructured output"))

# ── FastAPI ───────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

# ── main endpoint ─────────────────────────────────────────────────────────
@app.post("/agent")
async def endpoint(req: Request):
    data = await req.json()
    action = data.get("action")
    if action not in ("new_task", "new_message"):
        raise HTTPException(400, "Unknown action")

    task_id, user_id = data["task_id"], data["user_id"]
    user_text = data.get("user_prompt") or data.get("message")
    if not user_text:
        raise HTTPException(422, "Missing user_prompt or message")

    # NEW TASK --------------------------------------------------------------
    if action == "new_task":
        mgr_res = await Runner.run(manager_agent, input=user_text, max_turns=1)

        # a) manager asks clarification
        if getattr(mgr_res, "requires_user_input", None):
            await dispatch(CHAT_URL, clarify(task_id, user_id, "manager",
                                             mgr_res.requires_user_input,
                                             "Manager requested clarification"))
            return {"ok": True}

        # b) manager hand-off
        try:
            handoff_info = json.loads(mgr_res.final_output)
            route_to = handoff_info.get("handoff")
        except Exception:
            route_to = None

        # **normalize key**  ("ContentAgent" → "content")
        if route_to:
            route_to = route_to.lower().removesuffix("agent")

        if route_to in AGENT_MAP:
            # routing decision webhook
            await dispatch(CHAT_URL, clarify(task_id, user_id, "manager",
                                             json.dumps({"route_to": route_to,
                                                         "reason": handoff_info.get("reason", "")}),
                                             "Manager routing decision"))
            # downstream run + webhook
            ds_res = await Runner.run(AGENT_MAP[route_to], input=user_text, max_turns=10)
            await _dispatch(task_id, user_id, route_to, ds_res)
            return {"ok": True}

        # c) manager returned plain text
        await _dispatch(task_id, user_id, "manager", mgr_res)
        return {"ok": True}

    # NEW MESSAGE -----------------------------------------------------------
    agent_key = (data.get("agent_session_id") or "manager").lower().removesuffix("agent")
    if agent_key not in AGENT_MAP and agent_key != "manager":
        agent_key = "manager"
    agent_obj = manager_agent if agent_key == "manager" else AGENT_MAP[agent_key]

    res = await Runner.run(agent_obj, input=user_text, max_turns=10)
    await _dispatch(task_id, user_id, agent_key, res)
    return {"ok": True}
