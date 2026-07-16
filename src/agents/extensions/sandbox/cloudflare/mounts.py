from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ....sandbox.entries import GCSMount, Mount, R2Mount, S3Mount
from ....sandbox.entries.mounts.base import MountStrategyBase
from ....sandbox.errors import MountConfigError
from ....sandbox.materialization import MaterializedFile
from ....sandbox.session.base_sandbox_session import BaseSandboxSession

CloudflareBucketProvider = Literal["r2", "s3", "gcs"]


@dataclass(frozen=True)
class CloudflareBucketMountConfig:
    """Backend-neutral config for Cloudflare bucket mounts."""

    bucket_name: str
    bucket_endpoint_url: str
    provider: CloudflareBucketProvider
    key_prefix: str | None = None
    credentials: dict[str, str] | None = None
    read_only: bool = True

    def to_request_options(self) -> dict[str, object]:
        options: dict[str, object] = {
            "endpoint": self.bucket_endpoint_url,
            "readOnly": self.read_only,
        }
        if self.key_prefix is not None:
            options["prefix"] = self.key_prefix
        if self.credentials is not None:
            options["credentials"] = {
                "accessKeyId": self.credentials["access_key_id"],
                "secretAccessKey": self.credentials["secret_access_key"],
            }
        return options


class CloudflareBucketMountStrategy(MountStrategyBase):
    type: Literal["cloudflare_bucket_mount"] = "cloudflare_bucket_mount"

    def validate_mount(self, mount: Mount) -> None:
        _ = self._build_cloudflare_bucket_mount_config(mount)

    async def activate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        if type(session).__name__ != "CloudflareSandboxSession":
            raise MountConfigError(
                message="cloudflare bucket mounts are not supported by this sandbox backend",
                context={"mount_type": mount.type, "session_type": type(session).__name__},
            )
        _ = base_dir
        mount_path = mount._resolve_mount_path(session, dest)
        config = self._build_cloudflare_bucket_mount_config(mount)
        await session.mount_bucket(  # type: ignore[attr-defined]
            bucket=config.bucket_name,
            mount_path=mount_path,
            options=config.to_request_options(),
        )
        return []

    async def deactivate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        if type(session).__name__ != "CloudflareSandboxSession":
            raise MountConfigError(
                message="cloudflare bucket mounts are not supported by this sandbox backend",
                context={"mount_type": mount.type, "session_type": type(session).__name__},
            )
        _ = base_dir
        await session.unmount_bucket(mount._resolve_mount_path(session, dest))  # type: ignore[attr-defined]

    async def teardown_for_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        if type(session).__name__ != "CloudflareSandboxSession":
            raise MountConfigError(
                message="cloudflare bucket mounts are not supported by this sandbox backend",
                context={"mount_type": mount.type, "session_type": type(session).__name__},
            )
        _ = mount
        await session.unmount_bucket(path)  # type: ignore[attr-defined]

    async def restore_after_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        if type(session).__name__ != "CloudflareSandboxSession":
            raise MountConfigError(
                message="cloudflare bucket mounts are not supported by this sandbox backend",
                context={"mount_type": mount.type, "session_type": type(session).__name__},
            )
        config = self._build_cloudflare_bucket_mount_config(mount)
        await session.mount_bucket(  # type: ignore[attr-defined]
            bucket=config.bucket_name,
            mount_path=path,
            options=config.to_request_options(),
        )

    def build_docker_volume_driver_config(
        self,
        mount: Mount,
    ) -> tuple[str, dict[str, str], bool] | None:
        _ = mount
        return None

    def _build_cloudflare_bucket_mount_config(
        self,
        mount: Mount,
    ) -> CloudflareBucketMountConfig:
        if isinstance(mount, S3Mount):
            self._validate_credentials(
                access_key_id=mount.access_key_id,
                secret_access_key=mount.secret_access_key,
                mount_type=mount.type,
            )
            if mount.session_token is not None:
                raise MountConfigError(
                    message=(
                        "cloudflare bucket mounts do not support s3 session_token credentials"
                    ),
                    context={"type": mount.type},
                )
            return CloudflareBucketMountConfig(
                bucket_name=mount.bucket,
                bucket_endpoint_url=(
                    mount.endpoint_url
                    or (
                        f"https://s3.{mount.region}.amazonaws.com"
                        if mount.region is not None
                        else "https://s3.amazonaws.com"
                    )
                ),
                provider="s3",
                key_prefix=self._normalize_prefix(mount.prefix),
                credentials=self._build_credentials(
                    access_key_id=mount.access_key_id,
                    secret_access_key=mount.secret_access_key,
                ),
                read_only=mount.read_only,
            )

        if isinstance(mount, R2Mount):
            mount._validate_credential_pair()
            return CloudflareBucketMountConfig(
                bucket_name=mount.bucket,
                bucket_endpoint_url=(
                    mount.custom_domain or f"https://{mount.account_id}.r2.cloudflarestorage.com"
                ),
                provider="r2",
                credentials=self._build_credentials(
                    access_key_id=mount.access_key_id,
                    secret_access_key=mount.secret_access_key,
                ),
                read_only=mount.read_only,
            )

        if isinstance(mount, GCSMount):
            if not mount._use_s3_compatible_rclone():
                raise MountConfigError(
                    message=(
                        "gcs cloudflare bucket mounts require access_id and secret_access_key"
                    ),
                    context={"type": mount.type},
                )
            assert mount.access_id is not None
            assert mount.secret_access_key is not None
            return CloudflareBucketMountConfig(
                bucket_name=mount.bucket,
                bucket_endpoint_url=mount.endpoint_url or "https://storage.googleapis.com",
                provider="gcs",
                key_prefix=self._normalize_prefix(mount.prefix),
                credentials=self._build_credentials(
                    access_key_id=mount.access_id,
                    secret_access_key=mount.secret_access_key,
                ),
                read_only=mount.read_only,
            )

        raise MountConfigError(
            message="cloudflare bucket mounts are not supported for this mount type",
            context={"mount_type": mount.type},
        )

    @staticmethod
    def _normalize_prefix(prefix: str | None) -> str | None:
        if prefix is None:
            return None
        trimmed = prefix.strip("/")
        if trimmed == "":
            return "/"
        return f"/{trimmed}/"

    @staticmethod
    def _validate_credentials(
        *,
        access_key_id: str | None,
        secret_access_key: str | None,
        mount_type: str,
    ) -> None:
        if (access_key_id is None) != (secret_access_key is None):
            raise MountConfigError(
                message=(
                    "cloudflare bucket mounts require both access_key_id and "
                    "secret_access_key when either is provided"
                ),
                context={"type": mount_type},
            )

    @classmethod
    def _build_credentials(
        cls,
        *,
        access_key_id: str | None,
        secret_access_key: str | None,
    ) -> dict[str, str] | None:
        cls._validate_credentials(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            mount_type="cloudflare_bucket_mount",
        )
        if access_key_id is None or secret_access_key is None:
            return None
        return {
            "access_key_id": access_key_id,
            "secret_access_key": secret_access_key,
        }
