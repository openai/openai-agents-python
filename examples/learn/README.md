# Learning Scripts

Welcome to the progressive learning scripts for the OpenAI Agents Python SDK!

These examples are designed to teach you the internals of the SDK, moving from basic usage to advanced features like MCP and Session persistence.

## Compatibility Notes (Non-OpenAI Backends)

The SDK defaults to using the OpenAI Responses API. If you are using an OpenAI-compatible backend like DeepSeek, Qwen, or vLLM, you may encounter a few common differences:

1. **Strict Structured Outputs:** By default, many non-OpenAI backends do not support the strict `response_format={"type":"json_schema","strict":true}` feature. If you use Pydantic outputs, they might throw a 400 error. The workaround is to use a standard function tool that accepts JSON, and manually validate with Pydantic.
2. **Tracing:** When using non-OpenAI backends, you **must** call `set_tracing_disabled(True)`. If you do not, the SDK will silently attempt to retry the OpenAI trace endpoint, which will fail.
3. **OpenAIChatCompletionsModel:** For backends that don't support the Responses API, explicitly initialize the `OpenAIChatCompletionsModel` rather than relying on the default model wrapper.

Keep these in mind as you run through the examples!
