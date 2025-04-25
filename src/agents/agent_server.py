import os
import sys
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from agents import Agent, Runner
from .agent_onboarding import router as onboarding_router
from .agent_profilebuilder import router as profilebuilder_router

# Agent Definitions
manager_agent = Agent(
    name="Manager",
    instructions="""
You are an intelligent router for user requests.
Your outputs must follow one of these formats:

If routing to another agent:
{
  "type": "internal",
  "route_to": "strategy" // or "content", "repurpose", "feedback"
}

If asking for clarification:
{
  "type": "clarification",
  "content": "Could you clarify what platform you want to use?"
}

Respond ONLY in one of the above JSON formats.
"""
)

strategy_agent = Agent(
    name="StrategyAgent",
    instructions="""
You create detailed, actionable 7-day social media campaign strategies.
Your outputs must follow one of these formats:

If asking for clarification:
{
  "type": "clarification",
  "content": "Could you tell me your campaign tone and target audience?"
}

If outputting the full strategy:
{
  "type": "structured",
  "output_type": "strategy_plan",
  "contains_image": false,
  "details": {
    "days": [
      { "title": "Day 1", "theme": "Awareness", "cta": "Visit our page" },
      { "title": "Day 2", "theme": "Engagement", "cta": "Comment your thoughts" },
      ...
    ]
  }
}

Respond ONLY in one of the above JSON formats.
"""
)

content_agent = Agent(
    name="ContentAgent",
    instructions="""
You create brand-aligned social media content drafts.
Your outputs must follow one of these formats:

If asking for clarification:
{
  "type": "clarification",
  "content": "Which platform and tone should the posts match?"
}

If outputting content variations:
{
  "type": "structured",
  "output_type": "content_variants",
  "contains_image": false,
  "details": {
    "variants": [
      {
        "platform": "Instagram",
        "caption": "Life’s a journey 🚀 #MondayMotivation",
        "hook": "Feeling stuck?",
        "cta": "Check out our tips!"
      },
      ...
    ]
  }
}

Respond ONLY in one of the above JSON formats.
"""
)

repurpose_agent = Agent(
    name="RepurposeAgent",
    instructions="""
You transform existing social media posts into new formats for different platforms.
Your outputs must follow one of these formats:

If asking for clarification:
{
  "type": "clarification",
  "content": "Which platforms would you like to repurpose for?"
}

If outputting repurposed posts:
{
  "type": "structured",
  "output_type": "repurposed_posts",
  "contains_image": false,
  "details": {
    "original": "Original Instagram caption here...",
    "repurposed": [
      {
        "platform": "Twitter",
        "caption": "Short and punchy tweet version!"
      },
      ...
    ]
  }
}

Respond ONLY in one of the above JSON formats.
"""
)

feedback_agent = Agent(
    name="FeedbackAgent",
    instructions="""
You review social media posts and suggest improvements.
Your outputs must follow one of these formats:

If asking for clarification:
{
  "type": "clarification",
  "content": "Could you specify which post style (formal, casual, humorous) you want feedback on?"
}

If providing feedback:
{
  "type": "structured",
  "output_type": "content_feedback",
  "contains_image": false,
  "details": {
    "original": "Original caption here...",
    "feedback": "This caption is a bit generic. Consider adding a stronger emotional hook.",
    "suggested_edit": "Transform your life starting today! 🚀 #Motivation"
  }
}

Respond ONLY in one of the above JSON formats.
"""
)

AGENT_MAP = {
    "strategy": strategy_agent,
    "content": content_agent,
    "repurpose": repurpose_agent,
    "feedback": feedback_agent,
}

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(onboarding_router)
app.include_router(profilebuilder_router)

STRUCTURED_WEBHOOK_URL = os.getenv("BUBBLE_STRUCTURED_URL")
CLARIFICATION_WEBHOOK_URL = os.getenv("BUBBLE_CHAT_URL")

