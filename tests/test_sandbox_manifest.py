from pathlib import Path

import pytest

from agents.sandbox.codex_config import (
    DEFAULT_CODEX_VERSION,
    CodexConfig,
    apply_codex_to_manifest,
    manifest_has_codex_entry,
)
from agents.sandbox.entries import Codex, Dir, File, GCSMount
from agents.sandbox.errors import InvalidManifestPathError
from agents.sandbox.manifest import Manifest


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
            "logical": GCSMount(bucket="bucket", mount_path=Path("actual")),
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
    parent = GCSMount(bucket="parent", mount_path=Path("repo"))
    child = GCSMount(bucket="child", mount_path=Path("repo/sub"))
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
    mount = GCSMount(bucket="bucket", mount_path=Path("/workspace/repo/../actual"))
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
            "logical": GCSMount(bucket="bucket", mount_path=Path("/workspace/../../tmp")),
        },
    )

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        manifest.ephemeral_mount_targets()

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        manifest.ephemeral_persistence_paths()


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
            "data": GCSMount(bucket="bucket", description="shared data"),
        },
    )

    description = manifest.describe(depth=2)

    assert description.startswith("/workspace\n")
    assert "data/" in description
    assert "/workspace/data" in description
    assert "repo/" in description
    assert "/workspace/repo/README.md" in description


def test_apply_codex_to_manifest_adds_codex_entry_at_configured_path() -> None:
    manifest = apply_codex_to_manifest(
        Manifest(),
        CodexConfig(path="tools/codex"),
    )

    validated = manifest.validated_entries()

    assert Path("tools/codex") in validated
    entry = validated[Path("tools/codex")]
    assert isinstance(entry, Codex)
    assert entry.version == DEFAULT_CODEX_VERSION
    assert entry.ephemeral is True


def test_apply_codex_to_manifest_uses_reserved_default_codex_path() -> None:
    manifest = apply_codex_to_manifest(Manifest(), True)

    validated = manifest.validated_entries()

    assert Path(".codex_bin/codex") in validated
    assert manifest.ephemeral_persistence_paths() == {Path(".codex_bin/codex")}


def test_apply_codex_to_manifest_treats_home_relative_path_as_workspace_relative() -> None:
    manifest = apply_codex_to_manifest(
        Manifest(),
        CodexConfig(path="~/.codex/codex"),
    )

    validated = manifest.validated_entries()

    assert Path(".codex/codex") in validated
    entry = validated[Path(".codex/codex")]
    assert isinstance(entry, Codex)


def test_apply_codex_to_manifest_preserves_explicit_entry_at_configured_path() -> None:
    explicit = File(content=b"custom")
    manifest = apply_codex_to_manifest(
        Manifest(
            entries={"tools/codex": explicit},
        ),
        CodexConfig(path="tools/codex"),
    )

    validated = manifest.validated_entries()

    preserved = validated["tools/codex"]
    assert isinstance(preserved, File)
    assert preserved == explicit


def test_apply_codex_to_manifest_accepts_absolute_path_within_manifest_root() -> None:
    manifest = apply_codex_to_manifest(
        Manifest(root="/workspace"),
        CodexConfig(path="/workspace/tools/codex"),
    )

    validated = manifest.validated_entries()

    assert Path("tools/codex") in validated
    entry = validated[Path("tools/codex")]
    assert isinstance(entry, Codex)


def test_apply_codex_to_manifest_rejects_absolute_path_outside_manifest_root() -> None:
    with pytest.raises(InvalidManifestPathError, match="must be relative"):
        apply_codex_to_manifest(
            Manifest(root="/workspace"),
            CodexConfig(path="/tmp/codex"),
        )


def test_manifest_has_codex_entry_accepts_absolute_default_root_path_after_root_rewrite() -> None:
    manifest = Manifest(
        root="/tmp/session-root",
        entries={"tools/codex": Codex(version=DEFAULT_CODEX_VERSION)},
    )

    assert manifest_has_codex_entry(
        manifest,
        CodexConfig(path="/workspace/tools/codex"),
    )
