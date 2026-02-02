"""Direct tests for the apply_diff helpers to exercise corner cases."""

from __future__ import annotations

import pytest

from agents.apply_diff import (
    Chunk,
    ParserState,
    _apply_chunks,
    _find_context,
    _find_context_core,
    _is_done,
    _normalize_diff_lines,
    _read_section,
    _read_str,
)


def test_normalize_diff_lines_drops_trailing_blank() -> None:
    assert _normalize_diff_lines("a\nb\n") == ["a", "b"]


def test_is_done_true_when_index_out_of_range() -> None:
    state = ParserState(lines=["line"], index=1)
    assert _is_done(state, [])


def test_read_str_returns_empty_when_missing_prefix() -> None:
    state = ParserState(lines=["value"], index=0)
    assert _read_str(state, "nomatch") == ""
    assert state.index == 0


def test_read_section_returns_eof_flag() -> None:
    result = _read_section(["*** End of File"], 0)
    assert result.eof


def test_read_section_raises_on_invalid_marker() -> None:
    with pytest.raises(ValueError):
        _read_section(["*** Bad Marker"], 0)


def test_read_section_raises_when_empty_segment() -> None:
    with pytest.raises(ValueError):
        _read_section([], 0)


def test_find_context_eof_fallbacks() -> None:
    match = _find_context(["one"], ["missing"], start=0, eof=True)
    assert match.new_index == -1
    assert match.fuzz >= 10000


def test_find_context_core_stripped_matches() -> None:
    match = _find_context_core([" line "], ["line"], start=0)
    assert match.new_index == 0
    assert match.fuzz == 100


def test_apply_chunks_rejects_bad_chunks() -> None:
    with pytest.raises(ValueError):
        _apply_chunks("abc", [Chunk(orig_index=10, del_lines=[], ins_lines=[])], newline="\n")

    with pytest.raises(ValueError):
        _apply_chunks(
            "abc",
            [
                Chunk(orig_index=0, del_lines=["a"], ins_lines=[]),
                Chunk(orig_index=0, del_lines=["b"], ins_lines=[]),
            ],
            newline="\n",
        )
