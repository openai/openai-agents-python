"""Northflank-platform extensions for the openai-agents SDK.

Two surfaces live here, gated behind the optional ``northflank`` extra:

* :func:`northflank_tools` — a curated ``tool_namespace()`` bundle of
  typed ``@function_tool`` wrappers around the Northflank REST API
  (list/get projects + services, deploy + restart + pause + resume +
  scale + wait, one-shot exec in service/job/addon containers, fetch and
  tail logs, fetch metrics, and gated delete/secret/volume/domain
  mutations).
* :class:`NorthflankShellExecutor` — a :class:`agents.tool.ShellTool`
  executor that runs each shell command remotely inside a Northflank
  service container via the V1 exec WebSocket.

Both share :class:`NorthflankCtx`, a typed dataclass that carries the
``AsyncApiClient`` plus optional default ``project_id`` / ``team_id`` for
the run.
"""

from __future__ import annotations

from .context import NorthflankCtx
from .shell import NorthflankShellExecutor
from .tools import (
    nf_create_deployment_service,
    nf_delete_domain,
    nf_delete_secret,
    nf_delete_service,
    nf_delete_volume,
    nf_fetch_service_logs,
    nf_get_project,
    nf_get_service,
    nf_get_service_metrics,
    nf_list_projects,
    nf_list_services,
    nf_pause_service,
    nf_put_secret,
    nf_restart_service,
    nf_resume_service,
    nf_run_addon_command,
    nf_run_job_command,
    nf_run_service_command,
    nf_scale_service,
    nf_tail_service_logs,
    nf_wait_service_ready,
    northflank_tools,
)

__all__ = [
    "NorthflankCtx",
    "NorthflankShellExecutor",
    "nf_create_deployment_service",
    "nf_delete_domain",
    "nf_delete_secret",
    "nf_delete_service",
    "nf_delete_volume",
    "nf_fetch_service_logs",
    "nf_get_project",
    "nf_get_service",
    "nf_get_service_metrics",
    "nf_list_projects",
    "nf_list_services",
    "nf_pause_service",
    "nf_put_secret",
    "nf_restart_service",
    "nf_resume_service",
    "nf_run_addon_command",
    "nf_run_job_command",
    "nf_run_service_command",
    "nf_scale_service",
    "nf_tail_service_logs",
    "nf_wait_service_ready",
    "northflank_tools",
]