def log_and_send(webhook, payload):
    async def _send():
        async with httpx.AsyncClient() as client:
            print("=== Webhook Dispatch ===")
            print(f"Webhook URL: {webhook}")
            print("Payload being sent:")
            print(json.dumps(payload, indent=2))
            response = await client.post(webhook, json=payload)
            print(f"Response Status: {response.status_code}")
            print(f"Response Body: {response.text}")
            print("========================")
    return _send()

@app.post("/agent")
async def agent_endpoint(req: Request):
    data = await req.json()
    action = data.get("action")
    task_id = data.get("task_id")
    user_id = data.get("user_id")

    if action == "new_task":
        user_input = data["user_prompt"]
        manager_result = await Runner.run(manager_agent, input=user_input)

        try:
            parsed_mgr = json.loads(manager_result.final_output)
            output_type = parsed_mgr.get("type")

            if output_type == "internal":
                agent_type = parsed_mgr.get("route_to")
                agent = AGENT_MAP.get(agent_type)
                if not agent:
                    raise HTTPException(400, f"Unknown agent type: {agent_type}")

                agent_result = await Runner.run(agent, input=user_input)
                parsed_agent = json.loads(agent_result.final_output)

                if parsed_agent.get("type") == "clarification":
                    payload = {
                        "task_id": task_id,
                        "user_id": user_id,
                        "agent_type": agent_type,
                        "message": {
                            "type": "text",
                            "content": parsed_agent["content"]
                        },
                        "metadata": {"reason": "Agent requested clarification"},
                        "created_at": datetime.utcnow().isoformat()
                    }
                    return await log_and_send(CLARIFICATION_WEBHOOK_URL, payload)

                elif parsed_agent.get("type") == "structured":
                    payload = {
                        "task_id": task_id,
                        "user_id": user_id,
                        "agent_type": agent_type,
                        "message": parsed_agent,
                        "created_at": datetime.utcnow().isoformat()
                    }
                    return await log_and_send(STRUCTURED_WEBHOOK_URL, payload)

                else:
                    raise HTTPException(500, "Unexpected downstream agent output type.")

            elif output_type == "clarification":
                payload = {
                    "task_id": task_id,
                    "user_id": user_id,
                    "agent_type": "manager",
                    "message": {
                        "type": "text",
                        "content": parsed_mgr["content"]
                    },
                    "metadata": {"reason": "Manager requested clarification"},
                    "created_at": datetime.utcnow().isoformat()
                }
                return await log_and_send(CLARIFICATION_WEBHOOK_URL, payload)

            else:
                raise HTTPException(500, "Unexpected manager output type.")

        except Exception as e:
            raise HTTPException(500, f"Failed to parse manager output: {str(e)}")

    elif action == "new_message":
        user_msg = data.get("message") or data.get("user_prompt")
        if not user_msg:
            raise HTTPException(422, "Missing 'message' or 'user_prompt'")

        sess = data.get("agent_session_id")
        agent_type = sess if sess in AGENT_MAP else "manager"
        agent = AGENT_MAP.get(agent_type, manager_agent)

        result = await Runner.run(agent, input=user_msg)

        try:
            parsed = json.loads(result.final_output)
            output_type = parsed.get("type")

            if output_type == "clarification":
                payload = {
                    "task_id": task_id,
                    "user_id": user_id,
                    "agent_type": agent_type,
                    "message": {
                        "type": "text",
                        "content": parsed["content"]
                    },
                    "metadata": {"reason": "Agent requested clarification"},
                    "created_at": datetime.utcnow().isoformat()
                }
                return await log_and_send(CLARIFICATION_WEBHOOK_URL, payload)

            elif output_type == "structured":
                payload = {
                    "task_id": task_id,
                    "user_id": user_id,
                    "agent_type": agent_type,
                    "message": parsed,
                    "created_at": datetime.utcnow().isoformat()
                }
                return await log_and_send(STRUCTURED_WEBHOOK_URL, payload)

            else:
                raise HTTPException(500, "Unexpected agent output type.")

        except Exception as e:
            raise HTTPException(500, f"Failed to parse agent output: {str(e)}")

    else:
        raise HTTPException(400, "Unknown action")
