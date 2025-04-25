import asyncio
import os
import json
import requests
from openai import AsyncOpenAI
from dataclasses import dataclass
from agents import Agent, Runner, set_tracing_disabled
from agents.models.openai_responses import OpenAIResponsesModel, Converter
from agents.tool import FunctionTool, FileSearchTool
from typing import Dict, Any

"""
This example demonstrates how to create an agent that uses the built-in agentic_search tool 
to perform RAG-based search queries using the OpenResponses API.
"""

# Patch the Converter's _convert_tool method to handle our custom tool type
_original_convert_tool = Converter._convert_tool

def patched_convert_tool(tool):
    if isinstance(tool, AgenticSearchTool):
        # Create a tool definition in the format expected by the API
        tool_def = {
            "type": "agentic_search",
            "vector_store_ids": tool.vector_store_ids,
            "max_num_results": tool.max_num_results,
            "max_iterations": tool.max_iterations,
            "seed_strategy": tool.seed_strategy,
            "alpha": tool.alpha,
            "initial_seed_multiplier": tool.initial_seed_multiplier,
            "filters": tool.filters
        }
        return tool_def, None
    return _original_convert_tool(tool)

# Apply the patch
Converter._convert_tool = patched_convert_tool

# Define a local version of the tool to avoid import issues
@dataclass(init=False)
class AgenticSearchTool(FunctionTool):
    tool_name: str
    vector_store_ids: list
    max_num_results: int
    max_iterations: int
    seed_strategy: str
    alpha: float
    initial_seed_multiplier: int
    filters: dict

    def __init__(self, tool_name, **kwargs):
        # Store the provided tool name and attributes
        self.tool_name = tool_name
        self.name = tool_name
        self.description = tool_name
        # Leave the parameters schema empty
        self.params_json_schema = {}
        # Set a fixed, precomputed result
        self.precomputed_result = "Nothing to return"
        # Set the on_invoke_tool callback to always return the fixed result
        self.on_invoke_tool = lambda ctx, input: self.precomputed_result
        self.strict_json_schema = True
        
        # Set any additional attributes passed as kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)

BASE_URL = os.getenv("OPEN_RESPONSES_URL") or "http://localhost:8080/v1"
API_KEY = os.getenv("OPENAI_API_KEY") # throw error if not set
if not API_KEY:
    raise ValueError("Please set the OPENAI_API_KEY environment variable.")
MODEL_NAME = "openai@gpt-4.1-mini"  # You can change this to your preferred model

custom_headers = {
    "Authorization": f"Bearer {API_KEY}"
}

client = AsyncOpenAI(
    base_url=BASE_URL,
    api_key=API_KEY,
    default_headers=custom_headers
)

set_tracing_disabled(disabled=False)

# Create the agentic_search tool with additional configuration parameters
agentic_search_tool = AgenticSearchTool(
    tool_name="agentic_search",
    vector_store_ids=[],
    max_num_results=5,
    max_iterations=10,
    seed_strategy="hybrid",
    alpha=0.7,
    initial_seed_multiplier=3,
    filters={
        "type": "and",
        "filters": [
            {
                "type": "eq",
                "key": "category",
                "value": "documentation"
            },
            {
                "type": "eq",
                "key": "language",
                "value": "en"
            }
        ]
    }
)

rag_agent = Agent(
    name="RAG Search Agent",
    instructions=(
        "You are a research assistant that uses RAG-based search. "
        "When given a query, perform a search against the document vector store and provide a comprehensive analysis "
        "based on the retrieved information."
    ),
    tools=[agentic_search_tool],
    model=OpenAIResponsesModel(model=MODEL_NAME, openai_client=client)
)

def upload_file(file_path):
    """
    Upload a file to the OpenResponses API.
    
    Args:
        file_path: Path to the file to upload
        
    Returns:
        file_id: ID of the uploaded file
    """
    url = f"{BASE_URL}/files"
    headers = {"Authorization": f"Bearer {API_KEY}"}
    
    with open(file_path, "rb") as f:
        files = {"file": f}
        data = {"purpose": "user_data"}
        
        response = requests.post(url, headers=headers, files=files, data=data)
        response_json = response.json()
        
        print(f"File uploaded: {response_json}")
        return response_json.get("id")

def create_vector_store(name):
    """
    Create a vector store in the OpenResponses API.
    
    Args:
        name: Name of the vector store
        
    Returns:
        vector_store_id: ID of the created vector store
    """
    url = f"{BASE_URL}/vector_stores"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
    }
    
    data = json.dumps({"name": name})
    
    response = requests.post(url, headers=headers, data=data)
    response_json = response.json()
    
    print(f"Vector store created: {response_json}")
    return response_json.get("id")

def add_file_to_vector_store(vector_store_id, file_id, category="documentation", language="en"):
    """
    Add a file to a vector store in the OpenResponses API.
    
    Args:
        vector_store_id: ID of the vector store
        file_id: ID of the file to add
        category: Category attribute for the file
        language: Language attribute for the file
    """
    url = f"{BASE_URL}/vector_stores/{vector_store_id}/files"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
    }
    
    data = {
        "file_id": file_id,
        "chunking_strategy": {
            "type": "static",
            "static": {
                "max_chunk_size_tokens": 1000,
                "chunk_overlap_tokens": 200
            }
        },
        "attributes": {
            "category": category,
            "language": language
        }
    }
    
    response = requests.post(url, headers=headers, data=json.dumps(data))
    print(f"File added to vector store: {response.json()}")

