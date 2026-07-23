from __future__ import annotations

from typing import Literal

from pydantic import Field

from ....sandbox.session.sandbox_client import BaseSandboxClientOptions
from ....sandbox.session.sandbox_session_state import SandboxSessionState

DEFAULT_DISK = "ubuntu"
DEFAULT_AUTO_SUSPEND_SECONDS = 300
DEFAULT_POLLING_INTERVAL_SECONDS = 3.0
DEFAULT_POLLING_TIMEOUT_SECONDS = 300.0
DEFAULT_ENSURE_RUNNING_TIMEOUT_SECONDS = 300.0


class ACASandboxesClientOptions(BaseSandboxClientOptions):
    """Create-time options for the ACA Sandboxes provider."""

    type: Literal["aca_sandboxes"] = "aca_sandboxes"
    disk: str = DEFAULT_DISK
    cpu: str | None = None
    memory: str | None = None
    disk_size: str | None = None
    auto_suspend_seconds: int = Field(default=DEFAULT_AUTO_SUSPEND_SECONDS, gt=0)
    auto_suspend_mode: Literal["Memory", "Disk"] = "Memory"
    labels: dict[str, str] | None = None
    environment: dict[str, str] | None = None
    exposed_ports: tuple[int, ...] = ()
    exposed_port_anonymous: bool = False
    polling_interval_seconds: float = Field(default=DEFAULT_POLLING_INTERVAL_SECONDS, gt=0)
    polling_timeout_seconds: float = Field(default=DEFAULT_POLLING_TIMEOUT_SECONDS, gt=0)
    ensure_running_timeout_seconds: float = Field(
        default=DEFAULT_ENSURE_RUNNING_TIMEOUT_SECONDS,
        gt=0,
    )

    def __init__(
        self,
        disk: str = DEFAULT_DISK,
        cpu: str | None = None,
        memory: str | None = None,
        disk_size: str | None = None,
        auto_suspend_seconds: int = DEFAULT_AUTO_SUSPEND_SECONDS,
        auto_suspend_mode: Literal["Memory", "Disk"] = "Memory",
        labels: dict[str, str] | None = None,
        environment: dict[str, str] | None = None,
        exposed_ports: tuple[int, ...] = (),
        exposed_port_anonymous: bool = False,
        polling_interval_seconds: float = DEFAULT_POLLING_INTERVAL_SECONDS,
        polling_timeout_seconds: float = DEFAULT_POLLING_TIMEOUT_SECONDS,
        ensure_running_timeout_seconds: float = DEFAULT_ENSURE_RUNNING_TIMEOUT_SECONDS,
        *,
        type: Literal["aca_sandboxes"] = "aca_sandboxes",
    ) -> None:
        super().__init__(
            type=type,
            disk=disk,
            cpu=cpu,
            memory=memory,
            disk_size=disk_size,
            auto_suspend_seconds=auto_suspend_seconds,
            auto_suspend_mode=auto_suspend_mode,
            labels=labels,
            environment=environment,
            exposed_ports=exposed_ports,
            exposed_port_anonymous=exposed_port_anonymous,
            polling_interval_seconds=polling_interval_seconds,
            polling_timeout_seconds=polling_timeout_seconds,
            ensure_running_timeout_seconds=ensure_running_timeout_seconds,
        )


class ACASandboxesSessionState(SandboxSessionState):
    """Serialized ACA sandbox identity used for resume."""

    type: Literal["aca_sandboxes"] = "aca_sandboxes"
    sandbox_id: str
    subscription_id: str
    resource_group: str
    sandbox_group: str
    region: str
    disk: str | None = None
    disk_size: str | None = None
    auto_suspend_seconds: int = DEFAULT_AUTO_SUSPEND_SECONDS
    auto_suspend_mode: Literal["Memory", "Disk"] = "Memory"
    exposed_port_anonymous: bool = False
    ensure_running_timeout_seconds: float = DEFAULT_ENSURE_RUNNING_TIMEOUT_SECONDS
    labels: dict[str, str] | None = None
    environment: dict[str, str] | None = None


__all__ = [
    "ACASandboxesClientOptions",
    "ACASandboxesSessionState",
]
