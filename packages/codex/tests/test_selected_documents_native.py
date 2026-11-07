"""Drive the selected-document recipe through actual native function dispatch."""

import hashlib
import json

from agents.testing import ScriptedResponses, text, tool_call
from examples.codex.document_workspace import PAGE_CHARS, SelectedDocuments
from examples.codex.selected_documents import document_agent
from tests._asyncio import timeout


async def test_native_document_pages_search_edit_conflict_and_saved_history(tmp_path):
    source = "\ufeff" + "😀" * (PAGE_CHARS - 4) + "boundary-needle\r\nStatus: draft\r\n"
    original = source.encode("utf-8")
    path = tmp_path / "decoded.txt"
    path.write_bytes(original)
    unselected = tmp_path / "unselected.txt"
    unselected.write_bytes(b"private decoy never reaches the model")
    revision = hashlib.sha256(original).hexdigest()
    documents = SelectedDocuments(
        root=tmp_path, selected={"report": "decoded.txt"}, writable=["report"]
    )
    scripts = [
        [
            tool_call(
                "read_document",
                {"document": "report", "offset": 0, "revision": None},
                call_id="page",
            )
        ],
        [
            tool_call(
                "search_document",
                {
                    "document": "report",
                    "query": "boundary-needle",
                    "offset": 0,
                    "revision": revision,
                },
                call_id="first-search",
            )
        ],
        [
            tool_call(
                "search_document",
                {
                    "document": "report",
                    "query": "boundary-needle",
                    "offset": PAGE_CHARS - len("boundary-needle") + 1,
                    "revision": revision,
                },
                call_id="continued-search",
            )
        ],
        [
            tool_call(
                "revise_document",
                {
                    "document": "report",
                    "revision": revision,
                    "old_text": "Status: draft",
                    "new_text": "Status: reviewed",
                },
                call_id="edit",
            )
        ],
        [
            tool_call(
                "revise_document",
                {
                    "document": "report",
                    "revision": revision,
                    "old_text": "reviewed",
                    "new_text": "stale",
                },
                call_id="stale",
            )
        ],
        [text("Saved the reviewed status.")],
    ]
    async with timeout(30), ScriptedResponses(*scripts) as peer:
        async with document_agent(documents, peer.provider).session() as session:
            result = await (
                await session.submit("Find the phrase and revise the status.")
            ).get_result()
            history = [item async for item in session.items()]
        assert result.final_output == "Saved the reviewed status."
        page = json.loads(peer.tool_output("page"))
        assert page == {
            "document": "report",
            "revision": revision,
            "offset": 0,
            "text": source[:PAGE_CHARS],
            "next_offset": PAGE_CHARS,
        }
        assert json.loads(peer.tool_output("first-search"))["matches"] == []
        assert json.loads(peer.tool_output("continued-search"))["matches"] == [
            {"offset": PAGE_CHARS - 3, "text": source[PAGE_CHARS - 3 :]}
        ]
        saved = json.loads(peer.tool_output("edit"))
        expected = original.replace(b"Status: draft", b"Status: reviewed")
        assert saved == {
            "document": "report",
            "revision": hashlib.sha256(expected).hexdigest(),
            "status": "saved and reopened",
        }
        assert "Restart read_document" in peer.tool_output("stale")
        outputs = {
            item["call_id"]: item for item in history if item["type"] == "function_call_output"
        }
        assert set(outputs) == {"page", "first-search", "continued-search", "edit", "stale"}
        assert "saved and reopened" in json.dumps(outputs["edit"])
        assert "Restart read_document" in json.dumps(outputs["stale"])
        assert "private decoy" not in json.dumps(peer.requests)
    assert path.read_bytes() == expected
    assert unselected.read_bytes() == b"private decoy never reaches the model"
