# Prompt Injection for Structured Outputs

Some LLM providers don't natively support using tools and structured outputs simultaneously. The Agents SDK includes an opt-in prompt injection feature to work around this limitation.

!!! note

    This feature is specifically designed for models accessed via [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel], particularly **Google Gemini**. OpenAI models have native support and don't need this workaround.

## The Problem

Models like Google Gemini don't support using `tools` and `response_schema` (structured output) in the same API call. When you try:

```python
from agents import Agent, function_tool
from agents.extensions.models.litellm_model import LitellmModel
from pydantic import BaseModel

class WeatherReport(BaseModel):
    city: str
    temperature: float

@function_tool
def get_weather(city: str) -> dict:
    return {"city": city, "temperature": 22.5}

# This causes an error with Gemini
agent = Agent(
    model=LitellmModel("gemini/gemini-2.5-flash"),
    tools=[get_weather],
    output_type=WeatherReport,  # Error: can't use both!
)
```

You'll get an error like:

```
GeminiException BadRequestError - Function calling with a response mime type 
'application/json' is unsupported
```

## The Solution

Enable prompt injection by setting `enable_structured_output_with_tools=True` on the `LitellmModel`:

```python
agent = Agent(
    model=LitellmModel(
        "gemini/gemini-2.5-flash",
        enable_structured_output_with_tools=True,  # ← Enables the workaround
    ),
    tools=[get_weather],
    output_type=WeatherReport,
)
```

When enabled, the SDK:

1. Generates JSON formatting instructions from your Pydantic model.
2. Injects these instructions into the system prompt.
3. Disables the native `response_format` parameter to avoid API errors.
4. Parses the model's JSON response into your Pydantic model.

## Complete Example

```python
from __future__ import annotations

import asyncio
from pydantic import BaseModel, Field

from agents import Agent, Runner, function_tool
from agents.extensions.models.litellm_model import LitellmModel


class WeatherReport(BaseModel):
    city: str = Field(description="The city name")
    temperature: float = Field(description="Temperature in Celsius")
    conditions: str = Field(description="Weather conditions")


@function_tool
def get_weather(city: str) -> dict:
    """Get current weather for a city."""
    return {
        "city": city,
        "temperature": 22.5,
        "conditions": "sunny",
    }


async def main():
    agent = Agent(
        name="WeatherBot",
        instructions="Use the get_weather tool, then provide a structured report.",
        model=LitellmModel(
            "gemini/gemini-2.5-flash",
            enable_structured_output_with_tools=True,  # Required for Gemini
        ),
        tools=[get_weather],
        output_type=WeatherReport,
    )

    result = await Runner.run(agent, "What's the weather in Tokyo?")
    
    # Result is properly typed as WeatherReport
    report: WeatherReport = result.final_output
    print(f"City: {report.city}")
    print(f"Temperature: {report.temperature}")
    print(f"Conditions: {report.conditions}")


if __name__ == "__main__":
    asyncio.run(main())
```

## When to Use

| Model Provider | Access Via | Need `enable_structured_output_with_tools`? |
|----------------|-----------|------------------------------|
| Google Gemini | [`LitellmModel("gemini/...")`][agents.extensions.models.litellm_model.LitellmModel] | **Yes** - No native support |
| OpenAI | `"gpt-4o"` (default) | **No** - Has native support |
| Anthropic Claude | [`LitellmModel("claude-...")`][agents.extensions.models.litellm_model.LitellmModel] | **No** - Has native support |
| Other LiteLLM models | [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] | **Try without first** |

!!! tip

    If you're using [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] and getting errors when combining tools with structured outputs, set `enable_structured_output_with_tools=True`.

## How It Works

### Without Prompt Injection (Default)

The SDK uses the model's native structured output API:

```python
# API request
{
    "tools": [...],
    "response_format": {"type": "json_schema", ...}
}
```

This works for OpenAI and Anthropic models but fails for Gemini.

### With Prompt Injection

The SDK modifies the request:

```python
# API request
{
    "system_instruction": "...<injected JSON formatting instructions>...",
    "tools": [...],
    "response_format": None  # Disabled to avoid errors
}
```

The injected instructions tell the model:

- Which JSON fields to output.
- The type and description of each field.
- How to format the response (valid JSON only).

### Example Injected Instructions

For the `WeatherReport` model above, the SDK injects:

```
Provide your output as a JSON object containing the following fields:
<json_fields>
["city", "temperature", "conditions"]
</json_fields>

Here are the properties for each field:
<json_field_properties>
{
  "city": {
    "description": "The city name",
    "type": "string"
  },
  "temperature": {
    "description": "Temperature in Celsius",
    "type": "number"
  },
  "conditions": {
    "description": "Weather conditions",
    "type": "string"
  }
}
</json_field_properties>

IMPORTANT:
- Start your response with `{` and end it with `}`
- Your output will be parsed with json.loads()
- Make sure it only contains valid JSON
- Do NOT include markdown code blocks or any other formatting
```

## Debugging

Enable debug logging to see when prompt injection is active:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

Look for:

```
DEBUG: Injected JSON output prompt for structured output with tools
```

## Best Practices

1. **Use Pydantic Field descriptions**: The SDK uses these to generate better instructions.

    ```python
    class Report(BaseModel):
        # Good - includes description
        score: float = Field(description="Confidence score from 0 to 1")
        
        # Less helpful - no description
        count: int
    ```

2. **Test without prompt injection first**: Only enable it if you get errors.

3. **Use with LiteLLM models only**: OpenAI models ignore this parameter.

## Limitations

- The model must be able to follow JSON formatting instructions reliably.
- Parsing errors can occur if the model doesn't output valid JSON.
- This is a workaround, not a replacement for native API support.

## Related Documentation

- [Agents](../agents.md) - General agent configuration.
- [LiteLLM models](litellm.md) - Using any model via LiteLLM.
- [Tools](../tools.md) - Defining and using tools.
