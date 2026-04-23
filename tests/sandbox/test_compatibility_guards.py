from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterable
from typing import Any, TypeVar, cast

import pytest
from pydantic import TypeAdapter

import agents.sandbox as sandbox_package
import agents.sandbox.capabilities as capabilities_package
import agents.sandbox.entries as entries_package
import agents.sandbox.session as session_package
from agents import Agent
from agents.run_config import SandboxConcurrencyLimits, SandboxRunConfig
from agents.run_context import RunContextWrapper
from agents.run_state import RunState
from agents.sandbox import Manifest
from agents.sandbox.entries import (
    AzureBlobMount,
    Dir,
    DockerVolumeMountStrategy,
    File,
    GCSMount,
    GitRepo,
    InContainerMountStrategy,
    LocalDir,
    LocalFile,
    MountPattern,
    R2Mount,
    S3FilesMount,
    S3Mount,
)
from agents.sandbox.entries.base import BaseEntry
from agents.sandbox.entries.mounts.base import MountStrategyBase
from agents.sandbox.entries.mounts.patterns import (
    FuseMountPattern,
    MountpointMountPattern,
    RcloneMountPattern,
    S3FilesMountPattern,
)
from agents.sandbox.session.sandbox_client import BaseSandboxClientOptions
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import LocalSnapshot, NoopSnapshot, RemoteSnapshot, SnapshotBase
from tests.utils.factories import TestSessionState

StateT = TypeVar("StateT", bound=SandboxSessionState)


def _session_state_kwargs() -> dict[str, object]:
    return {
        "session_id": uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        "snapshot": NoopSnapshot(id="snapshot-123"),
        "manifest": Manifest(root="/workspace"),
        "exposed_ports": (8000,),
        "workspace_root_ready": True,
    }


def _make_session_state(cls: type[StateT], **overrides: object) -> StateT:
    return cls.model_validate({**_session_state_kwargs(), **overrides})


def _import_optional_class(module_name: str, class_name: str) -> type[Any]:
    module = pytest.importorskip(module_name)
    value = getattr(module, class_name)
    assert isinstance(value, type)
    return cast(type[Any], value)


def _instantiate_optional_class(
    module_name: str,
    class_name: str,
    *args: object,
    **kwargs: object,
) -> Any:
    cls = _import_optional_class(module_name, class_name)
    return cls(*args, **kwargs)


def _make_optional_session_state(
    module_name: str,
    class_name: str,
    **overrides: object,
) -> SandboxSessionState:
    cls = _import_optional_class(module_name, class_name)
    return cast(SandboxSessionState, cls.model_validate({**_session_state_kwargs(), **overrides}))


