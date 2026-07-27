# Custom HTTP Configuration Example

This example demonstrates how to configure custom HTTP client behaviour for
`MCPServerStreamableHttp` connections when using MCP Python SDK v2.

> **Note (mcp SDK v2):** The `httpx_client_factory` parameter has been removed.
> The `MCPServerStreamableHttp` transport now uses `httpx2` internally.
> To customise HTTP behaviour, use the built-in params shown below.

## Features Demonstrated

- **Custom Headers**: Add custom headers to all HTTP requests
- **Custom Timeouts**: Set custom timeout values for requests
- **Custom Authentication**: Pass an `httpx2.Auth` instance via the `auth` param

## Running the Example

1. Make sure you have `uv` installed: https://docs.astral.sh/uv/getting-started/installation/

2. Run the example:
   ```bash
   cd examples/mcp/streamablehttp_custom_client_example
   uv run main.py
   ```

## Code Examples

### Custom Headers and Timeout (recommended)

```python
from agents.mcp import MCPServerStreamableHttp

async with MCPServerStreamableHttp(
    name="Custom Config Server",
    params={
        "url": "http://localhost:<port>/mcp",
        "headers": {
            "X-Custom-Client": "my-app",
            "User-Agent": "MyApp/1.0",
        },
        "timeout": 60.0,          # connect timeout in seconds
        "sse_read_timeout": 120.0, # SSE read timeout in seconds
    },
) as server:
    # Use the server...
```

### Basic Authentication

```python
import httpx2
from agents.mcp import MCPServerStreamableHttp

async with MCPServerStreamableHttp(
    name="Auth Server",
    params={
        "url": "http://localhost:<port>/mcp",
        "auth": httpx2.BasicAuth(username="user", password="secret"),
    },
) as server:
    # Use the server...
```

## Use Cases

- **Corporate Networks**: Add proxy-bypass headers or authentication
- **Custom Authentication**: Use `httpx2.Auth` subclasses for OAuth token refresh
- **Network Optimization**: Set timeouts appropriate for your environment
- **Debugging**: Inspect headers via the `headers` param

This example will auto-pick a free localhost port unless you set `STREAMABLE_HTTP_PORT`;
use `STREAMABLE_HTTP_HOST` to change the bind address.
