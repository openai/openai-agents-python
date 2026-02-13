from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any

from typing_extensions import Literal

from agents.exceptions import UserError

ApprovalMode = Literal["never", "on-request", "on-failure", "untrusted"]
SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
ModelReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh"]
WebSearchMode = Literal["disabled", "cached", "live"]


@dataclass(frozen=True)
class ThreadOptions:
    # Model identifier passed to the Codex CLI (--model).
    model: str | None = None
    # Sandbox permissions for filesystem/network access.
    sandbox_mode: SandboxMode | None = None
    # Working directory for the Codex CLI process.
    working_directory: str | None = None
    # Allow running outside a Git repository.
    skip_git_repo_check: bool | None = None
    # Configure model reasoning effort.
    model_reasoning_effort: ModelReasoningEffort | None = None
    # Toggle network access in sandboxed workspace writes.
    network_access_enabled: bool | None = None
    # Configure web search mode via codex config.
    web_search_mode: WebSearchMode | None = None
    # Legacy toggle for web search behavior.
    web_search_enabled: bool | None = None
    # Approval policy for tool invocations within Codex.
    approval_policy: ApprovalMode | None = None
    # Additional filesystem roots available to Codex.
    additional_directories: Sequence[str] | None = None


def coerce_thread_options(
    options: ThreadOptions | Mapping[str, Any] | None,
) -> ThreadOptions | None:
    if options is None or isinstance(options, ThreadOptions):
        return options
    if not isinstance(options, Mapping):
        raise UserError("ThreadOptions must be a ThreadOptions or a mapping.")

    allowed = {field.name for field in fields(ThreadOptions)}
    unknown = set(options.keys()) - allowed
    if unknown:
        raise UserError(f"Unknown ThreadOptions field(s): {sorted(unknown)}")

    return ThreadOptions(**dict(options))
