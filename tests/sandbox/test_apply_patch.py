from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.editor import ApplyPatchOperation
from agents.sandbox import Manifest
from agents.sandbox.errors import (
    ApplyPatchDecodeError,
    ApplyPatchDiffError,
    ApplyPatchFileNotFoundError,
    ApplyPatchPathError,
)
from agents.sandbox.session.sandbox_session import SandboxSession
from tests.sandbox._apply_patch_test_session import (
    ApplyPatchSession,
    CaseFoldingApplyPatchSession,
    ConcurrentWriterApplyPatchSession,
    NormalizationFoldingApplyPatchSession,
    PosixHostApplyPatchSession,
    ProviderNotFoundApplyPatchSession,
    WriteFailureApplyPatchSession,
)


@pytest.mark.asyncio
async def test_apply_patch_update_invalid_context_raises() -> None:
    session = ApplyPatchSession()
    session.files[Path("/workspace/bad.txt")] = b"alpha\nbeta\n"

    with pytest.raises(ApplyPatchDiffError):
        await session.apply_patch(
            ApplyPatchOperation(
                type="update_file",
                path="bad.txt",
                diff="@@\n missing\n-beta\n+gamma\n",
            )
        )


@pytest.mark.asyncio
async def test_apply_patch_update_uses_anchor_jump() -> None:
    session = ApplyPatchSession()
    session.files[Path("/workspace/anchor.txt")] = b"a\nb\nmarker\nc\nd\n"

    await session.apply_patch(
        ApplyPatchOperation(
            type="update_file",
            path="anchor.txt",
            diff="@@ marker\n c\n-d\n+e\n",
        )
    )

    assert session.files[Path("/workspace/anchor.txt")] == b"a\nb\nmarker\nc\ne\n"


@pytest.mark.asyncio
async def test_apply_patch_update_uses_stacked_anchor_jump() -> None:
    """The tool description tells the model to stack ``@@`` headers when one is ambiguous."""
    session = ApplyPatchSession()
    session.files[Path("/workspace/stacked.py")] = (
        b"class First\n"
        b"    def target():\n"
        b"        return 0\n"
        b"\n"
        b"class Second\n"
        b"    def helper():\n"
        b"        pass\n"
        b"\n"
        b"    def target():\n"
        b"        pass\n"
    )

    await session.apply_patch(
        ApplyPatchOperation(
            type="update_file",
            path="stacked.py",
            diff="@@ class Second\n@@     def target():\n-        pass\n+        return 1\n",
        )
    )

    assert session.files[Path("/workspace/stacked.py")] == (
        b"class First\n"
        b"    def target():\n"
        b"        return 0\n"
        b"\n"
        b"class Second\n"
        b"    def helper():\n"
        b"        pass\n"
        b"\n"
        b"    def target():\n"
        b"        return 1\n"
    )


@pytest.mark.asyncio
async def test_apply_patch_update_rejects_partially_matched_stacked_anchors() -> None:
    session = ApplyPatchSession()
    path = Path("/workspace/stacked.py")
    original = (
        b"class Target\n    def helper():\n        pass\n\n    def desired():\n        return 1\n"
    )
    session.files[path] = original

    with pytest.raises(ApplyPatchDiffError, match="Invalid Anchor"):
        await session.apply_patch(
            ApplyPatchOperation(
                type="update_file",
                path="stacked.py",
                diff=(
                    "@@ class Target\n@@     def missing():\n-        pass\n+        return 99\n"
                ),
            )
        )

    assert session.files[path] == original


@pytest.mark.asyncio
async def test_apply_patch_update_matches_end_of_file_context() -> None:
    session = ApplyPatchSession()
    session.files[Path("/workspace/tail.txt")] = b"one\ntwo\nthree\n"

    await session.apply_patch(
        ApplyPatchOperation(
            type="update_file",
            path="tail.txt",
            diff="@@\n two\n-three\n+four\n*** End of File\n",
        )
    )

    assert session.files[Path("/workspace/tail.txt")] == b"one\ntwo\nfour\n"


@pytest.mark.asyncio
async def test_apply_patch_update_missing_diff_raises() -> None:
    session = ApplyPatchSession()

    with pytest.raises(ApplyPatchDiffError):
        await session.apply_patch(ApplyPatchOperation(type="update_file", path="file.txt"))


