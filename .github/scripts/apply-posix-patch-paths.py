from pathlib import Path

source = Path("src/agents/sandbox/apply_patch.py")
text = source.read_text()
old = '''    def _validate_path(self, path: str | Path) -> Path:\n        if isinstance(path, str):\n            if not path.strip():\n                raise ApplyPatchPathError(path=path, reason="empty")\n            normalized_path = Path(path)\n        else:\n            normalized_path = path\n\n        try:\n            return self._session._workspace_path_policy().relative_path(normalized_path)\n        except InvalidManifestPathError as exc:\n            raise ApplyPatchPathError(\n                path=normalized_path,\n                reason="escape_root",\n                cause=exc,\n            ) from exc\n'''
new = '''    def _validate_path(self, path: str | Path) -> Path:\n        if isinstance(path, str) and not path.strip():\n            raise ApplyPatchPathError(path=path, reason="empty")\n\n        # Keep raw model-provided strings intact until the sandbox path policy\n        # normalizes them. Converting through host-native Path first would make\n        # backslash handling depend on the SDK host operating system.\n        try:\n            return self._session._workspace_path_policy().relative_path(path)\n        except InvalidManifestPathError as exc:\n            raise ApplyPatchPathError(\n                path=path,\n                reason="escape_root",\n                cause=exc,\n            ) from exc\n'''
if old not in text:
    raise SystemExit("target _validate_path block not found")
source.write_text(text.replace(old, new, 1))

test_path = Path("tests/sandbox/test_apply_patch.py")
tests = test_path.read_text()
anchor = '''@pytest.mark.asyncio\nasync def test_apply_patch_allows_absolute_path_within_root() -> None:\n'''
insert = r'''@pytest.mark.asyncio
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
    assert Path(r"/workspace/nested\new.txt") not in session.files


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


'''
if anchor not in tests:
    raise SystemExit("test insertion anchor not found")
test_path.write_text(tests.replace(anchor, insert + anchor, 1))

Path(".github/workflows/apply-posix-patch-paths.yml").unlink(missing_ok=True)
Path(__file__).unlink(missing_ok=True)
