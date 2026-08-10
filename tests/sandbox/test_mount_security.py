from __future__ import annotations

import asyncio
import builtins
import importlib
import inspect
import sys
from pathlib import Path, PureWindowsPath
from typing import Any, ClassVar, Literal, cast

import pytest
from pydantic import ConfigDict, PrivateAttr, model_serializer, model_validator

from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountStrategy
from agents.extensions.sandbox.daytona.mounts import DaytonaCloudBucketMountStrategy
from agents.extensions.sandbox.e2b.mounts import E2BCloudBucketMountStrategy
from agents.extensions.sandbox.modal.mounts import ModalCloudBucketMountStrategy
from agents.extensions.sandbox.runloop.mounts import RunloopCloudBucketMountStrategy
from agents.run_config import SandboxRunConfig
from agents.sandbox import Manifest
from agents.sandbox._mount_security import (
    CREDENTIALLESS_MOUNT_AUTHORITY_KEY,
    REDACTED_MOUNT_AUTHORITY_KEY,
    redact_mount_error_data,
    redact_mount_error_data_sync,
    sanitize_manifest_mount_authority,
    sanitize_raw_session_state_mount_authority,
    validate_manifest_mount_credential_boundaries,
    validate_mount_activation_credential_boundary,
)
from agents.sandbox.entries import (
    AzureBlobMount,
    BaseEntry,
    BoxMount,
    Dir,
    DockerVolumeMountStrategy,
    File,
    FuseMountPattern,
    GCSMount,
    GitRepo,
    InContainerMountStrategy,
    LocalDir,
    LocalFile,
    Mount,
    MountpointMountPattern,
    MountStrategyBase,
    R2Mount,
    RcloneMountPattern,
    S3FilesMount,
    S3FilesMountPattern,
    S3Mount,
)
from agents.sandbox.entries.mounts.base import InContainerMountAdapter
from agents.sandbox.entries.mounts.patterns import (
    MountPattern,
    MountPatternConfig,
    RcloneMountConfig,
)
from agents.sandbox.errors import (
    ErrorCode,
    ExecNonZeroError,
    ExecTimeoutError,
    ExecTransportError,
    InvalidManifestPathError,
    MountCommandError,
    MountConfigError,
    MountToolMissingError,
    PtySessionNotFoundError,
    SandboxError,
)
from agents.sandbox.manifest import Environment
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.sandbox_client import BaseSandboxClient
from agents.sandbox.session.sandbox_session import SandboxSession
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot, SnapshotBase, SnapshotSpec
from agents.sandbox.types import ExecResult
from tests.utils.factories import TestSessionState

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup
else:
    BaseExceptionGroup = builtins.BaseExceptionGroup


class _SecurityTestClient(BaseSandboxClient[None]):
    backend_id = "test"

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: None,
    ) -> SandboxSession:
        _ = (snapshot, manifest, options)
        raise AssertionError("create() is not used in these tests")

    async def delete(self, session: SandboxSession) -> SandboxSession:
        raise AssertionError(f"delete() is not used in these tests: {session!r}")

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        raise AssertionError(f"resume() is not used in these tests: {state!r}")

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return self._deserialize_session_state_payload(payload, TestSessionState)


class _CustomTokenEntry(BaseEntry):
    type: Literal["custom_token_entry"] = "custom_token_entry"
    token: str

    async def apply(self, session: Any, dest: Path, base_dir: Path) -> list[Any]:
        _ = (session, dest, base_dir)
        return []


def _install_hostile_exception_descriptors(error_type: type[BaseException]) -> None:
    def get_base_args(error: BaseException) -> tuple[object, ...]:
        return cast(
            tuple[object, ...],
            cast(Any, BaseException.args).__get__(error, type(error)),
        )

    def reject_traceback_access(error: BaseException) -> object:
        _ = error
        raise AssertionError("provider-defined traceback descriptor was accessed")

    type.__setattr__(error_type, "args", property(get_base_args))
    type.__setattr__(error_type, "__traceback__", property(reject_traceback_access))


class _CustomChildrenEntry(BaseEntry):
    type: Literal["custom_children_entry"] = "custom_children_entry"
    children: Any

    async def apply(self, session: Any, dest: Path, base_dir: Path) -> list[Any]:
        _ = (session, dest, base_dir)
        return []


class _CustomCredentialSourceEntry(BaseEntry):
    type: Literal["custom_credential_source_entry"] = "custom_credential_source_entry"
    content: str
    source_token: str

    async def apply(self, session: Any, dest: Path, base_dir: Path) -> list[Any]:
        _ = (session, dest, base_dir)
        return []


class _DirectCustomMount(Mount):
    type: Literal["direct_custom_mount"] = "direct_custom_mount"
    bucket: str
    api_token: str

    def in_container_adapter(self) -> InContainerMountAdapter:
        return InContainerMountAdapter(self)

    def supported_in_container_patterns(
        self,
    ) -> tuple[builtins.type[RcloneMountPattern], ...]:
        return (RcloneMountPattern,)

    def supported_docker_volume_drivers(self) -> frozenset[str]:
        return frozenset({"rclone"})

    async def build_in_container_mount_config(
        self,
        session: Any,
        pattern: MountPattern,
        *,
        include_config_text: bool,
    ) -> MountPatternConfig:
        _ = (session, pattern, include_config_text)
        return RcloneMountConfig(
            remote_name="direct-custom",
            remote_path=self.bucket,
            remote_kind="s3",
            mount_type=self.type,
            config_text=f"api_token = {self.api_token}\n",
        )


class _CustomPatternStrategy(MountStrategyBase):
    type: Literal["custom_pattern_strategy"] = "custom_pattern_strategy"
    pattern: dict[str, Any]
    api_token: str | None = None

    def validate_mount(self, mount: Any) -> None:
        _ = mount

    async def activate(self, mount: Any, session: Any, dest: Path, base_dir: Path) -> list[Any]:
        _ = (mount, session, dest, base_dir)
        return []

    async def deactivate(self, mount: Any, session: Any, dest: Path, base_dir: Path) -> None:
        _ = (mount, session, dest, base_dir)

    async def teardown_for_snapshot(self, mount: Any, session: Any, path: Path) -> None:
        _ = (mount, session, path)

    async def restore_after_snapshot(self, mount: Any, session: Any, path: Path) -> None:
        _ = (mount, session, path)

    def build_docker_volume_driver_config(
        self, mount: Any
    ) -> tuple[str, dict[str, str], bool] | None:
        _ = mount
        return None


class _CustomInContainerStrategy(InContainerMountStrategy):
    type: Literal["custom_in_container_strategy"] = "custom_in_container_strategy"  # type: ignore[assignment]


class _CustomDockerVolumeStrategy(DockerVolumeMountStrategy):
    type: Literal["custom_docker_volume_strategy"] = "custom_docker_volume_strategy"  # type: ignore[assignment]


class _CustomModalCloudBucketStrategy(ModalCloudBucketMountStrategy):
    type: Literal["custom_modal_cloud_bucket_strategy"] = "custom_modal_cloud_bucket_strategy"  # type: ignore[assignment]


def _s3_mount(
    *,
    strategy: InContainerMountStrategy | DockerVolumeMountStrategy,
    credentialed: bool = False,
) -> S3Mount:
    return S3Mount(
        bucket="example-bucket",
        access_key_id="example-access-key" if credentialed else None,
        secret_access_key="example-secret-key" if credentialed else None,
        mount_strategy=strategy,
    )


def test_rejects_explicit_credentials_for_in_container_mounts() -> None:
    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                credentialed=True,
            )
        }
    )

    with pytest.raises(MountConfigError, match="mount-scoped credentials") as exc:
        validate_manifest_mount_credential_boundaries(manifest)

    assert exc.value.context["credential_fields"] == (
        "access_key_id",
        "secret_access_key",
    )
    assert "example-secret-key" not in str(exc.value)
    assert "example-secret-key" not in repr(exc.value.context)


def test_exact_path_acknowledgement_allows_supported_mount_scoped_credentials() -> None:
    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                credentialed=True,
            )
        }
    ).with_in_container_mount_credential_exposure_acknowledged("data")

    validate_manifest_mount_credential_boundaries(manifest)

    sibling = manifest.model_copy(deep=True)
    sibling.entries["other"] = sibling.entries.pop("data")
    with pytest.raises(MountConfigError, match="mount-scoped credentials"):
        validate_manifest_mount_credential_boundaries(sibling)

    mount = manifest.entries["data"]
    assert isinstance(mount, S3Mount)
    validate_mount_activation_credential_boundary(
        mount,
        mount.mount_strategy,
        manifest=manifest,
        mount_path="/workspace/data",
        provider_backend_id="docker",
    )
    with pytest.raises(MountConfigError, match="mount-scoped credentials"):
        validate_mount_activation_credential_boundary(
            mount,
            mount.mount_strategy,
            manifest=manifest,
            mount_path="/workspace/other",
            provider_backend_id="docker",
        )


@pytest.mark.parametrize(
    ("credentials", "invalid_fields"),
    [
        ({"access_key_id": "access-key"}, ("secret_access_key",)),
        ({"secret_access_key": "secret-key"}, ("access_key_id",)),
        (
            {"session_token": "session-token"},
            ("access_key_id", "secret_access_key"),
        ),
        (
            {"access_key_id": "access-key", "secret_access_key": ""},
            ("secret_access_key",),
        ),
        (
            {"access_key_id": " ", "secret_access_key": "secret-key"},
            ("access_key_id",),
        ),
        (
            {
                "access_key_id": "access-key",
                "secret_access_key": "secret-key",
                "session_token": " ",
            },
            ("session_token",),
        ),
    ],
)
def test_acknowledgement_rejects_incomplete_in_container_s3_credentials(
    credentials: dict[str, str],
    invalid_fields: tuple[str, ...],
) -> None:
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="example-bucket",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                **cast(Any, credentials),
            )
        }
    ).with_in_container_mount_credential_exposure_acknowledged("data")

    with pytest.raises(MountConfigError, match="complete non-empty credential set") as exc_info:
        validate_manifest_mount_credential_boundaries(manifest)

    assert exc_info.value.context["credential_fields"] == invalid_fields


@pytest.mark.parametrize(
    ("credentials", "invalid_fields"),
    [
        ({"access_id": "access-id"}, ("secret_access_key",)),
        ({"secret_access_key": "secret-key"}, ("access_id",)),
        (
            {"access_id": "access-id", "secret_access_key": ""},
            ("secret_access_key",),
        ),
        (
            {"access_id": " ", "secret_access_key": "secret-key"},
            ("access_id",),
        ),
        (
            {
                "access_id": "access-id",
                "service_account_credentials": '{"type":"service_account"}',
            },
            ("secret_access_key",),
        ),
    ],
)
def test_acknowledgement_rejects_incomplete_in_container_gcs_hmac_credentials(
    credentials: dict[str, str],
    invalid_fields: tuple[str, ...],
) -> None:
    manifest = Manifest(
        entries={
            "data": GCSMount(
                bucket="example-bucket",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                **cast(Any, credentials),
            )
        }
    ).with_in_container_mount_credential_exposure_acknowledged("data")

    with pytest.raises(MountConfigError, match="complete non-empty credential set") as exc_info:
        validate_manifest_mount_credential_boundaries(manifest)

    assert exc_info.value.context["credential_fields"] == invalid_fields


