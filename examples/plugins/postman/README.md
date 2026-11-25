# Status checks from a Postman collection

Two agent tools for a local [Postman Collection v2.1](https://schema.postman.com/collection/json/v2.1.0/draft-07/docs/index.html) export.

| Tool | Model input | Result |
| --- | --- | --- |
| `postman_list_requests` | None | Approved request names |
| `postman_check_request` | One name | HTTP status, such as `HTTP 200`, or a short error |

## Offline example

Prerequisites — Python 3.10 or later, an Agents SDK build with `agents.plugins`, and `httpx2>=2.12.0`. From this directory, in the same environment as the SDK

```sh
python -m pip install --no-deps .
python -m agents_postman
```

Expected output from the [example](src/agents_postman/__main__.py) with a scripted model and mock HTTP transport

```text
["Health"]
HTTP 200
```

## Your collection

Host setup for a public health endpoint, with a reviewed name and URL in application code

```python
import json
from pathlib import Path

import httpx2

from agents import Agent, Runner
from agents_postman import postman


async def check(path: Path, run_config):
    collection = json.loads(path.read_text(encoding="utf-8"))
    async with httpx2.AsyncClient(trust_env=False) as client:
        checks = postman(client, collection, approved={"Health": "https://store.test/health"})
        agent = Agent(name="Storefront", plugins=[checks])
        return await Runner.run(agent, "Check storefront health.", run_config=run_config)
```

`store.test` as a placeholder for your endpoint. A separate, trusted allowlist for each user, with names safe to show to the model. A fresh plugin after any permission change. No Postman account or API key.

- **Supported requests** — Top-level items with unique approved names, an explicit `GET` method and a literal HTTPS URL in `request.url` or `request.url.raw`. Exact match against the approved URL at setup. No credentials, query, fragment or template in the URL.
- **HTTP behavior** — One GET per check, a 10-second tool timeout and no redirects. No collection scripts, variables, headers, authentication or request bodies. No response body in the result.
- **Host responsibility** — Only endpoints safe for repeated unauthenticated GETs. A dedicated HTTP client without hooks, credentials or automatic retries. Network restrictions and a request budget in the application. No DNS or network isolation in the plugin.
