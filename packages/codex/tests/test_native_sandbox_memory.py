from __future__ import annotations

import asyncio
import json
import os
import sys
import tarfile
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
from typing import Any

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
from tests._asyncio import capture_cancellation, timeout
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


@pytest.mark.parametrize("process_backed", [False, True], ids=["local", "process-backed"])
async def test_native_sandbox_memory_only_consolidates_regular_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, process_backed: bool
):
    from agents.classic.sandbox.errors import WorkspaceArchiveReadError
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
        UnixLocalSandboxSession,
    )
    from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession

    if process_backed:
        from agents.classic.sandbox.session.base_sandbox_session import _READ_MEMORY_FILE_SCRIPT

        original_exec = UnixLocalSandboxSession.exec

        async def reader_exec(
            self: UnixLocalSandboxSession, command: str, *arguments: str, **kwargs: Any
        ):
            if (
                command == "/bin/sh"
                and len(arguments) >= 4
                and arguments[3] == _READ_MEMORY_FILE_SCRIPT
            ):
                assert arguments[:3] == (
                    "-c",
                    'PATH=/usr/local/bin:/usr/bin:/bin; export PATH; exec python3 -I -S -c "$@"',
                    "sh",
                )
                assert kwargs["shell"] is False
                # Exercise the remote descriptor script with the known test
                # interpreter; hosted macOS need not have image Python on PATH.
                return await original_exec(
                    self, sys.executable, "-I", "-S", "-c", *arguments[3:], **kwargs
                )
            return await original_exec(self, command, *arguments, **kwargs)

        monkeypatch.setattr(UnixLocalSandboxSession, "exec", reader_exec)
        monkeypatch.setattr(
            UnixLocalSandboxSession, "_read_memory_file", BaseSandboxSession._read_memory_file
        )

    workspace = tmp_path / "workspace"

    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [text("Source reply.")],
            [text('{"rollout_slug":"new","rollout_summary":"new","raw_memory":"New memory."}')],
            [text('{"memory":"combined","summary":"combined"}')],
        ) as peer,
    ):

        @asynccontextmanager
        async def checked_connect(resource: SandboxSession):
            raw = workspace / "memories" / "raw_memories"
            raw.mkdir(parents=True)
            private = workspace / "private.txt"
            private.write_text("private sentinel")
            (raw / "linked.md").symlink_to(private)
            (workspace / "linked-directory").symlink_to(raw, target_is_directory=True)
            (raw / "forged.md").write_text("rollout_id: linked\nrollout_summary_file: linked.md\n")
            (raw / "kept.md").write_text(
                "rollout_id: kept\nrollout_summary_file: kept.md\n\nPreviously retained memory."
            )
            for path in ("memories/raw_memories/linked.md", "linked-directory/kept.md"):
                with pytest.raises(WorkspaceArchiveReadError):
                    await resource._inner._read_memory_file(Path(path), max_bytes=64)
            assert (
                await resource._inner._read_memory_file(
                    Path("memories/raw_memories/kept.md"), max_bytes=4
                )
                == b"roll"
            )
            async with connect(resource) as environment:
                yield environment

        agent = Agent(instructions="Source.", provider=peer.provider)
        environment = sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(root=str(workspace)),
            options=UnixLocalSandboxClientOptions(),
            connect=checked_connect,
            memory=SandboxMemoryOptions(
                phase_one=Agent(instructions="Extract.", provider=peer.provider),
                phase_two=Agent(instructions="Consolidate.", provider=peer.provider),
            ),
        )
        async with agent.session(environment=environment) as session:
            await (await session.submit("Remember this.")).get_result()
            await session.flush_memory()
        request = json.dumps(peer.requests[-1]["input"])
        assert "Previously retained memory." in request
        assert "New memory." in request
        assert "private sentinel" not in request
        assert "forged" not in request


