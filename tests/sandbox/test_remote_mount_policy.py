from pathlib import Path

from agents.sandbox.entries import BaseEntry, DockerVolumeMountStrategy, S3Mount
from agents.sandbox.manifest import Manifest
from agents.sandbox.remote_mount_policy import build_remote_mount_policy_instructions


def _s3_mount(*, read_only: bool) -> S3Mount:
    return S3Mount(
        bucket="example-bucket",
        mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
        read_only=read_only,
    )


def _policy_for(entries: dict[str | Path, BaseEntry]) -> str:
    policy = build_remote_mount_policy_instructions(Manifest(entries=entries))
    assert policy is not None
    return policy


def test_remote_mount_policy_does_not_suggest_direct_edits_for_read_only_mounts() -> None:
    policy = _policy_for({"data": _s3_mount(read_only=True)})

    assert "/workspace/data (mounted in read-only mode)" in policy
    assert "`apply_patch` directly" not in policy
    assert "copy it back" not in policy
    assert "Do not edit paths marked read-only in place" in policy
    assert "including with `apply_patch`" in policy
    assert "do not write edited files back" in policy


def test_remote_mount_policy_keeps_direct_and_copy_back_guidance_for_read_write_mounts() -> None:
    policy = _policy_for({"data": _s3_mount(read_only=False)})

    assert "/workspace/data (mounted in read+write mode)" in policy
    assert "Use `apply_patch` directly for text edits on read+write mounts." in policy
    assert "For shell-based edits on read+write mounts" in policy
    assert "copy it back" in policy
    assert "Do not edit paths marked read-only" not in policy


def test_remote_mount_policy_handles_mixed_read_only_and_read_write_mounts() -> None:
    policy = _policy_for(
        {
            "input": _s3_mount(read_only=True),
            "output": _s3_mount(read_only=False),
        }
    )

    assert "/workspace/input (mounted in read-only mode)" in policy
    assert "/workspace/output (mounted in read+write mode)" in policy
    assert "Use `apply_patch` directly for text edits on read+write mounts." in policy
    assert "For shell-based edits on read+write mounts" in policy
    assert "Do not edit paths marked read-only in place" in policy
    assert "including with `apply_patch`" in policy
    assert "do not write edited files back" in policy
