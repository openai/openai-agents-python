"""
Conversational Profile Builder router
• Asks tailored questions until all required keys are filled
• Emits structured_profile JSON
"""

from __future__ import annotations
import os, sys, json, httpx
from datetime import datetime
from dotenv import load_dotenv
from fastapi import APIRouter, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware  # for local tests

# ── SDK --------------------------------------------------------------------
load_dotenv()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agents import Agent, Runner

# ── router -----------------------------------------------------------------
router = APIRouter()

# ── webhooks ---------------------------------------------------------------
CHAT_URL   = os.getenv("BUBBLE_CHAT_URL")   # questions / clarifications
STRUCT_URL = os.getenv("BUBBLE_STRUCTURED_URL")  # final profile JSON

# ── helper -----------------------------------------------------------------
def _now() -> str:
    from datetime import datetime
    return datetime.utcnow().isoformat()

async def _dispatch(url: str, payload: dict):
    async with httpx.AsyncClient() as c:
        print("=== PB Webhook ===\n", json.dumps(payload, indent=2))
        await c.post(url, json=payload)
        print("==================")

def clarify(task, user, text, reason="Agent requested clarification"):
    return {
        "task_id": task,
        "user_id": user,
        "agent_type": "profilebuilder",
        "message": {"type": "text", "content": text},
        "metadata": {"reason": reason},
        "created_at": _now(),
    }

def structured(task, user, obj):
    return {
        "task_id": task,
        "user_id": user,
        "agent_type": "profilebuilder",
        "message": obj,
        "created_at": _now(),
    }

# ── ProfileBuilderAgent ----------------------------------------------------
REQUIRED_KEYS = [
    "primary_SNSchannel", "profile_type", "core_topic", "sub_angle",
    "primary_objective", "content_strength", "time_budget_weekly",
    "inspiration_accounts", "provided_follower_count", "locale",
    "motivation_note"
]

PB_PROMPT = f"""
You are ProfileBuilderAgent.

Goal: collect each of these keys once: {", ".join(REQUIRED_KEYS)}.

Rules:
1. Ask ONE question at a time, tailored to previous answers.
2. After each user reply, decide which required key is still missing and
   ask the next most relevant question.
3. Reflect back occasionally so the user feels understood.
4. When ALL keys are collected, respond ONLY with:

{{
  "output_type": "structured_profile",
  "contains_image": false,
  "details": {{  ...all keys filled ... }}
}}

5. If you still need information, respond ONLY with:
{{ "requires_user_input": "your next question" }}

Do NOT wrap responses in markdown fences.
"""

profile_builder_agent = Agent(
    name="ProfileBuilderAgent",
    instructions=PB_PROMPT,
    tools=[],
)

# ── API endpoint -----------------------------------------------------------
@router.post("/profilebuilder")
async def profile_builder_endpoint(req: Request):
    """
    Expects:
    {
      "action": "new_task" | "new_message",
      "task_id": "...",
      "user_id": "...",
      "user_prompt": "...",          # for new_task
      "message": "...",              # for new_message
      "agent_session_id": "profilebuilder"   # for new_message
    }
    """
    data = await req.json()
    action = data.get("action")
    if action not in ("new_task", "new_message"):
        raise HTTPException(400, "Unknown action")

    task_id, user_id = data["task_id"], data["user_id"]
    user_text = data.get("user_prompt") or data.get("message")
    if not user_text:
        raise HTTPException(422, "Missing user_prompt or message")

    # run the agent
    result = await Runner.run(profile_builder_agent, input=user_text, max_turns=1)

    # clarification?
    if getattr(result, "requires_user_input", None):
        await _dispatch(CHAT_URL,
                        clarify(task_id, user_id, result.requires_user_input))
        return {"ok": True}

    # final structured?
    try:
        parsed = json.loads(result.final_output.strip())
        if parsed.get("output_type") == "structured_profile":
            await _dispatch(STRUCT_URL, structured(task_id, user_id, parsed))
            return {"ok": True}
    except Exception:
        pass

    # fallback: return raw text as chat
    await _dispatch(CHAT_URL,
                    clarify(task_id, user_id, result.final_output.strip(),
                            reason="Agent returned unstructured output"))
    return {"ok": True"}
