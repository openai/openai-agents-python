import os
import sys
import json
import asyncio
from datetime import datetime
from dotenv import load_dotenv

# 1) Load environment variables
load_dotenv()

# 2) Add project src folder so "agents" can import its own util subpackage
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# 3) Core SDK imports
from agents import Agent, Runner, tool

# 4) SDK guardrail types (so guardrail imports work)
from agents.util._types import MaybeAwaitable

# ───────────────────────────────────────────────────────────
# 5) Agent definitions (Phase 1: keep them here)
# ───────────────────────────────────────────────────────────
manager_agent = Agent(
    name="Manager",
    instructions="""
You are an intelligent router for user requests.
Decide the intent behind the message: strategy, content, repurpose, feedback.
If you are unsure or need more info, ask a clarifying question instead of routing.
If clarification is needed, respond only with a plain text clarification question.
Otherwise, respond in strict JSON like:
{ "route_to": "strategy", "reason": "User wants a campaign plan" }
"""
)

strategy_agent = Agent(
    name="StrategyAgent",
    instructions="""
You create clear, actionable 7-day social media campaign strategies.
If user input is unclear or missing platform, audience, or tone — ask for clarification.
Respond in structured JSON like:
{
  "output_type": "strategy_plan",
  "contains_image": false,
  "details": {
    "days": [
      { "title": "...", "theme": "...", "cta": "..." }
    ]
  }
}
Only return JSON in this format.
""",
    tools=[]
)

content_agent = Agent(
    name="ContentAgent",
    instructions="""
You write engaging, brand-aligned social content.
If user input lacks platform or goal, ask for clarification.
Return post drafts in this JSON format:
{
  "output_type": "content_variants",
  "contains_image": false,
  "details": {
    "variants": [
      {
        "platform": "Instagram",
        "caption": "...",
        "hook": "...",
        "cta": "..."
      }
    ]
  }
}
Only respond in this format.
""",
    tools=[]
)

repurpose_agent = Agent(
    name="RepurposeAgent",
    instructions="""
You convert existing posts into new formats for different platforms.
Respond using this format:
{
  "output_type": "repurposed_posts",
  "contains_image": false,
  "details": {
    "original": "...",
    "repurposed": [
      {
        "platform": "...",
        "caption": "...",
        "format": "..."
      }
    ]
  }
}
""",
    tools=[]
)

feedback_agent = Agent(
    name="FeedbackAgent",
    instructions="""
You evaluate content and offer improvements.
Respond in this structured format:
{
  "output_type": "content_feedback",
  "contains_image": false,
  "details": {
    "original": "...",
    "feedback": "...",
    "suggested_edit": "..."
  }
}
""",
    tools=[]
)

AGENT_MAP = {
    "strategy":  strategy_agent,
    "content":   content_agent,
    "repurpose": repurpose_agent,
    "feedback":  feedback_agent,
}

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from .agent_onboarding import router as onboarding_router
from .agent_profilebuilder import router as profilebuilder_router
app.include_router(onboarding_router)
app.include_router(profilebuilder_router)

STRUCTURED_WEBHOOK_URL    = os.getenv("BUBBLE_STRUCTURED_URL")
CLARIFICATION_WEBHOOK_URL = os.getenv("BUBBLE_CHAT_URL")