@pytest.mark.parametrize(
    ("root", "grant_read_only", "local_interpreter"),
    [("/usr/local/bin", None, False), ("/workspace", False, False), ("/workspace", True, True)],
)
async def test_memory_reader_excludes_writable_interpreter_locations(
    root: str, grant_read_only: bool | None, local_interpreter: bool
) -> None:
    from types import SimpleNamespace
    from typing import cast

    from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession
    from agents.classic.sandbox.types import ExecResult
    from agents.classic.sandbox.workspace_paths import SandboxPathGrant, WorkspacePathPolicy

    grants = (
        (SandboxPathGrant(path="/usr/local/bin", read_only=grant_read_only),)
        if grant_read_only is not None
        else ()
    )
    manifest = Manifest(root=root, extra_path_grants=grants)
    policy = WorkspacePathPolicy(root=manifest.root, extra_path_grants=manifest.extra_path_grants)
    commands = []

    async def execute(*arguments: str, **kwargs: Any) -> ExecResult:
        commands.append(arguments)
        assert kwargs["shell"] is False
        return ExecResult(stdout=b"safe", stderr=b"", exit_code=0)

    owner = cast(
        BaseSandboxSession,
        SimpleNamespace(
            _workspace_path_policy=lambda: policy,
            _memory_interpreter_path=lambda: BaseSandboxSession._memory_interpreter_path(owner),
            exec=execute,
        ),
    )
    assert (
        await BaseSandboxSession._read_memory_file(owner, Path("memory.md"), max_bytes=4) == b"safe"
    )
    search_path = "/usr/local/bin:/usr/bin:/bin" if local_interpreter else "/usr/bin:/bin"
    assert commands[0][:3] == (
        "/bin/sh",
        "-c",
        f'PATH={search_path}; export PATH; exec python3 -I -S -c "$@"',
    )


async def test_memory_reader_rejects_workspace_owned_bootstrap() -> None:
    from types import SimpleNamespace
    from typing import cast

    from agents.classic.sandbox.errors import WorkspaceArchiveReadError
    from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession
    from agents.classic.sandbox.workspace_paths import WorkspacePathPolicy

    owner = cast(
        BaseSandboxSession,
        SimpleNamespace(
            _workspace_path_policy=lambda: WorkspacePathPolicy(root="/usr/bin"),
            _memory_interpreter_path=lambda: BaseSandboxSession._memory_interpreter_path(owner),
        ),
    )
    # No exec implementation: rejection must precede provider execution.
    with pytest.raises(WorkspaceArchiveReadError):
        await BaseSandboxSession._read_memory_file(owner, Path("memory.md"), max_bytes=4)


async def test_native_sandbox_memory_replaces_output_links(tmp_path: Path, monkeypatch):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    workspace = tmp_path / "workspace"
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [text("Source reply.")],
            [text('{"rollout_slug":"new","rollout_summary":"new","raw_memory":"New memory."}')],
            [text('{"memory":"combined memory","summary":"combined summary"}')],
        ) as peer,
    ):

        @asynccontextmanager
        async def checked_connect(resource: SandboxSession):
            memory = workspace / "memories"
            memory.mkdir(parents=True)
            victim = workspace / "victim.txt"
            victim.write_text("unchanged sentinel")
            for name in ("MEMORY.md", "memory_summary.md", "raw_memories.md"):
                (memory / name).symlink_to(victim)
            async with connect(resource) as environment:
                yield environment

        agent = Agent(instructions="Source.", provider=peer.provider)
        environment = sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(root=str(workspace)),
            options=UnixLocalSandboxClientOptions(),
            connect=checked_connect,
            memory=SandboxMemoryOptions(
                phase_one=Agent(instructions="Extract.", provider=peer.provider),
                phase_two=Agent(instructions="Consolidate.", provider=peer.provider),
            ),
        )
        async with agent.session(environment=environment) as session:
            await (await session.submit("Remember native text.")).get_result()
            await session.flush_memory()
        assert (workspace / "victim.txt").read_text() == "unchanged sentinel"
        for name in ("MEMORY.md", "memory_summary.md", "raw_memories.md"):
            assert not (workspace / "memories" / name).is_symlink()
        assert (workspace / "memories/MEMORY.md").read_text() == "combined memory"


