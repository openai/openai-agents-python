import unittest

import httpx2
from agents_postman import postman

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message, function_call

URL = "https://store.test/health"


def collection(*items):
    return {
        "info": {
            "name": "Store",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": list(items),
    }


def health(url=URL, method="GET"):
    return {"name": "Health", "request": {"method": method, "url": url}}


def folder(*items):
    return {"name": "Checks", "item": list(items)}


class PostmanTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_request_uses_only_the_approved_url(self):
        calls = []

        def service(request):
            calls.append(request)
            return httpx2.Response(302, headers={"location": "https://other.test/"}, text="private")

        check = health(
            {
                "raw": URL,
                "protocol": "https",
                "host": ["store", "test"],
                "path": ["health"],
                "query": [{"key": "token", "value": "{{secret}}", "disabled": True}],
            }
        )
        check["request"]["header"] = [{"key": "X-Api-Key", "value": "private"}]
        write = {"name": "Order", "request": {"method": "POST", "url": "{{store}}/orders"}}
        export = collection(folder(folder(check), write))
        model = ScriptedModel(
            [
                [function_call("postman_list_requests", {}, call_id="list")],
                [function_call("postman_check_request", {"name": "Order"}, call_id="deny")],
                [function_call("postman_check_request", {"name": "Health"}, call_id="check")],
                [assistant_message("Done.")],
            ]
        )
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(service),
            auth=("user", "private"),
            headers={"X-Api-Key": "private"},
            cookies={"token": "private"},
            params={"token": "private"},
            follow_redirects=True,
            trust_env=False,
        ) as client:
            checks = postman(client, export, approved={"Health": URL})
            result = await Runner.run(
                Agent("Store", model=model, plugins=[checks]),
                "Check the store.",
                run_config=RunConfig(tracing_disabled=True),
            )
        model.assert_complete()
        self.assertEqual([(call.method, str(call.url)) for call in calls], [("GET", URL)])
        self.assertTrue({"authorization", "cookie", "x-api-key"}.isdisjoint(calls[0].headers))
        self.assertEqual(
            [item.output for item in result.new_items if item.type == "tool_call_output_item"],
            ['["Health"]', "Request not approved.", "HTTP 302"],
        )

    async def test_top_level_request_keeps_precedence(self):
        calls = []

        def service(request):
            calls.append(str(request.url))
            return httpx2.Response(200)

        root = "https://Store.test:443/My health%3F%23"
        export = collection(folder(health()), health(root))
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(service)) as client:
            with self.assertRaises(ValueError):
                postman(client, export, approved={"Health": URL})
            checks = postman(client, export, approved={"Health": root})
            model = ScriptedModel(
                [
                    [function_call("postman_check_request", {"name": "Health"}, call_id="check")],
                    [assistant_message("Done.")],
                ]
            )
            await Runner.run(
                Agent("Store", model=model, plugins=[checks]),
                "Check the store.",
                run_config=RunConfig(tracing_disabled=True),
            )
        self.assertEqual(calls, ["https://store.test/My%20health%3F%23"])

    async def test_ambiguous_or_unsafe_approvals_fail_at_setup(self):
        async with httpx2.AsyncClient(trust_env=False) as client:
            for case, items, approved_url in [
                ("duplicate at root", [health(), health()], URL),
                ("duplicate in folders", [folder(health()), folder(folder(health()))], URL),
                ("mismatch", [folder(health("https://other.test/"))], URL),
                ("write", [folder(health(method="POST"))], URL),
                ("missing raw URL", [folder(health({"host": "store.test"}))], URL),
                ("query", [folder(health(URL + "?secret=1"))], URL + "?secret=1"),
                ("template", [folder(health(URL + "/{{id}}"))], URL + "/{{id}}"),
                ("empty query", [health(URL + "?")], URL + "?"),
                ("empty fragment", [health(URL + "#")], URL + "#"),
                ("changed path", [health(URL + "/../other")], URL + "/../other"),
            ]:
                with self.subTest(case=case), self.assertRaises(ValueError):
                    postman(client, collection(*items), approved={"Health": approved_url})
