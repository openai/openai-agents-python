from pathlib import PurePosixPath, PureWindowsPath

import pytest

from agents.sandbox.workspace_paths import normalize_sandbox_cwd


def test_normalize_sandbox_cwd_rejects_backslash_in_pure_posix_path() -> None:
    with pytest.raises(ValueError, match="POSIX path separators"):
        normalize_sandbox_cwd(PurePosixPath(r"tasks\child"))


def test_normalize_sandbox_cwd_normalizes_relative_pure_windows_path() -> None:
    assert normalize_sandbox_cwd(PureWindowsPath(r"tasks\child")) == PurePosixPath(
        "tasks/child"
    )
