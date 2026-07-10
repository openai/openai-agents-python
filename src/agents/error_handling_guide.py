"""Error handling guide and best practices for agents.

Comprehensive documentation about error handling in the Agents SDK.
"""

__doc__ = """
Error Handling in OpenAI Agents SDK
====================================

This module documents how to handle different error types in the Agents SDK.

Error Types
-----------

1. **ModelRefusalError**
   - Raised when the model refuses to produce output
   - Usually due to safety guidelines or guardrails
   - Includes the refusal reason in the `refusal` attribute

2. **MaxTurnsExceeded**
   - Raised when the agent exceeds maximum turns
   - Indicates the agent needs more iterations than allowed
   - Check the configuration to increase max_turns if needed

3. **ToolTimeoutError**
   - Raised when a tool takes too long to execute
   - Includes tool name and timeout duration
   - May indicate performance issues or network problems

4. **InputGuardrailTripwireTriggered**
   - Raised when input guardrails reject the input
   - Contains guardrail result details
   - Review guardrail configuration

5. **OutputGuardrailTripwireTriggered**
   - Raised when output guardrails reject the output
   - Contains guardrail result details
   - May indicate safety concerns with output

Best Practices
--------------

1. **Catch Specific Exceptions**
   - Catch specific error types rather than generic Exception
   - This allows targeted error handling and recovery

2. **Log Error Context**
   - Always log the full error and context
   - Use the get_user_friendly_error_message() helper for user-facing errors

3. **Implement Retry Logic**
   - Use the retry utilities for transient failures
   - Configure appropriate backoff strategies

4. **User-Friendly Messages**
   - Use get_user_friendly_error_message() to convert errors to user-friendly text
   - Hide implementation details from end users

Example Usage
-------------

    from openai.agents import Agent
    from openai.agents.exceptions import ModelRefusalError, MaxTurnsExceeded
    from openai.agents.exceptions import get_user_friendly_error_message

    agent = Agent(model="gpt-4")
    
    try:
        result = agent.run("Prompt here")
    except ModelRefusalError as e:
        print(f"Model refused: {e.refusal}")
        user_msg = get_user_friendly_error_message(e)
        print(f"Tell user: {user_msg}")
    except MaxTurnsExceeded:
        print("Agent needs more iterations")
    except Exception as e:
        user_msg = get_user_friendly_error_message(e)
        print(f"Error: {user_msg}")
"""
