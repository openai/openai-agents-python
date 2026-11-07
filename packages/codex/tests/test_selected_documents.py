"""Exercise selected-file bounds, revisions, and file-worker ownership."""

import asyncio
import hashlib
import os
import threading
from contextlib import suppress
from pathlib import Path

import pytest

from examples.codex import document_workspace
from examples.codex.document_workspace import (
    MAX_FILE_BYTES,
    PAGE_CHARS,
    RevisionConflict,
    SelectedDocuments,
)


def selected(root: Path, name: str = "decoded.txt") -> SelectedDocuments:
    return SelectedDocuments(root=root, selected={"report": name}, writable=["report"])


async def test_unicode_pages_boundary_search_and_bounded_match_continuation(tmp_path: Path):
    source = "😀" * (PAGE_CHARS - 3) + "boundary-needle " + "x" * PAGE_CHARS
    (tmp_path / "decoded.txt").write_bytes(source.encode("utf-8"))
    documents = selected(tmp_path)
    first = await documents.read("report")
    assert len(first["text"]) == PAGE_CHARS
    assert first["next_offset"] == PAGE_CHARS
    second = await documents.read("report", PAGE_CHARS, first["revision"])
    assert first["text"] + second["text"] == source[: 2 * PAGE_CHARS]
    search = await documents.search("report", "boundary-needle")
    assert search["matches"] == []
    assert search["next_offset"] is not None
    found = await documents.search(
        "report", "boundary-needle", search["next_offset"], search["revision"]
    )
    assert found["matches"] == [
        {"offset": PAGE_CHARS - 3, "text": source[PAGE_CHARS - 3 : PAGE_CHARS - 3 + 128]}
    ]
    (tmp_path / "decoded.txt").write_bytes(("hit " * 50).encode())
    matches = await documents.search("report", "hit")
    assert len(matches["matches"]) == 16
    assert all(len(match["text"]) <= 128 for match in matches["matches"])
    assert matches["next_offset"] is not None
    continued = await documents.search("report", "hit", matches["next_offset"], matches["revision"])
    assert continued["matches"][0]["offset"] == 64
    assert await documents.read("report", 1000) == {
        "document": "report",
        "revision": hashlib.sha256(("hit " * 50).encode()).hexdigest(),
        "offset": 1000,
        "text": "",
        "next_offset": None,
    }


async def test_revision_writes_are_serial_and_preserve_bom_newlines_and_unrelated_bytes(tmp_path):
    path = tmp_path / "decoded.txt"
    original = "\ufeffStatus: draft\r\n🌿 Keep this line.\r\n".encode()
    path.write_bytes(original)
    documents = selected(tmp_path)
    page = await documents.read("report")
    assert page["revision"] == hashlib.sha256(original).hexdigest()
    results = await asyncio.gather(
        documents.revise("report", page["revision"], "draft", "reviewed"),
        documents.revise("report", page["revision"], "draft", "stale"),
        return_exceptions=True,
    )
    assert isinstance(results[0], dict) and results[0]["status"] == "saved and reopened"
    assert isinstance(results[1], RevisionConflict)
    assert path.read_bytes() == original.replace(b"draft", b"reviewed")
    with pytest.raises(RevisionConflict):
        await documents.read("report", PAGE_CHARS, page["revision"])
    assert list(tmp_path.glob(".agents-document-*")) == []


async def test_alias_and_writable_authority_are_application_owned(tmp_path):
    path = tmp_path / "decoded.txt"
    path.write_text("selected", encoding="utf-8")
    (tmp_path / "unselected.txt").write_text("private", encoding="utf-8")
    aliases = {"report": "decoded.txt"}
    documents = SelectedDocuments(root=tmp_path, selected=aliases)
    aliases["report"] = "unselected.txt"
    assert (await documents.read("report"))["text"] == "selected"
    with pytest.raises(ValueError, match="did not select"):
        await documents.read("unselected.txt")
    with pytest.raises(ValueError, match="for writing"):
        await documents.revise("report", "unknown", "selected", "bad")
    with pytest.raises(ValueError, match="must be selected"):
        SelectedDocuments(root=tmp_path, selected=aliases, writable=["unknown"])
    with pytest.raises(ValueError, match="selected path"):
        await selected(tmp_path, "../outside.txt").read("report")
    path.unlink()
    try:
        path.symlink_to(tmp_path / "unselected.txt")
    except OSError:
        pytest.skip("This host cannot create symlinks")
    with pytest.raises(ValueError, match="selected path"):
        await documents.read("report")
    assert (tmp_path / "unselected.txt").read_text() == "private"