@pytest.mark.asyncio
async def test_apply_patch_update_missing_file_raises() -> None:
    session = ApplyPatchSession()

    with pytest.raises(ApplyPatchFileNotFoundError):
        await session.apply_patch(
            ApplyPatchOperation(
                type="update_file",
                path="missing.txt",
                diff="@@\n-old\n+new\n",
            )
        )


@pytest.mark.asyncio
async def test_apply_patch_delete_missing_file_raises() -> None:
    session = ApplyPatchSession()

    with pytest.raises(ApplyPatchFileNotFoundError):
        await session.apply_patch(ApplyPatchOperation(type="delete_file", path="nope.txt"))


@pytest.mark.asyncio
async def test_apply_patch_missing_file_errors_use_workspace_path() -> None:
    session = ProviderNotFoundApplyPatchSession()

    with pytest.raises(ApplyPatchFileNotFoundError) as update_exc:
        await session.apply_patch(
            ApplyPatchOperation(
                type="update_file",
                path="missing.txt",
                diff="@@\n-old\n+new\n",
            )
        )

    update_message = str(update_exc.value)
    assert update_message == "apply_patch missing file: missing.txt"
    assert update_exc.value.context["path"] == "missing.txt"
    assert "/provider/private/root" not in update_message

    with pytest.raises(ApplyPatchFileNotFoundError) as delete_exc:
        await session.apply_patch(
            ApplyPatchOperation(type="delete_file", path="missing-delete.txt")
        )

    delete_message = str(delete_exc.value)
    assert delete_message == "apply_patch missing file: missing-delete.txt"
    assert delete_exc.value.context["path"] == "missing-delete.txt"
    assert "/provider/private/root" not in delete_message


@pytest.mark.asyncio
async def test_apply_patch_rejects_escape_root_path() -> None:
    session = ApplyPatchSession()

    with pytest.raises(ApplyPatchPathError):
        await session.apply_patch(
            ApplyPatchOperation(
                type="create_file",
                path="../escape.txt",
                diff="+nope",
            )
        )


@pytest.mark.asyncio
async def test_apply_patch_rejects_empty_path() -> None:
    session = ApplyPatchSession()

    with pytest.raises(ApplyPatchPathError):
        await session.apply_patch(
            ApplyPatchOperation(
                type="create_file",
                path="",
                diff="+nope",
            )
        )


@pytest.mark.asyncio
async def test_apply_patch_normalizes_backslashes_in_string_path() -> None:
    session = ApplyPatchSession()

    await session.apply_patch(
        ApplyPatchOperation(
            type="create_file",
            path=r"nested\new.txt",
            diff="+hello",
        )
    )

    assert session.files[Path("/workspace/nested/new.txt")] == b"hello"


@pytest.mark.asyncio
async def test_apply_patch_normalizes_backslashes_in_move_to() -> None:
    session = ApplyPatchSession()
    session.files[Path("/workspace/source.txt")] = b"alpha\n"

    await session.apply_patch(
        ApplyPatchOperation(
            type="update_file",
            path="source.txt",
            diff="@@\n-alpha\n+beta\n",
            move_to=r"nested\moved.txt",
        )
    )

    assert session.files[Path("/workspace/nested/moved.txt")] == b"beta\n"
    assert Path("/workspace/source.txt") not in session.files


@pytest.mark.asyncio
async def test_apply_patch_case_only_move_to_keeps_file_on_case_folding_filesystem() -> None:
    """A case-folding filesystem stores both names as one file, which the removal must keep."""
    session = CaseFoldingApplyPatchSession()
    session.files[cast(Path, PurePosixPath("/workspace/notes.txt"))] = b"alpha\nbeta\n"

    await session.apply_patch(
        ApplyPatchOperation(
            type="update_file",
            path="notes.txt",
            diff="@@\n alpha\n-beta\n+gamma\n",
            move_to="Notes.txt",
        )
    )

    assert session.files == {PurePosixPath("/workspace/Notes.txt"): b"alpha\ngamma\n"}


@pytest.mark.asyncio
async def test_apply_patch_case_only_move_to_moves_file_on_case_sensitive_filesystem() -> None:
    """A case-sensitive filesystem keeps the names apart, so the source must still be removed."""
    session = PosixHostApplyPatchSession()
    session.files[cast(Path, PurePosixPath("/workspace/notes.txt"))] = b"alpha\nbeta\n"

    await session.apply_patch(
        ApplyPatchOperation(
            type="update_file",
            path="notes.txt",
            diff="@@\n alpha\n-beta\n+gamma\n",
            move_to="Notes.txt",
        )
    )

    assert session.files == {PurePosixPath("/workspace/Notes.txt"): b"alpha\ngamma\n"}


