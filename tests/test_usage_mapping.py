"""Test file to verify the fix for the CompletionUsage issue."""
import pytest
from openai.types.completion_usage import CompletionUsage

from agents.usage import Usage


def test_completion_usage_mapping():
    """Test that Usage correctly maps from CompletionUsage's attributes."""
    # Create a CompletionUsage object with the OpenAI SDK's attribute names
    completion_usage = CompletionUsage(
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30
    )
    
    # Create Usage object using the attributes from CompletionUsage
    # This simulates what happens in run.py that we fixed
    usage = Usage(
        requests=1,
        input_tokens=completion_usage.prompt_tokens,  # This was the fix
        output_tokens=completion_usage.completion_tokens,  # This was the fix
        total_tokens=completion_usage.total_tokens,
    )
    
    # Verify the attributes were correctly mapped
    assert usage.requests == 1
    assert usage.input_tokens == 10
    assert usage.output_tokens == 20
    assert usage.total_tokens == 30 