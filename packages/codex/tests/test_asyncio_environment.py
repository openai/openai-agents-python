"""Test runners have been retaining real subprocess support on every platform."""

import asyncio
import sys


async def subprocess_roundtrip() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        "print('loop-ready')",
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0
        assert output.splitlines() == [b"loop-ready"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_pytest_loop_supports_subprocesses() -> None:
    await subprocess_roundtrip()


def test_asyncio_run_supports_subprocesses() -> None:
    asyncio.run(subprocess_roundtrip())