def test_acknowledgement_accepts_complete_in_container_gcs_hmac_credentials() -> None:
    manifest = Manifest(
        entries={
            "data": GCSMount(
                bucket="example-bucket",
                access_id="access-id",
                secret_access_key="secret-key",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    ).with_in_container_mount_credential_exposure_acknowledged("data")

    validate_manifest_mount_credential_boundaries(manifest)


@pytest.mark.parametrize("blank_value", ["", "   "])
@pytest.mark.parametrize(
    ("mount_factory", "broad", "invalid_field"),
    [
        (
            lambda value: GCSMount(
                bucket="example-bucket",
                access_token=value,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            False,
            "access_token",
        ),
        (
            lambda value: GCSMount(
                bucket="example-bucket",
                service_account_credentials=value,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            False,
            "service_account_credentials",
        ),
        (
            lambda value: GCSMount(
                bucket="example-bucket",
                service_account_file=value,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            True,
            "service_account_file",
        ),
        (
            lambda value: AzureBlobMount(
                account="example-account",
                container="example-container",
                account_key=value,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            False,
            "account_key",
        ),
        (
            lambda value: AzureBlobMount(
                account="example-account",
                container="example-container",
                identity_client_id=value,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            True,
            "identity_client_id",
        ),
    ],
)
def test_acknowledgement_rejects_empty_in_container_scalar_authority(
    mount_factory: Any,
    broad: bool,
    invalid_field: str,
    blank_value: str,
) -> None:
    manifest = Manifest(entries={"data": mount_factory(blank_value)})
    acknowledged = (
        manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
        if broad
        else manifest.with_in_container_mount_credential_exposure_acknowledged("data")
    )

    with pytest.raises(MountConfigError, match="must not be empty or whitespace-only") as exc_info:
        validate_manifest_mount_credential_boundaries(acknowledged)

    assert exc_info.value.context["credential_fields"] == (invalid_field,)


@pytest.mark.parametrize(
    ("credentials", "invalid_fields"),
    [
        ({"access_key_id": "access-key"}, ("secret_access_key",)),
        ({"secret_access_key": "secret-key"}, ("access_key_id",)),
        (
            {"access_key_id": "access-key", "secret_access_key": ""},
            ("secret_access_key",),
        ),
    ],
)
def test_acknowledgement_rejects_incomplete_in_container_r2_credentials(
    credentials: dict[str, str],
    invalid_fields: tuple[str, ...],
) -> None:
    manifest = Manifest(
        entries={
            "data": R2Mount(
                bucket="example-bucket",
                account_id="example-account",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                **cast(Any, credentials),
            )
        }
    ).with_in_container_mount_credential_exposure_acknowledged("data")

    with pytest.raises(MountConfigError, match="complete non-empty credential set") as exc_info:
        validate_manifest_mount_credential_boundaries(manifest)

    assert exc_info.value.context["credential_fields"] == invalid_fields


@pytest.mark.parametrize(
    "mount",
    [
        S3Mount(
            bucket="example-bucket",
            access_key_id="access-key",
            mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
        ),
        GCSMount(
            bucket="example-bucket",
            access_id="access-id",
            mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
        ),
        R2Mount(
            bucket="example-bucket",
            account_id="example-account",
            access_key_id="access-key",
            mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
        ),
        GCSMount(
            bucket="example-bucket",
            access_token="",
            mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
        ),
        AzureBlobMount(
            account="example-account",
            container="example-container",
            identity_client_id=" ",
            mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
        ),
    ],
)
def test_incomplete_credentials_remain_external_provider_configuration(mount: Mount) -> None:
    manifest = Manifest(entries={"data": mount})

    validate_manifest_mount_credential_boundaries(manifest, provider_backend_id="docker")


def test_mount_credential_acknowledgement_is_not_a_path_prefix() -> None:
    mount = _s3_mount(
        strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
        credentialed=True,
    )
    manifest = Manifest(
        entries={"parent": Dir(children={"data": mount})}
    ).with_in_container_mount_credential_exposure_acknowledged("parent")

    with pytest.raises(MountConfigError, match="mount-scoped credentials"):
        validate_manifest_mount_credential_boundaries(manifest)

    validate_manifest_mount_credential_boundaries(
        manifest.with_in_container_mount_credential_exposure_acknowledged("parent/data")
    )


def test_mount_credential_acknowledgement_preserves_path_whitespace() -> None:
    manifest = Manifest(
        entries={
            "data ": _s3_mount(
                strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                credentialed=True,
            )
        }
    ).with_in_container_mount_credential_exposure_acknowledged("data")

    with pytest.raises(MountConfigError, match="mount-scoped credentials"):
        validate_manifest_mount_credential_boundaries(manifest)

    validate_manifest_mount_credential_boundaries(
        manifest.with_in_container_mount_credential_exposure_acknowledged("data ")
    )


def test_mount_credential_acknowledgement_accepts_platform_path_objects() -> None:
    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                credentialed=True,
            )
        }
    ).with_in_container_mount_credential_exposure_acknowledged(PureWindowsPath("data"))

    mount = manifest.entries["data"]
    assert isinstance(mount, S3Mount)
    validate_mount_activation_credential_boundary(
        mount,
        mount.mount_strategy,
        manifest=manifest,
        mount_path=PureWindowsPath("/workspace/data"),
        provider_backend_id="docker",
    )
    assert not Manifest()._acknowledges_in_container_mount_credential_exposure(
        PureWindowsPath("/workspace/data"),
        "mount_scoped",
    )

    with pytest.raises(ValueError, match="use '/' separators"):
        Manifest().with_in_container_mount_credential_exposure_acknowledged("data\\child")


@pytest.mark.parametrize(
    "policy_key",
    [
        "in_container_mount_credential_exposure_acknowledged_paths",
        "_in_container_mount_credential_exposure_acknowledged_paths",
        "inContainerMountCredentialExposureAcknowledgedPaths",
        "in_container_mount_broad_credential_exposure_acknowledged_paths",
        "_mount_credential_exposure_policy",
    ],
)
def test_manifest_input_cannot_inject_mount_credential_acknowledgement(policy_key: str) -> None:
    with pytest.raises(TypeError, match="trusted Manifest instance"):
        Manifest.model_validate({policy_key: ["data"]})


def test_manifest_acknowledgement_is_runtime_only_and_rejects_root() -> None:
    manifest = Manifest().with_in_container_mount_credential_exposure_acknowledged("data")
    payload = manifest.model_dump(mode="json")

    assert all("credential_exposure" not in key for key in payload)
    restored = Manifest.model_validate(payload)
    assert not restored._acknowledges_in_container_mount_credential_exposure(
        "/workspace/data", "mount_scoped"
    )
    with pytest.raises(ValueError, match="non-root path"):
        Manifest().with_in_container_mount_credential_exposure_acknowledged("/workspace")
    with pytest.raises(TypeError, match="At least one"):
        Manifest().with_in_container_mount_credential_exposure_acknowledged()


@pytest.mark.parametrize(
    "method_name",
    [
        "with_in_container_mount_credential_exposure_acknowledged",
        "with_in_container_mount_broad_credential_exposure_acknowledged",
    ],
)
@pytest.mark.parametrize(
    "path",
    [
        "data/*",
        "data?",
        "data[0]",
        "data/../other",
        "/workspace/../outside",
    ],
)
def test_manifest_acknowledgement_rejects_wildcard_and_parent_paths(
    method_name: str,
    path: str,
) -> None:
    method = getattr(Manifest(), method_name)

    with pytest.raises(ValueError, match="wildcard syntax|parent segments"):
        method(path)


def test_manifest_acknowledgement_rejects_custom_mount_before_deepcopy() -> None:
    sentinel = "custom-mount-deepcopy-secret"

    class CustomS3Mount(S3Mount):
        type: Literal["custom_deepcopy_s3_mount"] = "custom_deepcopy_s3_mount"  # type: ignore[assignment]
        deepcopy_called: ClassVar[bool] = False

        def __deepcopy__(self, memo: dict[int, Any] | None = None) -> CustomS3Mount:
            _ = memo
            type(self).deepcopy_called = True
            raise RuntimeError(sentinel)

    manifest = Manifest(
        entries={
            "data": CustomS3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    )

    with pytest.raises(MountConfigError, match="custom mount implementations") as exc_info:
        manifest.with_in_container_mount_credential_exposure_acknowledged("data")

    assert CustomS3Mount.deepcopy_called is False
    assert sentinel not in repr(exc_info.value)


def test_manifest_acknowledgement_redacts_custom_provenance_traceback_locals() -> None:
    sentinel = "custom-provenance-traceback-secret"

    class CustomS3Mount(S3Mount):
        type: Literal["custom_traceback_s3_mount"] = "custom_traceback_s3_mount"  # type: ignore[assignment]
        api_token: str | None = None

    class CustomInContainerStrategy(InContainerMountStrategy):
        type: Literal["custom_traceback_strategy"] = "custom_traceback_strategy"  # type: ignore[assignment]
        api_token: str | None = None

    class CustomRclonePattern(RcloneMountPattern):
        api_token: str | None = None

    cases = [
        (
            Manifest(
                entries={
                    "data": CustomS3Mount(
                        bucket="bucket",
                        api_token=sentinel,
                        mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                    )
                }
            ),
            "custom mount implementations",
        ),
        (
            Manifest(
                entries={
                    "data": S3Mount(
                        bucket="bucket",
                        mount_strategy=CustomInContainerStrategy(
                            api_token=sentinel,
                            pattern=RcloneMountPattern(),
                        ),
                    )
                }
            ),
            "custom mount strategies",
        ),
        (
            Manifest(
                entries={
                    "data": S3Mount(
                        bucket="bucket",
                        mount_strategy=InContainerMountStrategy(
                            pattern=CustomRclonePattern(api_token=sentinel)
                        ),
                    )
                }
            ),
            "custom mount patterns",
        ),
    ]

    for method_name in (
        "with_in_container_mount_credential_exposure_acknowledged",
        "with_in_container_mount_broad_credential_exposure_acknowledged",
    ):
        for manifest, message in cases:
            method = getattr(manifest, method_name)
            with pytest.raises(MountConfigError, match=message) as exc:
                method("data")

            traceback_cursor = exc.value.__traceback__
            while traceback_cursor is not None:
                module_name = str(traceback_cursor.tb_frame.f_globals.get("__name__", ""))
                if module_name.startswith("agents."):
                    assert sentinel not in repr(traceback_cursor.tb_frame.f_locals)
                traceback_cursor = traceback_cursor.tb_next


def test_builtin_mount_subclass_is_rejected_by_execution_provenance() -> None:
    class CustomS3Mount(S3Mount):
        type: Literal["custom_s3_mount"] = "custom_s3_mount"  # type: ignore[assignment]
        api_token: str | None = None

        def _rclone_required_lines(self, remote_name: str) -> list[str]:
            lines = super()._rclone_required_lines(remote_name)
            if self.api_token is not None:
                lines.append(f"api_token = {self.api_token}")
            return lines

    in_container = Manifest(
        entries={
            "data": CustomS3Mount(
                bucket="example-bucket",
                api_token="custom-mount-secret",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    )

    with pytest.raises(MountConfigError, match="custom mount implementations"):
        validate_manifest_mount_credential_boundaries(in_container)

    external = Manifest(
        entries={
            "data": CustomS3Mount(
                bucket="example-bucket",
                access_key_id="example-access-key",
                secret_access_key="example-secret-key",
                api_token="custom-mount-secret",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )
    with pytest.raises(MountConfigError, match="custom mount implementations") as exc_info:
        sanitize_manifest_mount_authority(external)

    assert "example-secret-key" not in repr(exc_info.value)
    assert "custom-mount-secret" not in repr(exc_info.value)


def test_builtin_mount_subclass_rejects_pydantic_extra_configuration() -> None:
    class ExtraS3Mount(S3Mount):
        type: Literal["extra_s3_mount"] = "extra_s3_mount"  # type: ignore[assignment]
        model_config = ConfigDict(extra="allow")

        def _rclone_required_lines(self, remote_name: str) -> list[str]:
            lines = super()._rclone_required_lines(remote_name)
            lines.append(f"api_token = {cast(Any, self).api_token}")
            return lines

    mount = ExtraS3Mount.model_validate(
        {
            "bucket": "example-bucket",
            "api_token": "custom-mount-extra-secret",
            "mount_strategy": InContainerMountStrategy(pattern=RcloneMountPattern()),
        }
    )

    with pytest.raises(MountConfigError, match="custom mount implementations") as exc:
        validate_manifest_mount_credential_boundaries(Manifest(entries={"data": mount}))

    assert "custom-mount-extra-secret" not in str(exc.value)


def test_direct_custom_mount_configuration_is_opaque_authority() -> None:
    sentinel = "direct-custom-mount-secret"
    mount = _DirectCustomMount(
        bucket="bucket",
        api_token=sentinel,
        mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
    )

    with pytest.raises(MountConfigError, match="custom mount implementations") as exc:
        validate_manifest_mount_credential_boundaries(Manifest(entries={"data": mount}))

    assert sentinel not in str(exc.value)


def test_behavior_only_mount_subclass_is_rejected_before_config_generation() -> None:
    class BehaviorOnlyS3Mount(S3Mount):
        type: Literal["behavior_only_s3_mount"] = "behavior_only_s3_mount"  # type: ignore[assignment]
        config_called: ClassVar[bool] = False

        def _rclone_required_lines(self, remote_name: str) -> list[str]:
            type(self).config_called = True
            return [f"[{remote_name}]", "type = s3", "env_auth = true"]

    mount = BehaviorOnlyS3Mount(
        bucket="bucket",
        mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
    )

    with pytest.raises(MountConfigError, match="custom mount implementations"):
        validate_manifest_mount_credential_boundaries(Manifest(entries={"data": mount}))

    assert BehaviorOnlyS3Mount.config_called is False


def test_custom_mount_is_rejected_before_mount_path_resolution() -> None:
    sentinel = "custom-mount-path-secret"

    class CustomPathS3Mount(S3Mount):
        type: Literal["custom_path_s3_mount"] = "custom_path_s3_mount"  # type: ignore[assignment]
        _private_authority: str = PrivateAttr(default=sentinel)
        resolver_called: ClassVar[bool] = False

        def _resolve_mount_path_for_root(self, root: Path, dest: Path) -> Path:
            _ = (root, dest)
            type(self).resolver_called = True
            raise RuntimeError(self._private_authority)

    mount = CustomPathS3Mount(
        bucket="bucket",
        mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
    )

    with pytest.raises(MountConfigError, match="custom mount implementations") as exc_info:
        validate_manifest_mount_credential_boundaries(Manifest(entries={"data": mount}))

    assert CustomPathS3Mount.resolver_called is False
    assert sentinel not in repr(exc_info.value)


def test_custom_mount_cannot_self_declare_a_trusted_credential_boundary() -> None:
    sentinel = "private-custom-secret"

    class SelfDeclaredTrustedS3Mount(S3Mount):
        type: Literal["self_declared_trusted_s3_mount"] = "self_declared_trusted_s3_mount"  # type: ignore[assignment]
        _trusted_application_credential_boundary: ClassVar[bool] = True
        _private_credential: str = PrivateAttr(default=sentinel)
        config_called: ClassVar[bool] = False

        def _rclone_required_lines(self, remote_name: str) -> list[str]:
            type(self).config_called = True
            return [
                f"[{remote_name}]",
                "type = s3",
                f"secret_access_key = {self._private_credential}",
            ]

    mount = SelfDeclaredTrustedS3Mount(
        bucket="public-bucket",
        mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
    )

    with pytest.raises(MountConfigError, match="custom mount implementations") as exc_info:
        validate_manifest_mount_credential_boundaries(Manifest(entries={"data": mount}))

    assert sentinel not in str(exc_info.value)
    assert SelfDeclaredTrustedS3Mount.config_called is False


def test_direct_custom_mount_configuration_cannot_enter_durable_state() -> None:
    sentinel = "direct-custom-durable-secret"
    state = TestSessionState(
        manifest=Manifest(
            entries={
                "data": _DirectCustomMount(
                    bucket="bucket",
                    api_token=sentinel,
                    mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                )
            }
        ),
        snapshot=NoopSnapshot(id="snapshot"),
    )

    with pytest.raises(MountConfigError) as exc:
        _SecurityTestClient().serialize_session_state(state)

    assert sentinel not in str(exc.value)
    assert sentinel not in repr(exc.value)
    traceback = exc.value.__traceback__
    while traceback is not None:
        frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


def test_custom_mount_is_rejected_before_durable_serializer_runs() -> None:
    sentinel = "custom-mount-serializer-secret"

    class CustomSerializedS3Mount(S3Mount):
        type: Literal["custom_serialized_s3_mount"] = "custom_serialized_s3_mount"  # type: ignore[assignment]
        _private_authority: str = PrivateAttr(default=sentinel)
        serializer_called: ClassVar[bool] = False

        @model_serializer(mode="wrap")
        def _serialize(self, handler: Any) -> Any:
            _ = handler
            type(self).serializer_called = True
            raise RuntimeError(self._private_authority)

    state = TestSessionState(
        manifest=Manifest(
            entries={
                "data": CustomSerializedS3Mount(
                    bucket="bucket",
                    mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                )
            }
        ),
        snapshot=NoopSnapshot(id="snapshot"),
    )

    with pytest.raises(MountConfigError, match="custom mount implementations") as exc_info:
        _SecurityTestClient().serialize_session_state(state)

    assert CustomSerializedS3Mount.serializer_called is False
    assert sentinel not in repr(exc_info.value)


def test_public_mount_error_redactor_discards_untrusted_mount_discriminator() -> None:
    sentinel = "custom-mount-type-secret"

    class CustomS3Mount(S3Mount):
        type: Literal["custom-mount-type-secret"] = sentinel  # type: ignore[assignment]
        api_token: str | None = None

    manifest = Manifest(
        entries={
            "data": CustomS3Mount(
                bucket="example-bucket",
                api_token="configured",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    )

    @redact_mount_error_data_sync
    def validate(*, manifest: Manifest) -> None:
        validate_manifest_mount_credential_boundaries(manifest)

    with pytest.raises(MountConfigError, match="custom mount implementations") as exc:
        validate(manifest=manifest)

    assert exc.value.context == {}
    assert sentinel not in repr(exc.value)


def test_public_mount_error_redactor_discards_untrusted_field_names() -> None:
    sentinel = "custom-mount-field-secret"

    class ExtraS3Mount(S3Mount):
        type: Literal["custom_extra_s3_mount"] = "custom_extra_s3_mount"  # type: ignore[assignment]
        model_config = ConfigDict(extra="allow")

    mount = ExtraS3Mount.model_validate(
        {
            "bucket": "example-bucket",
            sentinel: "configured",
            "mount_strategy": InContainerMountStrategy(pattern=RcloneMountPattern()),
        }
    )
    manifest = Manifest(entries={"data": mount})

    @redact_mount_error_data_sync
    def validate(*, manifest: Manifest) -> None:
        validate_manifest_mount_credential_boundaries(manifest)

    with pytest.raises(MountConfigError, match="custom mount implementations") as exc:
        validate(manifest=manifest)

    assert exc.value.context == {}
    assert sentinel not in repr(exc.value)


def test_public_mount_error_redactor_rejects_before_custom_attribute_access() -> None:
    class CustomS3Mount(S3Mount):
        type: Literal["custom_attribute_s3_mount"] = "custom_attribute_s3_mount"  # type: ignore[assignment]
        authority_accessed: ClassVar[bool] = False

        def __getattribute__(self, name: str) -> Any:
            if name == "access_key_id":
                type(self).authority_accessed = True
            return super().__getattribute__(name)

    mount = CustomS3Mount(
        bucket="example-bucket",
        access_key_id="access-key",
        secret_access_key="custom-attribute-secret",
        mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
    )
    CustomS3Mount.authority_accessed = False
    manifest = Manifest(entries={"data": mount})

    @redact_mount_error_data_sync
    def validate(*, manifest: Manifest) -> None:
        validate_manifest_mount_credential_boundaries(manifest)

    with pytest.raises(MountConfigError, match="custom mount implementations"):
        validate(manifest=manifest)

    assert CustomS3Mount.authority_accessed is False


def test_rejection_redacts_sdk_traceback_frames_without_mutating_trusted_manifest() -> None:
    sentinel = "traceback-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="example-bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    )

    with pytest.raises(MountConfigError) as exc:
        validate_manifest_mount_credential_boundaries(manifest)

    mount = manifest.entries["data"]
    assert isinstance(mount, S3Mount)
    assert mount.access_key_id == "access-key"
    assert mount.secret_access_key == sentinel
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    traceback = exc.value.__traceback__
    while traceback is not None:
        module_name = traceback.tb_frame.f_globals.get("__name__", "")
        if isinstance(module_name, str) and module_name.startswith("agents."):
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


@pytest.mark.asyncio
async def test_authority_detection_keeps_invalid_manifest_paths_inside_redaction_boundary() -> None:
    sentinel = "invalid-path-secret"
    manifest = Manifest(
        entries={
            "../data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    @redact_mount_error_data
    async def validate(*, manifest: Manifest) -> None:
        validate_manifest_mount_credential_boundaries(manifest)

    with pytest.raises(InvalidManifestPathError, match="protected mount configuration") as exc:
        await validate(manifest=manifest)

    assert exc.value.error_code is ErrorCode.INVALID_MANIFEST_PATH
    assert exc.value.context == {}
    assert sentinel not in str(exc.value)
    traceback_cursor = exc.value.__traceback__
    while traceback_cursor is not None:
        module_name = traceback_cursor.tb_frame.f_globals.get("__name__", "")
        if isinstance(module_name, str) and module_name.startswith("agents."):
            assert sentinel not in repr(traceback_cursor.tb_frame.f_locals)
        traceback_cursor = traceback_cursor.tb_next


def test_preserves_credentialless_in_container_and_credentialed_docker_mounts() -> None:
    anonymous = Manifest(
        entries={"data": _s3_mount(strategy=InContainerMountStrategy(pattern=RcloneMountPattern()))}
    )
    docker = Manifest(
        entries={
            "data": _s3_mount(
                strategy=DockerVolumeMountStrategy(
                    driver="rclone",
                    driver_options={"vfs-cache-mode": "off"},
                ),
                credentialed=True,
            )
        }
    )

    validate_manifest_mount_credential_boundaries(anonymous)
    validate_manifest_mount_credential_boundaries(docker, provider_backend_id="docker")


@pytest.mark.parametrize(
    ("backend_id", "strategy"),
    [
        ("blaxel", BlaxelCloudBucketMountStrategy()),
        ("daytona", DaytonaCloudBucketMountStrategy()),
        ("e2b", E2BCloudBucketMountStrategy()),
        ("runloop", RunloopCloudBucketMountStrategy()),
    ],
)
def test_preserves_credentialless_hosted_mount_strategies(
    backend_id: str,
    strategy: MountStrategyBase,
) -> None:
    manifest = Manifest(entries={"data": S3Mount(bucket="example-bucket", mount_strategy=strategy)})

    validate_manifest_mount_credential_boundaries(
        manifest,
        provider_backend_id=backend_id,
    )

    credentialed = manifest.model_copy(deep=True)
    mount = credentialed.entries["data"]
    assert isinstance(mount, S3Mount)
    mount.access_key_id = "example-access-key"
    mount.secret_access_key = "example-secret-key"
    with pytest.raises(MountConfigError, match="mount-scoped credentials"):
        validate_manifest_mount_credential_boundaries(
            credentialed,
            provider_backend_id=backend_id,
        )


def test_custom_strategy_cannot_declare_itself_external() -> None:
    class ForgedExternalStrategy(InContainerMountStrategy):
        type: Literal["forged_external"] = "forged_external"  # type: ignore[assignment]
        _credential_boundary: ClassVar[str] = "external"

    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=ForgedExternalStrategy(pattern=RcloneMountPattern()),
                credentialed=True,
            )
        }
    )

    with pytest.raises(MountConfigError, match="custom mount strategies"):
        validate_manifest_mount_credential_boundaries(manifest, provider_backend_id="unix_local")


@pytest.mark.parametrize(
    "strategy",
    [
        _CustomDockerVolumeStrategy(
            driver="rclone",
            driver_options={"vfs-cache-mode": "off"},
        ),
        _CustomModalCloudBucketStrategy(secret_name="named-modal-secret"),
    ],
)
def test_unknown_sdk_strategy_subclasses_cannot_retain_opaque_authority(
    strategy: MountStrategyBase,
) -> None:
    manifest = Manifest(entries={"data": S3Mount(bucket="bucket", mount_strategy=strategy)})

    with pytest.raises(MountConfigError, match="custom mount strategies"):
        validate_manifest_mount_credential_boundaries(manifest, provider_backend_id="docker")


def test_custom_strategy_rejects_pydantic_extra_configuration() -> None:
    class ExtraStrategy(DockerVolumeMountStrategy):
        type: Literal["extra_strategy"] = "extra_strategy"  # type: ignore[assignment]
        model_config = ConfigDict(extra="allow")

    strategy = ExtraStrategy.model_validate(
        {"driver": "rclone", "api_token": "custom-strategy-extra-secret"}
    )
    manifest = Manifest(entries={"data": S3Mount(bucket="bucket", mount_strategy=strategy)})

    with pytest.raises(MountConfigError, match="custom mount strategies") as exc:
        validate_manifest_mount_credential_boundaries(manifest)

    assert "custom-strategy-extra-secret" not in str(exc.value)


def test_custom_strategy_cannot_forge_builtin_class_provenance() -> None:
    original_class = MountStrategyBase._subclass_registry["in_container"]
    forged_class = type(
        "InContainerMountStrategy",
        (InContainerMountStrategy,),
        {
            "__module__": InContainerMountStrategy.__module__,
            "__qualname__": InContainerMountStrategy.__qualname__,
            "__annotations__": {"api_token": str | None},
            "api_token": None,
        },
    )
    try:
        strategy = cast(Any, forged_class)(
            pattern=RcloneMountPattern(),
            api_token="forged-strategy-secret",
        )
        manifest = Manifest(entries={"data": S3Mount(bucket="bucket", mount_strategy=strategy)})

        with pytest.raises(MountConfigError, match="custom mount strategies"):
            validate_manifest_mount_credential_boundaries(manifest)
    finally:
        MountStrategyBase._subclass_registry["in_container"] = original_class


def test_behavior_only_mount_strategy_is_rejected_before_activate() -> None:
    class BehaviorOnlyStrategy(InContainerMountStrategy):
        type: Literal["behavior_only_strategy"] = "behavior_only_strategy"  # type: ignore[assignment]
        activate_called: ClassVar[bool] = False

        async def activate(
            self,
            mount: Mount,
            session: Any,
            dest: Path,
            base_dir: Path,
        ) -> list[Any]:
            _ = (mount, session, dest, base_dir)
            type(self).activate_called = True
            return []

    strategy = BehaviorOnlyStrategy(pattern=RcloneMountPattern())
    manifest = Manifest(entries={"data": S3Mount(bucket="bucket", mount_strategy=strategy)})

    with pytest.raises(MountConfigError, match="custom mount strategies"):
        validate_manifest_mount_credential_boundaries(manifest)

    assert BehaviorOnlyStrategy.activate_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("credentialed", [False, True])
async def test_mount_apply_rejects_behavior_only_mount_strategy(
    credentialed: bool,
) -> None:
    sentinel = "direct-apply-secret"

    class BehaviorOnlyStrategy(InContainerMountStrategy):
        type: Literal["direct_apply_behavior_only"] = "direct_apply_behavior_only"  # type: ignore[assignment]
        activate_called: ClassVar[bool] = False

        async def activate(
            self,
            mount: Mount,
            session: Any,
            dest: Path,
            base_dir: Path,
        ) -> list[Any]:
            _ = (mount, session, dest, base_dir)
            type(self).activate_called = True
            return []

    mount = S3Mount(
        bucket="bucket",
        access_key_id="access-key" if credentialed else None,
        secret_access_key=sentinel if credentialed else None,
        mount_strategy=BehaviorOnlyStrategy(pattern=RcloneMountPattern()),
    )
    session = cast(Any, type("Session", (), {"state": type("State", (), {"type": "test"})()})())

    with pytest.raises(MountConfigError, match="custom mount strategies") as exc:
        await mount.apply(session, Path("/workspace/data"), Path("/workspace"))

    assert BehaviorOnlyStrategy.activate_called is False
    assert sentinel not in str(exc.value)


def test_custom_mount_pattern_fields_are_rejected_before_apply() -> None:
    class CustomRclonePattern(RcloneMountPattern):
        api_token: str | None = None
        apply_called: ClassVar[bool] = False

        async def apply(self, session: Any, path: Path, config: Any) -> None:
            _ = (session, path, config)
            type(self).apply_called = True

    pattern = CustomRclonePattern(api_token="custom-pattern-secret")
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=pattern),
            )
        }
    )

    with pytest.raises(MountConfigError, match="custom mount patterns") as exc:
        validate_manifest_mount_credential_boundaries(manifest)

    assert CustomRclonePattern.apply_called is False
    assert "custom-pattern-secret" not in str(exc.value)


def test_behavior_only_mount_pattern_is_rejected_before_apply() -> None:
    class BehaviorOnlyRclonePattern(RcloneMountPattern):
        apply_called: ClassVar[bool] = False

        async def apply(self, session: Any, path: Path, config: Any) -> None:
            _ = (session, path, config)
            type(self).apply_called = True

    pattern = BehaviorOnlyRclonePattern()
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=pattern),
            )
        }
    )

    with pytest.raises(MountConfigError, match="custom mount patterns"):
        validate_manifest_mount_credential_boundaries(manifest)

    assert BehaviorOnlyRclonePattern.apply_called is False


def test_mount_activation_rejects_custom_pattern_before_deepcopy() -> None:
    sentinel = "custom-pattern-deepcopy-secret"

    class CustomRclonePattern(RcloneMountPattern):
        deepcopy_called: ClassVar[bool] = False

        def __deepcopy__(self, memo: dict[int, Any] | None = None) -> CustomRclonePattern:
            _ = memo
            type(self).deepcopy_called = True
            raise RuntimeError(sentinel)

    strategy = InContainerMountStrategy(pattern=CustomRclonePattern())
    mount = S3Mount(bucket="bucket", mount_strategy=strategy)

    with pytest.raises(MountConfigError, match="custom mount patterns") as exc_info:
        validate_mount_activation_credential_boundary(mount, strategy)

    assert CustomRclonePattern.deepcopy_called is False
    assert sentinel not in repr(exc_info.value)


def test_ignores_environment_values_already_exposed_to_the_sandbox() -> None:
    manifest = Manifest(
        entries={
            "data": _s3_mount(strategy=InContainerMountStrategy(pattern=MountpointMountPattern()))
        },
        environment=Environment(
            value={"AWS_SECRET_ACCESS_KEY": "secret", "GITHUB_TOKEN": "unrelated"}
        ),
    )

    validate_manifest_mount_credential_boundaries(manifest)


def test_blobfuse_mounts_require_broad_acknowledgement() -> None:
    manifest = Manifest(
        entries={
            "data": AzureBlobMount(
                account="example",
                container="public",
                mount_strategy=InContainerMountStrategy(pattern=FuseMountPattern()),
            )
        }
    )

    with pytest.raises(MountConfigError, match="broad credential authority"):
        validate_manifest_mount_credential_boundaries(manifest)

    validate_manifest_mount_credential_boundaries(
        manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
    )


def test_blobfuse_account_key_requires_mount_scoped_and_broad_acknowledgement() -> None:
    manifest = Manifest(
        entries={
            "data": AzureBlobMount(
                account="example",
                container="private",
                account_key="account-key",
                mount_strategy=InContainerMountStrategy(pattern=FuseMountPattern()),
            )
        }
    )

    mount_scoped = manifest.with_in_container_mount_credential_exposure_acknowledged("data")
    with pytest.raises(MountConfigError, match="broad credential authority"):
        validate_manifest_mount_credential_boundaries(mount_scoped)

    broad = manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
    with pytest.raises(MountConfigError, match="mount-scoped credentials"):
        validate_manifest_mount_credential_boundaries(broad)

    validate_manifest_mount_credential_boundaries(
        mount_scoped.with_in_container_mount_broad_credential_exposure_acknowledged("data")
    )


def test_s3_files_require_broad_acknowledgement_before_ambient_iam_can_be_used() -> None:
    safe = Manifest(
        entries={
            "data": S3FilesMount(
                file_system_id="fs-123",
                extra_options={"tlsport": "4049"},
                mount_strategy=InContainerMountStrategy(pattern=S3FilesMountPattern()),
            )
        }
    )
    with pytest.raises(MountConfigError, match="broad credential authority"):
        validate_manifest_mount_credential_boundaries(safe)

    validate_manifest_mount_credential_boundaries(
        safe.with_in_container_mount_broad_credential_exposure_acknowledged("data")
    )


@pytest.mark.parametrize(
    "mount",
    [
        AzureBlobMount(
            account="example",
            container="public",
            mount_strategy=_CustomInContainerStrategy(pattern=FuseMountPattern()),
        ),
        S3FilesMount(
            file_system_id="fs-123",
            mount_strategy=_CustomInContainerStrategy(pattern=S3FilesMountPattern()),
        ),
    ],
)
def test_rejects_credential_required_patterns_in_inherited_in_container_strategies(
    mount: Any,
) -> None:
    with pytest.raises(MountConfigError, match="custom mount strategies"):
        validate_manifest_mount_credential_boundaries(Manifest(entries={"data": mount}))


def test_acknowledgement_requires_a_matching_provider_owned_strategy() -> None:
    strategy = InContainerMountStrategy(pattern=RcloneMountPattern())
    cast(Any, strategy).type = "vercel_cloud_bucket"
    manifest = Manifest(entries={"data": _s3_mount(strategy=strategy, credentialed=True)})

    with pytest.raises(MountConfigError, match="custom mount strategies"):
        manifest.with_in_container_mount_credential_exposure_acknowledged("data")


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--config=/workspace/credentials.conf"],
        ["--s3-env-auth=true"],
        ["--s3-profile=production"],
        ["--azureblob-use-msi=true"],
        ["--header", "Authorization: Bearer secret"],
    ],
)
def test_rejects_rclone_credential_source_overrides(extra_args: list[str]) -> None:
    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=InContainerMountStrategy(
                    pattern=RcloneMountPattern(extra_args=extra_args),
                )
            )
        }
    )

    with pytest.raises(MountConfigError, match="does not support exposing"):
        validate_manifest_mount_credential_boundaries(manifest)


