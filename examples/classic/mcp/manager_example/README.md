# MCP Manager Example (FastAPI)

This repository example targets MCP Python SDK v2. The commands below use the repository's development environment and add FastAPI explicitly. The Agents SDK client itself supports both MCP v1 and v2.

This example shows how to use `MCPServerManager` to keep MCP server lifecycle management in a single task inside a FastAPI app with the Streamable HTTP transport.

## Configure the example

Complete [Classic example setup](../../README.md#setup). Run all commands below from the repository root.

The MCP server and FastAPI app run in separate terminals. Export any configuration in the terminal that starts the affected process, before starting it. Restart that process after changing its environment. To use the optional model-backed `/run` endpoint, set `OPENAI_API_KEY` in the FastAPI app terminal. The other endpoints and the smoke test need no model credentials.

## Run the MCP server (Streamable HTTP)

In the first terminal:

```
uv run --extra classic python examples/classic/mcp/manager_example/mcp_server.py
```

The server listens at `http://localhost:8000/mcp` by default. Set `STREAMABLE_HTTP_HOST` and `STREAMABLE_HTTP_PORT` before launching it to override that address. If you change it, set `MCP_SERVER_URL` to the matching full URL in the FastAPI app terminal.

The app also tries `http://localhost:8001/mcp` to demonstrate a failed connection. Set `INACTIVE_MCP_SERVER_URL` in the app terminal to change that URL.

## Run the FastAPI app

In the second terminal, set any app configuration and start the app:

```
uv run --extra classic --with 'fastapi>=0.150.0' python -m examples.classic.mcp.manager_example.app
```

The app listens at `http://127.0.0.1:9001`.

Run this unauthenticated demo only on a private host where all local users and processes are trusted. It accepts only `localhost` and `127.0.0.1` Host headers. It does not authenticate callers or isolate local users. Do not expose it through a public listener, proxy, or tunnel. FastAPI 0.150.0 or later is required for its strict JSON content-type checks; the app does not enable CORS.

## Run the smoke test

To verify the MCP manager and app integration without calling a model:

```
uv run --extra classic --with 'fastapi>=0.150.0' python -m examples.classic.mcp.manager_example.smoke_test
```

The smoke test starts the local MCP server on a temporary port, points both app MCP server settings at that server, and checks `/health`, `/tools`, and `/add`.

## Toggle MCP manager usage

By default, the app uses `MCPServerManager`. To disable it, stop the app, export this value in its terminal, and start the app again:

```
export USE_MCP_MANAGER=0
```

## Try the endpoints

With both servers running, use a third terminal:

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

For `/run`, first start or restart the FastAPI app with `OPENAI_API_KEY` set in its environment. Then send the request from the third terminal:

```
curl -X POST http://127.0.0.1:9001/run \
  -H 'Content-Type: application/json' \
  -d '{"input": "Add 4 and 9."}'
```
