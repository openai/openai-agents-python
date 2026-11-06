"""A synthetic local MCP service with server-side user credentials."""

from aiohttp import web


async def serve(request):
    service = request.app["service"]
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    owner = next((user for user, value in service.credentials.items() if value == token), None)
    if owner is None or owner in service.revoked:
        raise web.HTTPForbidden()
    body = await request.json()
    if "id" not in body:
        return web.Response(status=202)
    method = body.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "synthetic-records", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "read_record",
                    "description": "Read your synthetic record",
                    "annotations": {"readOnlyHint": True},
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
            ]
        }
    elif method == "tools/call" and body.get("params", {}).get("name") == "read_record":
        result = {
            "content": [{"type": "text", "text": f"Synthetic record for {owner}"}],
            "isError": False,
        }
    else:
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32601, "message": "Unknown method"},
            }
        )
    return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})
