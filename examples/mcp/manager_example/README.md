# MCP Manager Example (FastAPI)

This example shows how to use `MCPServerManager` to keep MCP server lifecycle
management in a single task inside a FastAPI app with the Streamable HTTP
transport.

## Run the MCP server (Streamable HTTP)

```
uv run python examples/mcp/manager_example/mcp_server.py
```

The server listens at `http://localhost:8000/mcp` by default.

You can override the host/port with:

```
export STREAMABLE_HTTP_HOST=127.0.0.1
export STREAMABLE_HTTP_PORT=8000
```

This example also configures an inactive MCP server at
`http://localhost:8001/mcp` to demonstrate how the manager drops failed
servers. You can override it with:

```
export INACTIVE_MCP_SERVER_URL=http://localhost:8001/mcp
```

## Run the FastAPI app

```
uv run python examples/mcp/manager_example/app.py
```

The app listens at `http://127.0.0.1:9001`.

## Toggle MCP manager usage

By default, the app uses `MCPServerManager`. To disable it:

```
export USE_MCP_MANAGER=0
```

## Try the endpoints

```
curl http://127.0.0.1:9001/health
curl http://127.0.0.1:9001/tools
curl -X POST http://127.0.0.1:9001/add \
  -H 'Content-Type: application/json' \
  -d '{"a": 2, "b": 3}'
```

Reconnect failed MCP servers (manager must be enabled):

```
curl -X POST http://127.0.0.1:9001/reconnect \
  -H 'Content-Type: application/json' \
  -d '{"failed_only": true}'
```

To use `/run`, set `OPENAI_API_KEY`:

```
export OPENAI_API_KEY=...
curl -X POST http://127.0.0.1:9001/run \
  -H 'Content-Type: application/json' \
  -d '{"input": "Add 4 and 9."}'
```
