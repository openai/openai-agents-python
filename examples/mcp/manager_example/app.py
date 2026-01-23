import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agents import Agent, Runner
from agents.mcp import MCPServer, MCPServerManager, MCPServerStreamableHttp
from agents.model_settings import ModelSettings

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8000/mcp")
INACTIVE_MCP_SERVER_URL = os.getenv("INACTIVE_MCP_SERVER_URL", "http://localhost:8001/mcp")
APP_HOST = "127.0.0.1"
APP_PORT = 9001
USE_MCP_MANAGER = os.getenv("USE_MCP_MANAGER", "1") != "0"


class AddRequest(BaseModel):
    a: int
    b: int


class RunRequest(BaseModel):
    input: str


class ReconnectRequest(BaseModel):
    failed_only: bool = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    server = MCPServerStreamableHttp({"url": MCP_SERVER_URL})
    inactive_server = MCPServerStreamableHttp({"url": INACTIVE_MCP_SERVER_URL})
    servers = [server, inactive_server]
    if USE_MCP_MANAGER:
        async with MCPServerManager(
            servers=servers,
            connect_in_parallel=True,
        ) as manager:
            app.state.mcp_manager = manager
            app.state.mcp_servers = servers
            yield
        return

    await server.connect()
    app.state.mcp_servers = servers
    app.state.active_servers = [server]
    try:
        yield
    finally:
        await server.cleanup()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, object]:
    if USE_MCP_MANAGER:
        manager: MCPServerManager = app.state.mcp_manager
        return {
            "connected_servers": [server.name for server in manager.active_servers],
            "failed_servers": [server.name for server in manager.failed_servers],
        }

    active_servers = _get_active_servers()
    return {
        "connected_servers": [server.name for server in active_servers],
        "failed_servers": [],
    }


@app.get("/tools")
async def list_tools() -> dict[str, object]:
    active_servers = _get_active_servers()
    if not active_servers:
        return {"tools": []}
    tools = await active_servers[0].list_tools()
    return {"tools": [tool.name for tool in tools]}


@app.post("/add")
async def add(req: AddRequest) -> dict[str, object]:
    active_servers = _get_active_servers()
    if not active_servers:
        raise HTTPException(status_code=503, detail="No MCP servers available")
    result = await active_servers[0].call_tool("add", {"a": req.a, "b": req.b})
    return {"result": result.model_dump(mode="json")}


@app.post("/run")
async def run_agent(req: RunRequest) -> dict[str, object]:
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is required")

    servers = _get_active_servers()
    if not servers:
        raise HTTPException(status_code=503, detail="No MCP servers available")

    agent = Agent(
        name="FastAPI Agent",
        instructions="Use the MCP tools when needed.",
        mcp_servers=servers,
        model_settings=ModelSettings(tool_choice="auto"),
    )
    result = await Runner.run(starting_agent=agent, input=req.input)
    return {"output": result.final_output}


@app.post("/reconnect")
async def reconnect(req: ReconnectRequest) -> dict[str, object]:
    if not USE_MCP_MANAGER:
        raise HTTPException(status_code=400, detail="MCPServerManager is disabled")
    manager: MCPServerManager = app.state.mcp_manager
    servers = await manager.reconnect(failed_only=req.failed_only)
    return {"connected_servers": [server.name for server in servers]}


def _get_active_servers() -> list[MCPServer]:
    if USE_MCP_MANAGER:
        manager: MCPServerManager = app.state.mcp_manager
        return list(manager.active_servers)
    return list(app.state.active_servers)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=APP_HOST, port=APP_PORT)
