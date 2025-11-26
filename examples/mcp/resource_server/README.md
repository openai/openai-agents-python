# MCP Resource Server Example

This example demonstrates MCP resource support for providing context data to agents. Resources allow MCP servers to expose data that agents can read and use as context for their responses.

## What are MCP Resources?

MCP resources are named, URI-addressable pieces of data that can be read by agents. Unlike prompts (which generate instructions) or tools (which perform actions), resources provide static or dynamic context data that agents can reference.

Common use cases:
- Configuration files and settings
- API documentation
- System metrics and logs
- Knowledge base articles
- Templates and examples

## Running the Example

```bash
uv run python examples/mcp/resource_server/main.py
```

## How it Works

### Server Side (`server.py`)

The example server provides four resources:

1. **`config://app/settings`** - Application configuration
2. **`docs://api/overview`** - API documentation  
3. **`data://metrics/summary`** - System metrics
4. **`docs://security/guidelines`** - Security best practices

Each resource is defined using the `@mcp.resource()` decorator:

```python
@mcp.resource("config://app/settings")
def get_app_settings() -> str:
    """Application configuration settings"""
    return """# Application Settings
    ...
    """
```

### Client Side (`main.py`)

The client demonstrates several ways to use resources:

1. **List available resources** using `list_resources()`
2. **Read resource content** using `read_resource(uri)`
3. **Provide resources as context** to agents

Example usage:

```python
# List resources
resources_result = await server.list_resources()
for resource in resources_result.resources:
    print(f"Resource: {resource.name} - {resource.uri}")

# Read a specific resource
resource_result = await server.read_resource("config://app/settings")
content = resource_result.contents[0].text

# Use resource content in agent instructions
agent = Agent(
    name="Config Assistant",
    instructions=f"You are a helpful assistant. Configuration: {content}",
    mcp_servers=[server],
)
```

## Demos Included

1. **Configuration Assistant** - Answers questions about app configuration
2. **API Documentation Assistant** - Helps users understand the API
3. **Security Reviewer** - Reviews code using security guidelines
4. **Metrics Analyzer** - Analyzes system performance metrics

## Resource URI Schemes

Resources can use any URI scheme. Common patterns:

- `file://` - File system resources
- `http://` or `https://` - Web resources  
- `config://` - Configuration resources
- `docs://` - Documentation resources
- `data://` - Data resources
- Custom schemes for domain-specific resources

## Next Steps

Try modifying the example to:

1. Add new resources (e.g., error codes, examples, schemas)
2. Use different URI schemes
3. Provide dynamic content (e.g., current timestamp, system status)
4. Combine multiple resources in agent context
5. Implement resource templates for parameterized content

