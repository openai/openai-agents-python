# Custom HTTP Client Factory Example

This repository example targets MCP Python SDK v2 and `httpx2`, and is intended to run with the repository's locked development environment. The Agents SDK client itself supports MCP v1 with `httpx` and MCP v2 with `httpx2`.

This example demonstrates how to use the new `httpx_client_factory` parameter in `MCPServerStreamableHttp` to configure custom HTTP client behavior for MCP StreamableHTTP connections.

## Client configuration

- **Custom Headers**: Add custom headers to all HTTP requests
- **Custom Timeouts**: Set custom timeout values for requests
- **Authentication**: Forward an optional `httpx2.Auth` object to the client

The factory preserves TLS certificate verification for HTTPS servers. If your server uses a private certificate authority, create an `ssl.SSLContext` with that authority's certificate and pass it as `verify` when constructing the client. The bundled server uses plain HTTP on localhost.

## Running the Example

1. Make sure you have `uv` installed: https://docs.astral.sh/uv/getting-started/installation/

2. Run the example:
   ```bash
   cd examples/classic/mcp/streamablehttp_custom_client_example
   uv run main.py
   ```

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
            "httpx_client_factory": create_custom_http_client,
        },
    ) as server:
        print(await server.list_tools())


asyncio.run(main())
```

Start a Streamable HTTP MCP server on port `8000` before running this snippet, or change the URL to your server's address.

## Use Cases

- **Corporate Networks**: Configure proxy settings for corporate environments
- **SSL/TLS Requirements**: Use custom SSL certificates for secure connections
- **Custom Authentication**: Add custom headers for API authentication
- **Network Optimization**: Configure timeouts and connection pooling

## Benefits

- **Flexibility**: Configure HTTP client behavior to match your network requirements
- **Security**: Use custom SSL certificates and authentication methods
- **Performance**: Optimize timeouts and connection settings for your use case
- **Compatibility**: Work with corporate proxies and network restrictions

This example will auto-pick a free localhost port unless you set `STREAMABLE_HTTP_PORT`; use `STREAMABLE_HTTP_HOST` to change the bind address.
