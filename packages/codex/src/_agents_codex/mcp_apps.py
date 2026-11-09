"""MCP Apps views over one settled native tool call."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any, cast

from ._public import preserve_module_identity as _preserve_module_identity
from .mcp import HttpMCP, StdioMCP
from .session import NativeError, Session
from .turn import Turn

_MIME = "text/html;profile=mcp-app"
_MAX_HTML_BYTES = 65_536


def _ui_resource(result: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    candidates = []
    for part in result.get("content", []):
        if not isinstance(part, dict):
            continue
        embedded = part.get("type") == "resource"
        resource = part.get("resource") if embedded else part
        if (
            isinstance(resource, dict)
            and (embedded or part.get("type") == "resource_link")
            and resource.get("mimeType") == _MIME
            and isinstance(resource.get("uri"), str)
            and resource["uri"].startswith("ui://")
        ):
            candidates.append((resource, embedded))
    if len(candidates) != 1:
        raise ValueError("MCP app result must contain exactly one UI resource or resource link")
    return candidates[0]


def _checked_resource(resource: dict[str, Any]) -> dict[str, Any]:
    text = resource.get("text")
    if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_HTML_BYTES:
        raise ValueError("MCP app resource must contain at most 64 KiB of HTML text")
    if resource.get("mimeType") != _MIME:
        raise ValueError("MCP app resource must use text/html;profile=mcp-app")
    return deepcopy(resource)


class MCPApp:
    """A borrowed view of an MCP tool result, for an authenticated browser host.

    Use ``await MCPApp.open(session, turn, call_id)`` after the latest root Turn
    succeeds. Its result must embed or link one ``ui://`` HTML resource with
    MIME type ``text/html;profile=mcp-app``. Serve ``view`` to an official MCP
    Apps AppBridge; forward its tools/call handler to ``call_tool``.

    This host supports fixed, preapproved ``HttpMCP`` declarations and host
    ``StdioMCP`` declarations. Callbacks, agent graphs, remote-environment
    execution and tool updates require another integration. It calls only the
    originating tool. A later turn, tool change or Session close invalidates
    the view. Actions return to the UI; they do not start model inference.

    The application owns HTTP authentication, origin checks and iframe sandbox
    and CSP enforcement. ``close`` revokes this view without closing its Session.
    """

    _session: Session[Any, Any]
    _turn: Turn[Any]
    _native: Any
    _generation: int
    _server: HttpMCP[Any] | StdioMCP[Any]
    _tool: str
    _closed: bool
    _view: dict[str, Any]

    def __init__(self) -> None:
        raise TypeError("Use await MCPApp.open(session, turn, call_id)")

    @classmethod
    async def open(cls, session: Session[Any, Any], turn: Turn[Any], call_id: str) -> MCPApp:
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("call_id must be a nonempty string")
        session._check_idle_update()
        if turn is not session._last_turn or turn.child_id is not None or turn.turn_id is None:
            raise ValueError("MCP apps require the latest settled root Turn of this Session")
        opening = session._opening_agent
        assert opening is not None
        if (
            session._default_tools is not None
            or session._native_agents is not None
            or getattr(opening, "handoffs", False)
        ):
            raise ValueError("MCP apps require fixed opening tools and no agent graph")

        async def capture() -> MCPApp:
            await turn.get_result()
            calls = [
                item
                async for item in session.items()
                if item["type"] == "mcp_call"
                and item["turn_id"] == turn.turn_id
                and item.get("call_id", item["id"]) == call_id
            ]
            if len(calls) != 1:
                raise ValueError("MCP app call is absent or ambiguous in the selected Turn")
            call = calls[0]
            result = call["output"]
            if (
                call["status"] != "completed"
                or not isinstance(result, dict)
                or result.get("isError")
            ):
                raise ValueError("MCP apps require a successful retained tool result")
            candidates = [
                tool
                for tool in opening.tools
                if isinstance(tool, (HttpMCP, StdioMCP))
                and type(tool) in (HttpMCP, StdioMCP)
                and tool.name == call["server_label"]
            ]
            if len(candidates) != 1:
                raise ValueError("MCP app server must be an unambiguous ordinary declaration")
            server = candidates[0]
            if (
                not any(server is selected for selected in session._mcp_tools)
                or call["name"] not in server.tools
                or server.approval_mode != "preapproved"
                or server.before is not None
                or server.approval is not None
                or server.request_metadata is not None
                or server.transform_result is not None
                or (isinstance(server, StdioMCP) and server.location != "host")
            ):
                raise ValueError(
                    "MCP apps require a selected, preapproved local server without callbacks"
                )
            resource, embedded = _ui_resource(result)
            if not embedded:
                raw = await session._publish_startup(
                    session._native.call(
                        "read_mcp_resource",
                        json.dumps({"server": server.name, "params": {"uri": resource["uri"]}}),
                    )
                )
                matches = [
                    item
                    for item in json.loads(raw).get("contents", [])
                    if isinstance(item, dict) and item.get("uri") == resource["uri"]
                ]
                if len(matches) != 1:
                    raise ValueError("MCP app resource response must match its linked URI")
                resource = matches[0]
            app = object.__new__(cls)
            app._session = session
            app._turn = turn
            app._native = session._native
            app._generation = session._catalog.generation
            app._server = server
            app._tool = call["name"]
            app._closed = False
            app._view = {
                "callId": call_id,
                "toolName": app._tool,
                "arguments": deepcopy(call["arguments"]),
                "result": deepcopy(result),
                "resource": _checked_resource(resource),
            }
            return app

        return await session._join_mutation(asyncio.create_task(capture()), cancellable=True)

    @property
    def view(self) -> dict[str, Any]:
        """Copy of the original tool input/result and untrusted HTML resource."""
        return deepcopy(self._view)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call the original tool while its Session remains idle and unchanged.

        Cancelling an in-flight action joins native cleanup and retires the
        Session. Close the Session before discarding it.
        """
        if name != self._tool or not isinstance(arguments, dict):
            raise ValueError(
                "MCP app action must call the originating tool with an arguments object"
            )
        encoded = json.dumps(arguments, allow_nan=False)
        session = self._session
        session._check_idle_update()
        if (
            self._closed
            or session._last_turn is not self._turn
            or session._native is not self._native
            or session._catalog.generation != self._generation
            or session._default_tools is not None
            or not any(self._server is selected for selected in session._mcp_tools)
        ):
            raise ValueError("MCP app view is closed or stale")

        async def invoke() -> dict[str, Any]:
            try:
                raw = await session._publish_startup(
                    self._native._call_mcp_app_tool(self._server.name, self._tool, encoded)
                )
                return cast(dict[str, Any], json.loads(raw))
            except RuntimeError as error:
                raise NativeError("MCP app tool request failed") from error

        return await session._join_mutation(asyncio.create_task(invoke()), cancellable=True)

    def close(self) -> None:
        """Revoke future actions from this view; leave the borrowed Session open."""
        self._closed = True


_preserve_module_identity(globals(), "agents.mcp_apps")
