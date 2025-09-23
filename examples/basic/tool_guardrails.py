import asyncio
import json

from agents import (
    Agent,
    Runner,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrailData,
    ToolOutputGuardrailTripwireTriggered,
    function_tool,
    tool_input_guardrail,
    tool_output_guardrail,
)


@function_tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email to the specified recipient."""
    return f"Email sent to {to} with subject '{subject}'"


@function_tool
def get_user_data(user_id: str) -> dict[str, str]:
    """Get user data by ID."""
    # Simulate returning sensitive data
    return {
        "user_id": user_id,
        "name": "John Doe",
        "email": "john@example.com",
        "ssn": "123-45-6789",  # Sensitive data that should be blocked!
        "phone": "555-1234",
    }


@tool_input_guardrail
def block_sensitive_words(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Block tool calls that contain sensitive words in arguments."""
    try:
        args = json.loads(data.tool_call.arguments)
    except json.JSONDecodeError:
        return ToolGuardrailFunctionOutput(
            tripwire_triggered=False, output_info="Invalid JSON arguments"
        )

    # Check for suspicious content
    sensitive_words = [
        "password",
        "hack",
        "exploit",
        "malware",
        "orange",
    ]  # to mock sensitive words
    for key, value in args.items():
        value_str = str(value).lower()
        for word in sensitive_words:
            if word in value_str:
                return ToolGuardrailFunctionOutput(
                    tripwire_triggered=True,
                    model_message=f"🚨 Tool call blocked: contains '{word}'",
                    output_info={"blocked_word": word, "argument": key},
                )

    return ToolGuardrailFunctionOutput(tripwire_triggered=False, output_info="Input validated")


@tool_output_guardrail
def block_sensitive_output(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Block tool outputs that contain sensitive data."""
    output_str = str(data.output).lower()

    # Check for sensitive data patterns
    if "ssn" in output_str or "123-45-6789" in output_str:
        return ToolGuardrailFunctionOutput(
            tripwire_triggered=True,
            model_message="🚨 Tool output blocked: contains sensitive data",
            output_info={"blocked_pattern": "SSN", "tool": data.tool_call.name},
        )

    return ToolGuardrailFunctionOutput(tripwire_triggered=False, output_info="Output validated")


# Apply guardrails to tools
send_email.tool_input_guardrails = [block_sensitive_words]
get_user_data.tool_output_guardrails = [block_sensitive_output]

agent = Agent(
    name="Secure Assistant",
    instructions="You are a helpful assistant with access to email and user data tools.",
    tools=[send_email, get_user_data],
)


async def main():
    print("=== Tool Guardrails Example ===\n")

    # Example 1: Normal operation - should work fine
    print("1. Normal email sending:")
    try:
        result = await Runner.run(agent, "Send a welcome email to john@example.com")
        print(f"✅ Success: {result.final_output}\n")
    except Exception as e:
        print(f"❌ Error: {e}\n")

    # Example 2: Input guardrail triggers - should block suspicious content
    print("2. Attempting to send email with suspicious content:")
    try:
        result = await Runner.run(
            agent, "Send an email to john@example.com with the subject: orange"
        )
        print(f"✅ Success: {result.final_output}\n")
    except ToolInputGuardrailTripwireTriggered as e:
        print(f"🚨 Input guardrail triggered: {e.output.model_message}")
        print(f"   Details: {e.output.output_info}\n")

    # Example 3: Output guardrail triggers - should block sensitive data
    print("3. Attempting to get user data (contains SSN):")
    try:
        result = await Runner.run(agent, "Get the data for user ID user123")
        print(f"✅ Success: {result.final_output}\n")
    except ToolOutputGuardrailTripwireTriggered as e:
        print(f"🚨 Output guardrail triggered: {e.output.model_message}")
        print(f"   Details: {e.output.output_info}\n")


if __name__ == "__main__":
    asyncio.run(main())

"""
Example output:

=== Tool Guardrails Example ===

1. Normal email sending:
✅ Success: I've sent a welcome email to john@example.com with an appropriate subject and greeting message.

2. Attempting to send email with suspicious content:
🚨 Input guardrail triggered: 🚨 Tool call blocked: contains 'orange'
   Details: {'blocked_word': 'orange', 'argument': 'subject'}

3. Attempting to get user data (contains SSN):
🚨 Output guardrail triggered: 🚨 Tool output blocked: contains sensitive data
   Details: {'blocked_pattern': 'SSN', 'tool': 'get_user_data'}
"""
