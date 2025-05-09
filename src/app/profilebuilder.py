# src/app/profilebuilder.py
from fastapi import APIRouter, Request, HTTPException
from datetime import datetime
from app.storage import get_storage
from agents.run import Runner
from app.profilebuilder_agent import profilebuilder_agent

router = APIRouter()
storage = get_storage()

@router.post("/profilebuilder")
async def profilebuilder_handler(req: Request):
    data    = await req.json()
    t, u, p  = data.get("task_id"), data.get("user_id"), data.get("prompt")
    if not (t and u and p):
        raise HTTPException(422, "Missing task_id, user_id, or prompt")

    # 1) Get the agent’s output
    result = await Runner.run(profilebuilder_agent, p)
    out    = result.final_output
    ts     = datetime.utcnow().isoformat()

    # 2) Save profile field (calls Bubble webhook or Supabase upsert)
    await storage.save_profile_field(t, u, out.field_name, out.field_value, ts)

    # 3) Send follow-up chat if needed
    if out.clarification_prompt:
        await storage.send_chat_message(t, u, out.clarification_prompt, ts)

    return {"ok": True}