@pytest.mark.parametrize(
    ("strategy", "backend_id"),
    [
        (InContainerMountStrategy(pattern=RcloneMountPattern()), None),
        (DaytonaCloudBucketMountStrategy(), "daytona"),
    ],
)
def test_box_mounts_with_direct_credentials_require_exact_acknowledgement(
    strategy: MountStrategyBase,
    backend_id: str | None,
) -> None:
    manifest = Manifest(
        entries={
            "data": BoxMount(
                access_token="box-access-token",
                mount_strategy=strategy,
            )
        }
    )

    with pytest.raises(MountConfigError, match="mount-scoped credentials"):
        validate_manifest_mount_credential_boundaries(
            manifest,
            provider_backend_id=backend_id,
        )

    validate_manifest_mount_credential_boundaries(
        manifest.with_in_container_mount_credential_exposure_acknowledged("data"),
        provider_backend_id=backend_id,
    )


def test_box_config_file_requires_broad_acknowledgement() -> None:
    manifest = Manifest(
        entries={
            "data": BoxMount(
                box_config_file="/run/secrets/box.json",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    )

    with pytest.raises(MountConfigError, match="broad credential authority"):
        validate_manifest_mount_credential_boundaries(manifest)
    with pytest.raises(MountConfigError, match="broad credential authority"):
        validate_manifest_mount_credential_boundaries(
            manifest.with_in_container_mount_credential_exposure_acknowledged("data")
        )
    validate_manifest_mount_credential_boundaries(
        manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
    )


@pytest.mark.parametrize(
    "mount",
    [
        BoxMount(mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern())),
        BoxMount(
            client_id="client-id",
            mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
        ),
        BoxMount(
            client_secret="client-secret",
            mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
        ),
    ],
)
def test_box_in_container_mount_requires_non_interactive_authentication(
    mount: BoxMount,
) -> None:
    manifest = Manifest(entries={"data": mount})

    with pytest.raises(MountConfigError, match="non-interactive authentication source"):
        validate_manifest_mount_credential_boundaries(manifest)


@pytest.mark.parametrize(
    ("mount", "broad"),
    [
        (
            BoxMount(
                access_token=value,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            False,
        )
        for value in ("", "   ")
    ]
    + [
        (
            BoxMount(
                token=value,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            False,
        )
        for value in ("", "   ")
    ]
    + [
        (
            BoxMount(
                config_credentials=value,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            False,
        )
        for value in ("", "   ")
    ]
    + [
        (
            BoxMount(
                box_config_file=value,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            True,
        )
        for value in ("", "   ")
    ],
)
def test_box_in_container_mount_rejects_empty_authentication_sources(
    mount: BoxMount,
    broad: bool,
) -> None:
    manifest = Manifest(entries={"data": mount})
    acknowledged = (
        manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
        if broad
        else manifest.with_in_container_mount_credential_exposure_acknowledged("data")
    )

    with pytest.raises(MountConfigError, match="authentication values must not be empty"):
        validate_manifest_mount_credential_boundaries(acknowledged)


@pytest.mark.parametrize(
    ("mount", "broad", "invalid_field"),
    [
        (
            BoxMount(
                access_token="box-access-token",
                box_config_file=value,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            False,
            "box_config_file",
        )
        for value in ("", "   ")
    ]
    + [
        (
            BoxMount(
                access_token=value,
                box_config_file="/run/secrets/box.json",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            True,
            "access_token",
        )
        for value in ("", "   ")
    ],
)
def test_box_in_container_mount_rejects_mixed_usable_and_empty_authentication_sources(
    mount: BoxMount,
    broad: bool,
    invalid_field: str,
) -> None:
    manifest = Manifest(entries={"data": mount})
    acknowledged = (
        manifest.with_in_container_mount_broad_credential_exposure_acknowledged("data")
        if broad
        else manifest.with_in_container_mount_credential_exposure_acknowledged("data")
    )

    with pytest.raises(MountConfigError, match="authentication values must not be empty") as exc:
        validate_manifest_mount_credential_boundaries(acknowledged)

    assert exc.value.context["credential_fields"] == (invalid_field,)


def test_preserves_box_mounts_with_an_external_strategy() -> None:
    manifest = Manifest(
        entries={
            "data": BoxMount(
                access_token="box-access-token",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    validate_manifest_mount_credential_boundaries(manifest, provider_backend_id="docker")


def test_preserves_multiline_external_mount_credentials() -> None:
    manifest = Manifest(
        entries={
            "data": GCSMount(
                bucket="bucket",
                service_account_credentials='{"private_key":"line-1\nline-2"}',
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    validate_manifest_mount_credential_boundaries(manifest, provider_backend_id="docker")


@pytest.mark.parametrize(
    ("module_name", "environment"),
    [
        (
            "examples.sandbox.docker.mounts.azure_mount_read_write",
            {
                "AZURE_STORAGE_ACCOUNT": "account",
                "AZURE_STORAGE_CONTAINER": "container",
                "AZURE_STORAGE_ACCOUNT_KEY": "example-key",
            },
        ),
        (
            "examples.sandbox.docker.mounts.gcs_mount_read_write",
            {
                "GCS_MOUNT_BUCKET": "bucket",
                "GCS_ACCESS_ID": "example-access-id",
                "GCS_SECRET_ACCESS_KEY": "example-secret-key",
            },
        ),
    ],
)
def test_docker_mount_examples_use_supported_external_strategies(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    environment: dict[str, str],
) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    module = importlib.import_module(module_name)

    cases = module._mount_cases()

    assert [case.name for case in cases] == ["docker_volume/rclone"]
    for case in cases:
        assert isinstance(case.mount.mount_strategy, DockerVolumeMountStrategy)
        validate_manifest_mount_credential_boundaries(
            Manifest(entries={case.mount_dir: case.mount}),
            provider_backend_id="docker",
        )


@pytest.mark.parametrize(
    ("mount", "field_name"),
    [
        (
            S3Mount(
                bucket="bucket",
                s3_provider="AWS\naccess_key_id = injected-value",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            "s3_provider",
        ),
        (
            AzureBlobMount(
                account="account\nkey = injected-value",
                container="container",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            "account",
        ),
        (
            R2Mount(
                bucket="bucket",
                account_id="account\nsecret_access_key = injected-value",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
            "account_id",
        ),
    ],
)
def test_rejects_and_redacts_rclone_config_line_injection(
    mount: S3Mount | AzureBlobMount | R2Mount,
    field_name: str,
) -> None:
    manifest = Manifest(entries={"data": mount})

    with pytest.raises(MountConfigError, match="must not contain line breaks") as exc:
        validate_manifest_mount_credential_boundaries(manifest)

    assert exc.value.context["configuration_fields"] == (field_name,)
    assert "injected-value" not in str(exc.value)

    sanitized, redacted = sanitize_manifest_mount_authority(manifest)
    sanitized_mount = sanitized.entries["data"]
    assert redacted is True
    assert getattr(sanitized_mount, field_name) == ""
    assert "injected-value" not in repr(sanitized)


@pytest.mark.parametrize(
    ("mount", "field_name"),
    [
        (
            S3Mount(
                bucket="bucket",
                endpoint_url=("https://s3.example,public_bucket=0,passwd_file=/workspace/creds"),
                mount_strategy=BlaxelCloudBucketMountStrategy(),
            ),
            "endpoint_url",
        ),
        (
            S3Mount(
                bucket="bucket",
                region="us-east-1,public_bucket=0,passwd_file=/workspace/creds",
                mount_strategy=BlaxelCloudBucketMountStrategy(),
            ),
            "region",
        ),
        (
            R2Mount(
                bucket="bucket",
                account_id="account",
                custom_domain=("https://r2.example,public_bucket=0,passwd_file=/workspace/creds"),
                mount_strategy=BlaxelCloudBucketMountStrategy(),
            ),
            "custom_domain",
        ),
        (
            R2Mount(
                bucket="bucket",
                account_id="account,public_bucket=0,passwd_file=/workspace/creds",
                mount_strategy=BlaxelCloudBucketMountStrategy(),
            ),
            "account_id",
        ),
    ],
)
def test_rejects_blaxel_s3fs_endpoint_option_injection(
    mount: S3Mount | R2Mount,
    field_name: str,
) -> None:
    sentinel = "s3fs-endpoint-secret"
    manifest = Manifest(
        entries={
            "creds": File(content=sentinel.encode()),
            "data": mount,
        }
    )

    with pytest.raises(MountConfigError, match="must not contain s3fs option delimiters") as exc:
        validate_manifest_mount_credential_boundaries(
            manifest,
            provider_backend_id="blaxel",
        )

    assert exc.value.context["configuration_fields"] == (field_name,)
    assert sentinel not in str(exc.value)

    state = TestSessionState(manifest=manifest, snapshot=NoopSnapshot(id="snapshot"))
    with pytest.raises(MountConfigError) as serialization_exc:
        _SecurityTestClient().serialize_session_state(state)
    assert sentinel not in str(serialization_exc.value)


def test_rejects_rclone_on_the_fly_remote_name() -> None:
    sentinel = "remote-name-secret"
    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=InContainerMountStrategy(
                    pattern=RcloneMountPattern(
                        remote_name=f":s3,access_key_id=access,secret_access_key={sentinel}"
                    )
                )
            )
        }
    )

    with pytest.raises(MountConfigError, match="does not support exposing") as exc:
        validate_manifest_mount_credential_boundaries(manifest)

    assert sentinel not in str(exc.value)


def test_serialization_redacts_rclone_on_the_fly_remote_name() -> None:
    sentinel = "serialized-remote-name-secret"
    state = TestSessionState(
        manifest=Manifest(
            entries={
                "data": _s3_mount(
                    strategy=InContainerMountStrategy(
                        pattern=RcloneMountPattern(remote_name=f":s3,secret_access_key={sentinel}")
                    )
                )
            }
        ),
        snapshot=NoopSnapshot(id="snapshot"),
    )

    payload = _SecurityTestClient().serialize_session_state(state)

    pattern = payload["manifest"]["entries"]["data"]["mount_strategy"]["pattern"]  # type: ignore[index]
    assert pattern["remote_name"] is None
    assert payload[REDACTED_MOUNT_AUTHORITY_KEY] is True
    assert sentinel not in repr(payload)


def test_preserves_ordinary_rclone_remote_name() -> None:
    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=InContainerMountStrategy(
                    pattern=RcloneMountPattern(remote_name="public bucket-1")
                )
            )
        }
    )

    validate_manifest_mount_credential_boundaries(manifest)
    sanitized, redacted = sanitize_raw_session_state_mount_authority(
        {
            "type": "test",
            "manifest": manifest.model_dump(mode="json"),
            "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
        }
    )

    assert redacted is False
    assert (
        sanitized["manifest"]["entries"]["data"]["mount_strategy"]["pattern"][  # type: ignore[index]
            "remote_name"
        ]
        == "public bucket-1"
    )


def test_preserves_supported_credentialless_rclone_extra_args() -> None:
    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=InContainerMountStrategy(
                    pattern=RcloneMountPattern(
                        extra_args=[
                            "--allow-other",
                            "--uid",
                            "123",
                            "--gid=456",
                            "--buffer-size",
                            "0",
                        ]
                    ),
                )
            )
        }
    )

    validate_manifest_mount_credential_boundaries(manifest)


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "https://user:malformed-secret@[invalid",
        "https:user:malformed-secret@example.test",
    ],
)
def test_rejects_malformed_inline_credential_url_without_mutating_trusted_manifest(
    endpoint_url: str,
) -> None:
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="example-bucket",
                endpoint_url=endpoint_url,
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    )

    with pytest.raises(MountConfigError, match="does not support exposing"):
        validate_manifest_mount_credential_boundaries(manifest)

    mount = manifest.entries["data"]
    assert isinstance(mount, S3Mount)
    assert mount.endpoint_url == endpoint_url


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "https://user:pattern-secret@example.test",
        "https://example.test?signature=pattern-secret",
    ],
)
def test_rejects_mountpoint_endpoint_authority(endpoint_url: str) -> None:
    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=InContainerMountStrategy(
                    pattern=MountpointMountPattern(
                        options=MountpointMountPattern.MountpointOptions(
                            endpoint_url=endpoint_url,
                        )
                    )
                )
            )
        }
    )

    with pytest.raises(MountConfigError, match="does not support exposing") as exc:
        validate_manifest_mount_credential_boundaries(manifest)

    assert exc.value.context["credential_fields"] == (
        "mount_strategy.pattern.options.endpoint_url",
    )
    assert "pattern-secret" not in str(exc.value)