def test_core_sandbox_public_export_surface_is_stable() -> None:
    expected_exports = {
        "agents.sandbox": {
            "Capability",
            "Dir",
            "ErrorCode",
            "ExecResult",
            "ExposedPortEndpoint",
            "ExposedPortUnavailableError",
            "ExecTimeoutError",
            "ExecTransportError",
            "FileMode",
            "Group",
            "LocalFile",
            "LocalSnapshot",
            "LocalSnapshotSpec",
            "Manifest",
            "MemoryLayoutConfig",
            "MemoryReadConfig",
            "MemoryGenerateConfig",
            "RemoteSnapshot",
            "RemoteSnapshotSpec",
            "Permissions",
            "SandboxAgent",
            "SandboxPathGrant",
            "SandboxConcurrencyLimits",
            "SandboxError",
            "SandboxRunConfig",
            "SnapshotSpec",
            "WorkspaceArchiveReadError",
            "WorkspaceArchiveWriteError",
            "WorkspaceReadNotFoundError",
            "WorkspaceWriteTypeError",
            "User",
            "resolve_snapshot",
        },
        "agents.sandbox.entries": {
            "AzureBlobMount",
            "BaseEntry",
            "BoxMount",
            "Dir",
            "File",
            "DockerVolumeMountStrategy",
            "FuseMountPattern",
            "GCSMount",
            "GitRepo",
            "InContainerMountStrategy",
            "LocalDir",
            "LocalFile",
            "Mount",
            "MountPattern",
            "MountPatternBase",
            "MountStrategy",
            "MountStrategyBase",
            "MountpointMountPattern",
            "R2Mount",
            "RcloneMountPattern",
            "S3Mount",
            "S3FilesMount",
            "S3FilesMountPattern",
            "resolve_workspace_path",
        },
        "agents.sandbox.capabilities": {
            "Capability",
            "Capabilities",
            "Compaction",
            "CompactionModelInfo",
            "CompactionPolicy",
            "DynamicCompactionPolicy",
            "FilesystemToolSet",
            "LazySkillSource",
            "LocalDirLazySkillSource",
            "Memory",
            "Shell",
            "ShellToolSet",
            "Skill",
            "SkillMetadata",
            "Skills",
            "StaticCompactionPolicy",
            "Filesystem",
        },
        "agents.sandbox.session": {
            "BaseSandboxClient",
            "BaseSandboxClientOptions",
            "BaseSandboxSession",
            "CallbackSink",
            "ChainedSink",
            "ClientOptionsT",
            "Dependencies",
            "DependenciesBindingError",
            "DependenciesError",
            "DependenciesMissingDependencyError",
            "DependencyKey",
            "ExposedPortEndpoint",
            "EventPayloadPolicy",
            "EventSink",
            "HttpProxySink",
            "Instrumentation",
            "JsonlOutboxSink",
            "SandboxSession",
            "SandboxSessionEvent",
            "SandboxSessionFinishEvent",
            "SandboxSessionStartEvent",
            "SandboxSessionState",
            "WorkspaceJsonlSink",
            "event_to_json_line",
            "validate_sandbox_session_event",
        },
    }
    modules = {
        "agents.sandbox": sandbox_package,
        "agents.sandbox.entries": entries_package,
        "agents.sandbox.capabilities": capabilities_package,
        "agents.sandbox.session": session_package,
    }

    for module_name, exports in expected_exports.items():
        module = modules[module_name]
        assert set(module.__all__) == exports
        for name in exports:
            assert getattr(module, name) is not None


