# Custom HTTP Client Factory Example

This repository example targets MCP Python SDK v2 and `httpx2`, and is intended to run with the repository's locked development environment. The Agents SDK client itself supports MCP v1 with `httpx` and MCP v2 with `httpx2`.

This example has been using `httpx_client_factory` in `MCPServerStreamableHttp` to set headers, timeouts, and authentication for Streamable HTTP connections.

## Client configuration

- **Custom Headers**: Add custom headers to all HTTP requests
- **Custom Timeouts**: Set custom timeout values for requests
- **Authentication**: Forward an optional `httpx2.Auth` object to the client

Both examples send the fictional credentials `demo-user` / `demo-password` to demonstrate forwarding. The bundled localhost server ignores these credentials and does not enforce authentication.

The factory preserves TLS certificate verification for HTTPS servers. If your server uses a private certificate authority, create an `ssl.SSLContext` with that authority's certificate and pass it as `verify` when constructing the client. The bundled server uses plain HTTP on localhost.

## Running the Example

1. Make sure you have `uv` installed: https://docs.astral.sh/uv/getting-started/installation/

2. Run the example:
   ```bash
   cd examples/classic/mcp/streamablehttp_custom_client_example
   uv run main.py
   ```

The example has been choosing a free localhost port unless `STREAMABLE_HTTP_PORT` has been selecting a fixed port. `STREAMABLE_HTTP_HOST` has been selecting the bind address.

## Code Examples

### Basic Custom Client

```python
import asyncio

import httpx2
from agents.classic.mcp import MCPServerStreamableHttp


def create_custom_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
) -> httpx2.AsyncClient:
    if headers is None:
        headers = {"X-Custom-Client": "my-app"}
    if timeout is None:
        timeout = httpx2.Timeout(60.0, read=120.0)
    return httpx2.AsyncClient(
        timeout=timeout,
        headers=headers,
        auth=auth,
    )


async def main() -> None:
    async with MCPServerStreamableHttp(
        name="Custom Client Server",
        params={
            "url": "http://localhost:8000/mcp",
            "timeout": 60.0,
            "sse_read_timeout": 120.0,
            # Fictional demo credentials; the bundled localhost server ignores them.
            "auth": httpx2.BasicAuth("demo-user", "demo-password"),
            "httpx_client_factory": create_custom_http_client,
        },
    ) as server:
        print(await server.list_tools())


asyncio.run(main())
```

Start a Streamable HTTP MCP server on port `8000` before running this snippet, or change the URL to your server's address.