@pytest.mark.parametrize(
    ("mount", "credential_path"),
    [
        (
            GCSMount(
                bucket="bucket",
                service_account_file="/workspace/credentials.json",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            ),
            "/workspace/credentials.json",
        ),
        (
            BoxMount(
                box_config_file="credentials.json",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            ),
            "credentials.json",
        ),
    ],
)
def test_rejects_manifest_backed_credential_files(
    mount: GCSMount | BoxMount,
    credential_path: str,
) -> None:
    _ = credential_path
    manifest = Manifest(
        entries={
            "credentials.json": File(content=b"credential-file-secret"),
            "data": mount,
        }
    )

    with pytest.raises(MountConfigError, match="credential files stored in the manifest"):
        validate_manifest_mount_credential_boundaries(
            manifest,
            provider_backend_id="docker",
        )


def test_broad_acknowledgement_does_not_allow_manifest_backed_rclone_config() -> None:
    manifest = Manifest(
        entries={
            "credentials.conf": File(content=b"credential-file-secret"),
            "data": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(
                    pattern=RcloneMountPattern(config_file_path=Path("credentials.conf"))
                ),
            ),
        }
    ).with_in_container_mount_broad_credential_exposure_acknowledged("data")

    with pytest.raises(MountConfigError, match="credential files stored in the manifest"):
        validate_manifest_mount_credential_boundaries(manifest)


@pytest.mark.parametrize(
    ("credential_path", "source"),
    [
        ("/workspace/credentials.json", LocalFile(src=Path("credentials.json"))),
        ("/workspace/imported/credentials.json", LocalDir(src=Path("imported"))),
        (
            "/workspace/repository/credentials.json",
            GitRepo(repo="example/repository", ref="main"),
        ),
        (
            "/workspace/secrets/credentials.json",
            S3Mount(
                bucket="secret-bucket",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            ),
        ),
    ],
)
def test_rejects_credential_files_from_manifest_materialization_sources(
    credential_path: str,
    source: BaseEntry,
) -> None:
    source_path = credential_path.removeprefix("/workspace/").split("/", 1)[0]
    if credential_path == "/workspace/credentials.json":
        source_path = "credentials.json"
    manifest = Manifest(
        entries={
            source_path: source,
            "data": GCSMount(
                bucket="bucket",
                service_account_file=credential_path,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            ),
        }
    )

    with pytest.raises(MountConfigError, match="credential files stored in the manifest"):
        validate_manifest_mount_credential_boundaries(
            manifest,
            provider_backend_id="docker",
        )