@pytest.mark.parametrize("at_root", [False, True], ids=["directories", "workspace-root"])
async def test_docker_memory_writes_replace_leaves_and_reject_ancestor_links(monkeypatch, at_root):
    import io

    docker = pytest.importorskip("docker")
    from agents.classic.sandbox.config import MemoryLayoutConfig
    from agents.classic.sandbox.errors import WorkspaceArchiveWriteError
    from agents.classic.sandbox.memory.storage import SandboxMemoryStorage
    from agents.classic.sandbox.sandboxes.docker import (
        DockerSandboxClient,
        DockerSandboxClientOptions,
    )

    image = "python:3.13.16-slim"
    try:
        daemon = docker.from_env()
        daemon.images.get(image)
    except docker.errors.DockerException:
        if "daemon" in locals():
            daemon.close()
        pytest.skip("Retained Docker Python image is unavailable")
    client = DockerSandboxClient(daemon)
    resource = await client.create(
        manifest=Manifest(root="/workspace"),
        options=DockerSandboxClientOptions(image=image, network_mode="none"),
    )
    inner = resource._inner
    try:
        await inner.start()
        storage = SandboxMemoryStorage(
            session=inner,
            layout=MemoryLayoutConfig(memories_dir=".", sessions_dir=".")
            if at_root
            else MemoryLayoutConfig(),
            bounded=True,
        )
        await storage.ensure_layout()
        await inner.write(Path("victim.txt"), io.BytesIO(b"unchanged sentinel"))
        generated = "generated native memory \u2603"
        original_stream = inner._stream_into_exec

        async def streamed_write(**options):
            assert generated not in json.dumps(options["cmd"], ensure_ascii=False)
            assert options["system_tools"] is True
            assert options["append_input_length"] is True
            return await original_stream(**options)

        monkeypatch.setattr(inner, "_stream_into_exec", streamed_write)
        destinations = tuple(
            str(path)
            for path in (
                storage.sessions_dir / "source.jsonl",
                storage.raw_memories_dir / "source.md",
                storage.rollout_summaries_dir / "source.md",
                storage.memories_dir / "raw_memories.md",
                storage.memories_dir / "MEMORY.md",
                storage.memories_dir / "memory_summary.md",
                storage.phase_two_selection_path,
            )
        )
        for destination in destinations:
            assert (
                await inner.exec("/bin/rm", "-f", "/workspace/" + destination, shell=False)
            ).ok()
            assert (
                await inner.exec(
                    "/bin/ln",
                    "-s",
                    "/workspace/victim.txt",
                    "/workspace/" + destination,
                    shell=False,
                )
            ).ok()
            await storage.write_text(Path(destination), generated)
            assert (
                await inner._read_memory_file(Path(destination), max_bytes=128)
                == generated.encode()
            )
            assert (
                await inner._read_memory_file(Path("victim.txt"), max_bytes=128)
                == b"unchanged sentinel"
            )
        hard_link = storage.memories_dir / "hard.md"
        assert (
            await inner.exec(
                "/bin/ln", "/workspace/victim.txt", "/workspace/" + str(hard_link), shell=False
            )
        ).ok()
        await storage.write_text(hard_link, generated)
        assert (
            await inner._read_memory_file(Path("victim.txt"), max_bytes=128)
            == b"unchanged sentinel"
        )
        with pytest.raises(WorkspaceArchiveWriteError):
            await storage.write_text(Path("."), generated)
        assert (await inner.exec("/bin/mkdir", "/workspace/other", shell=False)).ok()
        assert (
            await inner.exec("/bin/ln", "-s", "/workspace/other", "/workspace/alias", shell=False)
        ).ok()
        with pytest.raises(WorkspaceArchiveWriteError):
            await storage.write_text(Path("alias/out.md"), generated)
        with pytest.raises(WorkspaceArchiveWriteError):
            await inner._mkdir_memory_directory(Path("alias/nested"))
        assert (
            await inner.exec("/bin/test", "!", "-e", "/workspace/other/out.md", shell=False)
        ).ok()
        assert (
            await inner.exec("/bin/test", "!", "-e", "/workspace/other/nested", shell=False)
        ).ok()
    finally:
        await inner.aclose()
        daemon.close()


