from typing import Any

import httpx2
import pytest

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message, function_call
from examples.plugins.postman.src.agents_postman import postman

URL = "https://store.test/health"


def collection(method: str = "GET", url: str = URL) -> dict[str, Any]:
    return {
        "info": {
            "name": "Store",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": [
            {
                "name": "Health",
                "request": {
                    "method": method,
                    "url": {"raw": url},
                    "header": [{"key": "X-Api-Key", "value": "private"}],
                },
            },
            {"name": "Order", "request": {"method": "POST", "url": "https://store.test/order"}},
        ],
    }


@pytest.mark.asyncio
async def test_checks_only_approved_get_without_credentials_or_redirects() -> None:
    calls: list[httpx2.Request] = []

    def service(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(302, headers={"location": "https://other.test/"}, text="private")

    model = ScriptedModel(
        [
            [function_call("postman_list_requests", {}, call_id="list")],
            [function_call("postman_check_request", {"name": "Order"}, call_id="no")],
            [function_call("postman_check_request", {"name": "Health"}, call_id="yes")],
            [assistant_message("Done.")],
        ]
    )
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(service),
        headers={"X-Api-Key": "private"},
        cookies={"token": "private"},
        auth=("user", "private"),
        follow_redirects=True,
        trust_env=False,
    ) as client:
        checks = postman(client, collection(), approved={"Health": URL})
        agent = Agent(name="Storefront", model=model, plugins=[checks])
        result = await Runner.run(
            agent, "Check the store.", run_config=RunConfig(tracing_disabled=True)
        )
    assert [(call.method, str(call.url)) for call in calls] == [("GET", URL)]
    assert {"authorization", "cookie", "x-api-key"}.isdisjoint(calls[0].headers)
    assert [item.output for item in result.new_items if item.type == "tool_call_output_item"] == [
        '["Health"]',
        "Request not approved.",
        "HTTP 302",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "export, approved_url",
    [
        pytest.param(collection("POST"), URL, id="write"),
        pytest.param(collection(url="https://other.test"), URL, id="changed-destination"),
        pytest.param(
            collection(url="https://store.test/{{path}}"),
            "https://store.test/{{path}}",
            id="template",
        ),
        pytest.param(
            collection(url="https://user:secret@store.test"),
            "https://user:secret@store.test",
            id="credentials",
        ),
    ],
)
async def test_unsafe_approval_fails_at_registration(
    export: dict[str, Any], approved_url: str
) -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200))
    ) as client:
        with pytest.raises(ValueError):
            postman(client, export, approved={"Health": approved_url})
