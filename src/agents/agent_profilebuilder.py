import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import APIRouter, Request
from agents import Agent, Runner
from agents.tool import Tool  # Updated: import the Tool class
from datetime import datetime
import json
import httpx

router = APIRouter()

# Predefined webhook URL (set to your Bubble endpoint)
WEBHOOK_URL = "https://helpmeaiai.bubbleapps.io/version-test/api/1.1/wf/openai_profilebuilder_return"

# Define the ProfileBuilder agent with web search capability,
# now using a Tool instance with the expected attributes.
profile_builder_agent = Agent(
    name="ProfileBuilderAgent",
    instructions="""
You are a profile builder assistant with web search capability.
Based on the input text and any optionally linked external information, perform a web search for publicly available details about the influencer using the provided web search tool. Use any relevant data you find to enrich the influencer's profile.
Then, construct a structured influencer profile that includes a concise profile summary and a prompt snippet with key characteristics.

Respond in the following format:
{
  "output_type": "structured_profile",
  "contains_image": false,
  "details": {
    "profile_summary": "A concise summary of the influencer that includes details from web search if applicable.",
    "prompt_snippet": {
      "tone": "The influencer's style (e.g., playful, professional, authentic)",
      "goal": "Key content goals (e.g., brand storytelling, engagement)",
      "platform": "Primary platform (e.g., Instagram)"
    }
  }
}
Only reply in this format.
""",
    tools=[Tool(name="web_search_preview", search_context_size="low")]
)

@router.post("/profilebuilder")
async def build_profile(request: Request):
    data = await request.json()
    user_input = data.get("input", "")
    user_id = data.get("user_id", "anonymous")
    debug_info = {}

    # Run the ProfileBuilder agent with the given user input.
    result = await Runner.run(profile_builder_agent, input=user_input)

    # Clean the output in case it is wrapped in markdown code block formatting.
    clean_output = result.final_output.strip()
    if clean_output.startswith("```") and clean_output.endswith("```"):
        clean_output = clean_output.split("\n", 1)[-1].rsplit("\n", 1)[0]

    try:
        parsed_output = json.loads(clean_output)
        output_type = parsed_output.get("output_type")
        output_details = parsed_output.get("details")
        contains_image = parsed_output.get("contains_image", False)

        profile_summary = output_details.get("profile_summary")
        prompt_snippet = output_details.get("prompt_snippet")
    except Exception as e:
        output_type = "raw_text"
        profile_summary = result.final_output
        prompt_snippet = {}
        contains_image = False
        debug_info["validation_error"] = str(e)
        debug_info["raw_output"] = result.final_output

    profile_data = {
        "user_id": user_id,
        "profile_summary_text": profile_summary,
        "profile_prompt_snippet": prompt_snippet,
        "created_at": datetime.utcnow().isoformat(),
    }

    if debug_info:
        profile_data["debug_info"] = debug_info

    async with httpx.AsyncClient() as client:
        try:
            await client.post(WEBHOOK_URL, json=profile_data)
        except Exception as e:
            profile_data["webhook_error"] = str(e)

    return profile_data
