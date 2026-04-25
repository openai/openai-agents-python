from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath

from .snapshot import LocalSnapshotSpec

_DEFAULT_LOCAL_SNAPSHOT_TTL_SECONDS = 60 * 60 * 24 * 30
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
    resolved_env = env or os.environ
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
        base = Path(xdg_state_home) if xdg_state_home else resolved_home / ".local" / "state"

    return base / _DEFAULT_LOCAL_SNAPSHOT_SUBDIR


def cleanup_stale_default_local_snapshots(
    base_path: Path,
    *,
    now: float | None = None,
    max_age_seconds: int = _DEFAULT_LOCAL_SNAPSHOT_TTL_SECONDS,
) -> None:
    # This is intentionally limited to stale files in the SDK-managed default directory.
    # We do not delete snapshots during normal session teardown because pause/resume may still
    # need them. If we add explicit artifact cleanup later, it should be a separate opt-in path
    # that can also account for backend-specific remote artifacts.
    if max_age_seconds < 0 or not base_path.exists():
        return

    cutoff = (time.time() if now is None else now) - max_age_seconds
    try:
        candidates = list(base_path.glob("*.tar"))
    except OSError:
        return

    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            if candidate.stat().st_mtime >= cutoff:
                continue
            candidate.unlink(missing_ok=True)
        except OSError:
            continue


def resolve_default_local_snapshot_spec(
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    os_name: str | None = None,
    now: float | None = None,
) -> LocalSnapshotSpec:
    base_path = default_local_snapshot_base_dir(
        home=home,
        env=env,
        platform=platform,
        os_name=os_name,
    )
    base_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (os_name or os.name) != "nt":
        try:
            base_path.chmod(0o700)
        except OSError:
            pass
    return LocalSnapshotSpec(base_path=base_path)
