"""Run status checks for selected requests from a Postman collection."""

import json
from collections.abc import Iterator, Mapping
from typing import Any

import httpx2

from agents.decorators import tool
from agents.plugins import Plugin


def _approved_requests(
    items: list[dict[str, Any]], approved: Mapping[str, str]
) -> Iterator[dict[str, Any]]:
    # Preserve top-level bindings even when a folder contains a request with the same name.
    nested_names = set(approved) - {item.get("name") for item in items if "request" in item}
    pending = [(items, set(approved))]
    while pending:
        group, names = pending.pop()
        for item in group:
            if "item" in item:
                if nested_names:
                    pending.append((item["item"], nested_names))
            elif item.get("name") in names:
                yield item


def postman(
    client: httpx2.AsyncClient,
    collection: Mapping[str, Any],
    *,
    approved: Mapping[str, str],
) -> Plugin[Any]:
    """Bind approved request names to fixed HTTPS GETs from a trusted v2.1 export."""
    if (
        collection["info"]["schema"]
        != "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
    ):
        raise ValueError("Expected a Postman Collection v2.1 export.")

    targets: dict[str, httpx2.URL] = {}
    for item in _approved_requests(collection["item"], approved):
        name = item["name"]
        if name in targets:
            raise ValueError("Duplicate approved request name.")
        request = item.get("request")
        if not isinstance(request, dict) or request.get("method") != "GET":
            raise ValueError("Expected a GET request with an explicit method.")
        raw = request.get("url")
        if isinstance(raw, dict):
            raw = raw.get("raw")
        if raw != approved[name]:
            raise ValueError("Request URL differs from the host's approved URL.")
        url = httpx2.URL(approved[name])
        if (
            url.scheme != "https"
            or not url.host
            or url.userinfo
            or "?" in raw
            or "#" in raw
            or "{{" in raw
            or "/:" in url.path
            or any(part in (".", "..") for part in raw.split("/"))
        ):
            raise ValueError(
                "Expected a literal HTTPS URL without credentials, query, fragment or dot segments."
            )
        targets[name] = url

    if not targets or targets.keys() != approved.keys():
        raise ValueError("Every approved name must match a request.")

    @tool
    def postman_list_requests() -> str:
        """List the Postman request names approved for status checks."""
        return json.dumps(sorted(targets))

    @tool(timeout=10)
    async def postman_check_request(name: str) -> str:
        """Check one approved request by name and return only its HTTP status code."""
        if name not in targets:
            return "Request not approved."

        # A fresh Request omits client defaults such as headers, cookies and query parameters.
        request = httpx2.Request("GET", targets[name])
        try:
            response = await client.send(request, auth=None, stream=True, follow_redirects=False)
            await response.aclose()
        except httpx2.HTTPError:
            return "Status check failed."
        return f"HTTP {response.status_code}"

    return Plugin(name="postman", tools=[postman_list_requests, postman_check_request])
