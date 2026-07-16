from __future__ import annotations

from pathlib import Path

import pytest

from agents.editor import ApplyPatchOperation
from agents.sandbox import Manifest
from agents.sandbox.errors import (
    ApplyPatchDecodeError,
    ApplyPatchDiffError,
    ApplyPatchFileNotFoundError,
    ApplyPatchPathError,
)
from tests.sandbox._apply_patch_test_session import (
    ApplyPatchSession,
    ProviderNotFoundApplyPatchSession,
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