@pytest.mark.asyncio
async def test_apply_patch_move_to_leaves_the_source_alone_when_the_write_fails() -> None:
    """Nothing is removed until the replacement is committed, so a failed write changes nothing."""
    session = WriteFailureApplyPatchSession()
    session.files[cast(Path, PurePosixPath("/workspace/notes.txt"))] = b"alpha\nbeta\n"

    with pytest.raises(ConnectionError):
        await session.apply_patch(
            ApplyPatchOperation(
                type="update_file",
                path="notes.txt",
                diff="@@\n alpha\n-beta\n+gamma\n",
                move_to="Notes.txt",
            )
        )

    assert session.files == {PurePosixPath("/workspace/notes.txt"): b"alpha\nbeta\n"}
    # The file surviving is not enough. The refused implementation removed the source and then
    # wrote it back, which also ends here. The source may not be removed at all, and the only
    # path this is allowed to remove is the staging file it was in the middle of writing.
    assert PurePosixPath("/workspace/notes.txt") not in [path for path, _ in session.rm_calls]
    assert all(path.name.startswith(".apply_patch-") for path, _ in session.rm_calls)


@pytest.mark.asyncio
async def test_apply_patch_move_to_does_not_overwrite_a_concurrent_writer_after_a_failure() -> None:
    """A failed move must not restore the original over a file another writer just created.

    The operation cannot finish once the move fails. The question is what it leaves behind. An
    implementation that kept the original text in memory and wrote it back at the source path
    would destroy whatever arrived there in the meantime.
    """
    session = ConcurrentWriterApplyPatchSession()
    source = cast(Path, PurePosixPath("/workspace/notes.txt"))
    session.files[source] = b"alpha\nbeta\n"
    session.concurrent_source = source

    with pytest.raises(ConnectionError):
        await session.apply_patch(
            ApplyPatchOperation(
                type="update_file",
                path="notes.txt",
                diff="@@\n alpha\n-beta\n+gamma\n",
                move_to="Notes.txt",
            )
        )

    assert session.files[source] == b"written by someone else\n"
    assert PurePosixPath("/workspace/Notes.txt") not in session.files
    assert not [path for path in session.files if path.name.endswith(".tmp")]


@pytest.mark.asyncio
async def test_apply_patch_move_to_keeps_the_file_when_only_unicode_normalization_changes() -> None:
    """APFS folds NFC against NFD, so the two spellings of one accented name are one file.

    `str.casefold` does not normalize, so any fix that compares folded strings sends this pair
    down the path that destroys it. Asking the filesystem covers it without naming the case.
    """
    session = NormalizationFoldingApplyPatchSession()
    decomposed = "/workspace/cafe\u0301.txt"
    composed = "/workspace/caf\u00e9.txt"
    session.files[cast(Path, PurePosixPath(decomposed))] = b"alpha\nbeta\n"

    await session.apply_patch(
        ApplyPatchOperation(
            type="update_file",
            path=decomposed,
            diff="@@\n alpha\n-beta\n+gamma\n",
            move_to=composed,
        )
    )

    assert session.files == {PurePosixPath(composed): b"alpha\ngamma\n"}


@pytest.mark.asyncio
async def test_apply_patch_move_to_an_existing_directory_keeps_the_source() -> None:
    """`mv` moves a file into a directory destination and calls that success.

    The source would then be removed on the strength of that success, and the operation would
    report a move that did not happen. `move_to` comes from the model, so this is reachable.
    """
    session = PosixHostApplyPatchSession()
    source = cast(Path, PurePosixPath("/workspace/notes.txt"))
    session.files[source] = b"alpha\nbeta\n"
    session.directories.add(cast(Path, PurePosixPath("/workspace/docs")))

    with pytest.raises(IsADirectoryError):
        await session.apply_patch(
            ApplyPatchOperation(
                type="update_file",
                path="notes.txt",
                diff="@@\n alpha\n-beta\n+gamma\n",
                move_to="docs",
            )
        )

    assert session.files[source] == b"alpha\nbeta\n"
    assert not [path for path in session.files if path.name.endswith(".tmp")]


