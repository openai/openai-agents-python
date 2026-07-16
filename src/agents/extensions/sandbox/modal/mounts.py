from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ....sandbox.entries import GCSMount, Mount, R2Mount, S3Mount
from ....sandbox.entries.mounts.base import MountStrategyBase
from ....sandbox.errors import MountConfigError
from ....sandbox.materialization import MaterializedFile
from ....sandbox.session.base_sandbox_session import BaseSandboxSession


@dataclass(frozen=True)
class ModalCloudBucketMountConfig:
    """Backend-neutral config for Modal's native cloud bucket mounts."""

    bucket_name: str
    bucket_endpoint_url: str | None = None
    key_prefix: str | None = None
    credentials: dict[str, str] | None = None
    secret_name: str | None = None
    secret_environment_name: str | None = None
    read_only: bool = True


class ModalCloudBucketMountStrategy(MountStrategyBase):
    type: Literal["modal_cloud_bucket"] = "modal_cloud_bucket"
    secret_name: str | None = None
    secret_environment_name: str | None = None

    def validate_mount(self, mount: Mount) -> None:
        _ = self._build_modal_cloud_bucket_mount_config(mount)

    def supports_native_snapshot_detach(self, mount: Mount) -> bool:
        _ = mount
        return False

    async def activate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        if type(session).__name__ != "ModalSandboxSession":
            raise MountConfigError(
                message="modal cloud bucket mounts are not supported by this sandbox backend",
                context={"mount_type": mount.type, "session_type": type(session).__name__},
            )
        _ = (mount, session, dest, base_dir)
        return []

    async def deactivate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        if type(session).__name__ != "ModalSandboxSession":
            raise MountConfigError(
                message="modal cloud bucket mounts are not supported by this sandbox backend",
                context={"mount_type": mount.type, "session_type": type(session).__name__},
            )
        _ = (mount, session, dest, base_dir)
        return None

    async def teardown_for_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _ = (mount, session, path)
        return None

    async def restore_after_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _ = (mount, session, path)
        return None

    def build_docker_volume_driver_config(
        self,
        mount: Mount,
    ) -> tuple[str, dict[str, str], bool] | None:
        _ = mount
        return None

    def _build_modal_cloud_bucket_mount_config(
        self,
        mount: Mount,
    ) -> ModalCloudBucketMountConfig:
        if self.secret_name is not None and self.secret_name == "":
            raise MountConfigError(
                message="modal cloud bucket secret_name must be a non-empty string",
                context={"mount_type": mount.type},
            )
        if self.secret_environment_name is not None and self.secret_environment_name == "":
            raise MountConfigError(
                message="modal cloud bucket secret_environment_name must be a non-empty string",
                context={"mount_type": mount.type},
            )
        if self.secret_environment_name is not None and self.secret_name is None:
            raise MountConfigError(
                message=(
                    "modal cloud bucket secret_environment_name requires secret_name to also be set"
                ),
                context={"mount_type": mount.type},
            )

        if isinstance(mount, S3Mount):
            s3_credentials: dict[str, str] = {}
            if mount.access_key_id is not None:
                s3_credentials["AWS_ACCESS_KEY_ID"] = mount.access_key_id
            if mount.secret_access_key is not None:
                s3_credentials["AWS_SECRET_ACCESS_KEY"] = mount.secret_access_key
            if mount.session_token is not None:
                s3_credentials["AWS_SESSION_TOKEN"] = mount.session_token
            if self.secret_name is not None and s3_credentials:
                raise MountConfigError(
                    message=(
                        "modal cloud bucket mounts do not support both inline credentials "
                        "and secret_name"
                    ),
                    context={"mount_type": mount.type},
                )
            return ModalCloudBucketMountConfig(
                bucket_name=mount.bucket,
                bucket_endpoint_url=mount.endpoint_url,
                key_prefix=mount.prefix,
                credentials=s3_credentials or None,
                secret_name=self.secret_name,
                secret_environment_name=self.secret_environment_name,
                read_only=mount.read_only,
            )

        if isinstance(mount, R2Mount):
            mount._validate_credential_pair()
            r2_credentials: dict[str, str] = {}
            if mount.access_key_id is not None:
                r2_credentials["AWS_ACCESS_KEY_ID"] = mount.access_key_id
            if mount.secret_access_key is not None:
                r2_credentials["AWS_SECRET_ACCESS_KEY"] = mount.secret_access_key
            if self.secret_name is not None and r2_credentials:
                raise MountConfigError(
                    message=(
                        "modal cloud bucket mounts do not support both inline credentials "
                        "and secret_name"
                    ),
                    context={"mount_type": mount.type},
                )
            return ModalCloudBucketMountConfig(
                bucket_name=mount.bucket,
                bucket_endpoint_url=(
                    mount.custom_domain or f"https://{mount.account_id}.r2.cloudflarestorage.com"
                ),
                credentials=r2_credentials or None,
                secret_name=self.secret_name,
                secret_environment_name=self.secret_environment_name,
                read_only=mount.read_only,
            )

        if isinstance(mount, GCSMount):
            if not mount._use_s3_compatible_rclone() and self.secret_name is None:
                raise MountConfigError(
                    message=(
                        "gcs modal cloud bucket mounts require access_id and secret_access_key"
                    ),
                    context={"type": mount.type},
                )
            gcs_credentials: dict[str, str] | None = None
            if mount._use_s3_compatible_rclone():
                assert mount.access_id is not None
                assert mount.secret_access_key is not None
                gcs_credentials = {
                    "GOOGLE_ACCESS_KEY_ID": mount.access_id,
                    "GOOGLE_ACCESS_KEY_SECRET": mount.secret_access_key,
                }
            if self.secret_name is not None and gcs_credentials is not None:
                raise MountConfigError(
                    message=(
                        "modal cloud bucket mounts do not support both inline credentials "
                        "and secret_name"
                    ),
                    context={"mount_type": mount.type},
                )
            return ModalCloudBucketMountConfig(
                bucket_name=mount.bucket,
                bucket_endpoint_url=mount.endpoint_url or "https://storage.googleapis.com",
                key_prefix=mount.prefix,
                credentials=gcs_credentials,
                secret_name=self.secret_name,
                secret_environment_name=self.secret_environment_name,
                read_only=mount.read_only,
            )

        raise MountConfigError(
            message="modal cloud bucket mounts are not supported for this mount type",
            context={"mount_type": mount.type},
        )
