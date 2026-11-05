# Check a custom Responses endpoint

Use `NativeProvider` to connect the SDK to a custom Responses endpoint. Before using that endpoint in your application, check the endpoint and model against each feature the application needs. A Chat Completions-compatible route alone is insufficient.

Start with HTTP text responses, then test tools and strict output separately. A successful text response does not establish tool, strict-output, image, MCP or WebSocket support. `session.capabilities` reports whether the SDK configuration supports a feature; it does not test the remote service.

## Run text, Python tools and strict output separately

Each check makes live requests and can incur charges. Keep inputs synthetic and keep credentials out of logs.

1. [Install the preview wheel](installation.md) in the Python environment you will use for the checks.
2. Save the script below as `check_endpoint.py`.
3. Set `OPENAI_BASE_URL`, `OPENAI_API_KEY` and `MODEL` for the endpoint and model under test. The script passes the URL explicitly because the SDK does not read `OPENAI_BASE_URL` itself.
4. Run `python check_endpoint.py text`, then `python check_endpoint.py tools`, then `python check_endpoint.py strict`. Each successful check prints its mode, `passed:`, and the final output.

```python
import asyncio
import os
import sys

from pydantic import BaseModel

from agents import Agent, NativeProvider, function_tool


class Answer(BaseModel):
    label: str


async def main() -> None:
    mode = sys.argv[1]
    if mode not in {"text", "tools", "strict"}:
        raise ValueError("Choose text, tools or strict")
    calls: list[str] = []

    @function_tool
    def lookup_label() -> str:
        calls.append("lookup_label")
        return "atlas"

    async with NativeProvider(
        base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
        supports_websockets=False,
    ) as provider:
        agent = Agent(
            instructions="Follow the requested smoke check.",
            model=os.environ["MODEL"],
            provider=provider,
            tools=[lookup_label] if mode == "tools" else [],
            output_type=Answer if mode == "strict" else str,
        )
        prompt = {
            "text": "Reply with the word atlas.",
            "tools": "Call lookup_label and report its returned label.",
            "strict": "Return label atlas in the required object.",
        }[mode]
        result = await agent.run(prompt)
        if mode == "tools":
            assert calls == ["lookup_label"], calls
        elif mode == "strict":
            assert isinstance(result.final_output, Answer)
            assert result.final_output.label == "atlas"
        else:
            assert "atlas" in result.final_output.lower()
        print(mode, "passed:", result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

The checks cover only their selected cases:

| Check | What a pass establishes | Limit |
| --- | --- | --- |
| Text | The endpoint returned a response containing `atlas`. | It does not establish support for tools, structured output or other input types. |
| Tools | The model requested the Python function and the loop completed. | It does not cover every tool shape. |
| Strict output | The SDK requested native schema output and validated this object. | It does not prove server enforcement for every generation or support for every schema. |

If a check fails, inspect the assertion or service error and the corresponding request. A model can fail to follow the prompt even when the endpoint supports the protocol. Check whether the failure comes from the model response or the endpoint before deciding which feature is unsupported.

The script closes each one-shot Session before leaving the provider context. Follow the same order with shared providers: close every Session that uses the provider before closing the provider.

## Check WebSockets independently

The script forces HTTP. To check WebSockets:

1. Pass the required HTTP cases first.
2. Set `supports_websockets=True` and repeat selected cases against an endpoint that declares support.
3. Verify the actual upgrade and response in trusted server logs or approved transport monitoring tools. The runtime selects the final transport. A successful run or `websockets_allowed=True` does not prove negotiation.

Record the SDK source revision, wheel version, endpoint and model settings, transport evidence and case outcomes without secrets. Repeat affected cases after changing the endpoint, model, SDK or native revision, or features. Images and additional tool combinations also need representative checks.

See [provider configuration](configuration.md), [structured output limits](configuration.md#structured-output) and [feature combinations](choosing.md#choose-feature-combinations).