@pytest.mark.asyncio
async def test_apply_patch_move_to_commits_the_destination_before_removing_the_source() -> None:
    """The order is the fix. Assert it directly, so a future reordering fails here."""
    session = PosixHostApplyPatchSession()
    session.files[cast(Path, PurePosixPath("/workspace/notes.txt"))] = b"alpha\nbeta\n"

    await session.apply_patch(
        ApplyPatchOperation(
            type="update_file",
            path="notes.txt",
            diff="@@\n alpha\n-beta\n+gamma\n",
            move_to="Notes.txt",
        )
    )

    assert len(session.mv_calls) == 1
    staging, moved_to = session.mv_calls[0]
    assert staging.parent == PurePosixPath("/workspace")
    assert staging.name.endswith(".tmp")
    assert moved_to == PurePosixPath("/workspace/Notes.txt")
    assert session.rm_calls == [(cast(Path, PurePosixPath("/workspace/notes.txt")), False)]


@pytest.mark.asyncio
async def test_apply_patch_move_to_removes_a_source_symlink_pointing_at_the_destination() -> None:
    """`test -ef` follows symlinks, and the removal decision must not.

    A symlink and its target are one file by device and inode, and two directory entries.
    Removing the symlink leaves the target alone, so a rename that reads them as the same file
    leaves the old name behind pointing at the new one.
    """
    session = PosixHostApplyPatchSession()
    link = cast(Path, PurePosixPath("/workspace/notes.txt"))
    target = cast(Path, PurePosixPath("/workspace/Notes.txt"))
    session.files[link] = b"alpha\nbeta\n"
    session.files[target] = b"alpha\nbeta\n"
    session.symlinks[link] = target

    await session.apply_patch(
        ApplyPatchOperation(
            type="update_file",
            path="notes.txt",
            diff="@@\n alpha\n-beta\n+gamma\n",
            move_to="Notes.txt",
        )
    )

    assert session.files == {target: b"alpha\ngamma\n"}


@pytest.mark.asyncio
async def test_sandbox_session_forwards_follow_symlinks_to_the_inner_session() -> None:
    """The editor always talks to the instrumented wrapper, never to the session underneath.

    `BaseSandboxSession.apply_patch` builds the editor around `self`, and every client hands
    out a `SandboxSession`. A wrapper that accepts `follow_symlinks` and drops it leaves the
    inner session running the plain `-ef` test, and every test above uses a session double that
    never crosses the wrapper, so nothing else here would notice.
    """
    inner = MagicMock()
    inner.same_file = AsyncMock(return_value=True)
    session = SandboxSession(inner)

    await session.same_file("/workspace/link.txt", "/workspace/target.txt", follow_symlinks=False)

    # .get, not [], so a wrapper that drops the argument fails on the value rather than
    # raising KeyError from the assertion itself.
    assert inner.same_file.await_args.kwargs.get("follow_symlinks") is False


@pytest.mark.asyncio
async def test_apply_patch_move_to_the_same_path_writes_in_place() -> None:
    """A `move_to` that names the path it already has is an update, not a rename.

    Committing it through a staging file would replace the inode, and with it the mode and the
    extended attributes, for an operation that moves nothing.
    """
    session = PosixHostApplyPatchSession()
    source = cast(Path, PurePosixPath("/workspace/notes.txt"))
    session.files[source] = b"alpha\nbeta\n"

    await session.apply_patch(
        ApplyPatchOperation(
            type="update_file",
            path="notes.txt",
            diff="@@\n alpha\n-beta\n+gamma\n",
            move_to="notes.txt",
        )
    )

    assert session.files == {source: b"alpha\ngamma\n"}
    assert session.mv_calls == []
    assert session.rm_calls == []


@pytest.mark.asyncio
async def test_apply_patch_move_to_a_long_name_keeps_the_staging_name_within_the_limit() -> None:
    """A staging name built from the destination name overflows the 255-byte basename limit."""
    session = PosixHostApplyPatchSession()
    session.files[cast(Path, PurePosixPath("/workspace/notes.txt"))] = b"alpha\nbeta\n"
    long_name = "n" * 250 + ".txt"

    await session.apply_patch(
        ApplyPatchOperation(
            type="update_file",
            path="notes.txt",
            diff="@@\n alpha\n-beta\n+gamma\n",
            move_to=long_name,
        )
    )

    assert len(session.mv_calls) == 1
    staging, _ = session.mv_calls[0]
    assert len(staging.name.encode("utf-8")) <= 255
    assert session.files == {PurePosixPath(f"/workspace/{long_name}"): b"alpha\ngamma\n"}


@pytest.mark.asyncio
async def test_apply_patch_allows_absolute_path_within_root() -> None:
    session = ApplyPatchSession()

    await session.apply_patch(
        ApplyPatchOperation(
            type="create_file",
            path="/workspace/abs-ok.txt",
            diff="+hello",
        )
    )

    assert session.files[Path("/workspace/abs-ok.txt")] == b"hello"


