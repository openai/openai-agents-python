"""Check a Postman request without a network connection or model credentials."""

import asyncio

import httpx2

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message, function_call

from . import postman

collection = {
    "info": {
        "name": "Storefront",
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "item": [
        {"name": "Health", "request": {"method": "GET", "url": "https://store.test/health"}},
        {"name": "Order", "request": {"method": "POST", "url": "https://store.test/orders"}},
    ],
}


async def main() -> None:
    model = ScriptedModel(
        [
            [function_call("postman_list_requests", {}, call_id="discover")],
            [function_call("postman_check_request", {"name": "Health"}, call_id="check")],
            [assistant_message("Done.")],
        ]
    )
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200))
    async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
        checks = postman(client, collection, approved={"Health": "https://store.test/health"})
        agent = Agent(name="Storefront", model=model, plugins=[checks])
        result = await Runner.run(
            agent, "Check storefront health.", run_config=RunConfig(tracing_disabled=True)
        )
        for item in result.new_items:
            if item.type == "tool_call_output_item":
                print(item.output)


if __name__ == "__main__":
    asyncio.run(main())