@pytest.mark.parametrize(
    ("module_name", "expected_exports"),
    [
        (
            "agents.extensions.sandbox.e2b",
            {
                "_E2BSandboxFactoryAPI",
                "_encode_e2b_snapshot_ref",
                "_import_sandbox_class",
                "_sandbox_connect",
                "E2BCloudBucketMountStrategy",
                "E2BSandboxClient",
                "E2BSandboxClientOptions",
                "E2BSandboxSession",
                "E2BSandboxSessionState",
                "E2BSandboxTimeouts",
                "E2BSandboxType",
            },
        ),
        (
            "agents.extensions.sandbox.modal",
            {
                "_DEFAULT_TIMEOUT_S",
                "_MODAL_STDIN_CHUNK_SIZE",
                "_encode_modal_snapshot_ref",
                "_encode_snapshot_directory_ref",
                "_encode_snapshot_filesystem_ref",
                "ModalCloudBucketMountConfig",
                "ModalCloudBucketMountStrategy",
                "ModalImageSelector",
                "ModalSandboxClient",
                "ModalSandboxClientOptions",
                "ModalSandboxSelector",
                "ModalSandboxSession",
                "ModalSandboxSessionState",
                "resolve_snapshot",
                "tarfile",
            },
        ),
        (
            "agents.extensions.sandbox.daytona",
            {
                "DEFAULT_DAYTONA_WORKSPACE_ROOT",
                "DaytonaCloudBucketMountStrategy",
                "DaytonaSandboxResources",
                "DaytonaSandboxClient",
                "DaytonaSandboxClientOptions",
                "DaytonaSandboxSession",
                "DaytonaSandboxSessionState",
                "DaytonaSandboxTimeouts",
                "ExposedPortUnavailableError",
                "InvalidManifestPathError",
                "WorkspaceArchiveReadError",
            },
        ),
        (
            "agents.extensions.sandbox.blaxel",
            {
                "DEFAULT_BLAXEL_WORKSPACE_ROOT",
                "BlaxelCloudBucketMountConfig",
                "BlaxelCloudBucketMountStrategy",
                "BlaxelDriveMount",
                "BlaxelDriveMountConfig",
                "BlaxelDriveMountStrategy",
                "BlaxelSandboxClient",
                "BlaxelSandboxClientOptions",
                "BlaxelSandboxSession",
                "BlaxelSandboxSessionState",
                "BlaxelTimeouts",
                "ExposedPortUnavailableError",
                "InvalidManifestPathError",
                "WorkspaceArchiveReadError",
            },
        ),
        (
            "agents.extensions.sandbox.cloudflare",
            {
                "CloudflareBucketMountConfig",
                "CloudflareBucketMountStrategy",
                "CloudflareSandboxClient",
                "CloudflareSandboxClientOptions",
                "CloudflareSandboxSession",
                "CloudflareSandboxSessionState",
            },
        ),
        (
            "agents.extensions.sandbox.runloop",
            {
                "DEFAULT_RUNLOOP_WORKSPACE_ROOT",
                "DEFAULT_RUNLOOP_ROOT_WORKSPACE_ROOT",
                "RunloopAfterIdle",
                "RunloopGatewaySpec",
                "RunloopLaunchParameters",
                "RunloopMcpSpec",
                "RunloopPlatformAxonsClient",
                "RunloopPlatformBenchmarksClient",
                "RunloopPlatformBlueprintsClient",
                "RunloopPlatformClient",
                "RunloopPlatformNetworkPoliciesClient",
                "RunloopPlatformSecretsClient",
                "RunloopCloudBucketMountStrategy",
                "RunloopSandboxClient",
                "RunloopSandboxClientOptions",
                "RunloopSandboxSession",
                "RunloopSandboxSessionState",
                "RunloopTimeouts",
                "RunloopTunnelConfig",
                "RunloopUserParameters",
                "_decode_runloop_snapshot_ref",
                "_encode_runloop_snapshot_ref",
            },
        ),
        (
            "agents.extensions.sandbox.vercel",
            {
                "VercelSandboxClient",
                "VercelSandboxClientOptions",
                "VercelSandboxSession",
                "VercelSandboxSessionState",
            },
        ),
    ],
)
def test_extension_sandbox_package_export_surfaces_are_stable(
    module_name: str,
    expected_exports: set[str],
) -> None:
    module = pytest.importorskip(module_name)

    assert set(module.__all__) == expected_exports
    for name in expected_exports:
        assert getattr(module, name) is not None


def test_sandbox_dataclass_constructor_field_order_is_stable() -> None:
    assert _dataclass_field_names(SandboxConcurrencyLimits) == (
        "manifest_entries",
        "local_dir_files",
    )
    assert _dataclass_field_names(SandboxRunConfig) == (
        "client",
        "options",
        "session",
        "session_state",
        "manifest",
        "snapshot",
        "concurrency_limits",
    )


@pytest.mark.parametrize(
    ("module_name", "class_name", "expected_fields"),
    [
        (
            "agents.extensions.sandbox.blaxel",
            "BlaxelSandboxClientOptions",
            (
                "image",
                "memory",
                "region",
                "ports",
                "env_vars",
                "labels",
                "ttl",
                "name",
                "pause_on_exit",
                "timeouts",
                "exposed_port_public",
                "exposed_port_url_ttl_s",
            ),
        ),
    ],
)
def test_optional_sandbox_dataclass_constructor_field_order_is_stable(
    module_name: str,
    class_name: str,
    expected_fields: tuple[str, ...],
) -> None:
    cls = _import_optional_class(module_name, class_name)
    assert _dataclass_field_names(cls) == expected_fields


