# src/agents/profilebuilder.py

from agents.profilebuilder_agent import profilebuilder_agent
from agents.util.webhook import send_webhook
from fastapi import APIRouter, Request, HTTPException
import os
import json
from datetime import datetime

router = APIRouter()

# URLs pulled from environment variables
PROFILE_WEBHOOK_URL = os.getenv("PROFILE_WEBHOOK_URL")
CHAT_WEBHOOK_URL = os.getenv("CLARIFICATION_WEBHOOK_URL")  # This is the chat bubble webhook you already have

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

    # 1. Run ProfileBuilder agent
    result = await Runner.run(
        profilebuilder_agent,
        input=prompt,
        context={"task_id": task_id, "user_id": user_id},
        max_turns=3,
    )

    raw_output = result.final_output.strip()

    # 2. Parse partial profile field from agent output
    try:
        field_update = json.loads(raw_output)
        if not isinstance(field_update, dict) or len(field_update) != 1:
            raise ValueError("Agent must output a single-field JSON object")
        reason = "profile_partial"
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(500, f"Agent output invalid: {e}")

    # 3. Send webhook to update Profile fields
    profile_payload = {
        "task_id": task_id,
        "user_id": user_id,
        "agent_type": "profilebuilder",
        "message_type": "profile_partial",
        "message_content": field_update,
        "metadata_reason": reason,
        "created_at": datetime.utcnow().isoformat(),
    }
    if not PROFILE_WEBHOOK_URL:
        raise RuntimeError("Missing PROFILE_WEBHOOK_URL")

    await send_webhook(PROFILE_WEBHOOK_URL, profile_payload)

    # 4. Send webhook to update Chat (agent's next question)
    # Get the next outgoing prompt (already included in agent output after profile field is collected)
    next_prompt = result.next_prompt if hasattr(result, "next_prompt") else None

    if next_prompt:
        chat_payload = {
            "task_id": task_id,
            "user_id": user_id,
            "agent_type": "profilebuilder",
            "message_type": "text",
            "message_content": next_prompt,
            "metadata_reason": "follow_up",
            "created_at": datetime.utcnow().isoformat(),
        }
        if not CHAT_WEBHOOK_URL:
            raise RuntimeError("Missing CLARIFICATION_WEBHOOK_URL")

        await send_webhook(CHAT_WEBHOOK_URL, chat_payload)

    return {"ok": True}
