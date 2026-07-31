from __future__ import annotations

import os
from pathlib import Path

import pytest

from agents.sandbox.snapshot import LocalSnapshotSpec
from agents.sandbox.snapshot_defaults import (
    default_local_snapshot_base_dir,
    resolve_default_local_snapshot_spec,
)


def test_default_local_snapshot_base_dir_uses_xdg_state_home(tmp_path: Path) -> None:
    state_home = tmp_path / "state"
    result = default_local_snapshot_base_dir(
        home=tmp_path / "home",
        env={"XDG_STATE_HOME": str(state_home)},
        platform="linux",
        os_name="posix",
    )

    assert result == state_home / "openai-agents-python" / "sandbox" / "snapshots"


def test_default_local_snapshot_base_dir_honors_explicit_empty_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "host-state"))

    result = default_local_snapshot_base_dir(
        home=home,
        env={},
        platform="linux",
        os_name="posix",
    )

    assert result == home / ".local" / "state" / "openai-agents-python" / "sandbox" / "snapshots"


def test_default_local_snapshot_base_dir_ignores_relative_xdg_state_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"

    result = default_local_snapshot_base_dir(
        home=home,
        env={"XDG_STATE_HOME": "relative-state"},
        platform="linux",
        os_name="posix",
    )

    assert result == home / ".local" / "state" / "openai-agents-python" / "sandbox" / "snapshots"


def test_default_local_snapshot_base_dir_uses_macos_application_support(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = default_local_snapshot_base_dir(
        home=home,
        env={},
        platform="darwin",
        os_name="posix",
    )

    assert (
        result
        == home
        / "Library"
        / "Application Support"
        / "openai-agents-python"
        / "sandbox"
        / "snapshots"
    )


def test_default_local_snapshot_base_dir_uses_localappdata_on_windows(tmp_path: Path) -> None:
    local_app_data = Path(r"C:\Users\me\AppData\Local")
    result = default_local_snapshot_base_dir(
        home=tmp_path / "home",
        env={"LOCALAPPDATA": str(local_app_data)},
        platform="win32",
        os_name="nt",
    )

    assert result == local_app_data / "openai-agents-python" / "sandbox" / "snapshots"


def test_default_local_snapshot_base_dir_uses_absolute_appdata_when_localappdata_is_relative(
    tmp_path: Path,
) -> None:
    app_data = Path(r"C:\Users\me\AppData\Roaming")
    result = default_local_snapshot_base_dir(
        home=tmp_path / "home",
        env={"LOCALAPPDATA": "relative-local", "APPDATA": str(app_data)},
        platform="win32",
        os_name="nt",
    )

    assert result == app_data / "openai-agents-python" / "sandbox" / "snapshots"


def test_default_local_snapshot_base_dir_ignores_relative_windows_env_paths(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    result = default_local_snapshot_base_dir(
        home=home,
        env={"LOCALAPPDATA": "relative-local", "APPDATA": "relative-roaming"},
        platform="win32",
        os_name="nt",
    )

    assert result == home / "AppData" / "Local" / "openai-agents-python" / "sandbox" / "snapshots"


def test_default_local_snapshot_base_dir_ignores_posix_absolute_localappdata_on_windows(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    result = default_local_snapshot_base_dir(
        home=home,
        env={"LOCALAPPDATA": "/tmp/localappdata"},
        platform="win32",
        os_name="nt",
    )

    assert result == home / "AppData" / "Local" / "openai-agents-python" / "sandbox" / "snapshots"


def test_resolve_default_local_snapshot_spec_keeps_existing_stale_files(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    managed_dir = state_home / "openai-agents-python" / "sandbox" / "snapshots"
    managed_dir.mkdir(parents=True)
    stale = managed_dir / "stale.tar"
    stale.write_bytes(b"stale")
    os.utime(stale, (0, 0))

    spec = resolve_default_local_snapshot_spec(
        home=tmp_path / "home",
        env={"XDG_STATE_HOME": str(state_home)},
        platform="linux",
        os_name="posix",
    )

    assert isinstance(spec, LocalSnapshotSpec)
    assert spec.base_path == managed_dir
    assert managed_dir.exists()
    assert stale.exists()