@pytest.mark.parametrize(
    ("module_name", "class_name", "expected_fields"),
    [
        (
            "agents.sandbox.sandboxes.unix_local",
            "UnixLocalSandboxClientOptions",
            ("exposed_ports",),
        ),
        (
            "agents.sandbox.sandboxes.docker",
            "DockerSandboxClientOptions",
            ("image", "exposed_ports"),
        ),
        (
            "agents.extensions.sandbox.e2b",
            "E2BSandboxClientOptions",
            (
                "sandbox_type",
                "template",
                "timeout",
                "metadata",
                "envs",
                "secure",
                "allow_internet_access",
                "timeouts",
                "pause_on_exit",
                "exposed_ports",
                "workspace_persistence",
                "on_timeout",
                "auto_resume",
                "mcp",
            ),
        ),
        (
            "agents.extensions.sandbox.modal",
            "ModalSandboxClientOptions",
            (
                "app_name",
                "sandbox_create_timeout_s",
                "workspace_persistence",
                "snapshot_filesystem_timeout_s",
                "snapshot_filesystem_restore_timeout_s",
                "exposed_ports",
                "gpu",
                "timeout",
                "use_sleep_cmd",
                "image_builder_version",
                "idle_timeout",
            ),
        ),
        (
            "agents.extensions.sandbox.cloudflare",
            "CloudflareSandboxClientOptions",
            ("worker_url", "api_key", "exposed_ports"),
        ),
        (
            "agents.extensions.sandbox.daytona",
            "DaytonaSandboxClientOptions",
            (
                "sandbox_snapshot_name",
                "image",
                "resources",
                "env_vars",
                "pause_on_exit",
                "create_timeout",
                "start_timeout",
                "name",
                "auto_stop_interval",
                "timeouts",
                "exposed_ports",
                "exposed_port_url_ttl_s",
            ),
        ),
        (
            "agents.extensions.sandbox.runloop",
            "RunloopSandboxClientOptions",
            (
                "blueprint_id",
                "blueprint_name",
                "env_vars",
                "pause_on_exit",
                "name",
                "timeouts",
                "exposed_ports",
                "user_parameters",
                "launch_parameters",
                "tunnel",
                "gateways",
                "mcp",
                "metadata",
                "managed_secrets",
            ),
        ),
        (
            "agents.extensions.sandbox.vercel",
            "VercelSandboxClientOptions",
            (
                "project_id",
                "team_id",
                "timeout_ms",
                "runtime",
                "resources",
                "env",
                "exposed_ports",
                "interactive",
                "workspace_persistence",
                "snapshot_expiration_ms",
                "network_policy",
            ),
        ),
    ],
)
def test_optional_sandbox_client_options_positional_field_order_is_stable(
    module_name: str,
    class_name: str,
    expected_fields: tuple[str, ...],
) -> None:
    options_cls = _import_optional_class(module_name, class_name)
    assert _model_field_names(options_cls, exclude={"type"}) == expected_fields


