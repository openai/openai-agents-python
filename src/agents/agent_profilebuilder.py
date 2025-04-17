import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import APIRouter, Request
from agents import Agent, Runner
from agents.tool import WebSearchTool
from datetime import datetime
import json
import httpx

router = APIRouter()

# Predefined Bubble webhook URL
WEBHOOK_URL = "https://helpmeaiai.bubbleapps.io/version-test/api/1.1/wf/openai_profilebuilder_return"

# ProfileBuilder agent skeleton; tools will be set per-request for dynamic locale/fallback
profile_builder_agent = Agent(
    name="ProfileBuilderAgent",
    instructions="""
You are a profile builder assistant with web search capability.

You will receive a set of key-value inputs (e.g., profile_uuid, handle URL, etc.).
Your job:
1. Use the provided fields (including fallback follower count if given).
2. If a locale is provided, use it to tailor the web search tool's user_location.
3. Perform web searches and reasoning to determine follower_count, posting_style, industry, engagement_rate, and any notable public context.
4. Summarize this into JSON as follows:
{
  "output_type": "structured_profile",
  "contains_image": false,
  "details": {
    "profile_uuid": "...",
    "summary": "Concise profile summary...",
    "prompt_snippet": { "tone": "...", "goal": "...", "platform": "..." },
    "follower_count": 12345,
    "posting_style": "...",
    "industry": "...",
    "engagement_rate": "...",
    "additional_context": "..."
  }
}
Only return JSON with exactly these fields—no markdown or commentary.
""",
    tools=[]
)

@router.post("/profilebuilder")
async def build_profile(request: Request):
    data = await request.json()
    # Extract core identifiers and optional fallbacks
    profile_uuid = data.pop("profile_uuid", None)
    provided_fc = data.pop("provided_follower_count", None)
    locale_text = data.pop("locale", None)

    # Build tool list dynamically based on locale
    user_loc = {"type": "approximate", "region": locale_text} if locale_text else None
    tools = [WebSearchTool(user_location=user_loc, search_context_size="low")]
    profile_builder_agent.tools = tools

    # Flatten remaining inputs into prompt lines
    prompt_lines = []
    for key, val in data.items():
        if val not in (None, "", "null"):
            prompt_lines.append(f"{key}: {val}")
    if provided_fc is not None:
        prompt_lines.append(f"Provided follower count: {provided_fc}")

    # Construct the agent prompt
    agent_input = f"Profile UUID: {profile_uuid}\n" + "\n".join(prompt_lines)

    # Invoke the agent
    result = await Runner.run(profile_builder_agent, input=agent_input)

    # Clean markdown fences
    output = result.final_output.strip()
    if output.startswith("```") and output.endswith("```"):
        output = output.split("\n", 1)[-1].rsplit("\n", 1)[0]

    # Parse agent JSON response
    try:
        parsed = json.loads(output)
        details = parsed.get("details", {})
    except Exception:
        details = {}

    # Build profile_data payload dynamically
    profile_data = {"profile_uuid": profile_uuid}
    for k, v in details.items():
        profile_data[k] = v
    profile_data["created_at"] = datetime.utcnow().isoformat()

    # Post to Bubble webhook
    async with httpx.AsyncClient() as client:
        try:
            await client.post(WEBHOOK_URL, json=profile_data)
        except Exception as e:
            profile_data["webhook_error"] = str(e)

    return profile_data