@pytest.mark.parametrize("kind", ["directory", "invalid-utf8", "oversized"])
async def test_invalid_file_categories_are_rejected(tmp_path, kind):
    path = tmp_path / "decoded.txt"
    if kind == "directory":
        path.mkdir()
    elif kind == "invalid-utf8":
        path.write_bytes(b"\xff\xfe")
    elif kind == "oversized":
        with path.open("wb") as file:
            file.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises((ValueError, OSError)):
        await selected(tmp_path).read("report")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="This host has no FIFO support")
async def test_fifo_rejection_does_not_block_or_leave_a_file_worker(tmp_path):
    path = tmp_path / "decoded.txt"
    os.mkfifo(path)
    reader = asyncio.create_task(selected(tmp_path).read("report"))
    try:
        # A cancelling timeout cannot join a worker stuck opening a FIFO.
        done, _ = await asyncio.wait((reader,), timeout=1)
        assert done, "Reading a selected FIFO blocked instead of rejecting it"
        with pytest.raises((ValueError, OSError)):
            reader.result()
    finally:
        # Keep a writer open until even a regressed blocking open has returned.
        descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            with suppress(ValueError, OSError):
                await reader
        finally:
            os.close(descriptor)


async def test_edits_are_unique_bounded_and_do_not_overgrow_file(tmp_path):
    path = tmp_path / "decoded.txt"
    path.write_bytes(b"twice twice")
    documents = selected(tmp_path)
    revision = (await documents.read("report"))["revision"]
    for old, new in [("", "x"), ("x" * (PAGE_CHARS + 1), "x"), ("twice", "x" * (PAGE_CHARS + 1))]:
        with pytest.raises(ValueError, match="one page"):
            await documents.revise("report", revision, old, new)
    for old in ["missing", "twice"]:
        with pytest.raises(ValueError, match="exactly once"):
            await documents.revise("report", revision, old, "new")
    with path.open("wb") as file:
        file.write(b"unique" + b"x" * (MAX_FILE_BYTES - 6))
    revision = (await documents.read("report"))["revision"]
    with pytest.raises(ValueError, match="exceeds 16 MiB"):
        await documents.revise("report", revision, "unique", "unique-extra")
    assert path.stat().st_size == MAX_FILE_BYTES


@pytest.mark.parametrize("offset", [-1, True, 0.5])
async def test_invalid_page_offsets_are_rejected(tmp_path, offset):
    with pytest.raises(ValueError, match="nonnegative integer"):
        await selected(tmp_path).read("report", offset)


@pytest.mark.parametrize("query", ["", "x" * 257])
async def test_invalid_queries_are_rejected(tmp_path, query):
    with pytest.raises(ValueError, match="1 to 256"):
        await selected(tmp_path).search("report", query)


async def test_cancelled_write_keeps_ownership_until_worker_finishes(tmp_path, monkeypatch):
    path = tmp_path / "decoded.txt"
    path.write_bytes(b"Status: draft\r\n")
    documents = selected(tmp_path)
    revision = (await documents.read("report"))["revision"]
    entering_replace, release_replace = threading.Event(), threading.Event()
    replace = document_workspace.os.replace

    def held_replace(source, target):
        entering_replace.set()
        if not release_replace.wait(5):
            raise TimeoutError("Test did not release the file worker")
        replace(source, target)

    monkeypatch.setattr(document_workspace.os, "replace", held_replace)
    edit = asyncio.create_task(documents.revise("report", revision, "draft", "saved"))
    successor = None
    try:
        assert await asyncio.to_thread(entering_replace.wait, 3)
        edit.cancel()
        await asyncio.sleep(0)
        edit.cancel()
        successor = asyncio.create_task(documents.revise("report", revision, "draft", "other"))
        await asyncio.sleep(0)
        assert not edit.done() and not successor.done()
    finally:
        release_replace.set()
        results = await asyncio.gather(
            *([edit, successor] if successor is not None else [edit]), return_exceptions=True
        )
    assert isinstance(results[0], asyncio.CancelledError)
    assert isinstance(results[1], RevisionConflict)
    assert path.read_bytes() == b"Status: saved\r\n"
    assert list(tmp_path.glob(".agents-document-*")) == []


async def test_unexpected_write_error_propagates_and_cleans_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "decoded.txt"
    path.write_bytes(b"draft")
    documents = selected(tmp_path)
    revision = (await documents.read("report"))["revision"]

    def fail_replace(source, target):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(document_workspace.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic disk failure"):
        await documents.revise("report", revision, "draft", "saved")
    assert path.read_bytes() == b"draft"
    assert list(tmp_path.glob(".agents-document-*")) == []
