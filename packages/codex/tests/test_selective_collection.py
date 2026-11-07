"""Exercise collection boundaries and real native dispatch without a live model."""

import asyncio
import hashlib
import json
import re

import pytest

from agents.testing import ScriptedResponses, text, tool_call
from examples.codex.document_workspace import MAX_FILE_BYTES, PAGE_CHARS
from examples.codex.selective_collection import (
    SEARCH_ITEMS,
    DocumentMetadata,
    SelectiveCollection,
    SyntheticBackend,
    collection_agent,
)
from tests._asyncio import timeout


async def test_metadata_pages_are_bounded_and_never_fetch():
    async def fetch(document_id):
        pytest.fail("metadata search must not fetch a body")

    metadata = [DocumentMetadata(f"id-{i}", f"Report {i}") for i in range(10_000)]
    async with SelectiveCollection(metadata, fetch) as collection:
        seen = []
        offset = 0
        while True:
            page = collection.search("Report", offset)
            assert len(page["items"]) <= SEARCH_ITEMS
            assert all(set(item) == {"document_id", "title"} for item in page["items"])
            seen.extend(item["document_id"] for item in page["items"])
            offset = page["next_offset"]
            if offset is None:
                break
        assert seen == [item.document_id for item in metadata]
        assert await collection.read("../../private") == {"status": "denied"}
        assert collection.search("", 0) == {"status": "invalid_request"}
        assert collection.search("Report", -1) == {"status": "invalid_request"}


