# ──────────────────────────────────────────────────────────────
# src/app/profilebuilder.py
# ──────────────────────────────────────────────────────────────

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from datetime import datetime
import json, os

from app.profilebuilder_agent import profilebuilder_agent
from app.util.webhook import send_webhook

router = APIRouter()

PROFILE_WEBHOOK_URL = os.getenv("PROFILE_WEBHOOK_URL")
CHAT_WEBHOOK_URL    = os.getenv("CLARIFICATION_WEBHOOK_URL")

# ------------------------------------------------------------------ #
# Helper: convert any agent output to a plain dict                    #
# ------------------------------------------------------------------ #
def to_dict(agent_output):
    if isinstance(agent_output, BaseModel):
        return agent_output.model_dump()
    if isinstance(agent_output, (bytes, bytearray)):
        agent_output = agent_output.decode()
    if isinstance(agent_output, str):
        agent_output = agent_output.strip()
        if agent_output.startswith("{"):
            return json.loads(agent_output)
    raise ValueError("Unable to parse agent output")

# ------------------------------------------------------------------ #
# POST /profilebuilder                                               #
# ------------------------------------------------------------------ #
@router.post("/profilebuilder")
async def profilebuilder_handler(req: Request):
    body   = await req.json()
    task_id = body.get("task_id")
    user_id = body.get("user_id")
    prompt  = body.get("prompt") or body.get("user_prompt") or body.get("message")

    if not (task_id and user_id and prompt):
        raise HTTPException(422, "Missing task_id, user_id, or prompt")

    # 1. ── Run the agent ────────────────────────────────────────────
    agent_raw  = await profilebuilder_agent(prompt)
    try:
        agent_out = to_dict(agent_raw)
    except Exception as e:
        raise HTTPException(500, f"Agent returned unparsable output: {e}")

    # 2. ── Split into “profile fields” vs “prompt to ask” ───────────
    clarification_prompt = agent_out.pop("clarification_prompt", None)
    # Any keys left in agent_out are profile fields
    has_profile_update   = bool(agent_out)

    created_at = datetime.utcnow().isoformat()

    # 3-A. ── Send profile-partial webhook (if we have one) ──────────
    if has_profile_update:
        if not PROFILE_WEBHOOK_URL:
            raise RuntimeError("Missing PROFILE_WEBHOOK_URL env var")

        profile_payload = {
            "task_id":         task_id,
            "user_id":         user_id,
            "agent_type":      "profilebuilder",
            "message_type":    "profile_partial",
            "message_content": agent_out,            # ← the single-field dict
            "metadata_reason": "profile_partial",
            "created_at":      created_at,
        }
        await send_webhook(PROFILE_WEBHOOK_URL, profile_payload)

    # 3-B. ── Figure out what prompt (if any) to send back to UI ─────
    if not clarification_prompt and hasattr(agent_raw, "next_prompt"):
        clarification_prompt = getattr(agent_raw, "next_prompt")

    if clarification_prompt:
        if not CHAT_WEBHOOK_URL:
            raise RuntimeError("Missing CLARIFICATION_WEBHOOK_URL env var")

        chat_payload = {
            "task_id":         task_id,
            "user_id":         user_id,
            "agent_type":      "profilebuilder",
            "message_type":    "text",
            "message_content": clarification_prompt,
            "metadata_reason": "follow_up",
            "created_at":      created_at,
        }
        await send_webhook(CHAT_WEBHOOK_URL, chat_payload)

    return {"ok": True}