def setup_rag_system(file_path, vector_store_name):
    """
    Set up the RAG system by uploading a file, creating a vector store, and adding the file to it.
    
    Args:
        file_path: Path to the file to upload
        vector_store_name: Name for the vector store
        
    Returns:
        vector_store_id: ID of the created vector store
    """
    # Upload file
    file_id = upload_file(file_path)
    
    # Create vector store
    vector_store_id = create_vector_store(vector_store_name)
    
    # Add file to vector store
    add_file_to_vector_store(vector_store_id, file_id)
    
    return vector_store_id

async def call_direct_api(vector_store_id, query):
    """
    Demonstrate calling the agentic_search API directly without the agent framework
    
    Args:
        vector_store_id: ID of the vector store to search
        query: The search query
        
    Returns:
        The response from the API
    """
    url = f"{BASE_URL}/responses"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
    }
    
    payload = {
        "model": MODEL_NAME,
        "tools": [{
            "type": "agentic_search",
            "vector_store_ids": [vector_store_id],
            "max_num_results": 5,
            "max_iterations": 10,
            "seed_strategy": "hybrid",
            "alpha": 0.7,
            "initial_seed_multiplier": 3,
            "filters": {
                "type": "and",
                "filters": [
                    {
                        "type": "eq",
                        "key": "category",
                        "value": "documentation"
                    },
                    {
                        "type": "eq",
                        "key": "language",
                        "value": "en"
                    }
                ]
            }
        }],
        "input": query,
        "instructions": "Search for the answer to the query using the agentic_search tool."
    }
    
    response = requests.post(url, headers=headers, data=json.dumps(payload))
    return response.json()

async def main():
    # Set up the RAG system with our sample document
    file_path = os.path.join(os.path.dirname(__file__), "sample_document.txt")
    vector_store_name = "ml-documentation"
    
    
    print("Setting up new vector store...")
    vector_store_id = setup_rag_system(file_path, vector_store_name)
    print(f"Created vector store with ID: {vector_store_id}")
    print(f"Update VECTOR_STORE_ID in the script with this value for future runs.")
    
    # Option 1: Use the agent framework with our custom AgenticSearchTool
    # Update the agent's tool with the new vector store ID
    global agentic_search_tool
    agentic_search_tool = AgenticSearchTool(
        tool_name="agentic_search",
        vector_store_ids=[vector_store_id],
        max_num_results=5,
        max_iterations=10,
        seed_strategy="hybrid",
        alpha=0.7,
        initial_seed_multiplier=3,
        filters={
            "type": "and",
            "filters": [
                {
                    "type": "eq",
                    "key": "category",
                    "value": "documentation"
                },
                {
                    "type": "eq",
                    "key": "language",
                    "value": "en"
                }
            ]
        }
    )
    
    global rag_agent
    rag_agent = Agent(
        name="RAG Search Agent",
        instructions=(
            "You are a research assistant that uses RAG-based search. "
            "When given a query, perform a search against the document vector store and provide a comprehensive analysis "
            "based on the retrieved information."
        ),
        tools=[agentic_search_tool],
        model=OpenAIResponsesModel(model=MODEL_NAME, openai_client=client)
    )
    
    # Run a query using the RAG agent with AgenticSearchTool
    print("\nOption 1: Running agentic search query using agent framework...")
    query = "What are the three types of machine learning and their key differences?"
    result = await Runner.run(rag_agent, input=query)
    print("\nFinal output:", result.final_output)
    
    # Option 2: Use direct API call
    print("\nOption 2: Running agentic search query using direct API call...")
    api_result = await call_direct_api(vector_store_id, query)
    print("\nDirect API response:", api_result.get("output", "No output returned"))
    
    # Option 3: Use the built-in FileSearchTool
    # Create a FileSearchTool instance
    file_search_tool = FileSearchTool(
        vector_store_ids=[vector_store_id],
        max_num_results=5,
        include_search_results=True,
        filters={
            "type": "and",
            "filters": [
                {
                    "type": "eq",
                    "key": "category",
                    "value": "documentation"
                },
                {
                    "type": "eq",
                    "key": "language",
                    "value": "en"
                }
            ]
        }
    )
    
    # Create an agent with the FileSearchTool
    file_search_agent = Agent(
        name="File Search Agent",
        instructions=(
            "You are a research assistant that uses file search. "
            "When given a query, perform a search against the document vector store and provide a comprehensive analysis "
            "based on the retrieved information."
        ),
        tools=[file_search_tool],
        model=OpenAIResponsesModel(model=MODEL_NAME, openai_client=client)
    )
    
    # Run a query using the file search agent
    print("\nOption 3: Running search query using built-in FileSearchTool...")
    file_search_result = await Runner.run(file_search_agent, input=query)
    print("\nFileSearchTool output:", file_search_result.final_output)

if __name__ == "__main__":
    asyncio.run(main()) 