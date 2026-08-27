"""Worker startup diagnostics."""

from __future__ import annotations

YELLOW = "\033[1;33m"
RESET = "\033[0m"


def print_backend_warnings(registered_names: set[str]) -> None:
    """Print a prominent warning banner for any unconfigured sandbox backends."""
    import docker  # type: ignore[import-untyped]

    backend_env = {
        "daytona": "DAYTONA_API_KEY",
        "e2b": "E2B_API_KEY",
    }
    missing = {name: var for name, var in backend_env.items() if name not in registered_names}
    try:
        docker.from_env().ping()
    except Exception:
        missing["docker"] = "Docker daemon"

    if not missing:
        return

    lines = [
        "WARNING: Some sandbox backends are NOT available.",
        "Missing:",
    ]
    for name, var in sorted(missing.items()):
        lines.append(f"  - {name} ({var})")
    lines.append("The TUI will fail if you select an unconfigured backend.")
    lines.append("To use them, set the missing env vars and restart the worker.")
    width = max(len(line) for line in lines) + 4
    border = "!" * (width + 2)
    print(f"{YELLOW}{border}{RESET}")
    for line in lines:
        print(f"{YELLOW}! {line:<{width - 2}} !{RESET}")
    print(f"{YELLOW}{border}{RESET}")
