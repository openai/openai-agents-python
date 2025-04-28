# src/agents/profilebuilder.py

from fastapi import APIRouter, Request, HTTPException
from agents.util.webhook import send_webhook
import os
import json
from datetime import datetime

router = APIRouter()

# ENV var — Bubble webhook URL for profile save notifications
PROFILE_WEBHOOK_URL = os.getenv("PROFILE_WEBHOOK_URL")

# (Optional) timeout for slow webhook sendings
WEBHOOK_TIMEOUT_SECONDS = float(os.getenv("WEBHOOK_TIMEOUT", "10"))


@router.post("/profilebuilder")
async def profilebuilder_handler(req: Request):
    """
    Handle incoming POST requests to build or update a user profile.
    Expects fields: task_id, user_id, profile (dict)
    """
    data = await req.json()

    # Basic field checks
    task_id = data.get("task_id")
    user_id = data.get("user_id")
    profile = data.get("profile")

    if not task_id or not user_id:
        raise HTTPException(422, "Missing required field: task_id or user_id")

    if not profile:
        raise HTTPException(422, "Missing required field: profile object")

    # [TODO]: Save the profile blob to Bubble Data API if needed
    # await upsert_profile(user_id, profile)

    # Build outgoing webhook payload
    payload = {
        "task_id": task_id,
        "user_id": user_id,
        "agent_type": "profilebuilder",  # custom agent_type you define
        "message": {
            "type": "text",
            "content": "Profile saved successfully."
        },
        "metadata": {
            "reason": "profile_saved"
        },
        "created_at": datetime.utcnow().isoformat(),
    }

    # Fire webhook to Bubble
    if not PROFILE_WEBHOOK_URL:
        raise RuntimeError("Missing PROFILE_WEBHOOK_URL environment variable")

    await send_webhook(PROFILE_WEBHOOK_URL, payload)

    return {"ok": True, "message": "Profile processed and webhook sent."}
