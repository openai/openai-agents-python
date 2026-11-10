"""Packaged scoped facts survive fully closed native Sessions and stores."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agents import Agent
from agents.memory import SQLiteMemory
from agents.testing import ScriptedResponses, text, tool_call
from tests.test_native_agent import function_declarations


async def test_native_memory_reopens_scopes_and_durably_deletes(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite"
    async with ScriptedResponses(
        [tool_call("memory_set", {"key": "preference", "value": "Plain language"}, call_id="save")],
        [text("Saved")],
        [tool_call("memory_get", {"key": "preference"}, call_id="recall")],
        [text("Recalled")],
        [tool_call("memory_get", {"key": "preference"}, call_id="other-scope")],
        [text("Absent")],
        [tool_call("memory_delete", {"key": "preference"}, call_id="delete")],
        [text("Deleted")],
        [tool_call("memory_get", {"key": "preference"}, call_id="after-delete")],
        [text("Absent")],
    ) as peer:
        for scope, prompt in [
            ("alice", "Save"),
            ("alice", "Recall"),
            ("bob", "Recall"),
            ("alice", "Delete"),
            ("alice", "Recall"),
        ]:
            with SQLiteMemory(path, scope=scope) as memory:
                await Agent(
                    instructions="Use memory.", tools=memory.tools(), provider=peer.provider
                ).run(prompt)
        assert peer.tool_output("recall") == "Plain language"
        assert peer.tool_output("other-scope") == "null"
        assert peer.tool_output("after-delete") == "null"
        for declaration in function_declarations(peer.requests[0]):
            assert set(declaration["parameters"]["properties"]) <= {"key", "value"}
            assert declaration["parameters"]["additionalProperties"] is False
    with SQLiteMemory(path, scope="alice") as reopened:
        assert reopened.get("preference") is None


async def test_parallel_native_memory_writes_preserve_distinct_keys(tmp_path: Path) -> None:
    with SQLiteMemory(tmp_path / "memory.sqlite", scope="alice") as memory:
        async with ScriptedResponses(
            [
                tool_call(
                    "memory_set",
                    {"key": f"key-{index}", "value": f"value-{index}"},
                    call_id=f"save-{index}",
                )
                for index in range(8)
            ],
            [text("Saved")],
        ) as peer:
            await Agent(instructions="Save.", tools=memory.tools(), provider=peer.provider).run(
                "Save"
            )
        assert [memory.get(f"key-{index}") for index in range(8)] == [
            f"value-{index}" for index in range(8)
        ]


def test_scoped_sql_parameters_bounds_and_connection_lifetime(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite"
    with SQLiteMemory(path, scope="alice") as memory, SQLiteMemory(path, scope="bob") as other:
        key = "x'; DELETE FROM agents_memory; --"
        memory.set(key, "")
        other.set(key, "other")
        assert memory.get(key) == ""
        memory.delete(key)
        memory.delete(key)
        assert other.get(key) == "other"
        with pytest.raises(AttributeError):
            memory.scope = "bob"  # type: ignore[misc]
        for invalid in ["", " ", "x" * 257]:
            with pytest.raises(ValueError):
                memory.set(invalid, "value")
        with pytest.raises(ValueError):
            memory.set("key", "x" * 65_537)
        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(lambda index: memory.set(f"worker-{index}", str(index)), range(16)))
        assert all(memory.get(f"worker-{index}") == str(index) for index in range(16))
    memory.close()
    with pytest.raises(RuntimeError, match="closed"):
        memory.get("key")
    with pytest.raises(ValueError):
        SQLiteMemory(path, scope=" ")


@pytest.mark.parametrize(
    ("malformed", "replacement"),
    [
        ("\ud800", "\ufffd"),
        ("\udfff", "\ufffd"),
        ("before\ud800after", "before\ufffdafter"),
    ],
    ids=["high-surrogate", "low-surrogate", "embedded-surrogate"],
)
def test_malformed_unicode_fails_before_database_open_or_sql(
    tmp_path: Path, malformed: str, replacement: str
) -> None:
    unopened = tmp_path / "unopened.sqlite"
    with pytest.raises(ValueError, match="well-formed Unicode"):
        SQLiteMemory(unopened, scope=malformed)
    assert not unopened.exists()
    path = tmp_path / "memory.sqlite"
    with SQLiteMemory(path, scope=replacement) as protected:
        protected.set("secret", "Protected scope value")
        with pytest.raises(ValueError, match="well-formed Unicode"):
            with SQLiteMemory(path, scope=malformed) as invalid:
                invalid.get("secret")
        with SQLiteMemory(path, scope="alice") as memory:
            memory.set(replacement, "Protected key value")
            memory.set("value", "Previous value")
            with pytest.raises(ValueError, match="well-formed Unicode"):
                memory.get(malformed)
            with pytest.raises(ValueError, match="well-formed Unicode"):
                memory.set(malformed, "Overwrite")
            with pytest.raises(ValueError, match="well-formed Unicode"):
                memory.delete(malformed)
            assert memory.get(replacement) == "Protected key value"
            with pytest.raises(ValueError, match="well-formed Unicode"):
                memory.set("value", malformed)
            assert memory.get("value") == "Previous value"
        assert protected.get("secret") == "Protected scope value"


def test_valid_unicode_and_empty_values_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite"
    scope = "user-\0\U0001f680-\ufffd"
    key = "key-\0\U0001d11e-\ufffd"
    value = "value-\0\U0001f600-\ufffd"
    with SQLiteMemory(path, scope=scope) as memory:
        memory.set(key, value)
        memory.set("empty", "")
    with SQLiteMemory(path, scope=scope) as reopened:
        assert reopened.scope == scope
        assert reopened.get(key) == value
        assert reopened.get("empty") == ""
    with SQLiteMemory(path, scope="user-\0\ufffd-\ufffd") as different_scope:
        assert different_scope.get(key) is None
