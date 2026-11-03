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

Passing the tools check proves that the selected model requested the Python function and the loop completed. Passing the strict check shows that the SDK can request native schema output and validate the returned object for this small schema. It does not independently prove the service enforces the schema for every generation. Neither check covers every allowed tool or schema shape. The model can also make a wrong choice and fail a check. Inspect the service request and error before attributing a failure to protocol support. Keep prompts and results synthetic and avoid logging headers or credentials.

The provider context closes after the one-shot run closes its Session. When sharing a provider, close all Sessions before closing the provider.

## Check WebSockets independently

The script forces HTTP. After the required HTTP checks pass, run the same selected cases with `supports_websockets=True` against an endpoint that declares WebSocket support. The runtime still selects the final transport. Verify an actual WebSocket upgrade and response in the endpoint's trusted server logs or with your approved transport monitoring tools. A successful Agent run or `websockets_allowed=True` alone does not prove WebSocket negotiation.

Record the SDK source revision, wheel version, endpoint and model configuration, transport evidence and each case outcome. Do not put secrets in that record. Repeat the relevant cases after changing the endpoint, model, SDK revision, native revision or feature set. Test WebSockets, images and additional tool combinations with representative cases of their own. This script cannot establish compatibility for features it does not test.

See [provider configuration](configuration.md), [structured output limits](configuration.md#structured-output) and [feature combinations](choosing.md#choose-feature-combinations).
