"""Bounded tools over application-selected decoded UTF-8 documents."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from typing_extensions import TypedDict

from agents import ToolOutput, function_tool

PAGE_CHARS = 4096
MAX_FILE_BYTES = 16 * 1024 * 1024
T = TypeVar("T")


class RevisionConflict(ValueError):
    """The caller must read again before continuing or editing."""


class DocumentPage(TypedDict):
    document: str
    revision: str
    offset: int
    text: str
    next_offset: int | None


class SearchMatch(TypedDict):
    offset: int
    text: str


class SearchPage(TypedDict):
    document: str
    revision: str
    matches: list[SearchMatch]
    next_offset: int | None


class SavedDocument(TypedDict):
    document: str
    revision: str
    status: str


@dataclass(frozen=True)
class _Document:
    path: Path
    text: str
    mode: int
    revision: str


class SelectedDocuments:
    """The application chooses aliases, paths, and permission to write.

    Supply decoded text, including text extracted from a PDF. Keep the root
    and its parent directories under application control. External writers
    must share an application lock with this owner.
    """

    def __init__(
        self,
        *,
        root: Path,
        selected: Mapping[str, str],
        writable: Sequence[str] = (),
    ) -> None:
        self._root = root.resolve(strict=True)
        self._selected = dict(selected)
        self._writable = frozenset(writable)
        if not self._writable.issubset(self._selected):
            raise ValueError("Writable documents must be selected by the application")
        self._lock = asyncio.Lock()

    async def _exclusive(self, operation: Callable[[], T]) -> T:
        async with self._lock:
            worker = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Join file work before releasing ownership, even after repeated cancellation.
                while not worker.done():
                    with suppress(asyncio.CancelledError):
                        await asyncio.wait((worker,))
                worker.exception()
                raise

    def _load(self, alias: str) -> _Document:
        if alias not in self._selected:
            raise ValueError("The application did not select this document")
        path = Path(os.path.abspath(self._root / self._selected[alias]))
        if (
            path == self._root
            or not path.is_relative_to(self._root)
            or path.resolve(strict=True) != path
        ):
            raise ValueError("Selected document must remain at its selected path")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as file:
            details = os.fstat(file.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_FILE_BYTES:
                raise ValueError("Select a regular text file of at most 16 MiB")
            content = file.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("Selected document grew beyond 16 MiB")
        return _Document(
            path,
            content.decode("utf-8"),
            stat.S_IMODE(details.st_mode),
            hashlib.sha256(content).hexdigest(),
        )

    async def read(self, alias: str, offset: int = 0, revision: str | None = None) -> DocumentPage:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Offset must be a nonnegative integer")

        def read_page() -> DocumentPage:
            document = self._load(alias)
            if revision is not None and revision != document.revision:
                raise RevisionConflict("Stale revision; restart the read")
            end = offset + PAGE_CHARS
            return {
                "document": alias,
                "revision": document.revision,
                "offset": offset,
                "text": document.text[offset:end],
                "next_offset": end if end < len(document.text) else None,
            }

        return await self._exclusive(read_page)

    async def search(
        self, alias: str, query: str, offset: int = 0, revision: str | None = None
    ) -> SearchPage:
        if not 1 <= len(query) <= 256:
            raise ValueError("Search text must contain 1 to 256 code points")
        page = await self.read(alias, offset, revision)
        matches: list[SearchMatch] = []
        cursor = 0
        while cursor <= len(page["text"]) - len(query) and len(matches) < 16:
            found = page["text"].find(query, cursor)
            if found < 0:
                break
            matches.append({"offset": offset + found, "text": page["text"][found : found + 128]})
            cursor = found + len(query)
        next_offset = (
            offset + cursor
            if len(matches) == 16
            else None
            if page["next_offset"] is None
            else offset + len(page["text"]) - len(query) + 1
        )
        return {
            "document": alias,
            "revision": page["revision"],
            "matches": matches,
            "next_offset": next_offset,
        }

    async def revise(
        self, alias: str, revision: str, old_text: str, new_text: str
    ) -> SavedDocument:
        if alias not in self._writable:
            raise ValueError("The application did not select this document for writing")
        if not old_text or len(old_text) > PAGE_CHARS or len(new_text) > PAGE_CHARS:
            raise ValueError("Select one nonempty edit of at most one page")

        def save() -> SavedDocument:
            before = self._load(alias)
            if before.revision != revision:
                raise RevisionConflict("Stale revision; read before editing")
            first = before.text.find(old_text)
            if first < 0 or before.text.find(old_text, first + 1) >= 0:
                raise ValueError("Edit text must occur exactly once")
            after = before.text[:first] + new_text + before.text[first + len(old_text) :]
            content = after.encode("utf-8")
            if len(content) > MAX_FILE_BYTES:
                raise ValueError("Edited document exceeds 16 MiB")
            descriptor, name = tempfile.mkstemp(prefix=".agents-document-", dir=before.path.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                os.chmod(temporary, before.mode)
                if self._load(alias).revision != before.revision:
                    raise RevisionConflict("Document changed before the write")
                os.replace(temporary, before.path)
                saved = self._load(alias)
                if saved.text != after:
                    raise OSError("Artifact verification failed")
                return {
                    "document": alias,
                    "revision": saved.revision,
                    "status": "saved and reopened",
                }
            finally:
                temporary.unlink(missing_ok=True)

        return await self._exclusive(save)

    def tools(self):
        """Expose aliases and bounded operations through ordinary function tools."""

        @function_tool
        async def read_document(
            document: str, offset: int, revision: str | None
        ) -> DocumentPage | ToolOutput:
            """Read a selected page; continue with its next_offset and revision."""
            try:
                return await self.read(document, offset, revision)
            except RevisionConflict:
                return _revision_retry()

        @function_tool
        async def search_document(
            document: str, query: str, offset: int, revision: str | None
        ) -> SearchPage | ToolOutput:
            """Search one selected page; follow next_offset until it is null."""
            try:
                return await self.search(document, query, offset, revision)
            except RevisionConflict:
                return _revision_retry()

        @function_tool
        async def revise_document(
            document: str, revision: str, old_text: str, new_text: str
        ) -> SavedDocument | ToolOutput:
            """Replace unique text in a writable document using its current revision."""
            try:
                return await self.revise(document, revision, old_text, new_text)
            except RevisionConflict:
                return _revision_retry()

        return [read_document, search_document, revise_document]


def _revision_retry() -> ToolOutput:
    return ToolOutput.error(
        "The document changed. Restart read_document at offset 0 with revision null, "
        "then use the returned revision for later reads or edits."
    )