async def test_native_memory_rejects_linked_layout_before_generation(tmp_path: Path, monkeypatch):
    from agents.classic.sandbox.errors import WorkspaceArchiveWriteError
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    workspace = tmp_path / "workspace"
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses([text("Source reply.")]) as peer,
    ):

        @asynccontextmanager
        async def checked_connect(resource: SandboxSession):
            victim = workspace / "victim"
            victim.mkdir()
            (workspace / "memories").symlink_to(victim, target_is_directory=True)
            async with connect(resource) as environment:
                yield environment

        agent = Agent(instructions="Memory phase.", provider=peer.provider)
        environment = sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(root=str(workspace)),
            options=UnixLocalSandboxClientOptions(),
            connect=checked_connect,
            memory=SandboxMemoryOptions(
                phase_one=Agent(instructions="Memory phase.", provider=peer.provider),
                phase_two=Agent(instructions="Memory phase.", provider=peer.provider),
            ),
        )
        async with agent.session(environment=environment) as session:
            await (await session.submit("Remember native text.")).get_result()
            with pytest.raises(WorkspaceArchiveWriteError):
                await session.flush_memory()
        assert list((workspace / "victim").iterdir()) == []
        assert len(peer.requests) == 1


async def test_native_memory_requires_provider_atomic_operations(tmp_path: Path, monkeypatch):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
        UnixLocalSandboxSession,
    )
    from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession

    monkeypatch.setattr(
        UnixLocalSandboxSession, "_write_memory_file", BaseSandboxSession._write_memory_file
    )
    workspace = tmp_path / "workspace"
    async with _workspace_gateway(workspace, monkeypatch) as connect, ScriptedResponses() as peer:
        agent = Agent(instructions="Memory phase.", provider=peer.provider)
        environment = sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(root=str(workspace)),
            options=UnixLocalSandboxClientOptions(),
            connect=connect,
            memory=SandboxMemoryOptions(
                phase_one=Agent(instructions="Memory phase.", provider=peer.provider),
                phase_two=Agent(instructions="Memory phase.", provider=peer.provider),
            ),
        )
        with pytest.raises(ValueError, match="provider-owned atomic"):
            async with agent.session(environment=environment):
                pytest.fail("Unsupported memory writes were admitted")
        assert not (workspace / "memories").exists()
        assert not peer.requests


@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_docker_memory_cancellation_waits_for_physical_write(cleanup_error):
    import io
    from types import SimpleNamespace
    from typing import cast

    pytest.importorskip("docker")
    from agents.classic.sandbox.sandboxes.docker import DockerSandboxSession
    from agents.classic.sandbox.workspace_paths import WorkspacePathPolicy

    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = Event()
    finished = Event()

    def write():
        loop.call_soon_threadsafe(started.set)
        release.wait(10)
        finished.set()
        if cleanup_error:
            raise RuntimeError("synthetic write cleanup")

    async def stream(**_kwargs):
        await loop.run_in_executor(None, write)
        return 0

    owner = cast(
        DockerSandboxSession,
        SimpleNamespace(
            _workspace_path_policy=lambda: WorkspacePathPolicy(root="/workspace"),
            _memory_interpreter_path=lambda: "/usr/local/bin:/usr/bin:/bin",
            _stream_into_exec=stream,
        ),
    )
    cancellations: list[asyncio.CancelledError] = []
    task = asyncio.create_task(
        capture_cancellation(
            DockerSandboxSession._memory_file_operation(
                owner,
                Path("memories/MEMORY.md"),
                io.BytesIO(b"synthetic payload"),
                operation="file",
            ),
            cancellations,
        )
    )
    try:
        await started.wait()
        task.cancel("caller cancellation")
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert len(cancellations) == 1
    assert cancellations[0].args == ("caller cancellation",)
    if cleanup_error:
        assert isinstance(cancellations[0].__context__, RuntimeError)
        assert str(cancellations[0].__context__) == "synthetic write cleanup"
