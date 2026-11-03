"""Select the native executables required by the target platform."""

from pathlib import Path


def native_helpers(platform: str) -> dict[str, str]:
    """Map installed filenames to their compiled binaries."""
    runtime = "codex-sdk-runtime"
    helpers = {runtime: runtime, "codex-code-mode-host": "codex-code-mode-host"}
    if platform == "linux":
        helpers["codex-linux-sandbox"] = runtime
    if platform != "win32":
        helpers["codex-execve-wrapper"] = runtime
    suffix = ".exe" if platform == "win32" else ""
    return {name + suffix: source + suffix for name, source in helpers.items()}


def remove_obsolete_helpers(directory: Path, platform: str) -> None:
    """Remove aliases left by an earlier build in this checkout."""
    required = native_helpers(platform)
    suffix = ".exe" if platform == "win32" else ""
    for name in ("apply_patch", "applypatch", "codex-linux-sandbox", "codex-execve-wrapper"):
        filename = name + suffix
        if filename not in required:
            (directory / filename).unlink(missing_ok=True)
