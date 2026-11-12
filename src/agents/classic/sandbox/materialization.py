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


class _SkillCopyBudget:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.members = 0
        self.bytes = 0

    def __deepcopy__(self, memo: dict[int, object]) -> "_SkillCopyBudget":
        # Trusted state aliases have been sharing one physical resource's quota.
        memo[id(self)] = self
        return self

    def reserve(self, *, members: int = 0, bytes: int = 0) -> None:
        if self.members + members > 4096:
            raise ValueError("Skill materialization exceeds the sandbox member limit")
        if self.bytes + bytes > 64 * 1024 * 1024:
            raise ValueError("Skill materialization exceeds the sandbox byte limit")
        self.members += members
        self.bytes += bytes


_TaskResultT = TypeVar("_TaskResultT")
_MISSING = object()


async def gather_in_order(
    task_factories: Sequence[Callable[[], Awaitable[_TaskResultT]]],
    *,
    max_concurrency: int | None = None,
) -> list[_TaskResultT]:
    if max_concurrency is not None and max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    if not task_factories:
        return []

    results: list[_TaskResultT | object] = [_MISSING] * len(task_factories)
    worker_count = len(task_factories)
    if max_concurrency is not None:
        worker_count = min(worker_count, max_concurrency)
    next_index = 0

    async def _worker() -> None:
        nonlocal next_index
        while next_index < len(task_factories):
            index = next_index
            next_index += 1
            results[index] = await task_factories[index]()

    tasks = [asyncio.create_task(_worker()) for _ in range(worker_count)]
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
