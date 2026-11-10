"""Compatibility aliases for the shared implementation."""

import sys as _sys

from _agents_shared import apply_diff as _implementation
from _agents_shared.apply_diff import (
    END_FILE as END_FILE,
    END_PATCH as END_PATCH,
    END_SECTION_MARKERS as END_SECTION_MARKERS,
    SECTION_TERMINATORS as SECTION_TERMINATORS,
    ApplyDiffMode as ApplyDiffMode,
    Chunk as Chunk,
    ContextMatch as ContextMatch,
    ParsedUpdateDiff as ParsedUpdateDiff,
    ParserState as ParserState,
    ReadSectionResult as ReadSectionResult,
    __all__ as __all__,
    _advance_cursor_to_anchor as _advance_cursor_to_anchor,
    _apply_chunks as _apply_chunks,
    _detect_newline as _detect_newline,
    _detect_newline_from_text as _detect_newline_from_text,
    _eof_search_lines as _eof_search_lines,
    _equals_slice as _equals_slice,
    _find_context as _find_context,
    _find_context_core as _find_context_core,
    _is_done as _is_done,
    _normalize_diff_lines as _normalize_diff_lines,
    _normalize_text_newlines as _normalize_text_newlines,
    _parse_create_diff as _parse_create_diff,
    _parse_update_diff as _parse_update_diff,
    _read_anchors as _read_anchors,
    _read_section as _read_section,
    _read_str as _read_str,
    apply_diff as apply_diff,
)

_sys.modules[__name__] = _implementation
