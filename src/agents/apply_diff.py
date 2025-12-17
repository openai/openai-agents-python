"""Utility for applying V4A diffs against text inputs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Callable, Literal

ApplyDiffMode = Literal["default", "create"]


@dataclass
class Chunk:
    orig_index: int
    del_lines: list[str]
    ins_lines: list[str]


@dataclass
class ParserState:
    lines: list[str]
    index: int = 0
    fuzz: int = 0


@dataclass
class ParsedUpdateDiff:
    chunks: list[Chunk]
    fuzz: int


@dataclass
class ReadSectionResult:
    next_context: list[str]
    section_chunks: list[Chunk]
    end_index: int
    eof: bool


END_PATCH = "*** End Patch"
END_FILE = "*** End of File"
SECTION_TERMINATORS = [
    END_PATCH,
    "*** Update File:",
    "*** Delete File:",
    "*** Add File:",
]
END_SECTION_MARKERS = [*SECTION_TERMINATORS, END_FILE]


def apply_diff(input: str, diff: str, mode: ApplyDiffMode = "default") -> str:
    """Apply a V4A diff to the provided text.

    This parser understands both the create-file syntax (only "+" prefixed
    lines) and the default update syntax that includes context hunks.
    """

    diff_lines = _normalize_diff_lines(diff)
    if mode == "create":
        return _parse_create_diff(diff_lines)

    parsed = _parse_update_diff(diff_lines, input)
    return _apply_chunks(input, parsed.chunks)


def _normalize_diff_lines(diff: str) -> list[str]:
    lines = [line.rstrip("\r") for line in re.split(r"\r?\n", diff)]
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _is_done(state: ParserState, prefixes: Sequence[str]) -> bool:
    if state.index >= len(state.lines):
        return True
    if any(state.lines[state.index].startswith(prefix) for prefix in prefixes):
        return True
    return False


def _read_str(state: ParserState, prefix: str) -> str:
    if state.index >= len(state.lines):
        return ""
    current = state.lines[state.index]
    if current.startswith(prefix):
        state.index += 1
        return current[len(prefix) :]
    return ""


def _parse_create_diff(lines: list[str]) -> str:
    parser = ParserState(lines=[*lines, END_PATCH])
    output: list[str] = []

    while not _is_done(parser, SECTION_TERMINATORS):
        if parser.index >= len(parser.lines):
            break
        line = parser.lines[parser.index]
        parser.index += 1
        if not line.startswith("+"):
            raise ValueError(f"Invalid Add File Line: {line}")
        output.append(line[1:])

    return "\n".join(output)


def _parse_update_diff(lines: list[str], input: str) -> ParsedUpdateDiff:
    parser = ParserState(lines=[*lines, END_PATCH])
    input_lines = input.split("\n")
    chunks: list[Chunk] = []
    cursor = 0

    while not _is_done(parser, END_SECTION_MARKERS):
        anchor = _read_str(parser, "@@ ")
        has_bare_anchor = (
            anchor == "" and parser.index < len(parser.lines) and parser.lines[parser.index] == "@@"
        )
        if has_bare_anchor:
            parser.index += 1

        if not (anchor or has_bare_anchor or cursor == 0):
            current_line = parser.lines[parser.index] if parser.index < len(parser.lines) else ""
            raise ValueError(f"Invalid Line:\n{current_line}")

        if anchor.strip():
            cursor = _advance_cursor_to_anchor(anchor, input_lines, cursor, parser)

        section = _read_section(parser.lines, parser.index)
        find_result = _find_context(input_lines, section.next_context, cursor, section.eof)
        if find_result.new_index == -1:
            ctx_text = "\n".join(section.next_context)
            if section.eof:
                raise ValueError(f"Invalid EOF Context {cursor}:\n{ctx_text}")
            raise ValueError(f"Invalid Context {cursor}:\n{ctx_text}")

        cursor = find_result.new_index + len(section.next_context)
        parser.fuzz += find_result.fuzz
        parser.index = section.end_index

        for ch in section.section_chunks:
            chunks.append(
                Chunk(
                    orig_index=ch.orig_index + find_result.new_index,
                    del_lines=list(ch.del_lines),
                    ins_lines=list(ch.ins_lines),
                )
            )

    return ParsedUpdateDiff(chunks=chunks, fuzz=parser.fuzz)


def _advance_cursor_to_anchor(
    anchor: str,
    input_lines: list[str],
    cursor: int,
    parser: ParserState,
) -> int:
    found = False

    if not any(line == anchor for line in input_lines[:cursor]):
        for i in range(cursor, len(input_lines)):
            if input_lines[i] == anchor:
                cursor = i + 1
                found = True
                break

    if not found and not any(line.strip() == anchor.strip() for line in input_lines[:cursor]):
        for i in range(cursor, len(input_lines)):
            if input_lines[i].strip() == anchor.strip():
                cursor = i + 1
                parser.fuzz += 1
                found = True
                break

    return cursor


def _read_section(lines: list[str], start_index: int) -> ReadSectionResult:
    context: list[str] = []
    del_lines: list[str] = []
    ins_lines: list[str] = []
    section_chunks: list[Chunk] = []
    mode: Literal["keep", "add", "delete"] = "keep"
    index = start_index
    orig_index = index

    while index < len(lines):
        raw = lines[index]
        if (
            raw.startswith("@@")
            or raw.startswith(END_PATCH)
            or raw.startswith("*** Update File:")
            or raw.startswith("*** Delete File:")
            or raw.startswith("*** Add File:")
            or raw.startswith(END_FILE)
        ):
            break
        if raw == "***":
            break
        if raw.startswith("***"):
            raise ValueError(f"Invalid Line: {raw}")

        index += 1
        last_mode = mode
        line = raw if raw else " "
        prefix = line[0]
        if prefix == "+":
            mode = "add"
        elif prefix == "-":
            mode = "delete"
        elif prefix == " ":
            mode = "keep"
        else:
            raise ValueError(f"Invalid Line: {line}")

        line_content = line[1:]
        switching_to_context = mode == "keep" and last_mode != mode
        if switching_to_context and (del_lines or ins_lines):
            section_chunks.append(
                Chunk(
                    orig_index=len(context) - len(del_lines),
                    del_lines=list(del_lines),
                    ins_lines=list(ins_lines),
                )
            )
            del_lines = []
            ins_lines = []

        if mode == "delete":
            del_lines.append(line_content)
            context.append(line_content)
        elif mode == "add":
            ins_lines.append(line_content)
        else:
            context.append(line_content)

    if del_lines or ins_lines:
        section_chunks.append(
            Chunk(
                orig_index=len(context) - len(del_lines),
                del_lines=list(del_lines),
                ins_lines=list(ins_lines),
            )
        )

    if index < len(lines) and lines[index] == END_FILE:
        return ReadSectionResult(context, section_chunks, index + 1, True)

    if index == orig_index:
        next_line = lines[index] if index < len(lines) else ""
        raise ValueError(f"Nothing in this section - index={index} {next_line}")

    return ReadSectionResult(context, section_chunks, index, False)


@dataclass
class ContextMatch:
    new_index: int
    fuzz: int


def _find_context(lines: list[str], context: list[str], start: int, eof: bool) -> ContextMatch:
    if eof:
        end_start = max(0, len(lines) - len(context))
        end_match = _find_context_core(lines, context, end_start)
        if end_match.new_index != -1:
            return end_match
        fallback = _find_context_core(lines, context, start)
        return ContextMatch(new_index=fallback.new_index, fuzz=fallback.fuzz + 10000)
    return _find_context_core(lines, context, start)


def _find_context_core(lines: list[str], context: list[str], start: int) -> ContextMatch:
    if not context:
        return ContextMatch(new_index=start, fuzz=0)

    for i in range(start, len(lines)):
        if _equals_slice(lines, context, i, lambda value: value):
            return ContextMatch(new_index=i, fuzz=0)
    for i in range(start, len(lines)):
        if _equals_slice(lines, context, i, lambda value: value.rstrip()):
            return ContextMatch(new_index=i, fuzz=1)
    for i in range(start, len(lines)):
        if _equals_slice(lines, context, i, lambda value: value.strip()):
            return ContextMatch(new_index=i, fuzz=100)

    return ContextMatch(new_index=-1, fuzz=0)


def _equals_slice(
    source: list[str], target: list[str], start: int, map_fn: Callable[[str], str]
) -> bool:
    if start + len(target) > len(source):
        return False
    for offset, target_value in enumerate(target):
        if map_fn(source[start + offset]) != map_fn(target_value):
            return False
    return True


def _apply_chunks(input: str, chunks: list[Chunk]) -> str:
    orig_lines = input.split("\n")
    dest_lines: list[str] = []
    cursor = 0

    for chunk in chunks:
        if chunk.orig_index > len(orig_lines):
            raise ValueError(
                f"applyDiff: chunk.origIndex {chunk.orig_index} > input length {len(orig_lines)}"
            )
        if cursor > chunk.orig_index:
            raise ValueError(
                f"applyDiff: overlapping chunk at {chunk.orig_index} (cursor {cursor})"
            )

        dest_lines.extend(orig_lines[cursor : chunk.orig_index])
        cursor = chunk.orig_index

        if chunk.ins_lines:
            dest_lines.extend(chunk.ins_lines)

        cursor += len(chunk.del_lines)

    dest_lines.extend(orig_lines[cursor:])
    return "\n".join(dest_lines)


__all__ = ["apply_diff"]
