from __future__ import annotations

import asyncio
import json
import os
import tarfile
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event

import pytest

from agents import Agent, Environment, NativeError, function_tool
from agents.sandbox import (
    LocalSnapshot,
    LocalSnapshotSpec,
    Manifest,
    SandboxMemoryOptions,
    SandboxSession,
    sandbox_environment,
)
from agents.testing import ScriptedResponse, ScriptedResponses, text, tool_call
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer

pytestmark = pytest.mark.skipif(os.name == "nt", reason="UnixLocal memory fixture")


@asynccontextmanager
async def _workspace_gateway(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    async with ExecutorPeer(workspace).serve() as executor:
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(workspace.parent / "empty-certs"))

        @asynccontextmanager
        async def connect(resource: SandboxSession):
            assert Path(resource.state.manifest.root) == workspace
            yield Environment(
                url=executor.url,
                cwd=executor.root.as_uri(),
                tools=(),
                headers={"Authorization": "Bearer " + executor.token},
            )

        yield connect


async def test_native_sandbox_memory_runs_both_phases_once_per_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    workspace = tmp_path / "workspace"
    extraction = {
        "rollout_slug": "preference",
        "rollout_summary": "The user prefers plain language.",
        "raw_memory": "Prefer plain language.",
    }
    consolidation = {
        "summary": "Use plain language.",
        "memory": "The user prefers plain language in responses.",
    }
    source_text = "# AGENTS.md instructions\n\n<INSTRUCTIONS>\nUse plain language.\n</INSTRUCTIONS>"
    after_memory = asyncio.Event()
    descendants: list[asyncio.Task[int]] = []

    @function_tool
    async def schedule_memory_probe() -> str:
        with pytest.raises(RuntimeError, match="A sandbox memory phase cannot read"):
            await session.history_snapshot(limit=0)

        async def read_after_memory() -> int:
            await after_memory.wait()
            return (await session.history_snapshot(limit=0)).total

        descendants.append(asyncio.create_task(read_after_memory()))
        return "scheduled"

    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [text("I will use plain language.")],
            [tool_call("schedule_memory_probe", {}, call_id="memory-probe")],
            [text(json.dumps(extraction))],
            [text(json.dumps(consolidation))],
        ) as peer,
    ):
        options = SandboxMemoryOptions(
            phase_one=Agent(
                instructions="Extract memory.",
                provider=peer.provider,
                tools=[schedule_memory_probe],
            ),
            phase_two=Agent(instructions="Consolidate memory.", provider=peer.provider),
        )

        def environment(*, managed: bool = False):
            return sandbox_environment(
                UnixLocalSandboxClient(),
                manifest=Manifest(root=str(workspace)),
                options=UnixLocalSandboxClientOptions(),
                connect=connect,
                memory=options,
                managed_workspace=managed,
                snapshot=LocalSnapshotSpec(base_path=tmp_path / "archives") if managed else None,
            )

        agent = Agent(instructions="Source private instruction.", provider=peer.provider)
        async with agent.session(environment=environment()) as session:
            with pytest.raises(NativeError, match="successful source turn"):
                await session.flush_memory()
            assert not (workspace / "sessions").exists()
            turn = await session.submit(source_text)
            assert (await turn.get_result()).final_output == "I will use plain language."

            await asyncio.gather(session.flush_memory(), session.flush_memory())
            await session.flush_memory()
            after_memory.set()
            (descendant,) = descendants
            assert await descendant > 0

            saved = (workspace / "sessions" / f"{session.id}.jsonl").read_text().splitlines()
            assert len(saved) == 1
            rollout = json.loads(saved[0])
            assert rollout["native_thread_id"] == session.id
            assert rollout["terminal_metadata"]["terminal_state"] == "completed"
            assert [item["role"] for item in rollout["items"]] == ["user", "assistant"]
            assert rollout["items"][0]["content"] == [{"type": "input_text", "text": source_text}]
            assert "Source private instruction." not in saved[0]
            assert "Current SDK workspace snapshot" not in saved[0]
            assert (workspace / "memories" / "MEMORY.md").read_text() == consolidation["memory"]
            assert (workspace / "memories" / "memory_summary.md").read_text() == consolidation[
                "summary"
            ]
            assert (
                extraction["raw_memory"]
                in (workspace / "memories" / "raw_memories" / f"{session.id}.md").read_text()
            )
            selection = json.loads(
                (workspace / "memories" / "phase_two_selection.json").read_text()
            )
            assert [item["rollout_id"] for item in selection["selected"]] == [session.id]

            consolidation_started = asyncio.Event()
            release_consolidation = Event()
            loop = asyncio.get_running_loop()

            def block_consolidation(_request):
                loop.call_soon_threadsafe(consolidation_started.set)
                assert release_consolidation.wait(20)
                return [text(json.dumps({"memory": "late memory", "summary": "late summary"}))]

            peer.append(
                [text("I will keep doing that.")],
                [text(json.dumps(extraction))],
                block_consolidation,
            )
            turn = await session.submit("Keep doing that.")
            await turn.get_result()
            flush = asyncio.create_task(session.flush_memory())
            try:
                await consolidation_started.wait()
                await session.aclose()
                with pytest.raises(asyncio.CancelledError):
                    await flush
            finally:
                release_consolidation.set()
            assert (workspace / "memories" / "MEMORY.md").read_text() == consolidation["memory"]

        checkpoint_memory = {"memory": "checkpoint memory", "summary": "checkpoint summary"}
        peer.append(
            [text("Ready to suspend.")],
            [text(json.dumps(extraction))],
            ScriptedResponse(
                (),
                status=400,
                error_body={"error": {"message": "checkpoint memory failure"}},
            ),
            [text(json.dumps(extraction))],
            [text(json.dumps(checkpoint_memory))],
        )
        async with agent.session(environment=environment(managed=True)) as checkpointed:
            turn = await checkpointed.submit("Remember this before saving.")
            await turn.get_result()
            with pytest.raises(NativeError, match="checkpoint memory failure"):
                await checkpointed.suspend_workspace()
            assert not any(path.is_file() for path in (tmp_path / "archives").rglob("*"))
            pair, duplicate = await asyncio.gather(
                checkpointed.suspend_workspace(), checkpointed.suspend_workspace()
            )
            assert duplicate == pair
            rollout_path = workspace / "sessions" / f"{checkpointed.id}.jsonl"
            assert len(rollout_path.read_text().splitlines()) == 1
        state = UnixLocalSandboxClient().deserialize_session_state(pair.workspace["provider_state"])
        assert isinstance(state.snapshot, LocalSnapshot)
        with tarfile.open(state.snapshot._path(), "r:*") as archive:
            memory_entry = next(
                entry
                for entry in archive
                if Path(entry.name).parts[-2:] == ("memories", "MEMORY.md")
            )
            saved_memory = archive.extractfile(memory_entry)
            assert saved_memory is not None
            assert saved_memory.read().decode() == checkpoint_memory["memory"]

        raw_memories = workspace / "memories" / "raw_memories"
        for index in range(1024):
            (raw_memories / f"existing-{index}.md").touch()
        peer.append([text("Okay.")], [text(json.dumps(extraction))])
        async with agent.session(environment=environment()) as oversized_session:
            turn = await oversized_session.submit("Remember this.")
            await turn.get_result()
            with pytest.raises(ValueError, match="directory exceeds 1024 entries"):
                await oversized_session.flush_memory()

        async with agent.session() as default_session:
            with pytest.raises(TypeError, match="Configure memory on sandbox_environment"):
                await default_session.flush_memory()

        assert len(peer.requests) == 14
        assert "Use plain language." in json.dumps(peer.requests[1]["input"])
        assert "Source private instruction." not in json.dumps(peer.requests[1]["input"])
        assert "Current SDK workspace snapshot" not in json.dumps(peer.requests[1]["input"])
        assert extraction["raw_memory"] in json.dumps(peer.requests[3]["input"])


