"""Northflank-backed implementation of the agents ShellTool executor.

Plug into the agents SDK like so::

    from agents import Agent, ShellTool
    from agents.extensions.northflank import NorthflankShellExecutor, NorthflankCtx

    executor = NorthflankShellExecutor(service_id="my-svc", project_id="my-proj")
    agent = Agent(
        name="ops",
        tools=[ShellTool(executor=executor, needs_approval=True)],
    )
    await Runner.run(agent, "free -h", context=NorthflankCtx(client=client))

The executor pulls the ``AsyncApiClient`` (and any unset project/team) from
:class:`NorthflankCtx` on the run context, so a single executor instance can
be reused across runs that share the same target service.
"""

from __future__ import annotations

from ...tool import (
    ShellCallOutcome,
    ShellCommandOutput,
    ShellCommandRequest,
    ShellResult,
)
from ._helpers import ShellMode, wrap_shell_command
from .context import NorthflankCtx

try:
    from northflank import AsyncApiClient
except ImportError as exc:  # pragma: no cover - import path depends on optional extras
    raise ImportError(
        "Northflank shell executor requires the optional `northflank` extra.\n"
        "Install it with: pip install 'openai-agents[northflank]'"
    ) from exc


class NorthflankShellExecutor:
    """ShellTool executor that runs each command inside a Northflank service.

    Construction args bind the executor to one target container. ``client``,
    ``project_id`` and ``team_id`` fall back to the :class:`NorthflankCtx` on
    the request if not provided at construction time.
    """

    def __init__(
        self,
        *,
        service_id: str,
        project_id: str | None = None,
        team_id: str | None = None,
        client: AsyncApiClient | None = None,
        shell: ShellMode = "sh",
        instance_name: str | None = None,
        container_name: str | None = None,
        encoding: str = "utf-8",
        default_timeout_s: float = 60.0,
    ) -> None:
        self.service_id = service_id
        self.project_id = project_id
        self.team_id = team_id
        self.client = client
        self.shell = shell
        self.instance_name = instance_name
        self.container_name = container_name
        self.encoding = encoding
        self.default_timeout_s = default_timeout_s

    def _resolve(self, request: ShellCommandRequest) -> tuple[AsyncApiClient, str, str | None]:
        ctx_obj = getattr(request.ctx_wrapper, "context", None)
        client = self.client
        project_id = self.project_id
        team_id = self.team_id

        if isinstance(ctx_obj, NorthflankCtx):
            client = client or ctx_obj.client
            project_id = project_id or ctx_obj.project_id
            team_id = team_id or ctx_obj.team_id

        if client is None:
            raise RuntimeError(
                "NorthflankShellExecutor needs an AsyncApiClient: pass one to "
                "the constructor or provide a NorthflankCtx on the run."
            )
        if not project_id:
            raise RuntimeError(
                "NorthflankShellExecutor needs project_id: pass it to the "
                "constructor or set NorthflankCtx.project_id."
            )
        return client, project_id, team_id

    async def __call__(self, request: ShellCommandRequest) -> ShellResult:
        client, project_id, team_id = self._resolve(request)
        action = request.data.action
        timeout_s = action.timeout_ms / 1000.0 if action.timeout_ms else self.default_timeout_s

        outputs: list[ShellCommandOutput] = []
        for command in action.commands:
            sdk_command, sdk_shell = wrap_shell_command(command, self.shell)
            try:
                result = await client.exec.arun_service_command(
                    project_id=project_id,
                    service_id=self.service_id,
                    command=sdk_command,
                    team_id=team_id,
                    shell=sdk_shell,
                    instance_name=self.instance_name,
                    container_name=self.container_name,
                    encoding=self.encoding,
                    timeout=timeout_s,
                )
            except TimeoutError as exc:
                outputs.append(
                    ShellCommandOutput(
                        command=command,
                        stdout="",
                        stderr=str(exc),
                        outcome=ShellCallOutcome(type="timeout", exit_code=None),
                    )
                )
                break

            outputs.append(
                ShellCommandOutput(
                    command=command,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    outcome=ShellCallOutcome(type="exit", exit_code=result.exit_code),
                    provider_data={
                        "status": result.status,
                        "service_id": self.service_id,
                        "project_id": project_id,
                    },
                )
            )

            if result.exit_code not in (0, None):
                # Mirror local shell semantics: abort the batch on the first
                # non-zero exit so the model sees a clean failure.
                break

        return ShellResult(
            output=outputs,
            max_output_length=action.max_output_length,
            provider_data={"service_id": self.service_id, "project_id": project_id},
        )


__all__ = ["NorthflankShellExecutor"]
