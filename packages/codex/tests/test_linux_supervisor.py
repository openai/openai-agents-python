"""Owned Linux process lifetime checks; these do not establish OS confinement."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(sys.platform != "linux", reason="Linux child subreaper boundary"),
]

RUNTIME = Path(__file__).resolve().parents[3] / "src/agents/bin/codex-sdk-runtime"


@pytest.fixture
def owner(tmp_path: Path) -> Path:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    runtime = binary_dir / "codex-sdk-runtime"
    runtime.symlink_to(RUNTIME)
    helper = binary_dir / "codex-linux-sandbox"
    # A controlled child represents bwrap and the new-session PID namespace.
    # Readiness means the grandchild holds a lock after closing all stdio.
    helper.write_text(
        "#!/usr/bin/python3\n"
        "import fcntl,os,pathlib,signal,sys,time\n"
        "mode,lock_path,ready_path=sys.argv[1:]\n"
        "if mode=='status': sys.exit(23)\n"
        "if mode=='stdio':\n"
        "    data=os.read(0,32);os.write(1,b'received:'+data);sys.exit(0)\n"
        "if mode=='forced': signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "child=os.fork()\n"
        "if child==0:\n"
        "    os.setsid()\n"
        "    if os.fork(): os._exit(0)\n"
        "    signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "    with open(lock_path,'r+') as locked:\n"
        "        fcntl.flock(locked,fcntl.LOCK_EX)\n"
        "        os.close(0);os.close(1);os.close(2)\n"
        "        pathlib.Path(ready_path).touch()\n"
        "        time.sleep(60)\n"
        "    os._exit(0)\n"
        "os.waitpid(child,0)\n"
        "if mode=='normal':\n"
        "    while not pathlib.Path(ready_path).exists(): time.sleep(.002)\n"
        "    sys.exit(23)\n"
        "time.sleep(60)\n"
    )
    helper.chmod(0o700)
    return runtime


@pytest.mark.parametrize("mode", ["normal", "graceful", "forced"])
async def test_owner_joins_detached_child_even_without_stdio(
    owner: Path, tmp_path: Path, mode: str
) -> None:
    import fcntl
    import signal

    lock_path = tmp_path / "child.lock"
    ready = tmp_path / "child.ready"
    with lock_path.open("w+") as locked:
        process = await asyncio.create_subprocess_exec(
            str(owner),
            "--sdk-unix-local",
            mode,
            str(lock_path),
            str(ready),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            # A file provides a visible handshake from a process with no stdio.
            for _ in range(1000):
                if ready.exists() or process.returncode is not None:
                    break
                await asyncio.sleep(0.005)
            assert ready.exists(), await process.communicate()
            if mode != "normal":
                with pytest.raises(BlockingIOError):
                    fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
                process.terminate()
            await asyncio.wait_for(process.communicate(), timeout=5)
            assert (
                process.returncode
                == {
                    "normal": 23,
                    "graceful": -signal.SIGTERM,
                    "forced": -signal.SIGKILL,
                }[mode]
            )
            # This is immediate after the await; no polling for eventual cleanup.
            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.communicate(), timeout=5)


async def test_owner_preserves_primary_exit_status_and_inherited_stdio(
    owner: Path, tmp_path: Path
) -> None:
    for mode, expected in (("status", 23), ("stdio", 0)):
        process = await asyncio.create_subprocess_exec(
            str(owner),
            "--sdk-unix-local",
            mode,
            str(tmp_path / "unused.lock"),
            str(tmp_path / "unused.ready"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(b"fixture"), timeout=5)
        assert process.returncode == expected, stderr
        assert stdout == (b"received:fixture" if mode == "stdio" else b"")


async def test_cancelling_owner_at_spawn_cannot_leave_live_children(
    owner: Path, tmp_path: Path
) -> None:
    import fcntl

    # Cancellation may reach either native startup or the launched helper.
    # Each outcome has the same post-wait resource-ownership requirement.
    for index in range(12):
        lock_path = tmp_path / f"spawn-{index}.lock"
        ready = tmp_path / f"spawn-{index}.ready"
        with lock_path.open("w+") as locked:
            process = await asyncio.create_subprocess_exec(
                str(owner),
                "--sdk-unix-local",
                "forced",
                str(lock_path),
                str(ready),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            process.terminate()
            await asyncio.wait_for(process.communicate(), timeout=5)
            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
