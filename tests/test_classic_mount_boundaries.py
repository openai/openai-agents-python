"""Relocated SDK mount types retain their closed trust boundary."""

from typing import Literal

import pytest

from agents.classic.extensions.sandbox.modal.mounts import ModalCloudBucketMountStrategy
from agents.classic.sandbox import Manifest
from agents.classic.sandbox._mount_security import (
    sanitize_manifest_mount_authority,
    validate_manifest_mount_credential_boundaries,
)
from agents.classic.sandbox.entries import DockerVolumeMountStrategy, S3Mount
from agents.classic.sandbox.errors import MountConfigError


def test_classic_builtin_and_extension_strategies_are_trusted() -> None:
    for strategy in (
        DockerVolumeMountStrategy(driver="rclone"),
        ModalCloudBucketMountStrategy(),
    ):
        manifest = Manifest(
            entries={"data": S3Mount(bucket="synthetic-bucket", mount_strategy=strategy)}
        )
        validate_manifest_mount_credential_boundaries(manifest)
        sanitized, _ = sanitize_manifest_mount_authority(manifest)
        assert sanitized.entries["data"].mount_strategy.type == strategy.type


def test_application_mount_subclass_cannot_claim_sdk_trust() -> None:
    class CustomS3Mount(S3Mount):
        type: Literal["classic_custom_s3_mount"] = "classic_custom_s3_mount"  # type: ignore[assignment]

    manifest = Manifest(
        entries={
            "data": CustomS3Mount(
                bucket="synthetic-bucket",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )
    with pytest.raises(MountConfigError, match="custom mount implementations"):
        sanitize_manifest_mount_authority(manifest)
