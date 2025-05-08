# ──────────────────────────────────────────────────────────────
# src/app/profilebuilder.py
# ──────────────────────────────────────────────────────────────
from fastapi import APIRouter, Request, HTTPException
from datetime import datetime
import os

from app.profilebuilder_agent import profilebuilder_agent
from agents.run import Runner
from app.util.webhook import send_webhook   # ← adjust import path if needed

router = APIRouter()

PROFILE_WEBHOOK_URL = os.getenv("PROFILE_WEBHOOK_URL")
CHAT_WEBHOOK_URL    = os.getenv("CLARIFICATION_WEBHOOK_URL")

# ------------------------------------------------------------------ #
# POST /profilebuilder                                               #
# ------------------------------------------------------------------ #
@router.post("/profilebuilder")
async def profilebuilder_handler(req: Request):
    body    = await req.json()
    task_id = body.get("task_id")
    user_id = body.get("user_id")
    prompt  = body.get("prompt") or body.get("user_prompt") or body.get("message")

    if not (task_id and user_id and prompt):
        raise HTTPException(422, "Missing task_id, user_id, or prompt")

    # 1. ── Run the agent via Runner ─────────────────────────────────
    result      = await Runner.run(profilebuilder_agent, prompt)
    agent_out   = result.final_output            # ProfileFieldOut instance

    # convert to simple {field_name: field_value}
    field_name  = agent_out.field_name
    field_value = agent_out.field_value
    profile_fragment = {field_name: field_value}

    created_at  = datetime.utcnow().isoformat()

    # 2. ── Send profile-partial webhook ─────────────────────────────
    if not PROFILE_WEBHOOK_URL:
        raise RuntimeError("Missing PROFILE_WEBHOOK_URL env var")

    await send_webhook(
        PROFILE_WEBHOOK_URL,
        {
            "task_id":         task_id,
            "user_id":         user_id,
            "agent_type":      "profilebuilder",
            "message_type":    "profile_partial",
            "message_content": profile_fragment,
            "created_at":      created_at,
        },
    )

    # 3. ── Send the agent’s follow-up question as a chat message ───
    # (the agent’s textual reply is in result.chat_response)
    follow_up = result.chat_response
    if follow_up and CHAT_WEBHOOK_URL:
        await send_webhook(
            CHAT_WEBHOOK_URL,
            {
                "task_id":         task_id,
                "user_id":         user_id,
                "agent_type":      "profilebuilder",
                "message_type":    "text",
                "message_content": follow_up,
                "created_at":      created_at,
            },
        )

    return {"ok": True}
