"""Minimal newline-delimited MCP server for native transport integration tests."""

import json
import sys
from pathlib import Path


def send(message):
    print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)


def main():
    pending_call = None
    elicitation_id = 73
    workspace = Path(sys.argv[1]) if len(sys.argv) > 1 else None

    def complete(call_id, receipt, is_error=False):
        if workspace:
            receipt["execution_marker"] = (workspace / "marker.txt").read_text()
            receipt["cwd"] = str(Path.cwd())
        send(
            {
                "id": call_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(receipt)}],
                    "structuredContent": receipt,
                    "_meta": {"fixture_receipt": True},
                    "isError": is_error,
                },
            }
        )

    for line in sys.stdin:
        message = json.loads(line)
        if workspace:
            with (workspace / "mcp-journal.jsonl").open("a") as journal:
                journal.write(json.dumps(message) + "\n")
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            send(
                {
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "elicitation-fixture", "version": "1"},
                    },
                }
            )
        elif method == "tools/list":
            send(
                {
                    "id": request_id,
                    "result": {
                        "tools": [
                            {
                                "name": name,
                                "description": description,
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "token": {"type": "string"},
                                        "mode": {
                                            "type": "string",
                                            "enum": ["form", "url"],
                                        },
                                    },
                                    "required": ["token"],
                                },
                                "annotations": {
                                    "readOnlyHint": name == "ask",
                                    "destructiveHint": name == "write",
                                },
                            }
                            for name, description in (
                                ("ask", "Ask for input and return the MCP response."),
                                (
                                    "write",
                                    "Append a token to a file in the server workspace.",
                                ),
                            )
                        ]
                    },
                }
            )
        elif method == "tools/call":
            arguments = message["params"]["arguments"]
            token = arguments["token"]
            if message["params"]["name"] == "write":
                with Path("mcp-writes.txt").open("a") as output:
                    output.write(token + "\n")
                complete(request_id, {"token": token})
                continue
            pending_call = request_id
            mode = arguments.get("mode", "form")
            fields = (
                {"url": f"https://example.invalid/{token}", "elicitationId": token}
                if mode == "url"
                else {
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                    }
                }
            )
            send(
                {
                    "id": elicitation_id,
                    "method": "elicitation/create",
                    "params": {
                        "mode": mode,
                        "message": "Supply the requested value",
                        **fields,
                        "_meta": {"fixture_token": token},
                    },
                }
            )
        elif (
            method is None and request_id == elicitation_id and pending_call is not None
        ):
            receipt = {
                "server_request_id": request_id,
                "response": message.get("result"),
            }
            complete(pending_call, receipt, is_error="error" in message)
            pending_call = None
        elif method == "ping":
            send({"id": request_id, "result": {}})
        elif method is not None and request_id is not None:
            send(
                {
                    "id": request_id,
                    "error": {"code": -32601, "message": "Unknown method"},
                }
            )


if __name__ == "__main__":
    main()
