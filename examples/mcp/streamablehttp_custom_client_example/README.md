# Custom HTTP Client Factory Example

This example demonstrates how to use the new `httpx_client_factory` parameter in `MCPServerStreamableHttp` to configure custom HTTP client behavior for MCP StreamableHTTP connections.

## Features Demonstrated

- **Custom SSL Configuration**: Configure SSL certificates and verification settings
- **Custom Headers**: Add custom headers to all HTTP requests
- **Custom Timeouts**: Set custom timeout values for requests
- **Proxy Configuration**: Configure HTTP proxy settings
- **Custom Retry Logic**: Set up custom retry behavior (through httpx configuration)

## Running the Example

1. Make sure you have `uv` installed: https://docs.astral.sh/uv/getting-started/installation/

2. Run the example:
   ```bash
   cd examples/mcp/streamablehttp_custom_client_example
   uv run main.py
   ```

## Code Examples

### Basic Custom Client

```python
import httpx
from agents.mcp import MCPServerStreamableHttp

def create_custom_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        verify=False,  # Disable SSL verification for testing
        timeout=httpx.Timeout(60.0, read=120.0),
        headers={"X-Custom-Client": "my-app"},
    )

async with MCPServerStreamableHttp(
    name="Custom Client Server",
    params={
        "url": "http://localhost:8000/mcp",
        "httpx_client_factory": create_custom_http_client,
    },
) as server:
    # Use the server...
```

## Use Cases

- **Corporate Networks**: Configure proxy settings for corporate environments
- **SSL/TLS Requirements**: Use custom SSL certificates for secure connections
- **Custom Authentication**: Add custom headers for API authentication
- **Network Optimization**: Configure timeouts and connection pooling
- **Debugging**: Disable SSL verification for development environments

## Benefits

- **Flexibility**: Configure HTTP client behavior to match your network requirements
- **Security**: Use custom SSL certificates and authentication methods
- **Performance**: Optimize timeouts and connection settings for your use case
- **Compatibility**: Work with corporate proxies and network restrictions

