from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)

from ....sandbox.errors import (
    ConfigurationError,
    ErrorCode,
    ExecTransportError,
    SandboxError,
    SandboxRuntimeError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceIOError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
    WorkspaceStopError,
)

_MAX_PROVIDER_ERROR_CHARS = 2000


def _provider_detail(error: BaseException) -> str:
    detail = str(error).strip() or type(error).__name__
    if len(detail) <= _MAX_PROVIDER_ERROR_CHARS:
        return detail
    return detail[:_MAX_PROVIDER_ERROR_CHARS] + "... [truncated]"


def _context(
    *,
    operation: str,
    sandbox_id: str | None = None,
    extra: Mapping[str, object] | None = None,
    error: BaseException | None = None,
) -> dict[str, object]:
    context: dict[str, object] = {
        "backend": "aca_sandboxes",
        "operation": operation,
    }
    if sandbox_id is not None:
        context["sandbox_id"] = sandbox_id
    if error is not None:
        context["provider_error_type"] = type(error).__name__
        context["provider_error"] = _provider_detail(error)
    context.update(extra or {})
    return context


def _retryable(error: BaseException) -> bool | None:
    if isinstance(error, ResourceNotFoundError | ClientAuthenticationError):
        return False
    if isinstance(error, ServiceRequestError | ServiceResponseError):
        return True
    if isinstance(error, HttpResponseError):
        status_code = getattr(error, "status_code", None)
        if status_code in {408, 429}:
            return True
        if isinstance(status_code, int) and status_code >= 500:
            return True
        if isinstance(status_code, int) and 400 <= status_code < 500:
            return False
    return None


def create_error(error: BaseException) -> SandboxError:
    context = _context(operation="create", error=error)
    if isinstance(error, ClientAuthenticationError):
        return ConfigurationError(
            message="ACA Sandboxes authentication failed while creating a sandbox.",
            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
            op="start",
            context=context,
            cause=error,
            retryable=False,
        )
    return WorkspaceStartError(
        path=Path("<aca-sandbox>"),
        message=f"ACA Sandboxes create failed: {_provider_detail(error)}",
        context=context,
        cause=error,
        retryable=_retryable(error),
    )


def resume_error(
    error: BaseException,
    *,
    sandbox_id: str,
    timeout_s: float,
    state: str | None = None,
    stopped_reason: str | None = None,
) -> SandboxError:
    extra: dict[str, object] = {"timeout_s": timeout_s}
    if state is not None:
        extra["state"] = state
    if stopped_reason is not None:
        extra["stopped_reason"] = stopped_reason
    context = _context(
        operation="resume",
        sandbox_id=sandbox_id,
        extra=extra,
        error=error,
    )

    retryable: bool | None
    if isinstance(error, ResourceNotFoundError):
        message = (
            f"ACA sandbox {sandbox_id!r} no longer exists; the serialized session state is stale."
        )
        retryable = False
    elif isinstance(error, TimeoutError):
        message = (
            f"ACA sandbox {sandbox_id!r} did not reach Running state within {timeout_s:g} seconds."
        )
        retryable = True
    elif stopped_reason == "Disabled":
        message = f"ACA sandbox {sandbox_id!r} is administratively disabled and cannot be resumed."
        retryable = False
    elif isinstance(error, RuntimeError) and state is not None:
        message = f"ACA sandbox {sandbox_id!r} is in non-resumable state {state!r}."
        retryable = False
    else:
        message = f"ACA sandbox {sandbox_id!r} could not be resumed: {_provider_detail(error)}"
        retryable = _retryable(error)

    return WorkspaceStartError(
        path=Path("<aca-sandbox>"),
        message=message,
        context=context,
        cause=error,
        retryable=retryable,
    )


def exec_error(error: BaseException, *, command: Sequence[str | Path]) -> ExecTransportError:
    return ExecTransportError(
        command=command,
        message=f"ACA sandbox exec failed: {_provider_detail(error)}",
        context=_context(operation="exec", error=error),
        cause=error,
        retryable=_retryable(error),
    )


def read_error(error: BaseException, *, path: Path) -> WorkspaceIOError:
    if isinstance(error, ResourceNotFoundError):
        return WorkspaceReadNotFoundError(
            path=path,
            context=_context(operation="read", error=error),
            cause=error,
        )
    return WorkspaceArchiveReadError(
        path=path,
        context=_context(operation="read", error=error),
        cause=error,
        retryable=_retryable(error),
    )


def write_error(error: BaseException, *, path: Path) -> WorkspaceArchiveWriteError:
    return WorkspaceArchiveWriteError(
        path=path,
        context=_context(operation="write", error=error),
        cause=error,
        retryable=_retryable(error),
    )


def delete_error(error: BaseException, *, sandbox_id: str) -> WorkspaceStopError:
    return WorkspaceStopError(
        path=Path("<aca-sandbox>"),
        context=_context(operation="delete", sandbox_id=sandbox_id, error=error),
        cause=error,
        retryable=_retryable(error),
    )


def port_error(
    error: BaseException,
    *,
    sandbox_id: str,
    port: int,
) -> SandboxRuntimeError:
    return SandboxRuntimeError(
        message=(
            f"ACA sandbox {sandbox_id!r} could not expose port {port}: {_provider_detail(error)}"
        ),
        error_code=ErrorCode.EXPOSED_PORT_UNAVAILABLE,
        op="resolve_exposed_port",
        context=_context(
            operation="resolve_exposed_port",
            sandbox_id=sandbox_id,
            extra={"port": port},
            error=error,
        ),
        cause=error,
        retryable=_retryable(error),
    )


__all__ = [
    "create_error",
    "delete_error",
    "exec_error",
    "port_error",
    "read_error",
    "resume_error",
    "write_error",
]