@app.post("/agent")
async def agent_endpoint(req: Request):
    data = await req.json()
    action = data.get("action")

    # PATCHED SECTION: new_task action handling
    if action == "new_task":
        user_input = data["user_prompt"]
        mgr_result = await Runner.run(manager_agent, input=user_input)
        try:
            parsed_mgr = json.loads(mgr_result.final_output)

            # ✅ Case 1: Manager routes to downstream agent
            if isinstance(parsed_mgr, dict) and "route_to" in parsed_mgr:
                agent_type = parsed_mgr["route_to"]
                agent = AGENT_MAP.get(agent_type)
                if not agent:
                    raise HTTPException(400, f"Unknown agent: {agent_type}")

                result = await Runner.run(agent, input=user_input)

                try:
                    parsed_output = json.loads(result.final_output)
                    is_structured = "output_type" in parsed_output
                except Exception:
                    parsed_output = None
                    is_structured = False

                if getattr(result, "requires_user_input", None):
                    webhook = CLARIFICATION_WEBHOOK_URL
                    payload = {
                        "task_id": data.get("task_id"),
                        "user_id": data.get("user_id"),
                        "agent_type": agent_type,
                        "message_raw": json.dumps({
                            "type": "clarification",
                            "content": result.requires_user_input
                        }),
                        "metadata_raw": json.dumps({ "reason": "Agent requested clarification" }),
                        "created_at": datetime.utcnow().isoformat()
                    }
                elif is_structured:
                    webhook = STRUCTURED_WEBHOOK_URL
                    payload = {
                        "task_id": data.get("task_id"),
                        "user_id": data.get("user_id"),
                        "agent_type": agent_type,
                        "message_raw": json.dumps(parsed_output),
                        "metadata_raw": json.dumps({ "reason": "Structured agent output" }),
                        "created_at": datetime.utcnow().isoformat()
                    }
                else:
                    webhook = CLARIFICATION_WEBHOOK_URL
                    payload = {
                        "task_id": data.get("task_id"),
                        "user_id": data.get("user_id"),
                        "agent_type": agent_type,
                        "message_raw": json.dumps({
                            "type": "text",
                            "content": result.final_output
                        }),
                        "metadata_raw": json.dumps({ "reason": "Agent returned unstructured output" }),
                        "created_at": datetime.utcnow().isoformat()
                    }

            # ✅ Case 2: Manager is unclear and returns a clarification question (not a route_to)
            else:
                webhook = CLARIFICATION_WEBHOOK_URL
                payload = {
                    "task_id": data.get("task_id"),
                    "user_id": data.get("user_id"),
                    "agent_type": "manager",
                    "message_raw": json.dumps({
                        "type": "clarification",
                        "content": mgr_result.final_output.strip()
                    }),
                    "metadata_raw": json.dumps({ "reason": "Manager requested clarification" }),
                    "created_at": datetime.utcnow().isoformat()
                }

        except Exception:
            # Fallback for malformed manager output
            webhook = CLARIFICATION_WEBHOOK_URL
            payload = {
                "task_id": data.get("task_id"),
                "user_id": data.get("user_id"),
                "agent_type": "manager",
                "message_raw": json.dumps({
                    "type": "clarification",
                    "content": mgr_result.final_output.strip()
                }),
                "metadata_raw": json.dumps({ "reason": "Manager output parsing error" }),
                "created_at": datetime.utcnow().isoformat()
            }

        async with httpx.AsyncClient() as client:
            print("=== Webhook Dispatch ===")
            print(f"Webhook URL: {webhook}")
            print("Payload being sent:")
            print(json.dumps(payload, indent=2))
            response = await client.post(webhook, json=payload)
            print(f"Response Status: {response.status_code}")
            print(f"Response Body: {response.text}")
            print("========================")
        return {"ok": True"}

    
        try:
            parsed_mgr = json.loads(mgr_result.final_output)
            if isinstance(parsed_mgr, dict) and "route_to" in parsed_mgr:
                # Manager successfully routed to downstream agent
                agent_type = parsed_mgr["route_to"]
                agent = AGENT_MAP.get(agent_type)
                if not agent:
                    raise HTTPException(400, f"Unknown agent: {agent_type}")
    
                result = await Runner.run(agent, input=user_input)
                parsed_output = None
                is_structured = False
                try:
                    parsed_output = json.loads(result.final_output)
                    is_structured = "output_type" in parsed_output
                except Exception:
                    pass
    
                if getattr(result, "requires_user_input", None):
                    webhook = CLARIFICATION_WEBHOOK_URL
                    payload = {
                        "task_id": data.get("task_id"),
                        "user_id": data.get("user_id"),
                        "agent_type": agent_type,
                        "message": {
                            "type": "text",
                            "content": result.requires_user_input
                        },
                        "metadata": {
                            "reason": "Agent requested clarification"
                        },
                        "created_at": datetime.utcnow().isoformat()
                    }
                elif is_structured:
                    webhook = STRUCTURED_WEBHOOK_URL
                    payload = {
                        "task_id": data.get("task_id"),
                        "user_id": data.get("user_id"),
                        "agent_type": agent_type,
                        "message": parsed_output,
                        "created_at": datetime.utcnow().isoformat()
                    }
                else:
                    webhook = CLARIFICATION_WEBHOOK_URL
                    payload = {
                        "task_id": data.get("task_id"),
                        "user_id": data.get("user_id"),
                        "agent_type": agent_type,
                        "message": {
                            "type": "text",
                            "content": result.final_output
                        },
                        "metadata": {
                            "reason": "Agent returned unstructured output"
                        },
                        "created_at": datetime.utcnow().isoformat()
                    }
    
            else:
                # Manager requested clarification directly
                webhook = CLARIFICATION_WEBHOOK_URL
                payload = {
                    "task_id": data.get("task_id"),
                    "user_id": data.get("user_id"),
                    "agent_type": "manager",
                    "message": {
                        "type": "text",
                        "content": mgr_result.final_output.strip()
                    },
                    "metadata": {
                        "reason": "Manager requested clarification"
                    },
                    "created_at": datetime.utcnow().isoformat()
                }
    
        except Exception:
            # Manager returned malformed or unclear output
            webhook = CLARIFICATION_WEBHOOK_URL
            payload = {
                "task_id": data.get("task_id"),
                "user_id": data.get("user_id"),
                "agent_type": "manager",
                "message": {
                    "type": "text",
                    "content": mgr_result.final_output.strip()
                },
                "metadata": {
                    "reason": "Manager output parsing error"
                },
                "created_at": datetime.utcnow().isoformat()
            }
    
        async with httpx.AsyncClient() as client:
            print("=== Webhook Dispatch ===")
            print(f"Webhook URL: {webhook}")
            print("Payload being sent:")
            print(json.dumps(payload, indent=2))
            response = await client.post(webhook, json=payload)
            print(f"Response Status: {response.status_code}")
            print(f"Response Body: {response.text}")
            print("========================")
        return {"ok": True}

        try:
            parsed_output = json.loads(result.final_output)
            is_structured = "output_type" in parsed_output
        except Exception:
            parsed_output = None
            is_structured = False

        if getattr(result, "requires_user_input", None):
            webhook = CLARIFICATION_WEBHOOK_URL
            payload = {
                "task_id": data.get("task_id"),
                "user_id": data.get("user_id"),
                "agent_type": agent_type,
                "message": {
                    "type": "text",
                    "content": result.requires_user_input
                },
                "metadata": {"reason": "Agent requested clarification"},
                "created_at": datetime.utcnow().isoformat()
            }
        elif is_structured:
            webhook = STRUCTURED_WEBHOOK_URL
            payload = {
                "task_id": data.get("task_id"),
                "user_id": data.get("user_id"),
                "agent_type": agent_type,
                "message_type": "text",
                "message_content": result.requires_user_input if getattr(result, "requires_user_input", None) else result.final_output,
                "metadata_reason": "Agent requested clarification" if getattr(result, "requires_user_input", None) else "Auto-forwarded message",
                "created_at": datetime.utcnow().isoformat()
            }
        else:
            webhook = CLARIFICATION_WEBHOOK_URL
            payload = {
                "task_id": data.get("task_id"),
                "user_id": data.get("user_id"),
                "agent_type": agent_type,
                "message_type": "text",
                "message_content": result.requires_user_input if getattr(result, "requires_user_input", None) else result.final_output,
                "metadata_reason": "Agent requested clarification" if getattr(result, "requires_user_input", None) else "Auto-forwarded message",
                "created_at": datetime.utcnow().isoformat()
            }

        async with httpx.AsyncClient() as client:
            print("=== Webhook Dispatch ===")
            print(f"Webhook URL: {webhook}")
            print("Payload being sent:")
            print(json.dumps(payload, indent=2))
            response = await client.post(webhook, json=payload)
            print(f"Response Status: {response.status_code}")
            print(f"Response Body: {response.text}")
            print("========================")
        return {"ok": True}

    elif action == "new_message":
        user_msg = data.get("message") or data.get("user_prompt")
        if user_msg is None:
            raise HTTPException(422, "Missing 'message' or 'user_prompt'")
    
        sess = data.get("agent_session_id")
        agent_type = sess if sess in AGENT_MAP else "manager"
        agent = AGENT_MAP.get(agent_type, manager_agent)
        result = await Runner.run(agent, input=user_msg)

        try:
            parsed_output = json.loads(result.final_output)
            is_structured = "output_type" in parsed_output
        except Exception:
            parsed_output = None
            is_structured = False

        if getattr(result, "requires_user_input", None):
            webhook = CLARIFICATION_WEBHOOK_URL
            payload = {
                "task_id": data.get("task_id"),
                "user_id": data.get("user_id"),
                "agent_type": sess or "manager",
                "message": {
                    "type": "text",
                    "content": result.requires_user_input
                },
                "metadata": {"reason": "Agent requested clarification"},
                "created_at": datetime.utcnow().isoformat()
            }
        elif is_structured:
            webhook = STRUCTURED_WEBHOOK_URL
            payload = {
                "task_id": data.get("task_id"),
                "user_id": data.get("user_id"),
                "agent_type": agent_type,
                "message_type": "text",
                "message_content": result.requires_user_input if getattr(result, "requires_user_input", None) else result.final_output,
                "metadata_reason": "Agent requested clarification" if getattr(result, "requires_user_input", None) else "Auto-forwarded message",
                "created_at": datetime.utcnow().isoformat()
            }
        else:
            webhook = CLARIFICATION_WEBHOOK_URL
            payload = {
                "task_id": data.get("task_id"),
                "user_id": data.get("user_id"),
                "agent_type": agent_type,
                "message_type": "text",
                "message_content": result.requires_user_input if getattr(result, "requires_user_input", None) else result.final_output,
                "metadata_reason": "Agent requested clarification" if getattr(result, "requires_user_input", None) else "Auto-forwarded message",
                "created_at": datetime.utcnow().isoformat()
            }
        async with httpx.AsyncClient() as client:
            print("=== Webhook Dispatch ===")
            print(f"Webhook URL: {webhook}")
            print("Payload being sent:")
            print(json.dumps(payload, indent=2))
            response = await client.post(webhook, json=payload)
            print(f"Response Status: {response.status_code}")
            print(f"Response Body: {response.text}")
            print("========================")
            
        return {"ok": True}

    else:
        raise HTTPException(400, "Unknown action")