@pytest.mark.parametrize(
    ("state_cls_or_module", "class_name", "expected_fields"),
    [
        (
            SandboxSessionState,
            None,
            (
                "type",
                "session_id",
                "snapshot",
                "manifest",
                "exposed_ports",
                "snapshot_fingerprint",
                "snapshot_fingerprint_version",
                "workspace_root_ready",
            ),
        ),
        (
            "agents.sandbox.sandboxes.unix_local",
            "UnixLocalSandboxSessionState",
            (
                "type",
                "session_id",
                "snapshot",
                "manifest",
                "exposed_ports",
                "snapshot_fingerprint",
                "snapshot_fingerprint_version",
                "workspace_root_ready",
                "workspace_root_owned",
            ),
        ),
        (
            "agents.sandbox.sandboxes.docker",
            "DockerSandboxSessionState",
            (
                "type",
                "session_id",
                "snapshot",
                "manifest",
                "exposed_ports",
                "snapshot_fingerprint",
                "snapshot_fingerprint_version",
                "workspace_root_ready",
                "image",
                "container_id",
            ),
        ),
        (
            "agents.extensions.sandbox.e2b",
            "E2BSandboxSessionState",
            (
                "type",
                "session_id",
                "snapshot",
                "manifest",
                "exposed_ports",
                "snapshot_fingerprint",
                "snapshot_fingerprint_version",
                "workspace_root_ready",
                "sandbox_id",
                "sandbox_type",
                "template",
                "sandbox_timeout",
                "metadata",
                "base_envs",
                "secure",
                "allow_internet_access",
                "timeouts",
                "pause_on_exit",
                "workspace_persistence",
                "on_timeout",
                "auto_resume",
                "mcp",
            ),
        ),
        (
            "agents.extensions.sandbox.modal",
            "ModalSandboxSessionState",
            (
                "type",
                "session_id",
                "snapshot",
                "manifest",
                "exposed_ports",
                "snapshot_fingerprint",
                "snapshot_fingerprint_version",
                "workspace_root_ready",
                "app_name",
                "image_id",
                "image_tag",
                "sandbox_create_timeout_s",
                "sandbox_id",
                "workspace_persistence",
                "snapshot_filesystem_timeout_s",
                "snapshot_filesystem_restore_timeout_s",
                "gpu",
                "timeout",
                "use_sleep_cmd",
                "image_builder_version",
                "idle_timeout",
            ),
        ),
        (
            "agents.extensions.sandbox.cloudflare",
            "CloudflareSandboxSessionState",
            (
                "type",
                "session_id",
                "snapshot",
                "manifest",
                "exposed_ports",
                "snapshot_fingerprint",
                "snapshot_fingerprint_version",
                "workspace_root_ready",
                "worker_url",
                "sandbox_id",
            ),
        ),
        (
            "agents.extensions.sandbox.daytona",
            "DaytonaSandboxSessionState",
            (
                "type",
                "session_id",
                "snapshot",
                "manifest",
                "exposed_ports",
                "snapshot_fingerprint",
                "snapshot_fingerprint_version",
                "workspace_root_ready",
                "sandbox_id",
                "sandbox_snapshot_name",
                "image",
                "base_env_vars",
                "pause_on_exit",
                "create_timeout",
                "start_timeout",
                "name",
                "resources",
                "auto_stop_interval",
                "timeouts",
                "exposed_port_url_ttl_s",
            ),
        ),
        (
            "agents.extensions.sandbox.blaxel",
            "BlaxelSandboxSessionState",
            (
                "type",
                "session_id",
                "snapshot",
                "manifest",
                "exposed_ports",
                "snapshot_fingerprint",
                "snapshot_fingerprint_version",
                "workspace_root_ready",
                "sandbox_name",
                "image",
                "memory",
                "region",
                "base_env_vars",
                "labels",
                "ttl",
                "pause_on_exit",
                "timeouts",
                "sandbox_url",
                "exposed_port_public",
                "exposed_port_url_ttl_s",
            ),
        ),
        (
            "agents.extensions.sandbox.runloop",
            "RunloopSandboxSessionState",
            (
                "type",
                "session_id",
                "snapshot",
                "manifest",
                "exposed_ports",
                "snapshot_fingerprint",
                "snapshot_fingerprint_version",
                "workspace_root_ready",
                "devbox_id",
                "blueprint_id",
                "blueprint_name",
                "base_env_vars",
                "pause_on_exit",
                "name",
                "timeouts",
                "user_parameters",
                "launch_parameters",
                "tunnel",
                "gateways",
                "mcp",
                "metadata",
                "secret_refs",
            ),
        ),
        (
            "agents.extensions.sandbox.vercel",
            "VercelSandboxSessionState",
            (
                "type",
                "session_id",
                "snapshot",
                "manifest",
                "exposed_ports",
                "snapshot_fingerprint",
                "snapshot_fingerprint_version",
                "workspace_root_ready",
                "sandbox_id",
                "project_id",
                "team_id",
                "timeout_ms",
                "runtime",
                "resources",
                "env",
                "interactive",
                "workspace_persistence",
                "snapshot_expiration_ms",
                "network_policy",
            ),
        ),
    ],
)
def test_sandbox_session_state_field_order_is_stable(
    state_cls_or_module: type[SandboxSessionState] | str,
    class_name: str | None,
    expected_fields: tuple[str, ...],
) -> None:
    if isinstance(state_cls_or_module, str):
        assert class_name is not None
        state_cls = _import_optional_class(state_cls_or_module, class_name)
    else:
        state_cls = state_cls_or_module
    assert _model_field_names(state_cls) == expected_fields


