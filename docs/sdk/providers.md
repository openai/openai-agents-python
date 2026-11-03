# Check a custom Responses endpoint

A custom endpoint must implement the required Responses behavior. A Chat Completions-compatible route alone is insufficient.

Test each feature your application uses against the actual endpoint and model. A successful text response does not establish tool, strict-output, image, MCP or WebSocket support. `session.capabilities` describes SDK configuration support. It does not test a remote service.

## Run text, Python tools and strict output separately

Save this complete script as `check_endpoint.py`. Set `OPENAI_BASE_URL`, `OPENAI_API_KEY` and `MODEL` for the endpoint under test. Run `python check_endpoint.py text`, then `python check_endpoint.py tools`, then `python check_endpoint.py strict`. Each run makes live requests and can incur charges.

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
| Tools | The model requested the Python function and the loop completed. | It does not cover every tool shape. |
| Strict output | The SDK requested native schema output and validated this object. | It does not prove server enforcement for every generation or support for every schema. |

A model's wrong choice can also fail a check. Inspect the service request and error before blaming protocol support. Keep data synthetic and do not log headers or credentials.

The provider context closes after the one-shot Session. Shared providers follow the same rule: close all their Sessions first.

## Check WebSockets independently

The script forces HTTP. To check WebSockets:

1. Pass the required HTTP cases first.
2. Set `supports_websockets=True` and repeat selected cases against an endpoint that declares support.
3. Verify the actual upgrade and response in trusted server logs or approved transport monitoring tools. The runtime selects the final transport. A successful run or `websockets_allowed=True` does not prove negotiation.

Record the SDK source revision, wheel version, endpoint and model settings, transport evidence and case outcomes without secrets. Repeat affected cases after changing the endpoint, model, SDK or native revision, or features. Images and additional tool combinations also need representative checks.

See [provider configuration](configuration.md), [structured output limits](configuration.md#structured-output) and [feature combinations](choosing.md#choose-feature-combinations).
