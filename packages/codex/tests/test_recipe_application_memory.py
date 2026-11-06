"""Persist scoped facts through public tools and fresh native Sessions."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from agents.testing import ScriptedResponses, text, tool_call
from examples.codex.recipes.application_memory import MemoryScope, initialize, memory_agent
from tests._asyncio import timeout
from tests.test_native_agent import function_declarations


async def test_fact_survives_fresh_sessions_is_scoped_and_explicitly_deleted(
    tmp_path: Path,
) -> None:
    database = tmp_path / "facts.sqlite"
    initialize(database)
    alice = MemoryScope(database, "alice")
    bob = MemoryScope(database, "bob")
    script = [
        [tool_call("save_fact", {"fact": "Favorite color: blue"}, call_id="save-alice")],
        [text("Saved")],
        [tool_call("recall_fact", {}, call_id="recall-alice")],
        [text("Blue")],
        [tool_call("recall_fact", {}, call_id="recall-bob")],
        [text("No saved fact")],
        [tool_call("save_fact", {"fact": "Favorite color: red"}, call_id="save-bob")],
        [text("Saved")],
        [tool_call("forget_fact", {}, call_id="delete-alice")],
        [text("Deleted")],
        [tool_call("forget_fact", {}, call_id="delete-again")],
        [text("Deleted")],
        [tool_call("recall_fact", {}, call_id="recall-deleted")],
        [text("No saved fact")],
        [tool_call("recall_fact", {}, call_id="recall-bob-after")],
        [text("Red")],
    ]
    session_ids = []
    async with ScriptedResponses(*script) as peer, timeout(30):
        agent = memory_agent(peer.provider)
        for scope, prompt in [
            (alice, "Save Favorite color: blue"),
            (alice, "Recall my fact"),
            (bob, "Recall Alice's fact"),
            (bob, "Save Favorite color: red"),
            (alice, "Delete my fact"),
            (alice, "Delete my fact again"),
            (alice, "Recall my fact"),
            (bob, "Recall my fact"),
        ]:
            # No saved state is read or passed into these fresh Sessions.
            async with agent.session(context=scope) as session:
                session_ids.append(session.id)
                await (await session.submit(prompt)).get_result()
        assert len(set(session_ids)) == 8
        assert peer.tool_output("recall-alice") == "Favorite color: blue"
        assert json.loads(peer.tool_output("recall-bob")) is None
        assert json.loads(peer.tool_output("recall-deleted")) is None
        assert peer.tool_output("recall-bob-after") == "Favorite color: red"
        # Current identity is application context, never a model argument.
        for tool in function_declarations(peer.requests[0]):
            assert "user_id" not in tool.get("parameters", {}).get("properties", {})
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT user_id, fact FROM facts").fetchall() == [
            ("bob", "Favorite color: red")
        ]
