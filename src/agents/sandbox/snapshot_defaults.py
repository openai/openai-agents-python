from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath

from .snapshot import LocalSnapshotSpec

_DEFAULT_LOCAL_SNAPSHOT_SUBDIR = Path("openai-agents-python") / "sandbox" / "snapshots"


def _first_absolute_windows_env_path(env: Mapping[str, str], *names: str) -> Path | None:
    for name in names:
        value = env.get(name)
        if not value:
            continue
        if PureWindowsPath(value).is_absolute():
            return Path(value)
    return None


def default_local_snapshot_base_dir(
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    os_name: str | None = None,
) -> Path:
    resolved_home = home or Path.home()
    resolved_env = os.environ if env is None else env
    resolved_platform = platform or sys.platform
    resolved_os_name = os_name or os.name

    if resolved_platform == "darwin":
        base = resolved_home / "Library" / "Application Support"
    elif resolved_os_name == "nt":
        env_base = _first_absolute_windows_env_path(
            resolved_env,
            "LOCALAPPDATA",
            "APPDATA",
        )
        base = env_base if env_base is not None else resolved_home / "AppData" / "Local"
    else:
        xdg_state_home = resolved_env.get("XDG_STATE_HOME")
        xdg_base = Path(xdg_state_home) if xdg_state_home else None
        base = (
            xdg_base
            if xdg_base is not None and xdg_base.is_absolute()
            else resolved_home / ".local" / "state"
        )

    return base / _DEFAULT_LOCAL_SNAPSHOT_SUBDIR


def resolve_default_local_snapshot_spec(
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    os_name: str | None = None,
) -> LocalSnapshotSpec:
    base_path = default_local_snapshot_base_dir(
        home=home,
        env=env,
        platform=platform,
        os_name=os_name,
    )
    # Existing archives may still back paused or serialized resume state.
    base_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (os_name or os.name) != "nt":
        try:
            base_path.chmod(0o700)
        except OSError:
            pass
    return LocalSnapshotSpec(base_path=base_path)