def test_session_state_serialization_redacts_complete_opaque_authority_fields() -> None:
    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=DockerVolumeMountStrategy(
                    driver="rclone",
                    driver_options={
                        "vfs-cache-mode": "off",
                        "s3-secret-access-key": "driver-secret",
                        "s3-env-auth": "true",
                        "config": "/host/rclone.conf",
                    },
                ),
                credentialed=True,
            )
        }
    )
    state = TestSessionState(
        manifest=manifest,
        snapshot=NoopSnapshot(id="snapshot"),
    )
    client = _SecurityTestClient()

    payload = client.serialize_session_state(state)
    serialized_mount = payload["manifest"]["entries"]["data"]  # type: ignore[index]

    assert payload[REDACTED_MOUNT_AUTHORITY_KEY] is True
    assert serialized_mount["access_key_id"] is None
    assert serialized_mount["secret_access_key"] is None
    assert serialized_mount["mount_strategy"]["driver_options"] == {}
    assert "example-secret-key" not in repr(payload)
    assert "driver-secret" not in repr(payload)

    restored = client.deserialize_session_state(payload)
    assert restored.mount_authority_redacted is True

    trusted_manifest = manifest.model_copy(deep=True)
    rebound = restored.rebind_persisted_mount_authority(
        trusted_manifest,
        provider_backend_id="docker",
    )
    rebound_mount = rebound.manifest.entries["data"]
    assert isinstance(rebound_mount, S3Mount)
    trusted_mount = trusted_manifest.entries["data"]
    assert isinstance(trusted_mount, S3Mount)
    assert rebound_mount.access_key_id == "example-access-key"
    assert rebound_mount.secret_access_key == "example-secret-key"
    assert rebound_mount.mount_strategy == trusted_mount.mount_strategy
    assert rebound.mount_authority_redacted is False
    assert rebound.mount_authority_rebound is True
    validate_manifest_mount_credential_boundaries(
        rebound.manifest,
        provider_backend_id="docker",
    )


def test_session_state_round_trip_preserves_credentialless_external_mount() -> None:
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="example-bucket",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )
    state = TestSessionState(manifest=manifest, snapshot=NoopSnapshot(id="snapshot"))
    client = _SecurityTestClient()

    payload = client.serialize_session_state(state)
    restored = client.deserialize_session_state(payload)

    assert CREDENTIALLESS_MOUNT_AUTHORITY_KEY not in payload
    assert REDACTED_MOUNT_AUTHORITY_KEY not in payload
    assert restored.manifest == manifest
    assert restored.mount_authority_redacted is False
    assert restored.mount_authority_rebound is False


def test_credentialless_marker_does_not_override_configured_mount_authority() -> None:
    sentinel = "configured-secret-access-key"
    payload: dict[str, object] = {
        "type": "test",
        "manifest": Manifest(
            entries={
                "data": S3Mount(
                    bucket="example-bucket",
                    access_key_id="access-key",
                    secret_access_key=sentinel,
                    mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                )
            }
        ).model_dump(mode="json"),
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
        CREDENTIALLESS_MOUNT_AUTHORITY_KEY: True,
    }

    restored = _SecurityTestClient().deserialize_session_state(payload)

    assert restored.mount_authority_redacted is True
    assert CREDENTIALLESS_MOUNT_AUTHORITY_KEY not in payload
    assert payload[REDACTED_MOUNT_AUTHORITY_KEY] is True
    assert sentinel not in repr(payload)


def test_session_state_serialization_preserves_custom_non_mount_fields() -> None:
    state = TestSessionState(
        manifest=Manifest(entries={"custom": _CustomTokenEntry(token="ordinary-token-value")}),
        snapshot=NoopSnapshot(id="snapshot"),
    )

    payload = _SecurityTestClient().serialize_session_state(state)
    restored = _SecurityTestClient().deserialize_session_state(payload)

    assert REDACTED_MOUNT_AUTHORITY_KEY not in payload
    assert payload["manifest"]["entries"]["custom"]["token"] == "ordinary-token-value"  # type: ignore[index]
    entry = restored.manifest.entries["custom"]
    assert isinstance(entry, _CustomTokenEntry)
    assert entry.token == "ordinary-token-value"


@pytest.mark.parametrize(
    "children",
    [
        "ordinary-metadata",
        {
            "nested": {
                "type": "s3_mount",
                "access_key_id": "ordinary-access-metadata",
                "secret_access_key": "ordinary-secret-metadata",
            }
        },
    ],
)
def test_session_state_serialization_preserves_custom_non_dir_children(children: Any) -> None:
    state = TestSessionState(
        manifest=Manifest(entries={"custom": _CustomChildrenEntry(children=children)}),
        snapshot=NoopSnapshot(id="snapshot"),
    )

    payload = _SecurityTestClient().serialize_session_state(state)
    restored = _SecurityTestClient().deserialize_session_state(payload)

    assert REDACTED_MOUNT_AUTHORITY_KEY not in payload
    assert payload["manifest"]["entries"]["custom"]["children"] == children  # type: ignore[index]
    entry = restored.manifest.entries["custom"]
    assert isinstance(entry, _CustomChildrenEntry)
    assert entry.children == children


def test_session_state_serialization_rejects_registered_custom_strategy_configuration() -> None:
    pattern = {
        "type": "custom_pattern",
        "extra_args": ["--ordinary-option"],
        "remote_name": "ordinary-remote",
        "options": {"endpoint_url": "https://public.example.test"},
    }
    state = TestSessionState(
        manifest=Manifest(
            entries={
                "data": S3Mount(
                    bucket="example-bucket",
                    mount_strategy=_CustomPatternStrategy(
                        pattern=pattern,
                        api_token="custom-strategy-secret",
                    ),
                )
            }
        ),
        snapshot=NoopSnapshot(id="snapshot"),
    )

    with pytest.raises(MountConfigError, match="custom mount strategies") as exc:
        _SecurityTestClient().serialize_session_state(state)

    assert "custom-strategy-secret" not in str(exc.value)


def test_session_state_serialization_redacts_custom_strategy_with_known_discriminator() -> None:
    strategy = _CustomPatternStrategy(
        pattern={"type": "custom_pattern"},
        api_token="custom-strategy-secret",
    )
    cast(Any, strategy).type = "docker_volume"
    state = TestSessionState(
        manifest=Manifest(
            entries={
                "data": S3Mount(
                    bucket="example-bucket",
                    mount_strategy=strategy,
                )
            }
        ),
        snapshot=NoopSnapshot(id="snapshot"),
    )

    with pytest.raises(MountConfigError, match="custom mount strategies") as exc_info:
        _SecurityTestClient().serialize_session_state(state)

    assert "custom-strategy-secret" not in repr(exc_info.value)


def test_rejects_configured_custom_mount_strategies_before_side_effects() -> None:
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="example-bucket",
                mount_strategy=_CustomPatternStrategy(
                    pattern={},
                    api_token="custom-strategy-secret",
                ),
            )
        }
    )

    with pytest.raises(MountConfigError, match="custom mount strategies") as exc:
        validate_manifest_mount_credential_boundaries(manifest)

    assert "custom-strategy-secret" not in str(exc.value)


def test_custom_entry_at_credential_file_path_is_rejected() -> None:
    manifest = Manifest(
        entries={
            "credentials.json": _CustomTokenEntry(token="custom-source"),
            "data": GCSMount(
                bucket="bucket",
                service_account_file="/workspace/credentials.json",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            ),
        }
    )

    with pytest.raises(MountConfigError, match="credential files stored in the manifest"):
        validate_manifest_mount_credential_boundaries(
            manifest,
            provider_backend_id="docker",
        )


def test_session_state_serialization_rejects_custom_credential_file_materializer() -> None:
    sentinel = "custom-source-secondary-secret"
    state = TestSessionState(
        manifest=Manifest(
            entries={
                "credentials.json": _CustomCredentialSourceEntry(
                    content="ordinary-content",
                    source_token=sentinel,
                ),
                "data": GCSMount(
                    bucket="bucket",
                    service_account_file="/workspace/credentials.json",
                    mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                ),
            }
        ),
        snapshot=NoopSnapshot(id="snapshot"),
    )

    with pytest.raises(MountConfigError) as exc:
        _SecurityTestClient().serialize_session_state(state)

    assert sentinel not in str(exc.value)
    traceback = exc.value.__traceback__
    while traceback is not None:
        module_name = traceback.tb_frame.f_globals.get("__name__", "")
        if isinstance(module_name, str) and module_name.startswith("agents."):
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


def test_structural_local_dir_credential_path_remains_serializable() -> None:
    sentinel = "structural-local-dir-secret"
    state = TestSessionState(
        manifest=Manifest(
            entries={
                "credentials": LocalDir(src=None),
                "data": GCSMount(
                    bucket="bucket",
                    service_account_file="/workspace/credentials/key.json",
                    service_account_credentials=sentinel,
                    mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                ),
            }
        ),
        snapshot=NoopSnapshot(id="snapshot"),
    )

    validate_manifest_mount_credential_boundaries(state.manifest, provider_backend_id="docker")
    payload = _SecurityTestClient().serialize_session_state(state)

    assert payload[REDACTED_MOUNT_AUTHORITY_KEY] is True
    assert sentinel not in repr(payload)


@pytest.mark.parametrize(
    "backend_id",
    ["docker", "modal"],
)
def test_opaque_external_authority_remains_resumable_through_trusted_rebind(
    backend_id: str,
) -> None:
    if backend_id == "docker":
        strategy: MountStrategyBase = DockerVolumeMountStrategy(
            driver="rclone",
            driver_options={"vfs-cache-mode": "off"},
        )
    else:
        modal_mounts = importlib.import_module("agents.extensions.sandbox.modal.mounts")
        strategy = modal_mounts.ModalCloudBucketMountStrategy(
            secret_name="named-modal-secret",
            secret_environment_name="staging",
        )
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="example-bucket",
                mount_strategy=strategy,
            )
        }
    )
    client = _SecurityTestClient()
    state = TestSessionState(manifest=manifest, snapshot=NoopSnapshot(id="snapshot"))

    payload = client.serialize_session_state(state)
    restored = client.deserialize_session_state(payload)
    rebound = restored.rebind_persisted_mount_authority(
        manifest,
        provider_backend_id=backend_id,
    )

    assert payload[REDACTED_MOUNT_AUTHORITY_KEY] is True
    assert rebound.manifest == manifest
    assert rebound.mount_authority_redacted is False


def test_in_container_acknowledgement_is_rebound_only_from_trusted_manifest() -> None:
    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                credentialed=True,
            )
        }
    ).with_in_container_mount_credential_exposure_acknowledged("data")
    client = _SecurityTestClient()
    state = TestSessionState(manifest=manifest, snapshot=NoopSnapshot(id="snapshot"))

    payload = client.serialize_session_state(state)
    restored = client.deserialize_session_state(payload)

    assert "credential_exposure" not in repr(payload)
    with pytest.raises(ValueError, match="cannot be resumed"):
        restored.assert_path_grants_rebound()

    rebound = restored.rebind_persisted_mount_authority(
        manifest,
        provider_backend_id="docker",
    )
    validate_manifest_mount_credential_boundaries(
        rebound.manifest,
        provider_backend_id="docker",
    )
    assert rebound.manifest._acknowledges_in_container_mount_credential_exposure(
        "/workspace/data",
        "mount_scoped",
    )


@pytest.mark.parametrize(
    "mount",
    [
        AzureBlobMount(
            account="example",
            container="private",
            mount_strategy=InContainerMountStrategy(pattern=FuseMountPattern()),
        ),
        S3FilesMount(
            file_system_id="fs-123",
            mount_strategy=InContainerMountStrategy(pattern=S3FilesMountPattern()),
        ),
    ],
)
def test_implicit_broad_authority_is_rebound_only_from_trusted_manifest(
    mount: Mount,
) -> None:
    manifest = Manifest(
        entries={"data": mount}
    ).with_in_container_mount_broad_credential_exposure_acknowledged("data")
    client = _SecurityTestClient()
    state = TestSessionState(manifest=manifest, snapshot=NoopSnapshot(id="snapshot"))

    payload = client.serialize_session_state(state)
    restored = client.deserialize_session_state(payload)

    assert payload[REDACTED_MOUNT_AUTHORITY_KEY] is True
    assert "credential_exposure" not in repr(payload)
    assert restored.mount_authority_redacted is True
    rebound = restored.rebind_persisted_mount_authority(
        manifest,
        provider_backend_id="docker",
    )
    validate_manifest_mount_credential_boundaries(
        rebound.manifest,
        provider_backend_id="docker",
    )
    assert rebound.manifest._acknowledges_in_container_mount_credential_exposure(
        "/workspace/data",
        "broad",
    )


def test_mount_authority_rebind_requires_exact_credential_free_topology() -> None:
    original = Manifest(
        entries={
            "data": _s3_mount(
                strategy=DockerVolumeMountStrategy(driver="rclone"),
                credentialed=True,
            )
        }
    )
    state = TestSessionState(manifest=original, snapshot=NoopSnapshot(id="snapshot"))
    client = _SecurityTestClient()
    restored = client.deserialize_session_state(client.serialize_session_state(state))
    mismatched = original.model_copy(deep=True)
    mount = mismatched.entries["data"]
    assert isinstance(mount, S3Mount)
    mount.bucket = "different-bucket"

    with pytest.raises(MountConfigError, match="exactly matching"):
        restored.rebind_persisted_mount_authority(
            mismatched,
            provider_backend_id="docker",
        )

    trusted_mount = mismatched.entries["data"]
    assert isinstance(trusted_mount, S3Mount)
    assert trusted_mount.access_key_id == "example-access-key"
    assert trusted_mount.secret_access_key == "example-secret-key"

    root_mismatched = original.model_copy(deep=True)
    root_mismatched.root = "/different-workspace"

    with pytest.raises(MountConfigError, match="exactly matching"):
        restored.rebind_persisted_mount_authority(
            root_mismatched,
            provider_backend_id="docker",
        )


def test_resume_validation_rejects_wrong_provider_strategy() -> None:
    state = TestSessionState(
        manifest=Manifest(
            entries={
                "data": S3Mount(
                    bucket="example-bucket",
                    mount_strategy=DaytonaCloudBucketMountStrategy(),
                )
            }
        ),
        snapshot=NoopSnapshot(id="snapshot"),
    )

    with pytest.raises(MountConfigError, match="not supported by this sandbox backend"):
        state.assert_path_grants_rebound()


def test_session_state_serialization_redacts_pattern_authority() -> None:
    manifest = Manifest(
        entries={
            "credentials.conf": File(content=b"credential-file-secret"),
            "rclone": _s3_mount(
                strategy=InContainerMountStrategy(
                    pattern=RcloneMountPattern(
                        extra_args=[
                            "--vfs-cache-mode=off",
                            "--config=/workspace/credentials.conf",
                        ]
                    )
                )
            ),
            "s3files": S3FilesMount(
                file_system_id="fs-123",
                mount_strategy=InContainerMountStrategy(
                    pattern=S3FilesMountPattern(
                        options=S3FilesMountPattern.S3FilesOptions(
                            extra_options={
                                "tlsport": "4049",
                                "secret_access_key": "pattern-secret",
                            }
                        )
                    )
                ),
            ),
            "mountpoint": _s3_mount(
                strategy=InContainerMountStrategy(
                    pattern=MountpointMountPattern(
                        options=MountpointMountPattern.MountpointOptions(
                            endpoint_url="https://example.test?signature=pattern-secret"
                        )
                    )
                )
            ),
        }
    )
    state = TestSessionState(manifest=manifest, snapshot=NoopSnapshot(id="snapshot"))

    payload = _SecurityTestClient().serialize_session_state(state)
    entries = payload["manifest"]["entries"]  # type: ignore[index]

    assert payload[REDACTED_MOUNT_AUTHORITY_KEY] is True
    assert entries["credentials.conf"]["content"] == ""
    assert entries["rclone"]["mount_strategy"]["pattern"]["extra_args"] == []
    assert entries["s3files"]["mount_strategy"]["pattern"]["options"]["extra_options"] == {}
    assert entries["mountpoint"]["mount_strategy"]["pattern"]["options"]["endpoint_url"] is None
    assert "credential-file-secret" not in repr(payload)
    assert "pattern-secret" not in repr(payload)