@pytest.mark.parametrize(
    ("module_name", "class_name", "args", "expected_type"),
    [
        (
            "agents.sandbox.sandboxes.unix_local",
            "UnixLocalSandboxClientOptions",
            (),
            "unix_local",
        ),
        (
            "agents.sandbox.sandboxes.docker",
            "DockerSandboxClientOptions",
            ("python:3.12",),
            "docker",
        ),
        ("agents.extensions.sandbox.e2b", "E2BSandboxClientOptions", ("base",), "e2b"),
        ("agents.extensions.sandbox.modal", "ModalSandboxClientOptions", ("agents-sdk",), "modal"),
        (
            "agents.extensions.sandbox.cloudflare",
            "CloudflareSandboxClientOptions",
            ("https://worker.example",),
            "cloudflare",
        ),
        ("agents.extensions.sandbox.daytona", "DaytonaSandboxClientOptions", (), "daytona"),
        ("agents.extensions.sandbox.runloop", "RunloopSandboxClientOptions", (), "runloop"),
        ("agents.extensions.sandbox.vercel", "VercelSandboxClientOptions", (), "vercel"),
    ],
)
def test_optional_sandbox_client_options_json_round_trip_preserves_type(
    module_name: str,
    class_name: str,
    args: tuple[object, ...],
    expected_type: str,
) -> None:
    options = cast(
        BaseSandboxClientOptions,
        _instantiate_optional_class(module_name, class_name, *args),
    )
    payload = options.model_dump(mode="json")

    restored = BaseSandboxClientOptions.parse(payload)

    assert payload["type"] == expected_type
    assert _class_identity(restored) == _class_identity(options)
    assert restored.model_dump(mode="json") == payload


@pytest.mark.parametrize(
    ("module_name", "class_name", "overrides"),
    [
        (
            "agents.sandbox.sandboxes.unix_local",
            "UnixLocalSandboxSessionState",
            {"workspace_root_owned": True},
        ),
        (
            "agents.sandbox.sandboxes.docker",
            "DockerSandboxSessionState",
            {"image": "python:3.12", "container_id": "container-123"},
        ),
        ("agents.extensions.sandbox.e2b", "E2BSandboxSessionState", {"sandbox_id": "sandbox-123"}),
        (
            "agents.extensions.sandbox.modal",
            "ModalSandboxSessionState",
            {"app_name": "agents-sdk", "sandbox_id": "sandbox-123"},
        ),
        (
            "agents.extensions.sandbox.cloudflare",
            "CloudflareSandboxSessionState",
            {"worker_url": "https://worker.example", "sandbox_id": "sandbox-123"},
        ),
        (
            "agents.extensions.sandbox.daytona",
            "DaytonaSandboxSessionState",
            {"sandbox_id": "sandbox-123"},
        ),
        (
            "agents.extensions.sandbox.blaxel",
            "BlaxelSandboxSessionState",
            {"sandbox_name": "sandbox-123"},
        ),
        (
            "agents.extensions.sandbox.runloop",
            "RunloopSandboxSessionState",
            {"devbox_id": "devbox-123"},
        ),
        (
            "agents.extensions.sandbox.vercel",
            "VercelSandboxSessionState",
            {"sandbox_id": "sandbox-123"},
        ),
    ],
)
def test_optional_sandbox_session_state_json_round_trip_preserves_type(
    module_name: str,
    class_name: str,
    overrides: dict[str, object],
) -> None:
    state = _make_optional_session_state(module_name, class_name, **overrides)
    payload = state.model_dump(mode="json")

    restored = SandboxSessionState.parse(payload)

    assert _class_identity(restored) == _class_identity(state)
    assert restored.model_dump(mode="json") == payload


