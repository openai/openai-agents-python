"""Curated Northflank tools for the OpenAI Agents Python SDK.

Each tool is a top-level ``FunctionTool`` produced by ``@function_tool``.
They share a single typed context (:class:`NorthflankCtx`) so the agent can
operate over an authenticated SDK client without re-passing credentials at
every call.

Use :func:`northflank_tools` to get the curated list grouped under a tool
namespace. Pass ``include_secrets``, ``include_volumes``, ``include_domains``,
or ``include_delete`` to opt-in to gated mutations.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from ...run_context import RunContextWrapper
from ...tool import FunctionTool, function_tool, tool_namespace
from ._helpers import ShellMode, resolve_project_id, resolve_team_id, unwrap, wrap_shell_command
from .context import NorthflankCtx

# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@function_tool
async def nf_list_projects(
    ctx: RunContextWrapper[NorthflankCtx],
    team_id: str | None = None,
) -> dict[str, Any]:
    """List every Northflank project visible to the configured API token.

    Walks pagination so the model sees the full set, not just the first
    page.

    Args:
        team_id: Optional team scope. Falls back to NorthflankCtx.team_id.
    """
    response = await ctx.context.client.list.projects.all(team_id=resolve_team_id(ctx, team_id))
    return unwrap(response)


@function_tool
async def nf_get_project(
    ctx: RunContextWrapper[NorthflankCtx],
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Fetch a single Northflank project by ID."""
    response = await ctx.context.client.get.project(
        project_id=resolve_project_id(ctx, project_id),
        team_id=resolve_team_id(ctx, team_id),
    )
    return unwrap(response)


