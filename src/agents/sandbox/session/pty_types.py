from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

from ..util.token_truncation import formatted_truncate_text_with_token_count

PTY_YIELD_TIME_MS_MIN = 250
PTY_EMPTY_YIELD_TIME_MS_MIN = 5_000
PTY_YIELD_TIME_MS_MAX = 30_000

PTY_PROCESSES_MAX = 64
PTY_PROCESSES_WARNING = 60
PTY_PROCESSES_PROTECTED_RECENT = 8

PTY_PROCESS_ID_MIN = 1_000
PTY_PROCESS_ID_MAX_EXCLUSIVE = 100_000


@dataclass(frozen=True)
class PtyExecUpdate:
    process_id: int | None
    output: bytes
    exit_code: int | None
    original_token_count: int | None


def clamp_pty_yield_time_ms(yield_time_ms: int) -> int:
    return max(PTY_YIELD_TIME_MS_MIN, min(PTY_YIELD_TIME_MS_MAX, yield_time_ms))


def resolve_pty_write_yield_time_ms(*, yield_time_ms: int, input_empty: bool) -> int:
    normalized = clamp_pty_yield_time_ms(yield_time_ms)
    if input_empty:
        return max(normalized, PTY_EMPTY_YIELD_TIME_MS_MIN)
    return normalized


def allocate_pty_process_id(used_process_ids: set[int]) -> int:
    while True:
        process_id = random.randrange(PTY_PROCESS_ID_MIN, PTY_PROCESS_ID_MAX_EXCLUSIVE)
        if process_id not in used_process_ids:
            return process_id


def process_id_to_prune_from_meta(meta: Sequence[tuple[int, float, bool]]) -> int | None:
    if not meta:
        return None

    by_recency = sorted(meta, key=lambda item: item[1], reverse=True)
    protected = {
        process_id
        for process_id, _last_used, _exited in by_recency[:PTY_PROCESSES_PROTECTED_RECENT]
    }

    lru = sorted(meta, key=lambda item: item[1])

    for process_id, _last_used, exited in lru:
        if process_id in protected:
            continue
        if exited:
            return process_id

    for process_id, _last_used, _exited in lru:
        if process_id not in protected:
            return process_id

    return None


def truncate_text_by_tokens(text: str, max_output_tokens: int | None) -> tuple[str, int | None]:
    return formatted_truncate_text_with_token_count(text, max_output_tokens)