def test_core_discriminator_type_strings_are_stable() -> None:
    expected_types = {
        LocalSnapshot: "local",
        NoopSnapshot: "noop",
        RemoteSnapshot: "remote",
        Dir: "dir",
        File: "file",
        LocalFile: "local_file",
        LocalDir: "local_dir",
        GitRepo: "git_repo",
        S3Mount: "s3_mount",
        R2Mount: "r2_mount",
        GCSMount: "gcs_mount",
        AzureBlobMount: "azure_blob_mount",
        S3FilesMount: "s3_files_mount",
        FuseMountPattern: "fuse",
        MountpointMountPattern: "mountpoint",
        RcloneMountPattern: "rclone",
        S3FilesMountPattern: "s3files",
        InContainerMountStrategy: "in_container",
        DockerVolumeMountStrategy: "docker_volume",
    }

    for cls, expected_type in expected_types.items():
        assert _model_type_default(cls) == expected_type


@pytest.mark.parametrize(
    ("module_name", "class_name", "expected_type"),
    [
        ("agents.sandbox.sandboxes.unix_local", "UnixLocalSandboxClientOptions", "unix_local"),
        ("agents.sandbox.sandboxes.unix_local", "UnixLocalSandboxSessionState", "unix_local"),
        ("agents.sandbox.sandboxes.docker", "DockerSandboxClientOptions", "docker"),
        ("agents.sandbox.sandboxes.docker", "DockerSandboxSessionState", "docker"),
    ],
)
def test_optional_sandbox_discriminator_type_strings_are_stable(
    module_name: str,
    class_name: str,
    expected_type: str,
) -> None:
    cls = _import_optional_class(module_name, class_name)

    assert _model_type_default(cls) == expected_type


@pytest.mark.parametrize(
    ("strategy", "expected_type"),
    [
        (InContainerMountStrategy(pattern=MountpointMountPattern()), "in_container"),
        (DockerVolumeMountStrategy(driver="rclone"), "docker_volume"),
    ],
)
def test_mount_strategy_type_strings_round_trip_through_registry(
    strategy: MountStrategyBase,
    expected_type: str,
) -> None:
    payload = strategy.model_dump(mode="json")

    restored = MountStrategyBase.parse(payload)

    assert payload["type"] == expected_type
    assert _class_identity(restored) == _class_identity(strategy)
    assert restored.model_dump(mode="json") == payload