@function_tool
async def nf_list_services(
    ctx: RunContextWrapper[NorthflankCtx],
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """List every service in a Northflank project (walks pagination)."""
    response = await ctx.context.client.list.services.all(
        project_id=resolve_project_id(ctx, project_id),
        team_id=resolve_team_id(ctx, team_id),
    )
    return unwrap(response)


@function_tool
async def nf_get_service(
    ctx: RunContextWrapper[NorthflankCtx],
    service_id: str,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Fetch a service's full configuration and current status."""
    response = await ctx.context.client.get.service(
        project_id=resolve_project_id(ctx, project_id),
        service_id=service_id,
        team_id=resolve_team_id(ctx, team_id),
    )
    return unwrap(response)


# ---------------------------------------------------------------------------
# Deploy / lifecycle tools (mutating — needs_approval=True)
# ---------------------------------------------------------------------------


@function_tool(needs_approval=True, strict_mode=False)
async def nf_create_deployment_service(
    ctx: RunContextWrapper[NorthflankCtx],
    name: str,
    image_path: str,
    instances: int = 1,
    deployment_plan: str = "nf-compute-20",
    description: str | None = None,
    ports: list[dict[str, Any]] | None = None,
    runtime_environment: dict[str, str] | None = None,
    image_credentials: str | None = None,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Create a deployment service from an external Docker image.

    Args:
        name: New service name.
        image_path: Fully-qualified image reference (e.g. ``nginx:1.27``).
        instances: Initial replica count.
        deployment_plan: Northflank compute plan ID (defaults to nf-compute-20).
        description: Optional human-readable description.
        ports: Optional list of port specs (each with internalPort, name, protocol).
        runtime_environment: Optional environment variables (name -> value).
        image_credentials: Optional registry credentials addon ID for private images.
    """
    data: dict[str, Any] = {
        "name": name,
        "billing": {"deploymentPlan": deployment_plan},
        "deployment": {
            "instances": instances,
            "external": {"imagePath": image_path},
        },
    }
    if image_credentials:
        data["deployment"]["external"]["credentials"] = image_credentials
    if description:
        data["description"] = description
    if ports:
        data["ports"] = ports
    if runtime_environment:
        data["runtimeEnvironment"] = runtime_environment

    response = await ctx.context.client.create.service.deployment(
        project_id=resolve_project_id(ctx, project_id),
        team_id=resolve_team_id(ctx, team_id),
        data=data,
    )
    return unwrap(response)


@function_tool(needs_approval=True)
async def nf_restart_service(
    ctx: RunContextWrapper[NorthflankCtx],
    service_id: str,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Restart all running containers of a service."""
    response = await ctx.context.client.restart.service(
        project_id=resolve_project_id(ctx, project_id),
        service_id=service_id,
        team_id=resolve_team_id(ctx, team_id),
    )
    return unwrap(response)


@function_tool(needs_approval=True)
async def nf_pause_service(
    ctx: RunContextWrapper[NorthflankCtx],
    service_id: str,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Pause a service (scale to zero, retain configuration)."""
    response = await ctx.context.client.pause.service(
        project_id=resolve_project_id(ctx, project_id),
        service_id=service_id,
        team_id=resolve_team_id(ctx, team_id),
    )
    return unwrap(response)


@function_tool(needs_approval=True)
async def nf_resume_service(
    ctx: RunContextWrapper[NorthflankCtx],
    service_id: str,
    instances: int | None = None,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Resume a paused service.

    Args:
        instances: Optional target replica count on resume.
    """
    data: dict[str, Any] = {}
    if instances is not None:
        data["instances"] = instances
    response = await ctx.context.client.resume.service(
        project_id=resolve_project_id(ctx, project_id),
        service_id=service_id,
        team_id=resolve_team_id(ctx, team_id),
        data=data,
    )
    return unwrap(response)


@function_tool(needs_approval=True)
async def nf_scale_service(
    ctx: RunContextWrapper[NorthflankCtx],
    service_id: str,
    instances: int | None = None,
    deployment_plan: str | None = None,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Change replica count or compute plan for a service."""
    data: dict[str, Any] = {}
    if instances is not None:
        data["instances"] = instances
    if deployment_plan is not None:
        data["deploymentPlan"] = deployment_plan
    if not data:
        raise ValueError("nf_scale_service requires instances or deployment_plan.")
    response = await ctx.context.client.scale.service(
        project_id=resolve_project_id(ctx, project_id),
        service_id=service_id,
        team_id=resolve_team_id(ctx, team_id),
        data=data,
    )
    return unwrap(response)


@function_tool
async def nf_wait_service_ready(
    ctx: RunContextWrapper[NorthflankCtx],
    service_id: str,
    timeout_s: float = 300.0,
    poll_interval_s: float = 3.0,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Poll a service until its deployment status is COMPLETED."""
    payload: dict[str, Any] = await ctx.context.client.helpers.wait_for_service_ready(
        project_id=resolve_project_id(ctx, project_id),
        service_id=service_id,
        team_id=resolve_team_id(ctx, team_id),
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
    )
    return payload


# ---------------------------------------------------------------------------
# Exec tools (mutating — needs_approval=True)
# ---------------------------------------------------------------------------


def _exec_to_dict(result: Any) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code,
        "status": result.status,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "ok": result.ok,
        "message": getattr(result, "message", ""),
    }


@function_tool(needs_approval=True)
async def nf_run_service_command(
    ctx: RunContextWrapper[NorthflankCtx],
    service_id: str,
    command: str,
    shell: ShellMode = "sh",
    instance_name: str | None = None,
    container_name: str | None = None,
    timeout: float = 60.0,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Run a one-shot command inside a service container via Northflank exec.

    Args:
        command: A single shell command line (e.g. "ls -la /tmp | head").
        shell: Shell to run the command in. ``sh`` / ``bash`` wrap the command
            in ``[shell, "-lc", command]`` so pipes and redirection work.
            Use ``none`` to skip the shell — in that case ``command`` is passed
            to ``exec`` directly with the kernel splitting on whitespace, so
            shell features will not be interpreted.
        instance_name: Specific replica to target; otherwise the proxy picks one.
        container_name: Specific sidecar container name, if the service has many.
        timeout: Seconds to wait for completion.
    """
    sdk_command, sdk_shell = wrap_shell_command(command, shell)
    result = await ctx.context.client.exec.arun_service_command(
        project_id=resolve_project_id(ctx, project_id),
        service_id=service_id,
        command=sdk_command,
        team_id=resolve_team_id(ctx, team_id),
        shell=sdk_shell,
        instance_name=instance_name,
        container_name=container_name,
        timeout=timeout,
    )
    return _exec_to_dict(result)


@function_tool(needs_approval=True)
async def nf_run_job_command(
    ctx: RunContextWrapper[NorthflankCtx],
    job_id: str,
    command: str,
    shell: ShellMode = "sh",
    instance_name: str | None = None,
    container_name: str | None = None,
    timeout: float = 60.0,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Run a one-shot command inside a running job container.

    See :func:`nf_run_service_command` for the ``shell`` semantics.
    """
    sdk_command, sdk_shell = wrap_shell_command(command, shell)
    result = await ctx.context.client.exec.arun_job_command(
        project_id=resolve_project_id(ctx, project_id),
        job_id=job_id,
        command=sdk_command,
        team_id=resolve_team_id(ctx, team_id),
        shell=sdk_shell,
        instance_name=instance_name,
        container_name=container_name,
        timeout=timeout,
    )
    return _exec_to_dict(result)


@function_tool(needs_approval=True)
async def nf_run_addon_command(
    ctx: RunContextWrapper[NorthflankCtx],
    addon_id: str,
    command: str,
    shell: ShellMode = "sh",
    instance_name: str | None = None,
    container_name: str | None = None,
    timeout: float = 60.0,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Run a one-shot command inside an addon (e.g. database) container.

    See :func:`nf_run_service_command` for the ``shell`` semantics.
    """
    sdk_command, sdk_shell = wrap_shell_command(command, shell)
    result = await ctx.context.client.exec.arun_addon_command(
        project_id=resolve_project_id(ctx, project_id),
        addon_id=addon_id,
        command=sdk_command,
        team_id=resolve_team_id(ctx, team_id),
        shell=sdk_shell,
        instance_name=instance_name,
        container_name=container_name,
        timeout=timeout,
    )
    return _exec_to_dict(result)


# ---------------------------------------------------------------------------
# Logs tools
# ---------------------------------------------------------------------------


def _log_lines_to_list(lines: Any) -> list[dict[str, Any]]:
    return [
        {
            "ts": getattr(ln, "ts", ""),
            "container_id": getattr(ln, "container_id", ""),
            "log": ln.log,
        }
        for ln in lines
    ]


@function_tool
async def nf_fetch_service_logs(
    ctx: RunContextWrapper[NorthflankCtx],
    service_id: str,
    log_type: Literal["runtime", "build"] = "runtime",
    line_limit: int = 200,
    duration_seconds: int = 600,
    direction: Literal["backward", "forward"] = "backward",
    text_includes: str | None = None,
    project_id: str | None = None,
    team_id: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch a bounded range of service logs over plain HTTP.

    Args:
        log_type: ``runtime`` for app logs, ``build`` for build logs.
        line_limit: Max number of log lines to return (defaults to 200).
        duration_seconds: How far back to look from ``now`` (when direction is backward).
        direction: ``backward`` searches from now into the past; ``forward`` is the inverse.
        text_includes: Optional substring filter applied server-side.
    """
    pid = resolve_project_id(ctx, project_id)
    tid = resolve_team_id(ctx, team_id)
    # The SDK exposes ``fetch_service_logs`` as a sync method; offload it so
    # we don't block the event loop.
    lines = await asyncio.to_thread(
        ctx.context.client.logs.fetch_service_logs,
        project_id=pid,
        service_id=service_id,
        team_id=tid,
        log_type=log_type,
        line_limit=line_limit,
        duration_seconds=duration_seconds,
        direction=direction,
        text_includes=text_includes,
    )
    return _log_lines_to_list(lines)


@function_tool
async def nf_tail_service_logs(
    ctx: RunContextWrapper[NorthflankCtx],
    service_id: str,
    max_lines: int = 100,
    timeout_s: float = 10.0,
    project_id: str | None = None,
    team_id: str | None = None,
) -> list[dict[str, Any]]:
    """Tail live service logs for a bounded duration / line count.

    Useful when ``fetch_service_logs`` is empty because the service has only
    just started emitting output.
    """
    pid = resolve_project_id(ctx, project_id)
    tid = resolve_team_id(ctx, team_id)
    collected: list[dict[str, Any]] = []

    async def _run() -> None:
        tail = await ctx.context.client.logs.atail_service_logs(
            project_id=pid, service_id=service_id, team_id=tid
        )
        async with tail:
            async for ln in tail:
                collected.append(
                    {
                        "ts": getattr(ln, "ts", ""),
                        "container_id": getattr(ln, "container_id", ""),
                        "log": ln.log,
                    }
                )
                if len(collected) >= max_lines:
                    break

    try:
        await asyncio.wait_for(_run(), timeout=timeout_s)
    except asyncio.TimeoutError:
        pass
    return collected


# ---------------------------------------------------------------------------
# Metrics tools
# ---------------------------------------------------------------------------


MetricType = Literal[
    "cpu",
    "memory",
    "networkIngress",
    "networkEgress",
    "tcpConnectionsOpen",
    "diskUsage",
    "requests",
    "http4xxResponses",
    "http5xxResponses",
    "bandwidth",
    "bandwidthVolume",
]


@function_tool
async def nf_get_service_metrics(
    ctx: RunContextWrapper[NorthflankCtx],
    service_id: str,
    metric_types: list[MetricType] | None = None,
    query_type: Literal["range", "single"] = "range",
    duration: int | None = 300,
    start_time: str | None = None,
    end_time: str | None = None,
    container_name: str | None = None,
    deployment_id: str | None = None,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Fetch service metrics from the Northflank monitoring API.

    Args:
        metric_types: Which metrics to fetch (default: cpu and memory).
        query_type: ``range`` for a time series, ``single`` for one timestamp.
        duration: For range queries, lookback window in seconds.
        start_time / end_time: ISO-8601 timestamps overriding ``duration``.
        container_name: Restrict to a single sidecar.
        deployment_id: Restrict to a specific deployment.
    """
    response = await ctx.context.client.get.service.metrics(
        project_id=resolve_project_id(ctx, project_id),
        service_id=service_id,
        team_id=resolve_team_id(ctx, team_id),
        metric_types=metric_types or ["cpu", "memory"],
        query_type=query_type,
        duration=duration,
        start_time=start_time,
        end_time=end_time,
        container_name=container_name,
        deployment_id=deployment_id,
    )
    return unwrap(response)


# ---------------------------------------------------------------------------
# Gated mutations (opt-in via northflank_tools(include_delete=True, ...))
# ---------------------------------------------------------------------------


@function_tool(needs_approval=True)
async def nf_delete_service(
    ctx: RunContextWrapper[NorthflankCtx],
    service_id: str,
    delete_child_objects: bool = False,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Delete a service. Pass delete_child_objects=True to remove dependent
    resources (volumes, secrets) as well."""
    response = await ctx.context.client.delete.service(
        project_id=resolve_project_id(ctx, project_id),
        service_id=service_id,
        team_id=resolve_team_id(ctx, team_id),
        delete_child_objects=delete_child_objects,
    )
    return unwrap(response)


@function_tool(needs_approval=True, strict_mode=False)
async def nf_put_secret(
    ctx: RunContextWrapper[NorthflankCtx],
    data: dict[str, Any],
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Create or update a project-scoped secret.

    The ``data`` payload is a Northflank ``CreateSecretRequest``:

    - ``name`` (required): secret group name within the project.
    - ``priority`` (required): integer used to resolve precedence when
      multiple secrets define the same key (higher wins).
    - ``secretType`` (required): one of ``"environment-arguments"``,
      ``"environment"``, ``"arguments"``.
    - ``secrets`` (optional): the actual values, with sub-keys ``variables``
      (env vars), ``files`` (mounted files), and ``dockerSecretMounts``.
    - Additional optional fields: ``description``, ``restrictions``,
      ``stageId``, ``tags``, ``type`` (``"secret"`` | ``"config"``),
      ``addonDependencies``, ``externalAddonDependencies``.

    Minimum example::

        {
            "name": "api-keys",
            "priority": 10,
            "secretType": "environment",
            "secrets": {"variables": {"OPENAI_API_KEY": "sk-..."}},
        }
    """
    response = await ctx.context.client.put.secret(
        project_id=resolve_project_id(ctx, project_id),
        team_id=resolve_team_id(ctx, team_id),
        data=data,
    )
    return unwrap(response)


@function_tool(needs_approval=True)
async def nf_delete_secret(
    ctx: RunContextWrapper[NorthflankCtx],
    secret_id: str,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Delete a project-scoped secret."""
    response = await ctx.context.client.delete.secret(
        project_id=resolve_project_id(ctx, project_id),
        secret_id=secret_id,
        team_id=resolve_team_id(ctx, team_id),
    )
    return unwrap(response)


@function_tool(needs_approval=True)
async def nf_delete_volume(
    ctx: RunContextWrapper[NorthflankCtx],
    volume_id: str,
    project_id: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Delete a persistent volume."""
    response = await ctx.context.client.delete.volume(
        project_id=resolve_project_id(ctx, project_id),
        volume_id=volume_id,
        team_id=resolve_team_id(ctx, team_id),
    )
    return unwrap(response)


@function_tool(needs_approval=True)
async def nf_delete_domain(
    ctx: RunContextWrapper[NorthflankCtx],
    domain: str,
    team_id: str | None = None,
) -> dict[str, Any]:
    """Delete a custom domain registration."""
    response = await ctx.context.client.delete.domain(
        domain=domain,
        team_id=resolve_team_id(ctx, team_id),
    )
    return unwrap(response)


# ---------------------------------------------------------------------------
# Public namespace factory
# ---------------------------------------------------------------------------


_READ_TOOLS: list[FunctionTool] = [
    nf_list_projects,
    nf_get_project,
    nf_list_services,
    nf_get_service,
]

_DEPLOY_TOOLS: list[FunctionTool] = [
    nf_create_deployment_service,
    nf_restart_service,
    nf_pause_service,
    nf_resume_service,
    nf_scale_service,
    nf_wait_service_ready,
]

_EXEC_TOOLS: list[FunctionTool] = [
    nf_run_service_command,
    nf_run_job_command,
    nf_run_addon_command,
]

_LOG_TOOLS: list[FunctionTool] = [nf_fetch_service_logs, nf_tail_service_logs]
_METRICS_TOOLS: list[FunctionTool] = [nf_get_service_metrics]


def northflank_tools(
    *,
    include_delete: bool = False,
    include_secrets: bool = False,
    include_volumes: bool = False,
    include_domains: bool = False,
    namespace: str = "northflank",
    description: str = (
        "Read, deploy, scale, exec, log, and inspect Northflank services. "
        "Mutating actions require approval."
    ),
) -> list[FunctionTool]:
    """Return the curated Northflank tool list grouped under a tool namespace.

    Gated mutating tools (delete, secrets, volumes, domains) are off by
    default — opt-in per category to keep the model's tool surface small.
    """
    tools: list[FunctionTool] = []
    tools.extend(_READ_TOOLS)
    tools.extend(_DEPLOY_TOOLS)
    tools.extend(_EXEC_TOOLS)
    tools.extend(_LOG_TOOLS)
    tools.extend(_METRICS_TOOLS)
    if include_delete:
        tools.append(nf_delete_service)
    if include_secrets:
        tools.extend([nf_put_secret, nf_delete_secret])
    if include_volumes:
        tools.append(nf_delete_volume)
    if include_domains:
        tools.append(nf_delete_domain)

    return tool_namespace(name=namespace, description=description, tools=tools)


__all__ = [
    "northflank_tools",
    # Read tools
    "nf_list_projects",
    "nf_get_project",
    "nf_list_services",
    "nf_get_service",
    # Deploy / lifecycle
    "nf_create_deployment_service",
    "nf_restart_service",
    "nf_pause_service",
    "nf_resume_service",
    "nf_scale_service",
    "nf_wait_service_ready",
    # Exec
    "nf_run_service_command",
    "nf_run_job_command",
    "nf_run_addon_command",
    # Logs
    "nf_fetch_service_logs",
    "nf_tail_service_logs",
    # Metrics
    "nf_get_service_metrics",
    # Gated
    "nf_delete_service",
    "nf_put_secret",
    "nf_delete_secret",
    "nf_delete_volume",
    "nf_delete_domain",
]
