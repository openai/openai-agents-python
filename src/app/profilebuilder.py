# ──────────────────────────────────────────────────────────────
# src/app/profilebuilder.py   (← keep the same path as before)
# ──────────────────────────────────────────────────────────────

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from datetime import datetime
import json
import os

from app.profilebuilder_agent import profilebuilder_agent         # the Agent
from app.util.webhook import send_webhook                         # Bubble helper

router = APIRouter()

PROFILE_WEBHOOK_URL = os.getenv("PROFILE_WEBHOOK_URL")
CHAT_WEBHOOK_URL    = os.getenv("CLARIFICATION_WEBHOOK_URL")


@router.post("/profilebuilder")
async def profilebuilder_handler(req: Request):
    # ------------------------------------------------------------------ #
    # 1. Validate inbound payload from Bubble                            #
    # ------------------------------------------------------------------ #
    data   = await req.json()
    task_id = data.get("task_id")
    user_id = data.get("user_id")
    prompt  = data.get("prompt") or data.get("user_prompt") or data.get("message")

    if not (task_id and user_id and prompt):
        raise HTTPException(422, "Missing task_id, user_id, or prompt")

    # ------------------------------------------------------------------ #
    # 2. Run the agent and normalise its output                          #
    # ------------------------------------------------------------------ #
    result = await profilebuilder_agent.run(prompt)

    # `result` can be a Pydantic model (ProfileFieldOut / ClarificationOut)
    # or – if something odd happens – a raw string.  Convert to dict ↓
    if isinstance(result, BaseModel):
        result_dict = result.model_dump()
    else:
        try:
            result_dict = json.loads(result.strip())
        except Exception:
            raise HTTPException(500, "Agent returned unparsable output")

    # ------------------------------------------------------------------ #
    # 3. Decide what kind of message it is                               #
    # ------------------------------------------------------------------ #
    is_clarification = "clarification_prompt" in result_dict
    created_at       = datetime.utcnow().isoformat()

    if is_clarification:
        # ----- send follow-up prompt to chat UI ----------------------- #
        if not CHAT_WEBHOOK_URL:
            raise RuntimeError("Missing CLARIFICATION_WEBHOOK_URL env var")

        chat_payload = {
            "task_id":         task_id,
            "user_id":         user_id,
            "agent_type":      "profilebuilder",
            "message_type":    "text",
            "message_content": result_dict["clarification_prompt"],
            "metadata_reason": "follow_up",
            "created_at":      created_at,
        }
        await send_webhook(CHAT_WEBHOOK_URL, chat_payload)

    else:
        # ----- send partial profile field to DB ----------------------- #
        if not PROFILE_WEBHOOK_URL:
            raise RuntimeError("Missing PROFILE_WEBHOOK_URL env var")

        profile_payload = {
            "task_id":         task_id,
            "user_id":         user_id,
            "agent_type":      "profilebuilder",
            "message_type":    "profile_partial",
            "message_content": result_dict,          # the single-field dict
            "metadata_reason": "profile_partial",
            "created_at":      created_at,
        }
        await send_webhook(PROFILE_WEBHOOK_URL, profile_payload)

    return {"ok": True}