@pytest.mark.parametrize(
    "failure, expected",
    [
        (PermissionError("secret credential at /private/backend"), "denied"),
        (FileNotFoundError("secret credential at /private/backend"), "unavailable"),
        (RuntimeError("secret credential at /private/backend"), "unavailable"),
        (b"\xff", "unavailable"),
        (b"x" * (MAX_FILE_BYTES + 1), "unavailable"),
    ],
)
async def test_fetch_failures_are_sanitized_and_retryable(failure, expected):
    calls = 0

    async def fetch(document_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return b"Recovered source"

    async with SelectiveCollection([DocumentMetadata("one", "Report")], fetch) as collection:
        assert await collection.read("one") == {"status": expected}
        assert list(collection._root.iterdir()) == []
        assert (await collection.read("one"))["text"] == "Recovered source"
        assert calls == 2


async def test_concurrent_reads_stage_once_and_preserve_revision_and_utf8():
    calls = []
    started, release = asyncio.Event(), asyncio.Event()
    source = ("😀" * PAGE_CHARS + "final page").encode()

    async def fetch(document_id):
        calls.append(document_id)
        started.set()
        await release.wait()
        return source

    # Even a path-shaped authorized ID never controls the generated filename.
    document_id = "../../opaque-id"
    async with SelectiveCollection([DocumentMetadata(document_id, "Report")], fetch) as collection:
        root = collection._root
        first = asyncio.create_task(collection.read(document_id))
        await started.wait()
        second = asyncio.create_task(collection.read(document_id))
        release.set()
        page, duplicate = await asyncio.gather(first, second)
        assert duplicate == page
        assert calls == [document_id]
        assert page["text"] == "😀" * PAGE_CHARS
        assert page["revision"] == hashlib.sha256(source).hexdigest()
        final = await collection.read(document_id, page["next_offset"], page["revision"])
        assert final["text"] == "final page"
        assert final["next_offset"] is None
        assert (await collection.read(document_id, 0, "stale"))["status"] == "stale_revision"
        assert [path.name for path in root.iterdir()] == ["document-00000.txt"]
    assert not root.exists()
    assert await collection.read(document_id) == {"status": "unavailable"}


async def test_cancelled_fetch_is_not_staged_and_close_joins_inflight_work():
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch(document_id):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return b"source"

    async with timeout(10):
        collection = SelectiveCollection([DocumentMetadata("one", "Report")], fetch)
        root = collection._root
        pending = asyncio.create_task(collection.read("one"))
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert list(root.iterdir()) == []
        started.clear()
        retry = asyncio.create_task(collection.read("one"))
        await started.wait()
        closing = asyncio.create_task(collection.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert root.exists()
        assert not closing.done()
        release.set()
        assert (await retry)["text"] == "source"
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert calls == 2
        assert not root.exists()


async def test_partial_staging_failure_removes_file_and_retries(monkeypatch):
    calls = []

    async def fetch(document_id):
        calls.append(document_id)
        return b"Complete decoded source"

    async with SelectiveCollection([DocumentMetadata("one", "Report")], fetch) as collection:
        path_type = type(collection._root)
        original_write = path_type.write_bytes

        def fail_after_partial_write(path, content):
            original_write(path, content[:3])
            raise OSError("private staging path must not reach the model")

        with monkeypatch.context() as patch:
            patch.setattr(path_type, "write_bytes", fail_after_partial_write)
            assert await collection.read("one") == {"status": "unavailable"}
        assert list(collection._root.iterdir()) == []
        assert (await collection.read("one"))["text"] == "Complete decoded source"
        assert calls == ["one", "one"]


async def test_native_search_selected_pages_answer_and_failures(tmp_path):
    backend = SyntheticBackend(tmp_path / "sources")
    metadata = [
        *backend.metadata,
        DocumentMetadata("denied", "Restricted report"),
        DocumentMetadata("missing", "Missing report"),
    ]
    fetch_attempts = []

    async def fetch(document_id):
        fetch_attempts.append(document_id)
        if document_id == "denied":
            raise PermissionError("secret token /backend/private")
        if document_id == "missing":
            raise OSError("secret token /backend/private")
        return await backend.fetch(document_id)

    def read(document_id, call_id, offset=0, revision=None):
        return [
            tool_call(
                "read_collection_document",
                {"document_id": document_id, "offset": offset, "revision": revision},
                call_id=call_id,
            )
        ]

    east_revision = hashlib.sha256(backend.paths["doc-00007"].read_bytes()).hexdigest()
    scripts = [
        [tool_call("search_collection", {"query": "Archive", "offset": 0}, call_id="catalog")],
        [
            tool_call(
                "search_collection", {"query": "Archive", "offset": 21}, call_id="catalog-next"
            )
        ],
        [tool_call("search_collection", {"query": "Orchid", "offset": 0}, call_id="search")],
        read("doc-00007", "east"),
        read("doc-00007", "east-next", PAGE_CHARS, east_revision),
        read("doc-09001", "west"),
        read("doc-00007", "repeat"),
        read("doc-00007", "stale", 0, "old-revision"),
        read("../../private", "unknown"),
        read("denied", "denied"),
        read("missing", "missing"),
        [
            text(
                "Orchid revenue is 42 million (doc-00007 + doc-09001). "
                "Restricted and missing sources were denied and unavailable."
            )
        ],
    ]
    async with timeout(30), SelectiveCollection(metadata, fetch) as collection:
        async with ScriptedResponses(*scripts) as peer:
            result = await collection_agent(collection, peer.provider).run("Sum Orchid revenue.")
            catalog = json.loads(peer.tool_output("catalog"))
            continued = json.loads(peer.tool_output("catalog-next"))
            assert len(catalog["items"]) == len(continued["items"]) == SEARCH_ITEMS
            assert catalog["next_offset"] == 21
            assert continued["items"][0]["document_id"] == "doc-00021"
            assert continued["next_offset"] == 41
            search = json.loads(peer.tool_output("search"))
            assert search == {
                "items": [
                    {"document_id": "doc-00007", "title": "Orchid revenue east"},
                    {"document_id": "doc-09001", "title": "Orchid revenue west"},
                ],
                "next_offset": None,
            }
            pages = [json.loads(peer.tool_output(key)) for key in ("east", "east-next", "west")]
            assert all(len(page["text"]) <= PAGE_CHARS for page in pages)
            assert pages[0]["next_offset"] == PAGE_CHARS
            assert pages[1]["next_offset"] is None
            assert pages[0]["text"] + pages[1]["text"] == backend.paths["doc-00007"].read_text()
            values = [
                int(re.search(r"revenue: (\d+)", page["text"])[1]) for page in (pages[0], pages[2])
            ]
            assert sum(values) == 42
            assert "42 million (doc-00007 + doc-09001)" in result.final_output
            assert json.loads(peer.tool_output("repeat")) == pages[0]
            assert json.loads(peer.tool_output("unknown")) == {"status": "denied"}
            assert json.loads(peer.tool_output("denied")) == {"status": "denied"}
            assert json.loads(peer.tool_output("missing")) == {"status": "unavailable"}
            assert json.loads(peer.tool_output("stale"))["status"] == "stale_revision"
            wire = json.dumps(peer.requests)
            assert "secret token" not in wire
            assert str(tmp_path) not in wire
            assert "Unrelated synthetic report" not in wire
            assert "revise_document" not in wire
            assert "search_document" not in wire
        assert fetch_attempts == ["doc-00007", "doc-09001", "denied", "missing"]
        assert backend.transfers == [
            {"filename": backend.paths[key].name, "bytes": backend.paths[key].stat().st_size}
            for key in ("doc-00007", "doc-09001")
        ]
        assert sorted(path.name for path in collection._root.iterdir()) == [
            "document-00007.txt",
            "document-09001.txt",
        ]