async def test_native_sandbox_memory_serializes_across_trusted_workspace_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    workspace = tmp_path / "workspace"
    first_selection = asyncio.Event()
    release_first = Event()
    loop = asyncio.get_running_loop()

    def extraction(name: str):
        return [
            text(json.dumps({"rollout_slug": name, "rollout_summary": name, "raw_memory": name}))
        ]

    def held_consolidation(_request):
        loop.call_soon_threadsafe(first_selection.set)
        assert release_first.wait(20)
        return [text('{"memory":"first","summary":"first"}')]

    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses([text("First result.")], extraction("first"), held_consolidation) as one,
        ScriptedResponses(
            [text("Second result.")],
            extraction("second"),
            [text('{"memory":"both","summary":"both"}')],
        ) as two,
    ):
        client = UnixLocalSandboxClient()
        manifest = Manifest(root=str(workspace))
        resource = await client.create(manifest=manifest)
        alias = await client.resume(resource.state.model_copy(deep=True))

        def source(peer: ScriptedResponses, selected: SandboxSession):
            return sandbox_environment(
                client,
                manifest=manifest,
                options=UnixLocalSandboxClientOptions(),
                session=selected,
                connect=connect,
                memory=SandboxMemoryOptions(
                    phase_one=Agent(instructions="Extract.", provider=peer.provider),
                    phase_two=Agent(instructions="Consolidate.", provider=peer.provider),
                ),
            )

        first = Agent(instructions="Source.", provider=one.provider)
        second = Agent(instructions="Source.", provider=two.provider)
        async with (
            first.session(environment=source(one, resource)) as a,
            second.session(environment=source(two, alias)) as b,
        ):
            await (await a.submit("Remember first.")).get_result()
            await (await b.submit("Remember second.")).get_result()
            flush_a = asyncio.create_task(a.flush_memory())
            await first_selection.wait()
            flush_b = asyncio.create_task(b.flush_memory())
            try:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(flush_b), 1)
            finally:
                release_first.set()
            await asyncio.gather(flush_a, flush_b)
            selection = json.loads(
                (workspace / "memories" / "phase_two_selection.json").read_text()
            )
            assert {item["rollout_id"] for item in selection["selected"]} == {a.id, b.id}
