from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/run-asyncio-teardown-stability.sh"
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="The Bash command tests require POSIX executable shims.",
)
EXPECTED_NODES = [
    "tests/test_asyncio_progress.py::test_deadline",
    "tests/test_asyncio_progress.py::test_external_wait[first]",
    "tests/test_asyncio_progress.py::test_external_wait[second]",
    "tests/test_run_step_execution.py::test_cancel_sibling",
    "tests/test_run_step_execution.py::test_post_invoke",
]


@pytest.fixture
def command_environment(tmp_path: Path) -> dict[str, str]:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tests / "test_asyncio_progress.py").write_text(
        textwrap.dedent(
            """\
            import pytest

            def test_deadline():
                pass

            @pytest.mark.parametrize("value", [1, 2], ids=["first", "second"])
            def test_external_wait(value):
                assert value > 0
            """
        ),
        encoding="utf-8",
    )
    (tests / "test_run_step_execution.py").write_text(
        textwrap.dedent(
            """\
            def test_cancel_sibling():
                pass

            def test_post_invoke():
                pass

            def test_unrelated():
                raise AssertionError("The stability command must not select this test.")
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent(
            """\
            import json
            import os
            from pathlib import Path

            import pytest

            seen = set()

            @pytest.fixture(autouse=True)
            def check_fresh_state(request):
                assert request.node.nodeid not in seen
                seen.add(request.node.nodeid)
                if request.node.nodeid == os.environ.get("STABILITY_FAIL_NODE"):
                    pytest.fail("Injected selected-test failure.")

            def pytest_sessionfinish(session, exitstatus):
                with Path("sessions.jsonl").open("a", encoding="utf-8") as log:
                    log.write(json.dumps({
                        "pid": os.getpid(),
                        "nodes": [item.nodeid for item in session.items],
                        "exitstatus": int(exitstatus),
                    }) + "\\n")
            """
        ),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    # Forward to real pytest without installing packages in the temporary suite.
    uv.write_text(
        '#!/usr/bin/env bash\nset -eu\n[[ "$1" == "run" ]]\nshift\n'
        f'exec {shlex.quote(sys.executable)} -m "$@"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    environment = os.environ.copy()
    for name in ("OPENAI_API_KEY", "PYTHONPATH", "PYTEST_PLUGINS", "STABILITY_FAIL_NODE"):
        environment.pop(name, None)
    environment.update(
        PATH=str(bin_dir) + os.pathsep + environment.get("PATH", ""),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_ADDOPTS="",
    )
    return environment


@pytest.mark.parametrize("arguments, repetitions", [([], 5), (["2"], 2)])
def test_stability_command_preserves_selection_in_fresh_processes(
    tmp_path: Path, command_environment: dict[str, str], arguments: list[str], repetitions: int
) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), *arguments],
        cwd=tmp_path,
        env=command_environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    sessions = [json.loads(line) for line in (tmp_path / "sessions.jsonl").read_text().splitlines()]
    assert len(sessions) == repetitions
    assert len({session["pid"] for session in sessions}) == repetitions
    assert [session["nodes"] for session in sessions] == [EXPECTED_NODES] * repetitions
    assert all(session["exitstatus"] == 0 for session in sessions)
    assert result.stdout.count("Async teardown stability run ") == repetitions


@pytest.mark.parametrize("failed_node", [EXPECTED_NODES[0], EXPECTED_NODES[-1]])
def test_stability_command_stops_after_selected_test_failure(
    tmp_path: Path, command_environment: dict[str, str], failed_node: str
) -> None:
    command_environment["STABILITY_FAIL_NODE"] = failed_node
    result = subprocess.run(
        ["bash", str(SCRIPT), "3"],
        cwd=tmp_path,
        env=command_environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Injected selected-test failure." in result.stdout
    sessions = [json.loads(line) for line in (tmp_path / "sessions.jsonl").read_text().splitlines()]
    assert len(sessions) == 1
    assert sessions[0]["exitstatus"] == 1
    assert result.stdout.count("Async teardown stability run ") == 1