@pytest.mark.parametrize(
    ("module_name", "class_name", "expected_type"),
    [
        ("agents.extensions.sandbox.e2b", "E2BCloudBucketMountStrategy", "e2b_cloud_bucket"),
        ("agents.extensions.sandbox.modal", "ModalCloudBucketMountStrategy", "modal_cloud_bucket"),
        (
            "agents.extensions.sandbox.daytona",
            "DaytonaCloudBucketMountStrategy",
            "daytona_cloud_bucket",
        ),
        (
            "agents.extensions.sandbox.cloudflare",
            "CloudflareBucketMountStrategy",
            "cloudflare_bucket_mount",
        ),
        (
            "agents.extensions.sandbox.blaxel",
            "BlaxelCloudBucketMountStrategy",
            "blaxel_cloud_bucket",
        ),
        ("agents.extensions.sandbox.blaxel", "BlaxelDriveMountStrategy", "blaxel_drive"),
        (
            "agents.extensions.sandbox.runloop",
            "RunloopCloudBucketMountStrategy",
            "runloop_cloud_bucket",
        ),
    ],
)
def test_optional_mount_strategy_type_strings_round_trip_through_registry(
    module_name: str,
    class_name: str,
    expected_type: str,
) -> None:
    strategy = cast(
        MountStrategyBase,
        _instantiate_optional_class(module_name, class_name),
    )
    payload = strategy.model_dump(mode="json")

    restored = MountStrategyBase.parse(payload)

    assert payload["type"] == expected_type
    assert _class_identity(restored) == _class_identity(strategy)
    assert restored.model_dump(mode="json") == payload


def test_core_discriminator_registries_parse_released_payload_shapes() -> None:
    assert isinstance(SnapshotBase.parse({"type": "noop", "id": "snapshot-123"}), NoopSnapshot)
    assert isinstance(
        BaseEntry.parse({"type": "dir", "permissions": {"directory": True}}),
        Dir,
    )
    assert isinstance(
        TypeAdapter(MountPattern).validate_python({"type": "mountpoint"}),
        MountpointMountPattern,
    )
    assert isinstance(
        MountStrategyBase.parse({"type": "docker_volume", "driver": "rclone"}),
        DockerVolumeMountStrategy,
    )


@pytest.mark.asyncio
async def test_run_state_sandbox_payload_json_shape_is_stable() -> None:
    agent = Agent(name="sandbox", instructions="Use the sandbox.")
    session_state = TestSessionState(
        session_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        snapshot=NoopSnapshot(id="snapshot-123"),
        manifest=Manifest(root="/workspace"),
        exposed_ports=(8000,),
        workspace_root_ready=True,
    ).model_dump(mode="json")
    sandbox_payload = {
        "backend_id": "fake",
        "current_agent_key": "sandbox",
        "current_agent_name": "sandbox",
        "session_state": session_state,
        "sessions_by_agent": {
            "sandbox": {
                "agent_name": "sandbox",
                "session_state": session_state,
            },
        },
    }
    state: RunState[dict[str, Any], Agent[Any]] = RunState(
        context=RunContextWrapper(context={}),
        original_input="hello",
        starting_agent=agent,
    )
    state._sandbox = sandbox_payload

    state_json = state.to_json()
    restored = await RunState.from_json(agent, state_json)

    assert state_json["sandbox"] == sandbox_payload
    assert tuple(state_json["sandbox"]) == (
        "backend_id",
        "current_agent_key",
        "current_agent_name",
        "session_state",
        "sessions_by_agent",
    )
    assert tuple(state_json["sandbox"]["session_state"]) == (
        "type",
        "session_id",
        "snapshot",
        "manifest",
        "exposed_ports",
        "snapshot_fingerprint",
        "snapshot_fingerprint_version",
        "workspace_root_ready",
    )
    assert restored._sandbox == sandbox_payload


def _dataclass_field_names(cls: type[Any]) -> tuple[str, ...]:
    return tuple(field.name for field in dataclasses.fields(cls) if field.init)


def _model_field_names(
    cls: type[Any],
    *,
    exclude: Iterable[str] = (),
) -> tuple[str, ...]:
    excluded = set(exclude)
    return tuple(name for name in cls.model_fields if name not in excluded)


def _model_type_default(cls: type[Any]) -> str:
    type_field = cls.model_fields["type"]
    assert isinstance(type_field.default, str)
    return type_field.default


def _class_identity(value: object) -> tuple[str, str]:
    value_type = type(value)
    return value_type.__module__, value_type.__qualname__