@pytest.mark.asyncio
async def test_apply_patch_rejects_absolute_path_outside_root() -> None:
    session = ApplyPatchSession()

    with pytest.raises(ApplyPatchPathError):
        await session.apply_patch(
            ApplyPatchOperation(
                type="create_file",
                path="/tmp/outside.txt",
                diff="+nope",
            )
        )


@pytest.mark.asyncio
async def test_apply_patch_create_requires_plus_lines() -> None:
    session = ApplyPatchSession()

    with pytest.raises(ApplyPatchDiffError):
        await session.apply_patch(
            ApplyPatchOperation(
                type="create_file",
                path="new.txt",
                diff="oops",
            )
        )


@pytest.mark.asyncio
async def test_apply_patch_rejects_invalid_diff_line_prefix() -> None:
    session = ApplyPatchSession()
    session.files[Path("/workspace/oops.txt")] = b"alpha\nbeta\n"

    with pytest.raises(ApplyPatchDiffError):
        await session.apply_patch(
            ApplyPatchOperation(
                type="update_file",
                path="oops.txt",
                diff="oops",
            )
        )


@pytest.mark.asyncio
async def test_apply_patch_update_non_utf8_payload_raises() -> None:
    session = ApplyPatchSession()
    session.files[Path("/workspace/binary.txt")] = b"\xff\xfe\xfd"

    with pytest.raises(ApplyPatchDecodeError):
        await session.apply_patch(
            ApplyPatchOperation(
                type="update_file",
                path="binary.txt",
                diff="@@\n+\n",
            )
        )


@pytest.mark.asyncio
async def test_apply_patch_uses_custom_patch_format() -> None:
    session = ApplyPatchSession()
    session.files[Path("/workspace/custom.txt")] = b"hello\nworld\n"

    class StubFormat:
        @staticmethod
        def apply_diff(input: str, diff: str, mode: str = "default") -> str:
            del diff
            return input.replace("world", mode)

    result = await session.apply_patch(
        ApplyPatchOperation(
            type="update_file",
            path="custom.txt",
            diff="@@\n hello\n-world\n+ignored\n",
        ),
        patch_format=StubFormat(),
    )

    assert result == "Done!"
    assert session.files[Path("/workspace/custom.txt")] == b"hello\ndefault\n"


@pytest.mark.asyncio
async def test_apply_patch_supports_non_default_root() -> None:
    session = ApplyPatchSession(Manifest(root="/custom-workspace"))

    await session.apply_patch(
        ApplyPatchOperation(
            type="create_file",
            path="new.txt",
            diff="+hello",
        )
    )

    assert session.files[Path("/custom-workspace/new.txt")] == b"hello"


@pytest.mark.asyncio
async def test_apply_patch_mapping_operation_moves_file() -> None:
    session = ApplyPatchSession()
    session.files[Path("/workspace/old.txt")] = b"alpha\n"

    result = await session.apply_patch(
        {
            "type": "update_file",
            "path": "old.txt",
            "diff": "@@\n-alpha\n+beta\n",
            "move_to": "renamed/new.txt",
        }
    )

    assert result == "Done!"
    assert session.files[Path("/workspace/renamed/new.txt")] == b"beta\n"
    assert Path("/workspace/old.txt") not in session.files


@pytest.mark.asyncio
async def test_apply_patch_mapping_operation_without_move_to_updates_in_place() -> None:
    session = ApplyPatchSession()
    session.files[Path("/workspace/keep.txt")] = b"alpha\n"

    await session.apply_patch(
        {
            "type": "update_file",
            "path": "keep.txt",
            "diff": "@@\n-alpha\n+beta\n",
        }
    )

    assert session.files[Path("/workspace/keep.txt")] == b"beta\n"
    assert session.rm_calls == []


@pytest.mark.asyncio
async def test_apply_patch_mapping_operation_rejects_non_string_move_to() -> None:
    session = ApplyPatchSession()
    session.files[Path("/workspace/old.txt")] = b"alpha\n"

    with pytest.raises(ApplyPatchDiffError):
        await session.apply_patch(
            {
                "type": "update_file",
                "path": "old.txt",
                "diff": "@@\n-alpha\n+beta\n",
                "move_to": 5,
            }
        )

    assert session.files[Path("/workspace/old.txt")] == b"alpha\n"
