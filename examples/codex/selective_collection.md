# Read selected sources from a collection

[`selective_collection.py`](selective_collection.py) searches an application-authorized catalog and fetches only the document bodies the agent selects. The runnable fixture contains 10,000 local text files. Two Orchid reports contain revenue of 17 and 25 million; the requested total is 42 million.

From a source checkout with the SDK and its matching native extension installed, run:

```sh
python -m examples.codex.selective_collection
```

This command uses `NativeProvider` and needs the normal model credentials. Fixture creation writes 10,000 source files before the agent starts. Those files represent the backend, not a sandbox copy. The example prints the answer and the source filenames and byte counts fetched by the application. Follow the repository's contributor setup for source checkouts; this recipe adds no dependencies.

## Connect a backend

The trusted application supplies `DocumentMetadata(document_id, title)` entries and an async `fetch(document_id) -> bytes` callback. Give the catalog only IDs and titles the caller may discover. Keep IDs opaque; do not put credentials or private paths in either field. IDs must be unique and contain 1–128 characters. Titles must contain 1–256 characters.

The callback keeps credentials, backend paths, and access checks inside the application. Return decoded UTF-8 bytes of at most 16 MiB. Extract text from PDF or Office files before returning it. Raise `PermissionError` for a denied source; other backend failures produce an unavailable result. Do not write staging files in the callback or detach work that continues after cancellation.

```python
from agents import NativeProvider
from examples.codex.selective_collection import (
    DocumentMetadata,
    SelectiveCollection,
    collection_agent,
)


async def answer_question():
    # Replace this synthetic callback with your authenticated backend client.
    async def fetch(document_id: str) -> bytes:
        if document_id != "report":
            raise PermissionError()
        return b"Orchid revenue: 17 million.\n"

    catalog = [DocumentMetadata("report", "Orchid revenue")]
    async with SelectiveCollection(catalog, fetch) as collection:
        async with NativeProvider() as provider:
            result = await collection_agent(collection, provider).run(
                "What revenue does the Orchid report state?"
            )
            return result.final_output
```

Close all agent Sessions before leaving the collection context. The provider context above closes before staging cleanup. The collection owns its private temporary directory and generated filenames; model arguments never select paths. One lock serializes staging and reading, so concurrent first reads fetch a source once. Failed or cancelled fetches do not mark a source as staged. Closing waits for an in-flight read before removing the temporary files, including when close is cancelled. The callback must cooperate with cancellation.

## Tool results and limits

The agent receives two ordinary native function tools:

- `search_collection(query, offset)` searches titles without fetching bodies. It returns at most 20 ID/title pairs and a `next_offset`. Queries contain 1–128 characters. Continue until `next_offset` is null when more results are needed.
- `read_collection_document(document_id, offset, revision)` fetches an authorized source on its first read and stages it once. It delegates page reads to `SelectedDocuments`. Each page contains at most 4,096 Unicode characters, a revision, and a `next_offset`. Continue with the returned revision. On `stale_revision`, restart at offset zero with revision null.

Unknown IDs return `{"status": "denied"}` before the application calls the backend. A backend `PermissionError` returns the same status. Other fetch, UTF-8, size, or file failures return `{"status": "unavailable"}` without exception details. Invalid offsets or queries return `{"status": "invalid_request"}`. Cancellation propagates to the caller. The tools do not expose document edits or content search.

These bounds limit model output, not selected-file I/O or memory use: `SelectedDocuments` loads, decodes, and hashes the full staged file on every page. Metadata discovery uses an ordinary linear scan. This example does not provide an index, a global context budget, or a cloud storage adapter.

Each collection keeps a fixed metadata snapshot. Each successfully fetched body stays available for that collection's lifetime. Bodies reflect their first fetch, not an atomic snapshot of the whole backend. Create a fresh collection and Sessions when access rights or source versions change. This recipe does not enforce live entitlement revocation. Keep staging under application control and do not let other processes modify it.

## Reproduce local measurements

```sh
python -m examples.codex.selective_collection_benchmark > collection-measurements.json
```

The benchmark needs no model credentials. It alternates five selected and five bulk trials over the same 10,000-file fixture. Each side reads the two selected pages and checks the total against 42. The selected side fetches two sources into private staging. The bulk baseline copies every authorized source before reading the same selected pages through `SelectedDocuments`. JSON records each actual source and staged filename, bytes, elapsed seconds, answers, failure counts, and the median and range for each side. An error stops the run instead of producing a successful report.

Fixture creation, result checks, manifest enumeration, and scratch cleanup are outside the timed staging regions. Timings include directory setup, copying or fetching, metadata selection, and selected-page reads. Byte counts describe body transfers at this local backend/staging boundary; they exclude metadata and model requests. OS file caches may be warm, and the host may run other work.

Five separate native workflow trials use `ScriptedResponses` to drive search, selection, reads, and a fixed answer through the real native Agent. Their elapsed times cover `Agent.run`, including Session cleanup, but exclude peer startup and collection setup. The script checks observed tool results against the answer. This verifies dispatch and data flow; it does not evaluate model reasoning, live cloud throughput, customer workloads, or a production speedup.

Focused tests also check bounded continuation pages, unknown IDs, backend denial, unavailable sources, revision conflicts, concurrent first reads, and cancellation:

```sh
python -m pytest -q packages/codex/tests/test_selective_collection.py
```
