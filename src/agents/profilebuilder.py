# src/agents/profilebuilder.py

from agents.profilebuilder_agent import profilebuilder_agent
from agents.utils.webhook import send_webhook
from fastapi import APIRouter, Request, HTTPException
import os
import json
from datetime import datetime

router = APIRouter()

PROFILE_WEBHOOK_URL = os.getenv("PROFILE_WEBHOOK_URL")

@router.post("/profilebuilder")
async def profilebuilder_handler(req: Request):
    data = await req.json()

    task_id = data.get("task_id")
    user_id = data.get("user_id")
    prompt = (
        data.get("prompt") or data.get("user_prompt") or data.get("message")
    )

    if not (task_id and user_id and prompt):
        raise HTTPException(422, "Missing task_id, user_id, or prompt")

    result = await Runner.run(
        profilebuilder_agent,
        input=prompt,
        context={"task_id": task_id, "user_id": user_id},
        max_turns=3,
    )

    raw = result.final_output.strip()

    # Try to parse single field JSON
    try:
        field_update = json.loads(raw)
        if not isinstance(field_update, dict) or len(field_update) != 1:
            raise ValueError("Must output a single-field JSON object")
        reason = "profile_partial"
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(500, f"Agent output invalid: {e}")

    # Build payload to send to Bubble
    payload = {
        "task_id": task_id,
        "user_id": user_id,
        "agent_type": "profilebuilder",
        "message": {
            "type": "profile_partial",
            "content": field_update
        },
        "metadata": {
            "reason": reason
        },
        "created_at": datetime.utcnow().isoformat(),
    }

    if not PROFILE_WEBHOOK_URL:
        raise RuntimeError("Missing PROFILE_WEBHOOK_URL")

    await send_webhook(PROFILE_WEBHOOK_URL, payload)

    return {"ok": True}
