"""Experimental MCP Tasks provider for a host-registered deferred FunctionTool.

Install ``mcp==2.0.0`` and ``mcp-types==2.0.0`` to use this module. Register
one explicitly approved low-impact operation with ``require_reconciliation=False``.
MCP cannot reconcile an accepted create whose reply was lost. The application owns
its journal, access policy, verified result projection, and wake-up schedule.
"""

# The base SDK can be checked without optional MCP packages. Their missing
# symbols remain unknown when forwarded from the implementation module.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from _agents_codex.experimental.mcp_tasks import (
    _FORM_TYPES as _FORM_TYPES,
    Any as Any,
    AsyncIterator as AsyncIterator,
    Awaitable as Awaitable,
    BaseModel as BaseModel,
    Callable as Callable,
    CallToolResult as CallToolResult,
    ClientSession as ClientSession,
    Collection as Collection,
    ElicitRequest as ElicitRequest,
    ElicitRequestFormParams as ElicitRequestFormParams,
    ElicitResult as ElicitResult,
    Iterator as Iterator,
    JobClaim as JobClaim,
    JobUpdate as JobUpdate,
    JsonValue as JsonValue,
    Mapping as Mapping,
    MappingProxyType as MappingProxyType,
    MCPInputReply as MCPInputReply,
    MCPTasksProvider as MCPTasksProvider,
    PackageNotFoundError as PackageNotFoundError,
    Self as Self,
    _accepts_form as _accepts_form,
    _ApprovedResponses as _ApprovedResponses,
    _canonical as _canonical,
    _capture_public_json as _capture_public_json,
    _digest as _digest,
    _FixedEndpoint as _FixedEndpoint,
    _form_contains_token as _form_contains_token,
    _issued_credentials as _issued_credentials,
    _poll_at as _poll_at,
    _private_text as _private_text,
    _supported as _supported,
    _valid_endpoint as _valid_endpoint,
    _wait_for_job as _wait_for_job,
    astuple as astuple,
    asynccontextmanager as asynccontextmanager,
    asyncio as asyncio,
    copy_context as copy_context,
    dataclass as dataclass,
    hashlib as hashlib,
    httpx2 as httpx2,
    json as json,
    math as math,
    partial as partial,
    time as time,
    urlsplit as urlsplit,
    version as version,
)
from agents.experimental import (
    _mcp_tasks_wire as wire,  # noqa: F401 - Preserve the existing module binding.
)

__all__ = ["MCPInputReply", "MCPTasksProvider"]
