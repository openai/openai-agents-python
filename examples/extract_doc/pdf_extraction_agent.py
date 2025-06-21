#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "openai>=1.12.0",
#     "openai-agents",
# ]
# ///

"""
Example of creating an Agent that extracts information from a PDF document
using the input_file content option of the OpenAI responses API.
"""

import asyncio
import base64
import json
import os
import sys
from typing import Any, Dict, List

try:
    from agents import Agent, Runner, set_default_openai_api
except ImportError:
    print("Required packages not found. Please run this script with uv:")
    print("uv run examples/extract_doc/pdf_extraction_agent.py")
    sys.exit(1)


async def extract_data_from_pdf(agent: Agent, pdf_path: str) -> Dict[str, Any]:
    """
    Extract structured data from a PDF document using the OpenAI responses API.
    
    Args:
        agent: The agent to use for extraction
        pdf_path: Path to the PDF file
        
    Returns:
        Extracted structured data from the PDF
    """
    # Read the PDF file and encode it as base64
    with open(pdf_path, "rb") as f:
        pdf_data = f.read()
    
    pdf_base64 = base64.b64encode(pdf_data).decode("utf-8")
    pdf_name = os.path.basename(pdf_path)
    
    # Define the extraction schema - modify this based on what you want to extract
    extraction_schema = {
        "title": "string",
        "authors": ["string"],
        "publication_date": "string",
        "abstract": "string",
        "sections": [
            {
                "heading": "string",
                "content": "string"
            }
        ],
        "tables": [
            {
                "caption": "string",
                "data": [["string"]]
            }
        ],
        "figures": [
            {
                "caption": "string",
                "description": "string"
            }
        ],
        "references": ["string"]
    }
    
    # Create the input with the PDF file
    input_with_pdf = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Extract the following information from the PDF document in a structured format:\n"
                        f"{json.dumps(extraction_schema, indent=2)}\n\n"
                        "Return the extracted data as a JSON object that follows this schema exactly."
                    )
                },
                {
                    "type": "input_file",
                    "filename": pdf_name,
                    "file_data": f"data:application/pdf;base64,{pdf_base64}"
                }
            ]
        }
    ]
    
    # Run the agent with the PDF input
    result = await Runner.run(agent, input=input_with_pdf)
    
    # Extract the JSON response
    response_text = result.final_output
    
    # Parse the JSON from the response text
    # This handles cases where the model might include markdown code blocks
    json_str = extract_json_from_text(response_text)
    
    try:
        extracted_data = json.loads(json_str)
        return extracted_data
    except json.JSONDecodeError:
        print("Failed to parse JSON response. Raw response:")
        print(response_text)
        return {"error": "Failed to parse response"}


def extract_json_from_text(text: str) -> str:
    """
    Extract JSON string from text that might contain markdown or other formatting.
    """
    # Check if the text contains a code block
    if "```json" in text:
        # Extract content between ```json and ```
        start = text.find("```json") + 7
        end = text.find("```", start)
        return text[start:end].strip()
    elif "```" in text:
        # Extract content between ``` and ```
        start = text.find("```") + 3
        end = text.find("```", start)
        return text[start:end].strip()
    
    # If no code block, try to find JSON object directly
    # Look for the first { and the last }
    start = text.find("{")
    end = text.rfind("}") + 1
    
    if start >= 0 and end > start:
        return text[start:end].strip()
    
    # If all else fails, return the original text
    return text


# Add a verification function to check if the extraction was successful
async def verify_extraction(agent: Agent, pdf_path: str, extracted_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Verify if the extracted data is grounded in the PDF content.
    
    Args:
        agent: The agent to use for verification
        pdf_path: Path to the PDF file
        extracted_data: The extracted data to verify
        
    Returns:
        Verification results
    """
    # Read the PDF file and encode it as base64
    with open(pdf_path, "rb") as f:
        pdf_data = f.read()
    
    pdf_base64 = base64.b64encode(pdf_data).decode("utf-8")
    pdf_name = os.path.basename(pdf_path)
    
    # Create the input with the PDF file and extracted data
    input_with_pdf = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Check if the following extracted data is grounded in the PDF content:\n\n"
                        f"Extracted data:\n{json.dumps(extracted_data, indent=2)}\n\n"
                        "Return a JSON object with the following structure:\n"
                        "{ \"is_grounded\": boolean, \"ungrounded_items\": [{ \"path\": \"path.to.item\", \"value\": \"extracted value\", \"issue\": \"description of issue\" }] }"
                    )
                },
                {
                    "type": "input_file",
                    "filename": pdf_name,
                    "file_data": f"data:application/pdf;base64,{pdf_base64}"
                }
            ]
        }
    ]
    
    # Run the agent with the PDF input
    result = await Runner.run(agent, input=input_with_pdf)
    
    # Extract the JSON response
    response_text = result.final_output
    json_str = extract_json_from_text(response_text)
    
    try:
        verification_result = json.loads(json_str)
        return verification_result
    except json.JSONDecodeError:
        print("Failed to parse verification JSON. Raw response:")
        print(response_text)
        return {"error": "Failed to parse verification response"}


# Example usage with verification
async def extract_and_verify():
    # Set up the agent
    set_default_openai_api("responses")
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        raise ValueError("Please set the OPENAI_API_KEY environment variable")
    
    # Use the sample document created by the other script
    current_dir = os.path.dirname(os.path.abspath(__file__))
    pdf_path = os.path.join(current_dir, "sample_document.pdf")
    
    if not os.path.exists(pdf_path):
        print(f"Sample PDF not found at {pdf_path}")
        print("Please run the sample_document.py script first:")
        print("uv run examples/extract_doc/sample_document.py")
        return None, None
    
    pdf_agent = Agent(
        name="PDF Processing Agent",
        instructions="An agent that extracts and verifies information from PDF documents.",
        model="gpt-4o",
    )
    
    # Extract data
    print("Extracting data from PDF...")
    extracted_data = await extract_data_from_pdf(pdf_agent, pdf_path)
    print("Extracted data:")
    print(json.dumps(extracted_data, indent=2))
    
    # Verify extraction
    print("\nVerifying extraction...")
    verification = await verify_extraction(pdf_agent, pdf_path, extracted_data)
    print("Verification results:")
    print(json.dumps(verification, indent=2))
    
    return extracted_data, verification


if __name__ == "__main__":
    asyncio.run(extract_and_verify())
