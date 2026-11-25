# Status checks from a Postman collection

Give an agent two tools for unauthenticated HTTPS checks. Select each request from a [Postman Collection v2.1](https://schema.postman.com/collection/json/v2.1.0/draft-07/docs/index.html) by name and exact URL in application code.

| Tool | Model input | Result |
| --- | --- | --- |
| `postman_list_requests` | `{}` | Sorted names as JSON, such as `["Health"]` |
| `postman_check_request` | `{"name": "Health"}` | An HTTP status, `Request not approved.`, or `Status check failed.` |

A name outside the allowlist returns `Request not approved.` without sending a request. An HTTP response returns its status, including `HTTP 302` and `HTTP 503`. An HTTP client error, such as a failed TLS handshake, returns `Status check failed.`.

## Try it offline

Install the [v0 preview wheel](../README.md#install-the-preview) with Python 3.10 or later. From this directory, run the [example](src/agents_postman/__main__.py) in the same environment.

```sh
python -m pip install .
python -m agents_postman
```

Expected output with a scripted model and mock HTTP transport

```text
["Health"]
HTTP 200
```

## Check a saved request

Load a JSON export or fetch the collection in your application. Use this exact `info.schema` value.

```json
{
  "info": {
    "name": "Storefront",
    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  "item": [{
    "name": "Store",
    "item": [{
      "name": "Health",
      "request": {"method": "GET", "url": "https://store.test/health"}
    }]
  }]
}
```

Replace `store.test` with your reviewed endpoint in both the collection and the allowlist. For `Health` inside the `Store` folder, use `Health` as the key.

```python
import json
from pathlib import Path

import httpx2

from agents import Agent, RunConfig, Runner
from agents_postman import postman


async def check(collection: dict, run_config: RunConfig):
    async with httpx2.AsyncClient(trust_env=False) as client:
        checks = postman(client, collection, approved={"Health": "https://store.test/health"})
        agent = Agent(name="Storefront", plugins=[checks])
        return await Runner.run(agent, "Check storefront health.", run_config=run_config)


async def check_file(path: Path, run_config: RunConfig):
    return await check(json.loads(path.read_text(encoding="utf-8")), run_config)
```

### Fetch a collection from Postman

For [Get a collection](https://learning.postman.com/api-docs/api-reference/collections/get-collection/), set `POSTMAN_COLLECTION_ID` and `POSTMAN_API_KEY` in the host environment. Use a separate client and disable redirects to keep the key on the Postman API origin. Pass only the `collection` object to `check` above.

```python
import os


async def check_from_postman(run_config: RunConfig):
    collection_id = os.environ["POSTMAN_COLLECTION_ID"]
    async with httpx2.AsyncClient(trust_env=False) as api:
        response = await api.get(
            f"https://api.postman.com/collections/{collection_id}",
            headers={"X-Api-Key": os.environ["POSTMAN_API_KEY"]},
            follow_redirects=False,
        )
        response.raise_for_status()
        collection = response.json()["collection"]
    return await check(collection, run_config)
```

## Approval rules

- **Names** — Use a unique top-level request first. Without a top-level match, use a unique request across all folders. Expect a setup error for missing or duplicate names, an unsafe method or a URL mismatch. No fallback to a folder after a top-level mismatch.
- **URLs** — Supply an explicit `GET` and a literal HTTPS URL in `request.url` or `request.url.raw`. Match the approved string exactly. No credentials, query, fragment, templates or literal `.` or `..` path segments. No URL construction from other fields.
- **Checks** — One GET per check, a 10-second tool timeout and no redirects. No collection scripts, variables, headers, authentication or request bodies. No response body in the result.

Approve only endpoints safe for repeated GETs and names safe to show to the model. Keep the allowlist separate from the collection and specific to the current user. After a permission change, stop using the old plugin and create a new one. Use a dedicated client without hooks, credentials or automatic retries. Enforce DNS and network restrictions and a request budget in the application.
