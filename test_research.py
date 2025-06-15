#!/usr/bin/env python3
"""
Test script for the multi-agent web research system.
This script runs the research system with a sample query without requiring interactive input.
"""

import asyncio
import os
import sys

# Add the project root to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from examples.multi_agent_web_research.manager import ResearchManager


async def main():
    # Sample research query
    query = "What are the best practices for building AI agents with OpenAI's GPT models?"
    
    print(f"🔍 Starting research on: {query}")
    print("=" * 80)
    
    try:
        manager = ResearchManager()
        await manager.run(query)
    except Exception as e:
        print(f"❌ Error occurred: {e}")
        print("\nNote: This example requires:")
        print("1. OPENAI_API_KEY environment variable to be set")
        print("2. Internet connection for web search")
        print("3. All dependencies installed (pip install -e .)")


if __name__ == "__main__":
    asyncio.run(main())
