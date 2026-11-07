"""Matched local staging measurements; no cloud throughput or model reasoning claim."""

import asyncio
import json
import re
import shutil
import statistics
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from agents.testing import ScriptedResponses, text, tool_call
from examples.codex.document_workspace import SelectedDocuments
from examples.codex.selective_collection import (
    SelectiveCollection,
    SyntheticBackend,
    collection_agent,
)

SELECTED = ("doc-00007", "doc-09001")
ANSWER = "Orchid revenue: 42 million (doc-00007 + doc-09001)."


def answer_from_pages(pages: list[dict]) -> int:
    return sum(int(re.search(r"revenue: (\d+)", page["text"])[1]) for page in pages)


def manifest(root: Path) -> list[dict]:
    return [
        {"filename": path.name, "bytes": path.stat().st_size} for path in sorted(root.iterdir())
    ]


async def selected_trial(backend: SyntheticBackend) -> dict:
    backend.transfers.clear()
    start = perf_counter()
    async with SelectiveCollection(backend.metadata, backend.fetch) as collection:
        found = collection.search("Orchid")
        ids = [item["document_id"] for item in found["items"]]
        pages = [await collection.read(key) for key in ids]
        elapsed = perf_counter() - start
        staged = manifest(collection._root)
    answer = answer_from_pages(pages)
    assert ids == list(SELECTED) and answer == 42
    assert len(staged) == len(backend.transfers) == 2
    return {
        "mode": "selected",
        "elapsed_seconds": elapsed,
        "source_files": list(backend.transfers),
        "staged_files": staged,
        "answer_million": answer,
        "failures": 0,
    }


async def bulk_trial(backend: SyntheticBackend) -> dict:
    # The same authorized source bytes and selected-page task, after a full copy.
    start = perf_counter()
    with TemporaryDirectory(prefix="agents-bulk-") as directory:
        root = Path(directory)
        names = {}
        transfers = []
        for index, (key, source) in enumerate(backend.paths.items()):
            names[key] = f"document-{index:05d}.txt"
            destination = root / names[key]
            shutil.copyfile(source, destination)
            transfers.append({"filename": source.name, "bytes": destination.stat().st_size})
        documents = SelectedDocuments(root=root, selected=names)
        # Both sides use the same authorized metadata selection and page reader.
        ids = [item.document_id for item in backend.metadata if "Orchid" in item.title]
        pages = [await documents.read(key) for key in ids]
        elapsed = perf_counter() - start
        staged = manifest(root)
    answer = answer_from_pages(pages)
    assert ids == list(SELECTED) and answer == 42
    assert len(staged) == len(transfers) == len(backend.metadata)
    return {
        "mode": "bulk",
        "elapsed_seconds": elapsed,
        "source_files": transfers,
        "staged_files": staged,
        "answer_million": answer,
        "failures": 0,
    }


async def native_trial(backend: SyntheticBackend) -> dict:
    backend.transfers.clear()
    scripts = [
        [tool_call("search_collection", {"query": "Orchid", "offset": 0}, call_id="search")],
        *[
            [
                tool_call(
                    "read_collection_document",
                    {"document_id": key, "offset": 0, "revision": None},
                    call_id=key,
                )
            ]
            for key in SELECTED
        ],
        [text(ANSWER)],
    ]
    async with SelectiveCollection(backend.metadata, backend.fetch) as collection:
        async with ScriptedResponses(*scripts) as peer:
            start = perf_counter()
            result = await collection_agent(collection, peer.provider).run("Sum Orchid revenue.")
            elapsed = perf_counter() - start
            pages = [json.loads(peer.tool_output(key)) for key in SELECTED]
            assert answer_from_pages(pages) == 42 and result.final_output == ANSWER
            assert [
                item["document_id"] for item in json.loads(peer.tool_output("search"))["items"]
            ] == list(SELECTED)
    assert len(backend.transfers) == 2
    return {
        "workflow_seconds": elapsed,
        "source_files": list(backend.transfers),
        "output": result.final_output,
        "failures": 0,
    }


async def main() -> None:
    with TemporaryDirectory() as directory:
        # Fixture creation is deliberately outside every timed region.
        backend = SyntheticBackend(Path(directory) / "sources")
        trials = []
        native_trials = []
        for trial in range(5):
            # A/B/A/B: each side runs five times against the same local fixture.
            for operation in (selected_trial, bulk_trial):
                record = await operation(backend)
                record["trial"] = trial + 1
                record["source_bytes"] = sum(item["bytes"] for item in record["source_files"])
                record["staged_bytes"] = sum(item["bytes"] for item in record["staged_files"])
                trials.append(record)
            native_trials.append(await native_trial(backend))
        summary = {}
        for mode in ("selected", "bulk"):
            values = [item["elapsed_seconds"] for item in trials if item["mode"] == mode]
            summary[mode] = {
                "median_seconds": statistics.median(values),
                "min_seconds": min(values),
                "max_seconds": max(values),
            }
        print(
            json.dumps(
                {
                    "scope": "Local synthetic fixture and staging only; warm OS caches; no live model or cloud throughput claim",
                    "metadata_entries": len(backend.metadata),
                    "summary": summary,
                    "trials": trials,
                    "native_workflow_trials": native_trials,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
