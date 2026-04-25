from pathlib import Path

import pytest

from agents.sandbox.entries import (
    Dir,
    File,
    GCSMount,
    InContainerMountStrategy,
    MountpointMountPattern,
)
from agents.sandbox.errors import InvalidManifestPathError
from agents.sandbox.manifest import Manifest
from agents.sandbox.manifest_render import _truncate_manifest_description


def test_manifest_rejects_nested_child_paths_that_escape_workspace() -> None:
    manifest = Manifest(
        entries={
            "safe": Dir(
                children={
                    "../outside.txt": File(content=b"nope"),
                }
            )
        }
    )

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        manifest.validated_entries()


def test_manifest_rejects_nested_absolute_child_paths() -> None:
    manifest = Manifest(
        entries={
            "safe": Dir(
                children={
                    "/tmp/outside.txt": File(content=b"nope"),
                }
            )
        }
    )

    with pytest.raises(InvalidManifestPathError, match="must be relative"):
        manifest.validated_entries()


def test_manifest_rejects_windows_drive_absolute_entry_paths() -> None:
    manifest = Manifest(entries={"C:\\tmp\\outside.txt": File(content=b"nope")})

    with pytest.raises(InvalidManifestPathError) as exc_info:
        manifest.validated_entries()

    assert str(exc_info.value) == "manifest path must be relative: C:/tmp/outside.txt"
    assert exc_info.value.context == {"rel": "C:/tmp/outside.txt", "reason": "absolute"}


def test_manifest_ephemeral_entry_paths_include_nested_children() -> None:
    manifest = Manifest(
        entries={
            "dir": Dir(
                children={
                    "keep.txt": File(content=b"keep"),
                    "tmp.txt": File(content=b"tmp", ephemeral=True),
                }
            )
        }
    )

    assert manifest.ephemeral_entry_paths() == {Path("dir/tmp.txt")}


def test_manifest_ephemeral_persistence_paths_include_resolved_mount_targets() -> None:
    manifest = Manifest(
        root="/workspace",
        entries={
            "logical": GCSMount(
                bucket="bucket",
                mount_path=Path("actual"),
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
            "dir": Dir(
                children={
                    "tmp.txt": File(content=b"tmp", ephemeral=True),
                }
            ),
        },
    )

    assert manifest.ephemeral_persistence_paths() == {
        Path("logical"),
        Path("actual"),
        Path("dir/tmp.txt"),
    }


def test_manifest_ephemeral_mount_targets_sort_by_resolved_depth() -> None:
    parent = GCSMount(
        bucket="parent",
        mount_path=Path("repo"),
        mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
    )
    child = GCSMount(
        bucket="child",
        mount_path=Path("repo/sub"),
        mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
    )
    manifest = Manifest(
        root="/workspace",
        entries={
            "parent": parent,
            "nested": Dir(children={"child": child}),
        },
    )

    assert manifest.ephemeral_mount_targets() == [
        (child, Path("/workspace/repo/sub")),
        (parent, Path("/workspace/repo")),
    ]


def test_manifest_ephemeral_mount_targets_normalize_non_escaping_mount_paths() -> None:
    mount = GCSMount(
        bucket="bucket",
        mount_path=Path("/workspace/repo/../actual"),
        mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
    )
    manifest = Manifest(root="/workspace", entries={"logical": mount})

    assert manifest.ephemeral_mount_targets() == [
        (mount, Path("/workspace/actual")),
    ]
    assert manifest.ephemeral_persistence_paths() == {
        Path("logical"),
        Path("actual"),
    }


def test_manifest_ephemeral_mount_targets_reject_escaping_mount_paths() -> None:
    manifest = Manifest(
        root="/workspace",
        entries={
            "logical": GCSMount(
                bucket="bucket",
                mount_path=Path("/workspace/../../tmp"),
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
        },
    )

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        manifest.ephemeral_mount_targets()

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        manifest.ephemeral_persistence_paths()


def test_manifest_ephemeral_mount_targets_reject_windows_drive_mount_path() -> None:
    manifest = Manifest(
        root="/workspace",
        entries={
            "logical": GCSMount(
                bucket="bucket",
                mount_path=Path("C:\\tmp\\mount"),
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
        },
    )

    with pytest.raises(InvalidManifestPathError) as exc_info:
        manifest.ephemeral_mount_targets()

    assert str(exc_info.value) == "manifest path must be relative: C:/tmp/mount"
    assert exc_info.value.context == {"rel": "C:/tmp/mount", "reason": "absolute"}


def test_manifest_describe_preserves_tree_rendering_after_renderer_extract() -> None:
    manifest = Manifest(
        root="/workspace",
        entries={
            "repo": Dir(
                description="project root",
                children={
                    "README.md": File(content=b"hi", description="overview"),
                },
            ),
            "data": GCSMount(
                bucket="bucket",
                description="shared data",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
        },
    )

    description = manifest.describe(depth=2)

    assert description.startswith("/workspace\n")
    assert "data/" in description
    assert "/workspace/data" in description
    assert "repo/" in description
    assert "/workspace/repo/README.md" in description


def test_manifest_description_truncation_respects_short_limits() -> None:
    description = "0123456789" * 20

    for max_chars in range(0, 40):
        truncated = _truncate_manifest_description(description, max_chars)
        assert len(truncated) <= max_chars


def test_manifest_description_truncation_preserves_unbounded_description() -> None:
    description = "short"

    assert _truncate_manifest_description(description, None) == description
