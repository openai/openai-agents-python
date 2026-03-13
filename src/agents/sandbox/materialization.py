import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast


@dataclass(frozen=True)
class MaterializedFile:
    path: Path
    sha256: str


@dataclass(frozen=True)
class MaterializationResult:
    files: list[MaterializedFile]


_TaskResultT = TypeVar("_TaskResultT")
_MISSING = object()


async def gather_in_order(
    task_factories: Sequence[Callable[[], Awaitable[_TaskResultT]]],
) -> list[_TaskResultT]:
    if not task_factories:
        return []

    results: list[_TaskResultT | object] = [_MISSING] * len(task_factories)

    async def _run(index: int, factory: Callable[[], Awaitable[_TaskResultT]]) -> None:
        results[index] = await factory()

    tasks = [
        asyncio.create_task(_run(index, factory)) for index, factory in enumerate(task_factories)
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        first_error: BaseException | None = None
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                continue
            except BaseException as error:
                first_error = error
                break

        if first_error is not None:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise first_error

        if pending:
            await asyncio.gather(*pending)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    for task in tasks:
        task.result()

    return [cast(_TaskResultT, result) for result in results]
