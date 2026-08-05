from __future__ import annotations

import fnmatch
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTEST_FILE_PATTERNS = ("test_*.py", "*_test.py")
SERIAL_MARKER = "pytest.mark.serial"


def _test_files() -> list[Path]:
    return sorted(
        path
        for path in (ROOT / "tests").rglob("*.py")
        if any(fnmatch.fnmatchcase(path.name, pattern) for pattern in PYTEST_FILE_PATTERNS)
    )


def _serial_test_files() -> list[Path]:
    return [path for path in _test_files() if SERIAL_MARKER in path.read_text(encoding="utf-8")]


def _relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _serial_args() -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        *(_relative(path) for path in _serial_test_files()),
        "-m",
        "serial",
    ]


def main() -> None:
    os.chdir(ROOT)
    os.execv(sys.executable, _serial_args())


if __name__ == "__main__":
    main()