def test_session_state_rejects_inherited_in_container_strategy() -> None:
    manifest = Manifest(
        entries={
            "credentials.conf": File(content=b"credential-file-secret"),
            "data": _s3_mount(
                strategy=_CustomInContainerStrategy(
                    pattern=RcloneMountPattern(
                        extra_args=["--config=/workspace/credentials.conf"],
                    )
                )
            ),
        }
    )

    with pytest.raises(MountConfigError, match="custom mount strategies") as exc:
        _SecurityTestClient().serialize_session_state(
            TestSessionState(manifest=manifest, snapshot=NoopSnapshot(id="snapshot"))
        )

    assert "credential-file-secret" not in str(exc.value)


def test_raw_state_sanitization_preserves_explicit_sandbox_environment() -> None:
    manifest = Manifest(
        entries={
            "credentials.json": File(content=b"credential-file-secret"),
            "data": GCSMount(
                bucket="bucket",
                service_account_file="/workspace/credentials.json",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
        }
    )
    payload: dict[str, object] = {
        "type": "test",
        "manifest": manifest.model_dump(mode="json"),
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
        "base_envs": {
            "AWS_SECRET_ACCESS_KEY": "ambient-secret",
            "GITHUB_TOKEN": "unrelated",
        },
    }

    sanitized, redacted = sanitize_raw_session_state_mount_authority(payload)

    assert redacted is True
    assert isinstance(sanitized, dict)
    assert sanitized[REDACTED_MOUNT_AUTHORITY_KEY] is True
    assert sanitized["manifest"]["entries"]["credentials.json"]["content"] == ""
    assert sanitized["base_envs"] == {
        "AWS_SECRET_ACCESS_KEY": "ambient-secret",
        "GITHUB_TOKEN": "unrelated",
    }
    assert "credential-file-secret" not in repr(sanitized)
    assert "ambient-secret" in repr(sanitized)


def test_raw_state_sanitization_rejects_credential_content_without_file_discriminator() -> None:
    manifest = Manifest(
        entries={
            "credentials.json": File(content=b"credential-file-secret"),
            "data": GCSMount(
                bucket="bucket",
                service_account_file="/workspace/credentials.json",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            ),
        }
    ).model_dump(mode="json")
    manifest["entries"]["credentials.json"]["type"] = "unknown_file"
    payload: dict[str, object] = {"manifest": manifest}

    with pytest.raises(ValueError) as exc:
        sanitize_raw_session_state_mount_authority(payload)

    assert "credential-file-secret" not in str(exc.value)


def test_legacy_non_inline_credential_file_source_cannot_survive_deserialization() -> None:
    manifest = Manifest(
        entries={
            "credentials.json": LocalFile(src=Path("trusted/credentials.json")),
            "data": GCSMount(
                bucket="bucket",
                service_account_file="/workspace/credentials.json",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            ),
        }
    )
    payload: dict[str, object] = {
        "type": "test",
        "manifest": manifest.model_dump(mode="json"),
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
    }

    with pytest.raises(ValueError, match="sandbox session state payload is invalid"):
        _SecurityTestClient().deserialize_session_state(payload)

    assert payload == {}


def test_raw_state_sanitization_rejects_unknown_pattern_discriminator() -> None:
    sentinel = "unknown-pattern-secret"
    manifest = Manifest(
        entries={
            "data": _s3_mount(strategy=InContainerMountStrategy(pattern=RcloneMountPattern())),
        }
    ).model_dump(mode="json")
    manifest["entries"]["data"]["mount_strategy"]["pattern"]["type"] = sentinel
    payload: dict[str, object] = {"manifest": manifest}

    with pytest.raises(ValueError, match="unknown type") as exc_info:
        sanitize_raw_session_state_mount_authority(payload)

    assert sentinel not in str(exc_info.value)


def test_raw_state_rejects_registered_custom_mount_before_validation() -> None:
    class CustomValidatedS3Mount(S3Mount):
        type: Literal["custom_validated_s3_mount"] = "custom_validated_s3_mount"  # type: ignore[assignment]
        validator_called: ClassVar[bool] = False

        @model_validator(mode="before")
        @classmethod
        def _record_validation(cls, value: Any) -> Any:
            cls.validator_called = True
            return value

    payload: dict[str, object] = {
        "type": "test",
        "manifest": {
            "entries": {
                "data": {
                    "type": "custom_validated_s3_mount",
                    "bucket": "example-bucket",
                    "access_key_id": "access-key",
                    "secret_access_key": "custom-validator-secret",
                    "mount_strategy": {
                        "type": "in_container",
                        "pattern": {"type": "rclone"},
                    },
                }
            }
        },
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
    }

    with pytest.raises(ValueError, match="sandbox session state payload is invalid") as exc_info:
        _SecurityTestClient().deserialize_session_state(payload)

    assert CustomValidatedS3Mount.validator_called is False
    assert payload == {}
    assert "custom-validator-secret" not in str(exc_info.value)


def test_raw_state_rejects_replaced_strategy_registry_before_validation() -> None:
    class CustomValidatedStrategy(InContainerMountStrategy):
        type: Literal["custom_validated_strategy"] = "custom_validated_strategy"  # type: ignore[assignment]
        validator_called: ClassVar[bool] = False

        @model_validator(mode="before")
        @classmethod
        def _record_validation(cls, value: Any) -> Any:
            cls.validator_called = True
            return value

    original_class = MountStrategyBase._subclass_registry["in_container"]
    MountStrategyBase._subclass_registry["in_container"] = CustomValidatedStrategy
    payload: dict[str, object] = {
        "type": "test",
        "manifest": {
            "entries": {
                "data": {
                    "type": "s3_mount",
                    "bucket": "example-bucket",
                    "mount_strategy": {
                        "type": "in_container",
                        "pattern": {"type": "rclone"},
                    },
                }
            }
        },
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
    }
    try:
        with pytest.raises(
            ValueError, match="sandbox session state payload is invalid"
        ) as exc_info:
            _SecurityTestClient().deserialize_session_state(payload)
    finally:
        MountStrategyBase._subclass_registry["in_container"] = original_class
        MountStrategyBase._subclass_registry.pop("custom_validated_strategy", None)

    assert CustomValidatedStrategy.validator_called is False
    assert payload == {}
    assert "custom-validator-secret" not in str(exc_info.value)


def test_raw_state_rejects_malformed_credential_file_locator() -> None:
    manifest = Manifest(
        entries={
            "credentials.json": File(content=b"credential-file-secret"),
            "data": GCSMount(
                bucket="bucket",
                service_account_file="/workspace/credentials.json",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            ),
        }
    ).model_dump(mode="json")
    manifest["entries"]["data"]["service_account_file"] = ["credentials.json"]
    payload: dict[str, object] = {
        "type": "test",
        "manifest": manifest,
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
    }

    with pytest.raises(ValueError, match="sandbox session state payload is invalid") as exc:
        _SecurityTestClient().deserialize_session_state(payload)

    assert payload == {}
    assert "credential-file-secret" not in str(exc.value)


@pytest.mark.parametrize("boundary", ["async", "sync"])
@pytest.mark.parametrize("error_kind", ["command", "tool_missing"])
@pytest.mark.asyncio
async def test_protected_structured_mount_error_preserves_safe_contract(
    boundary: str,
    error_kind: str,
) -> None:
    sentinel = f"{boundary}-{error_kind}-structured-mount-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )
    child_error = RuntimeError(sentinel)
    expected_type: type[SandboxError]
    if error_kind == "command":
        source_error: SandboxError = MountCommandError(
            command=sentinel,
            stderr=sentinel,
            context={"credential": sentinel},
            cause=child_error,
            retryable=True,
        )
        expected_type = MountCommandError
        expected_code = ErrorCode.MOUNT_FAILED
        expected_retryable = True
    else:
        source_error = MountToolMissingError(
            tool=sentinel,
            context={"credential": sentinel},
            cause=child_error,
        )
        expected_type = MountToolMissingError
        expected_code = ErrorCode.MOUNT_MISSING_TOOL
        expected_retryable = False
    if sys.version_info >= (3, 11):
        source_error.add_note(sentinel)

    if boundary == "async":

        @redact_mount_error_data
        async def fail(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(expected_type) as exc_info:
            await fail(manifest=manifest)
    else:

        @redact_mount_error_data_sync
        def fail_sync(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(expected_type) as exc_info:
            fail_sync(manifest=manifest)

    safe_error = exc_info.value
    assert type(safe_error) is expected_type
    assert safe_error is not source_error
    assert safe_error.error_code is expected_code
    assert safe_error.op == "materialize"
    assert safe_error.retryable is expected_retryable
    assert safe_error.context == {}
    assert safe_error.cause is None
    assert safe_error.__cause__ is None
    assert safe_error.__context__ is None
    assert sentinel not in repr(safe_error)
    assert cast(Any, BaseException.args).__get__(source_error, type(source_error)) == ()
    assert cast(Any, BaseException.__traceback__).__get__(source_error, type(source_error)) is None
    assert child_error.args == ()
    assert child_error.__traceback__ is None


@pytest.mark.parametrize("boundary", ["async", "sync"])
@pytest.mark.parametrize("invalid_state", ["missing_retryable", "invalid_op"])
@pytest.mark.asyncio
async def test_protected_malformed_mount_config_error_falls_back(
    boundary: str,
    invalid_state: str,
) -> None:
    sentinel = f"{boundary}-{invalid_state}-mount-config-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )
    source_error = MountConfigError(message=sentinel)
    if invalid_state == "missing_retryable":
        del source_error.retryable
    else:
        cast(Any, source_error).op = "invalid"

    if boundary == "async":

        @redact_mount_error_data
        async def fail(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            await fail(manifest=manifest)
    else:

        @redact_mount_error_data_sync
        def fail_sync(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            fail_sync(manifest=manifest)

    assert type(exc_info.value) is RuntimeError
    assert sentinel not in repr(exc_info.value)
    assert cast(Any, BaseException.args).__get__(source_error, type(source_error)) == ()


@pytest.mark.parametrize("boundary", ["async", "sync"])
@pytest.mark.asyncio
async def test_protected_mount_config_error_preserves_valid_structured_fields(
    boundary: str,
) -> None:
    sentinel = f"{boundary}-mount-config-structured-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )
    source_error = MountConfigError(message=sentinel)
    source_error.error_code = ErrorCode.EXEC_TIMEOUT
    source_error.op = "exec"
    source_error.retryable = True

    if boundary == "async":

        @redact_mount_error_data
        async def fail(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(MountConfigError) as exc_info:
            await fail(manifest=manifest)
    else:

        @redact_mount_error_data_sync
        def fail_sync(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(MountConfigError) as exc_info:
            fail_sync(manifest=manifest)

    safe_error = exc_info.value
    assert safe_error is not source_error
    assert safe_error.error_code is ErrorCode.EXEC_TIMEOUT
    assert safe_error.op == "exec"
    assert safe_error.retryable is True
    assert sentinel not in repr(safe_error)
    assert source_error.args == ()
    assert source_error.__traceback__ is None


@pytest.mark.parametrize("boundary", ["async", "sync"])
@pytest.mark.asyncio
async def test_untrusted_nested_authority_owner_fails_closed_without_descriptor_access(
    boundary: str,
) -> None:
    sentinel = f"{boundary}-untrusted-owner-descriptor-secret"

    class OpaqueOwner:
        descriptor_accessed = False

        @property
        def __dict__(self) -> dict[str, object]:  # type: ignore[override]
            type(self).descriptor_accessed = True
            raise AssertionError("untrusted owner descriptor was accessed")

    client = _SecurityTestClient()
    cast(Any, client).state = OpaqueOwner()
    source_error = RuntimeError(sentinel)

    if boundary == "async":

        @redact_mount_error_data
        async def fail(*, client: _SecurityTestClient) -> None:
            _ = client
            raise source_error

        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            await fail(client=client)
    else:

        @redact_mount_error_data_sync
        def fail_sync(*, client: _SecurityTestClient) -> None:
            _ = client
            raise source_error

        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            fail_sync(client=client)

    assert exc_info.value is not source_error
    assert OpaqueOwner.descriptor_accessed is False
    assert sentinel not in repr(exc_info.value)
    assert source_error.args == ()


@pytest.mark.parametrize(
    ("error_kind", "expected_state"),
    [
        ("transport", {"command": ()}),
        (
            "nonzero",
            {"command": (), "exit_code": 1, "stdout": b"", "stderr": b""},
        ),
        ("timeout", {"command": (), "timeout_s": None}),
        ("pty", {"session_id": -1}),
    ],
)
def test_protected_structured_sandbox_error_preserves_safe_subtype_state(
    error_kind: str,
    expected_state: dict[str, object],
) -> None:
    sentinel = f"{error_kind}-structured-sandbox-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )
    if error_kind == "transport":
        source_error: SandboxError = ExecTransportError(
            command=(sentinel,),
            message=sentinel,
            retryable=True,
        )
    elif error_kind == "nonzero":
        source_error = ExecNonZeroError(
            ExecResult(stdout=sentinel.encode(), stderr=sentinel.encode(), exit_code=42),
            command=(sentinel,),
        )
    elif error_kind == "timeout":
        source_error = ExecTimeoutError(command=(sentinel,), timeout_s=42.0)
    else:
        source_error = PtySessionNotFoundError(session_id=42, context={"secret": sentinel})

    @redact_mount_error_data_sync
    def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise source_error

    with pytest.raises(type(source_error)) as exc_info:
        fail(manifest=manifest)

    safe_error = exc_info.value
    assert type(safe_error) is type(source_error)
    assert safe_error is not source_error
    for field_name, field_value in expected_state.items():
        assert getattr(safe_error, field_name) == field_value
    assert sentinel not in repr(safe_error)


@pytest.mark.parametrize("boundary", ["async", "sync"])
@pytest.mark.parametrize("retryable_present", [False, True])
@pytest.mark.asyncio
async def test_protected_structured_sandbox_error_requires_retryable_field(
    boundary: str,
    retryable_present: bool,
) -> None:
    sentinel = f"{boundary}-{retryable_present}-missing-retryable-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )
    source_error = SandboxError(
        message=sentinel,
        error_code=ErrorCode.EXEC_TRANSPORT_ERROR,
        op="exec",
        context={"secret": sentinel},
        retryable=None,
    )
    if not retryable_present:
        del source_error.retryable
    expected_type: type[BaseException] = SandboxError if retryable_present else RuntimeError

    if boundary == "async":

        @redact_mount_error_data
        async def fail(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(expected_type) as exc_info:
            await fail(manifest=manifest)
    else:

        @redact_mount_error_data_sync
        def fail_sync(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(expected_type) as exc_info:
            fail_sync(manifest=manifest)

    safe_error = exc_info.value
    if retryable_present:
        assert type(safe_error) is SandboxError
        assert safe_error.retryable is None
    else:
        assert type(safe_error) is RuntimeError
    assert sentinel not in repr(safe_error)
    assert cast(Any, BaseException.args).__get__(source_error, type(source_error)) == ()


@pytest.mark.asyncio
async def test_protected_custom_sandbox_error_falls_back_without_source_state() -> None:
    sentinel = "custom-sandbox-error-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    class CustomSandboxError(SandboxError):
        pass

    source_error = CustomSandboxError(
        message=sentinel,
        error_code=ErrorCode.MOUNT_FAILED,
        op="materialize",
        context={"credential": sentinel},
        retryable=True,
    )

    @redact_mount_error_data
    async def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise source_error

    with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
        await fail(manifest=manifest)

    assert type(exc_info.value) is RuntimeError
    assert sentinel not in repr(exc_info.value)
    assert cast(Any, BaseException.args).__get__(source_error, type(source_error)) == ()
    assert cast(Any, BaseException.__traceback__).__get__(source_error, type(source_error)) is None


@pytest.mark.parametrize("boundary", ["async", "sync"])
@pytest.mark.asyncio
async def test_protected_hostile_exception_type_cannot_escape_redaction(boundary: str) -> None:
    sentinel = f"{boundary}-hostile-exception-type-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    class HostileMeta(type):
        def __hash__(cls) -> int:
            raise RuntimeError(sentinel)

    class ProviderError(Exception, metaclass=HostileMeta):
        pass

    source_error = ProviderError(sentinel)
    child_error = RuntimeError(sentinel)
    source_error.__cause__ = child_error

    if boundary == "async":

        @redact_mount_error_data
        async def fail(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            await fail(manifest=manifest)
    else:

        @redact_mount_error_data_sync
        def fail_sync(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            fail_sync(manifest=manifest)

    assert sentinel not in repr(exc_info.value)
    assert cast(Any, BaseException.args).__get__(source_error, type(source_error)) == ()
    assert cast(Any, BaseException.__traceback__).__get__(source_error, type(source_error)) is None
    assert child_error.args == ()
    assert child_error.__traceback__ is None


@pytest.mark.parametrize("boundary", ["async", "sync"])
@pytest.mark.asyncio
async def test_protected_hostile_exception_state_key_cannot_escape_redaction(
    boundary: str,
) -> None:
    sentinel = f"{boundary}-hostile-exception-state-key-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    class HostileKey:
        def __hash__(self) -> int:
            return hash("_agents_data_redacted")

        def __eq__(self, other: object) -> bool:
            _ = other
            raise RuntimeError(sentinel)

    source_error = RuntimeError(sentinel)
    cast(dict[object, object], source_error.__dict__)[HostileKey()] = sentinel

    if boundary == "async":

        @redact_mount_error_data
        async def fail(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            await fail(manifest=manifest)
    else:

        @redact_mount_error_data_sync
        def fail_sync(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            fail_sync(manifest=manifest)

    assert sentinel not in repr(exc_info.value)
    assert source_error.args == ()
    assert source_error.__dict__ == {}
    assert source_error.__traceback__ is None


@pytest.mark.parametrize("boundary", ["async", "sync"])
@pytest.mark.asyncio
async def test_protected_direct_manifest_precedes_opaque_owner_descriptors(
    boundary: str,
) -> None:
    sentinel = f"{boundary}-opaque-owner-descriptor-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    class OpaqueOwner:
        @property
        def state(self) -> object:
            raise KeyboardInterrupt(sentinel)

        @property
        def default_manifest(self) -> object:
            raise KeyboardInterrupt(sentinel)

        @property
        def _sandbox_config(self) -> object:
            raise KeyboardInterrupt(sentinel)

    source_error = RuntimeError(sentinel)

    if boundary == "async":

        @redact_mount_error_data
        async def fail(owner: object, manifest: Manifest) -> None:
            _ = (owner, manifest)
            raise source_error

        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            await fail(OpaqueOwner(), manifest)
    else:

        @redact_mount_error_data_sync
        def fail_sync(owner: object, manifest: Manifest) -> None:
            _ = (owner, manifest)
            raise source_error

        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            fail_sync(OpaqueOwner(), manifest)

    assert sentinel not in repr(exc_info.value)
    assert source_error.args == ()
    assert source_error.__traceback__ is None


def test_protected_mount_config_error_cannot_forge_safe_message_marker() -> None:
    sentinel = "forged-safe-mount-message-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )
    source_error = MountConfigError(message=sentinel)
    cast(Any, source_error)._agents_data_redacted = True
    cast(Any, source_error)._agents_safe_mount_validation_message = True

    @redact_mount_error_data_sync
    def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise source_error

    with pytest.raises(MountConfigError, match="sandbox mount configuration is invalid") as exc:
        fail(manifest=manifest)

    assert sentinel not in repr(exc.value)
    assert source_error.args == ()
    assert source_error.__traceback__ is None


@pytest.mark.asyncio
async def test_protected_exception_group_is_not_retained_by_safe_error() -> None:
    sentinel = "protected-exception-group-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )
    child_error = RuntimeError(sentinel)
    source_error = BaseExceptionGroup(sentinel, [child_error])

    @redact_mount_error_data
    async def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise source_error

    with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
        await fail(manifest=manifest)

    safe_error = exc_info.value
    assert safe_error.__cause__ is None
    assert safe_error.__context__ is None
    assert sentinel not in repr(safe_error)
    source_args = cast(Any, BaseException.args).__get__(source_error, type(source_error))
    assert source_args[0] == "Error details are redacted."
    assert child_error.args == ()
    assert child_error.__traceback__ is None
    traceback_cursor = safe_error.__traceback__
    while traceback_cursor is not None:
        frame_path = Path(traceback_cursor.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            assert sentinel not in repr(traceback_cursor.tb_frame.f_locals)
            assert source_error not in traceback_cursor.tb_frame.f_locals.values()
        traceback_cursor = traceback_cursor.tb_next


def test_protected_exception_group_children_are_collected_without_subclass_callbacks() -> None:
    sentinel = "protected-exception-group-callback-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    class HostileGroup(BaseExceptionGroup):
        callbacks = 0

        def __getattribute__(self, name: str) -> Any:
            if name == "_exceptions":
                type(self).callbacks += 1
                raise AssertionError("provider group state was accessed")
            return super().__getattribute__(name)

    child_error = RuntimeError(sentinel)
    source_error = HostileGroup(sentinel, [child_error])
    HostileGroup.callbacks = 0

    @redact_mount_error_data_sync
    def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise source_error

    with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
        fail(manifest=manifest)

    assert HostileGroup.callbacks == 0
    assert child_error.args == ()
    assert child_error.__traceback__ is None
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_protected_nested_exception_is_scrubbed_beside_hostile_object() -> None:
    sentinel = "protected-nested-exception-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    class HostileMeta(type):
        def __hash__(cls) -> int:
            raise RuntimeError(sentinel)

    class Opaque(metaclass=HostileMeta):
        pass

    child_error = RuntimeError(sentinel)
    source_error = RuntimeError([child_error, Opaque()])

    @redact_mount_error_data_sync
    def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise source_error

    with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
        fail(manifest=manifest)

    assert sentinel not in repr(exc_info.value)
    assert source_error.args == ()
    assert source_error.__traceback__ is None
    assert child_error.args == ()
    assert child_error.__traceback__ is None


@pytest.mark.parametrize(
    ("source_error", "expected_type", "expected_args"),
    [
        (SystemExit("protected-system-exit-secret"), SystemExit, (1,)),
        (GeneratorExit("protected-generator-exit-secret"), GeneratorExit, ()),
        (KeyboardInterrupt("protected-keyboard-interrupt-secret"), KeyboardInterrupt, ()),
    ],
)
def test_protected_process_control_is_replaced_without_payload(
    source_error: BaseException,
    expected_type: type[BaseException],
    expected_args: tuple[object, ...],
) -> None:
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key="protected-process-control-authority",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    @redact_mount_error_data_sync
    def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise source_error

    with pytest.raises(expected_type) as exc_info:
        fail(manifest=manifest)

    assert type(exc_info.value) is expected_type
    assert exc_info.value is not source_error
    assert exc_info.value.args == expected_args
    assert source_error.args == ()
    assert source_error.__traceback__ is None


def test_closing_protected_coroutine_preserves_generator_exit() -> None:
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key="protected-generator-exit-authority",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    @redact_mount_error_data
    async def suspend(*, manifest: Manifest) -> None:
        _ = manifest
        await asyncio.sleep(0)

    coroutine = suspend(manifest=manifest)
    assert coroutine.send(None) is None
    coroutine.close()
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.asyncio
async def test_slot_backed_session_state_preserves_mount_authority_classification(
    protected: bool,
) -> None:
    sentinel = f"slot-backed-session-secret-{protected}"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
        if protected
        else {}
    )
    source_error = RuntimeError(sentinel)

    class SlotBackedSession(BaseSandboxSession):
        __slots__ = ("state",)

        async def _ensure_backend_started(self) -> None:
            raise source_error

    class SlotBackedSessionState(SandboxSessionState):
        type: Literal["docker"] = "docker"

    SlotBackedSession.__abstractmethods__ = frozenset()
    session = cast(Any, SlotBackedSession)()
    session.state = SlotBackedSessionState(
        manifest=manifest,
        snapshot=NoopSnapshot(id="slot-backed-session"),
    )
    assert vars(session) == {}

    if protected:
        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            await session.start()

        assert exc_info.value is not source_error
        assert source_error.args == ()
        assert source_error.__traceback__ is None
    else:
        with pytest.raises(RuntimeError) as exc_info:
            await session.start()

        assert exc_info.value is source_error
        assert source_error.args == (sentinel,)


@pytest.mark.asyncio
async def test_property_backed_session_state_fails_closed_without_descriptor_access() -> None:
    sentinel = "property-backed-session-secret"
    source_error = RuntimeError(sentinel)

    class PropertyBackedSession(BaseSandboxSession):
        state_accessed = False

        @property
        def state(self) -> SandboxSessionState:
            type(self).state_accessed = True
            raise AssertionError("state property was accessed during classification")

        @state.setter
        def state(self, value: SandboxSessionState) -> None:
            _ = value
            type(self).state_accessed = True
            raise AssertionError("state property was accessed during classification")

        async def _ensure_backend_started(self) -> None:
            raise source_error

    PropertyBackedSession.__abstractmethods__ = frozenset()
    session = cast(Any, PropertyBackedSession)()

    @redact_mount_error_data
    async def fail(session: BaseSandboxSession) -> None:
        _ = session
        raise source_error

    with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
        await fail(session)

    assert exc_info.value is not source_error
    assert PropertyBackedSession.state_accessed is False
    assert source_error.args == ()
    assert source_error.__traceback__ is None


@pytest.mark.parametrize("boundary", ["async", "sync"])
@pytest.mark.parametrize(
    ("construction_code", "active_code", "expected_args"),
    [
        ("protected-stale-system-exit-secret", 0, (0,)),
        (0, "protected-active-system-exit-secret", (1,)),
    ],
)
@pytest.mark.asyncio
async def test_protected_system_exit_uses_active_safe_status(
    boundary: str,
    construction_code: object,
    active_code: str | int | None,
    expected_args: tuple[object, ...],
) -> None:
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key="protected-system-exit-authority",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )
    source_error = SystemExit(construction_code)
    source_error.code = active_code

    if boundary == "async":

        @redact_mount_error_data
        async def fail(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(SystemExit) as exc_info:
            await fail(manifest=manifest)
    else:

        @redact_mount_error_data_sync
        def fail_sync(*, manifest: Manifest) -> None:
            _ = manifest
            raise source_error

        with pytest.raises(SystemExit) as exc_info:
            fail_sync(manifest=manifest)

    assert type(exc_info.value) is SystemExit
    assert exc_info.value is not source_error
    assert exc_info.value.args == expected_args
    assert source_error.args == ()
    assert source_error.__traceback__ is None


def test_credentialless_structured_mount_error_is_unchanged() -> None:
    source_error = MountCommandError(
        command="credentialless-command",
        stderr="credentialless-stderr",
        retryable=True,
    )

    @redact_mount_error_data_sync
    def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise source_error

    with pytest.raises(MountCommandError) as exc_info:
        fail(manifest=Manifest())

    assert exc_info.value is source_error
    assert source_error.retryable is True
    assert source_error.context == {
        "command": "credentialless-command",
        "stderr": "credentialless-stderr",
    }


@pytest.mark.parametrize("boundary", ["async", "sync"])
@pytest.mark.parametrize("link_name", ["_client", "_inner", "session", "_session"])
@pytest.mark.asyncio
async def test_credentialless_external_client_ignores_opaque_provider_link(
    boundary: str,
    link_name: str,
) -> None:
    class ExternalClient(_SecurityTestClient):
        pass

    client = ExternalClient()
    setattr(client, link_name, object())
    source_error = MountCommandError(
        command="credentialless-command",
        stderr="credentialless-stderr",
        retryable=True,
    )

    if boundary == "async":

        @redact_mount_error_data
        async def fail(*, client: BaseSandboxClient[Any]) -> None:
            _ = client
            raise source_error

        with pytest.raises(MountCommandError) as exc_info:
            await fail(client=client)
    else:

        @redact_mount_error_data_sync
        def fail_sync(*, client: BaseSandboxClient[Any]) -> None:
            _ = client
            raise source_error

        with pytest.raises(MountCommandError) as exc_info:
            fail_sync(client=client)

    assert exc_info.value is source_error
    assert source_error.retryable is True
    assert source_error.context == {
        "command": "credentialless-command",
        "stderr": "credentialless-stderr",
    }


@pytest.mark.parametrize("boundary", ["async", "sync"])
@pytest.mark.parametrize("owner_kind", ["run_config", "external_client"])
@pytest.mark.asyncio
async def test_required_authority_carrier_dominates_aliased_optional_link(
    boundary: str,
    owner_kind: str,
) -> None:
    opaque = object()
    if owner_kind == "run_config":
        owner: object = SandboxRunConfig(
            session=cast(Any, opaque),
            session_state=cast(Any, opaque),
        )
    else:

        class ExternalClient(_SecurityTestClient):
            pass

        client = ExternalClient()
        cast(Any, client).state = opaque
        cast(Any, client)._inner = opaque
        owner = client

    source_error = RuntimeError("protected-aliased-authority-secret")

    if boundary == "async":

        @redact_mount_error_data
        async def fail(owner: object) -> None:
            _ = owner
            raise source_error

        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            await fail(owner)
    else:

        @redact_mount_error_data_sync
        def fail_sync(owner: object) -> None:
            _ = owner
            raise source_error

        with pytest.raises(RuntimeError, match="protected mount configuration") as exc_info:
            fail_sync(owner)

    assert exc_info.value is not source_error
    assert source_error.args == ()
    assert source_error.__traceback__ is None


@pytest.mark.asyncio
async def test_operation_error_with_mount_authority_is_replaced() -> None:
    sentinel = "provider-operation-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    class HostileProviderError(RuntimeError):
        pass

    _install_hostile_exception_descriptors(HostileProviderError)
    provider_error = HostileProviderError(f"provider failed with {sentinel}")

    @redact_mount_error_data
    async def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise provider_error

    with pytest.raises(RuntimeError, match="protected mount configuration") as exc:
        await fail(manifest=manifest)

    assert type(exc.value) is RuntimeError
    assert sentinel not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert cast(Any, BaseException.args).__get__(provider_error, type(provider_error)) == ()
    assert (
        cast(Any, BaseException.__traceback__).__get__(provider_error, type(provider_error)) is None
    )
    traceback = exc.value.__traceback__
    while traceback is not None:
        frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


@pytest.mark.parametrize(
    "mount",
    [
        AzureBlobMount(
            account="example",
            container="private",
            mount_strategy=InContainerMountStrategy(pattern=FuseMountPattern()),
        ),
        S3FilesMount(
            file_system_id="fs-123",
            mount_strategy=InContainerMountStrategy(pattern=S3FilesMountPattern()),
        ),
    ],
)
@pytest.mark.asyncio
async def test_operation_error_with_implicit_broad_authority_is_replaced(
    mount: Mount,
) -> None:
    sentinel = "implicit-broad-provider-secret"
    manifest = Manifest(
        entries={"data": mount}
    ).with_in_container_mount_broad_credential_exposure_acknowledged("data")
    provider_error = RuntimeError(sentinel)

    @redact_mount_error_data
    async def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise provider_error

    with pytest.raises(RuntimeError, match="protected mount configuration") as exc:
        await fail(manifest=manifest)

    assert sentinel not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert provider_error.args == ()
    assert provider_error.__traceback__ is None
    traceback = exc.value.__traceback__
    while traceback is not None:
        frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


def test_sync_operation_error_with_mount_authority_clears_source_arguments() -> None:
    sentinel = "sync-provider-operation-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    class HostileProviderError(RuntimeError):
        pass

    _install_hostile_exception_descriptors(HostileProviderError)
    provider_error = HostileProviderError(f"provider failed with {sentinel}")

    @redact_mount_error_data_sync
    def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise provider_error

    with pytest.raises(RuntimeError, match="protected mount configuration") as exc:
        fail(manifest=manifest)

    assert type(exc.value) is RuntimeError
    assert sentinel not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert cast(Any, BaseException.args).__get__(provider_error, type(provider_error)) == ()
    assert (
        cast(Any, BaseException.__traceback__).__get__(provider_error, type(provider_error)) is None
    )


@pytest.mark.parametrize(
    "mount",
    [
        AzureBlobMount(
            account="example",
            container="private",
            mount_strategy=InContainerMountStrategy(pattern=FuseMountPattern()),
        ),
        S3FilesMount(
            file_system_id="fs-123",
            mount_strategy=InContainerMountStrategy(pattern=S3FilesMountPattern()),
        ),
    ],
)
def test_sync_operation_error_with_implicit_broad_authority_is_replaced(
    mount: Mount,
) -> None:
    sentinel = "sync-implicit-broad-provider-secret"
    manifest = Manifest(
        entries={"data": mount}
    ).with_in_container_mount_broad_credential_exposure_acknowledged("data")
    provider_error = RuntimeError(sentinel)

    @redact_mount_error_data_sync
    def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise provider_error

    with pytest.raises(RuntimeError, match="protected mount configuration") as exc:
        fail(manifest=manifest)

    assert sentinel not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert provider_error.args == ()
    assert provider_error.__traceback__ is None


@pytest.mark.asyncio
async def test_operation_error_with_read_only_provider_attributes_is_replaced() -> None:
    sentinel = "read-only-provider-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    class ReadOnlyProviderError(Exception):
        @property
        def context(self) -> str:
            return sentinel

        @property
        def cause(self) -> str:
            return sentinel

    provider_error = ReadOnlyProviderError(sentinel)

    @redact_mount_error_data
    async def fail(*, manifest: Manifest) -> None:
        _ = manifest
        raise provider_error

    with pytest.raises(RuntimeError, match="protected mount configuration") as exc:
        await fail(manifest=manifest)

    assert sentinel not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert provider_error.__traceback__ is None
    traceback_cursor = exc.value.__traceback__
    while traceback_cursor is not None:
        frame_path = Path(traceback_cursor.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            assert sentinel not in repr(traceback_cursor.tb_frame.f_locals)
        traceback_cursor = traceback_cursor.tb_next


@pytest.mark.asyncio
async def test_cancellation_with_mount_authority_preserves_redacted_cancellation() -> None:
    sentinel = "cancelled-provider-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    class HostileCancelledError(asyncio.CancelledError):
        pass

    _install_hostile_exception_descriptors(HostileCancelledError)
    provider_error = HostileCancelledError(sentinel)

    @redact_mount_error_data
    async def cancel(*, manifest: Manifest) -> None:
        _ = manifest
        raise provider_error

    with pytest.raises(asyncio.CancelledError) as exc:
        await cancel(manifest=manifest)

    assert type(exc.value) is asyncio.CancelledError
    assert sentinel not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert cast(Any, BaseException.args).__get__(provider_error, type(provider_error)) == ()
    assert (
        cast(Any, BaseException.__traceback__).__get__(provider_error, type(provider_error)) is None
    )
    traceback = exc.value.__traceback__
    while traceback is not None:
        frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
        if "/src/agents/" in frame_path:
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


def test_generic_session_state_parser_sanitizes_legacy_mount_authority() -> None:
    sentinel = "legacy-session-state-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="access-key",
                secret_access_key=sentinel,
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )
    payload: dict[str, object] = {
        "type": "test",
        "manifest": manifest.model_dump(mode="json"),
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
    }

    restored = SandboxSessionState.parse(payload)

    assert restored.mount_authority_redacted is True
    mount = restored.manifest.entries["data"]
    assert isinstance(mount, S3Mount)
    assert mount.access_key_id is None
    assert mount.secret_access_key is None
    assert sentinel not in repr(restored)


def test_direct_session_state_round_trip_redacts_mount_authority() -> None:
    sentinel = "direct-state-secret"
    state = TestSessionState(
        manifest=Manifest(
            entries={
                "data": S3Mount(
                    bucket="bucket",
                    access_key_id="access-key",
                    secret_access_key=sentinel,
                    mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                )
            }
        ),
        snapshot=NoopSnapshot(id="snapshot"),
    )

    payload = state.model_dump_json()
    restored = TestSessionState.model_validate_json(payload)

    assert sentinel not in payload
    assert REDACTED_MOUNT_AUTHORITY_KEY in payload
    assert restored.mount_authority_redacted is True
    with pytest.raises(ValueError, match="requires a current trusted manifest"):
        restored.rebind_persisted_mount_authority(None, provider_backend_id="docker")


def test_raw_state_sanitization_clears_pattern_authority() -> None:
    manifest = Manifest(
        entries={
            "credentials.conf": File(content=b"credential-file-secret"),
            "rclone": _s3_mount(
                strategy=InContainerMountStrategy(
                    pattern=RcloneMountPattern(
                        extra_args=["--config", "/workspace/credentials.conf"]
                    )
                )
            ),
            "s3files": S3FilesMount(
                file_system_id="fs-123",
                mount_strategy=InContainerMountStrategy(
                    pattern=S3FilesMountPattern(
                        options=S3FilesMountPattern.S3FilesOptions(
                            extra_options={
                                "tlsport": "4049",
                                "secret_access_key": "pattern-secret",
                            }
                        )
                    )
                ),
            ),
        }
    )
    payload: dict[str, object] = {
        "type": "test",
        "manifest": manifest.model_dump(mode="json"),
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
    }

    sanitized, redacted = sanitize_raw_session_state_mount_authority(payload)

    assert redacted is True
    assert isinstance(sanitized, dict)
    entries = sanitized["manifest"]["entries"]
    assert entries["credentials.conf"]["content"] == ""
    assert entries["rclone"]["mount_strategy"]["pattern"]["extra_args"] == []
    assert entries["s3files"]["mount_strategy"]["pattern"]["options"]["extra_options"] == {}
    assert "credential-file-secret" not in repr(sanitized)
    assert "pattern-secret" not in repr(sanitized)


@pytest.mark.parametrize("location", ["strategy", "pattern"])
def test_raw_state_sanitization_rejects_unknown_nested_discriminators(
    location: str,
) -> None:
    sentinel = f"unknown-{location}-secret"
    manifest = Manifest(
        entries={
            "docker": S3Mount(
                bucket="bucket",
                mount_strategy=DockerVolumeMountStrategy(
                    driver="rclone",
                    driver_options={"password": "driver-secret"},
                ),
            ),
            "s3files": S3FilesMount(
                file_system_id="fs-123",
                mount_strategy=InContainerMountStrategy(
                    pattern=S3FilesMountPattern(
                        options=S3FilesMountPattern.S3FilesOptions(
                            extra_options={"password": "pattern-secret"}
                        )
                    )
                ),
            ),
        }
    ).model_dump(mode="json")
    if location == "strategy":
        manifest["entries"]["docker"]["mount_strategy"]["type"] = sentinel
    else:
        manifest["entries"]["s3files"]["mount_strategy"]["pattern"]["type"] = sentinel

    with pytest.raises(ValueError, match="unknown type") as exc_info:
        sanitize_raw_session_state_mount_authority({"type": "test", "manifest": manifest})

    assert sentinel not in str(exc_info.value)


def test_raw_state_sanitization_strips_opaque_fields_with_known_strategy_type() -> None:
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    ).model_dump(mode="json")
    manifest["entries"]["data"]["mount_strategy"]["api_token"] = "raw-strategy-secret"

    sanitized, redacted = sanitize_raw_session_state_mount_authority(
        {"type": "test", "manifest": manifest}
    )

    strategy = sanitized["manifest"]["entries"]["data"]["mount_strategy"]  # type: ignore[index]
    assert redacted is True
    assert "api_token" not in strategy
    assert "raw-strategy-secret" not in repr(sanitized)


def test_raw_state_sanitization_strips_opaque_nested_pattern_fields() -> None:
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    ).model_dump(mode="json")
    pattern = manifest["entries"]["data"]["mount_strategy"]["pattern"]
    pattern["api_token"] = "nested-pattern-secret"
    pattern["options"] = {"authorization": "nested-options-secret"}

    sanitized, redacted = sanitize_raw_session_state_mount_authority(
        {"type": "test", "manifest": manifest}
    )

    sanitized_pattern = sanitized["manifest"]["entries"]["data"]["mount_strategy"]["pattern"]  # type: ignore[index]
    assert redacted is True
    assert "api_token" not in sanitized_pattern
    assert "options" not in sanitized_pattern
    assert "nested-pattern-secret" not in repr(sanitized)
    assert "nested-options-secret" not in repr(sanitized)


def test_deserialization_sanitizes_input_before_validation_errors() -> None:
    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=DockerVolumeMountStrategy(driver="rclone"),
                credentialed=True,
            )
        }
    )
    payload: dict[str, Any] = {
        "type": "test",
        "session_id": "not-a-uuid",
        "manifest": manifest.model_dump(mode="json"),
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
    }

    with pytest.raises(ValueError):
        _SecurityTestClient().deserialize_session_state(payload)

    assert payload == {}


def test_deserialization_sanitizes_non_string_endpoint_before_validation_errors() -> None:
    sentinel = "raw-endpoint-secret"
    manifest = Manifest(
        entries={
            "data": _s3_mount(
                strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    ).model_dump(mode="json")
    manifest["entries"]["data"]["endpoint_url"] = {"credential": sentinel}
    payload: dict[str, Any] = {
        "type": "test",
        "session_id": "not-a-uuid",
        "manifest": manifest,
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
    }

    with pytest.raises(ValueError) as exc:
        _SecurityTestClient().deserialize_session_state(payload)

    assert payload == {}
    assert sentinel not in str(exc.value)


def test_serialization_failure_redacts_mount_authority_from_sdk_traceback_frames() -> None:
    sentinel = "typed-serialization-secret"
    state = TestSessionState(
        snapshot=NoopSnapshot(id="snapshot"),
        manifest=Manifest(
            entries={
                "data": S3Mount(
                    bucket="example-bucket",
                    access_key_id="access-key",
                    secret_access_key=sentinel,
                    mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                )
            }
        ),
    )
    state.snapshot = cast(Any, object())

    with pytest.raises(MountConfigError) as exc:
        _SecurityTestClient().serialize_session_state(state)

    assert sentinel not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    traceback = exc.value.__traceback__
    while traceback is not None:
        module_name = traceback.tb_frame.f_globals.get("__name__", "")
        if isinstance(module_name, str) and module_name.startswith("agents."):
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


def test_direct_state_serialization_replaces_manifest_sanitizer_failure() -> None:
    sentinel = "direct-state-sanitizer-secret"
    state = TestSessionState(
        manifest=Manifest(
            entries={
                "credentials.json": LocalFile(src=Path("credentials.json")),
                "data": GCSMount(
                    bucket="bucket",
                    service_account_file="/workspace/credentials.json",
                    service_account_credentials=sentinel,
                    mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                ),
            }
        ),
        snapshot=NoopSnapshot(id="snapshot"),
    )

    with pytest.raises(Exception) as exc:
        state.model_dump(mode="json")

    assert sentinel not in str(exc.value)
    error: BaseException | None = exc.value
    while error is not None:
        traceback = error.__traceback__
        while traceback is not None:
            module_name = traceback.tb_frame.f_globals.get("__name__", "")
            if isinstance(module_name, str) and module_name.startswith("agents."):
                assert sentinel not in repr(traceback.tb_frame.f_locals)
            traceback = traceback.tb_next
        error = error.__cause__


def test_deserialization_scrubs_authority_before_invalid_strategy_discriminator() -> None:
    sentinel = "malformed-strategy-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="example-bucket",
                mount_strategy=DockerVolumeMountStrategy(
                    driver="rclone",
                    driver_options={"password": sentinel},
                ),
            )
        }
    ).model_dump(mode="json")
    manifest["entries"]["data"]["mount_strategy"]["type"] = {"invalid": "discriminator"}
    payload: dict[str, Any] = {
        "type": "test",
        "manifest": manifest,
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
    }

    with pytest.raises(ValueError) as exc:
        _SecurityTestClient().deserialize_session_state(payload)

    assert payload == {}
    assert sentinel not in str(exc.value)
    traceback = exc.value.__traceback__
    while traceback is not None:
        module_name = traceback.tb_frame.f_globals.get("__name__", "")
        if isinstance(module_name, str) and module_name.startswith("agents."):
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


@pytest.mark.parametrize("location", ["strategy", "pattern"])
def test_deserialization_rejects_unknown_string_discriminators_without_values(
    location: str,
) -> None:
    sentinel = f"unknown-{location}-secret"
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="example-bucket",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    ).model_dump(mode="json")
    strategy = manifest["entries"]["data"]["mount_strategy"]
    if location == "strategy":
        strategy["type"] = sentinel
    else:
        strategy["pattern"]["type"] = sentinel
    payload: dict[str, Any] = {
        "type": "test",
        "manifest": manifest,
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
    }

    with pytest.raises(ValueError, match="payload is invalid") as exc:
        _SecurityTestClient().deserialize_session_state(payload)

    assert payload == {}
    assert sentinel not in str(exc.value)
    traceback = exc.value.__traceback__
    while traceback is not None:
        module_name = traceback.tb_frame.f_globals.get("__name__", "")
        if isinstance(module_name, str) and module_name.startswith("agents."):
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


def test_deserialization_rejects_malformed_entry_container_without_values() -> None:
    sentinel = "malformed-entry-container-secret"
    payload: dict[str, Any] = {
        "type": "test",
        "manifest": {
            "version": 1,
            "root": "/workspace",
            "entries": [sentinel],
            "environment": {"value": {}},
        },
        "snapshot": NoopSnapshot(id="snapshot").model_dump(mode="json"),
    }

    with pytest.raises(ValueError, match="payload is invalid") as exc:
        _SecurityTestClient().deserialize_session_state(payload)

    assert payload == {}
    assert sentinel not in str(exc.value)
    traceback = exc.value.__traceback__
    while traceback is not None:
        module_name = traceback.tb_frame.f_globals.get("__name__", "")
        if isinstance(module_name, str) and module_name.startswith("agents."):
            assert sentinel not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
