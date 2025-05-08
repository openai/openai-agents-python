from app.profilebuilder_agent import profilebuilder_agent
from agents.run import Runner
from app.util.webhook import send_webhook          # make sure this import path is right
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
import os

router = APIRouter()
PROFILE_WEBHOOK_URL = os.getenv("PROFILE_WEBHOOK_URL")
CHAT_WEBHOOK_URL    = os.getenv("CLARIFICATION_WEBHOOK_URL")


@router.post("/profilebuilder")
async def profilebuilder_handler(req: Request):
    body    = await req.json()
    task_id = body.get("task_id")
    user_id = body.get("user_id")
    prompt  = body.get("prompt") or body.get("user_prompt") or body.get("message")

    if not (task_id and user_id and prompt):
        raise HTTPException(422, "Missing task_id, user_id, or prompt")

    # 1. Run the agent -------------------------------------------------------------------
    result     = await Runner.run(profilebuilder_agent, prompt)
    out        = result.final_output          # this is a ProfileFieldOut

    profile_fragment = {out.field_name: out.field_value}
    follow_up        = out.clarification_prompt

    created_at = datetime.utcnow().isoformat()

    # 2. Send profile-partial webhook -----------------------------------------------------
    if not PROFILE_WEBHOOK_URL:
        raise RuntimeError("PROFILE_WEBHOOK_URL env var is missing")

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

    # 3. Send follow-up chat webhook (if any) ---------------------------------------------
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