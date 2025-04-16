import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import APIRouter, Request
from agents import Agent, Runner
from datetime import datetime
import json
import httpx

router = APIRouter()

# Predefined webhook URL (set to your Bubble endpoint)
WEBHOOK_URL = "https://helpmeaiai.bubbleapps.io/version-test/api/1.1/wf/openai_profilebuilder_return"

# Define the ProfileBuilder agent
profile_builder_agent = Agent(
    name="ProfileBuilderAgent",
    instructions="""
You are a profile builder assistant.
Based on the input text and optionally any linked information, construct a structured influencer profile.

Respond in the following format:
{
  "output_type": "structured_profile",
  "contains_image": false,
  "details": {
    "profile_summary": "...",
    "prompt_snippet": {
      "tone": "...",
      "goal": "...",
      "platform": "..."
    }
  }
}
Only respond in this format.
"""
)

@router.post("/profilebuilder")
async def build_profile(request: Request):
    data = await request.json()
    user_input = data.get("input", "")
    user_id = data.get("user_id", "anonymous")
    debug_info = {}

    # Run the ProfileBuilder agent with the given user input
    result = await Runner.run(profile_builder_agent, input=user_input)

    # Clean the output in case it is wrapped in markdown code block formatting
    clean_output = result.final_output.strip()
    if clean_output.startswith("```") and clean_output.endswith("```"):
        # Remove the first and last lines (the markdown fences)
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

    # Build the profile data that will be sent to Bubble
    profile_data = {
        "user_id": user_id,
        "profile_summary_text": profile_summary,
        "profile_prompt_snippet": prompt_snippet,
        "created_at": datetime.utcnow().isoformat(),
    }

    if debug_info:
        profile_data["debug_info"] = debug_info

    # Post the profile data to your predefined Bubble webhook
    async with httpx.AsyncClient() as client:
        try:
            await client.post(WEBHOOK_URL, json=profile_data)
        except Exception as e:
            profile_data["webhook_error"] = str(e)

    return profile_data
