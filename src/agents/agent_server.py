import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from agents import Agent, Runner, tool
from datetime import datetime
import httpx
import json
import os

# === Predefined Webhook URLs ===
STRUCTURED_WEBHOOK_URL = "https://helpmeaiai.bubbleapps.io/version-test/api/1.1/wf/openai_return_output"
CLARIFICATION_WEBHOOK_URL = "https://helpmeaiai.bubbleapps.io/version-test/api/1.1/wf/openai_chat_response"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Define Agents ===
manager_agent = Agent(
    name="Manager",
    instructions="""
You are an intelligent router for user requests.
Decide the intent behind the message: strategy, content, repurpose, feedback.
If you are unsure or need more info, ask a clarifying question instead of routing.
Respond in strict JSON like:
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
    "strategy": strategy_agent,
    "content": content_agent,
    "repurpose": repurpose_agent,
    "feedback": feedback_agent,
}

@app.post("/agent")
async def run_agent(request: Request):
    data = await request.json()
    user_input = data.get("input", "")
    user_id = data.get("user_id", "anonymous")
    linked_profile_strategy = data.get("linked_profile_strategy")
    agent_type = data.get("agent_type")  # Optional shortcut
    image_url = data.get("image_url")
    debug_info = {}

    if image_url:
        user_input += f"\nHere is the image to consider: {image_url}"

    # Step 1: If no agent_type, use Manager Agent to decide
    if not agent_type:
        manager_result = await Runner.run(manager_agent, input=user_input)
        try:
            parsed = json.loads(manager_result.final_output)
            agent_type = parsed.get("route_to")
        except Exception as e:
            return {
                "needs_clarification": True,
                "message": "Could not understand intent.",
                "debug_info": str(e)
            }

    agent = AGENT_MAP.get(agent_type)
    if not agent:
        return {"error": f"Unknown agent type: {agent_type}"}

    # Step 2: Run the selected agent
    result = await Runner.run(agent, input=user_input)
    if hasattr(result, "requires_user_input"):
        return {
            "needs_clarification": True,
            "message": result.requires_user_input,
        }

    try:
        parsed_output = json.loads(result.final_output)
        output_type = parsed_output.get("output_type")
        output_details = parsed_output.get("details")
        contains_image = parsed_output.get("contains_image", False)

        if not output_type or not output_details:
            raise ValueError("Missing required output keys")
    except Exception as e:
        parsed_output = None
        output_type = "raw_text"
        output_details = result.final_output
        contains_image = False
        debug_info["validation_error"] = str(e)
        debug_info["raw_output"] = result.final_output

    # Step 3: Format AgentSession
    session = {
        "agent_type": agent_type,
        "user_id": user_id,
        "input_details": data.get("input_details", {}),
        "output_type": output_type,
        "contains_image": contains_image,
        "output_details": output_details,
        "linked_profile_strategy": linked_profile_strategy,
        "source_content_piece": data.get("source_content_piece"),
        "created_at": datetime.utcnow().isoformat(),
    }

    if debug_info:
        session["debug_info"] = debug_info

    # Step 4: Post to correct webhook
    async with httpx.AsyncClient() as client:
        try:
            if parsed_output:
                await client.post(STRUCTURED_WEBHOOK_URL, json=session)
            else:
                await client.post(CLARIFICATION_WEBHOOK_URL, json={
                    "user_id": user_id,
                    "message": result.final_output,
                    "agent_type": agent_type,
                })
        except Exception as e:
            session["webhook_error"] = str(e)

    return session
