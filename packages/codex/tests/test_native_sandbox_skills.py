"""Current sandbox ownership for inline and lazy native skills."""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agents import Agent, Environment
from agents.classic.sandbox.entries import LocalDir
from agents.sandbox import (
    LazySkillSource,
    LocalDirLazySkillSource,
    Manifest,
    Skill,
    SkillMetadata,
    Skills,
    sandbox_environment,
)
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider
from tests.test_sandbox_environment import Client, Options


def test_native_inline_skill_preserves_document_bytes() -> None:
    skills = Skills(
        skills=[Skill(name="report", description="INLINE_REPORT_INDEX", content="plain")]
    )
    manifest = skills.process_manifest(Manifest(root="/workspace"))
    assert manifest.entries[Path(".agents/report")].children["SKILL.md"].content == b"plain"


async def test_native_local_skill_metadata_keeps_only_complete_headers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    good = tmp_path / "skills/report/SKILL.md"
    oversized = tmp_path / "skills/oversized/SKILL.md"
    good.parent.mkdir(parents=True)
    oversized.parent.mkdir(parents=True)
    content = b"---\nname: report\ndescription: COMPLETE_HEADER\n---\n" + b"a" * (1024 * 1024)
    good.write_bytes(content)
    oversized.write_bytes(
        b"---\nname: oversize\ndescription: INCOMPLETE_HEADER" + b"x" * (64 * 1024)
    )
    skills = Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=Path("skills"))))

    instructions = await skills.instructions(Manifest(root=str(tmp_path)))

    assert instructions is not None
    assert "COMPLETE_HEADER" in instructions
    assert ".agents/oversized" not in instructions
    assert good.read_bytes() == content


@pytest.mark.parametrize("limit", ["entries", "bytes"])
async def test_native_local_skill_metadata_rejects_scan_overflow(tmp_path, monkeypatch, limit):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "skills"
    source.mkdir()
    if limit == "entries":
        for index in range(1025):
            (source / str(index)).mkdir()
        error = "entry limit"
    else:
        for index in range(65):
            directory = source / str(index)
            directory.mkdir()
            (directory / "SKILL.md").write_bytes(b"---\nname: report\n---\n" + b"x" * (64 * 1024))
        error = "aggregate byte limit"
    skills = Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=Path("skills"))))

    with pytest.raises(ValueError, match=error):
        await skills.instructions(Manifest(root=str(tmp_path)))


class Source(LazySkillSource):
    effects: list[str]
    index_reads: int = 0
    description: str = "LAZY_REPORT_INDEX"

    def list_skill_metadata(self, *, skills_path, source_grants=()):
        self.index_reads += 1
        return [SkillMetadata("report", self.description, Path(skills_path) / "report")]

    async def load_skill(self, *, skill_name, session, skills_path, user=None):
        self.effects.append(skill_name)
        path = Path(session.state.manifest.root) / skills_path / "report"
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_bytes(b"lazy body")
        return {"status": "loaded", "skill_name": "report", "path": f"{skills_path}/report"}


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="POSIX sandbox resource fixture"
)
async def test_inline_descriptor_reaches_native_wire_without_changing_content(
    tmp_path, monkeypatch, responses
):
    async with ExecutorPeer(tmp_path / "executor").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        client = Client([])

        @asynccontextmanager
        async def connect(session):
            entry = session.state.manifest.entries[Path(".agents/report")]
            target = peer.root / ".agents/report/SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_bytes(entry.children["SKILL.md"].content)
            yield Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=(),
                capability_directories=[str(peer.root / ".agents")],
            )

        source = sandbox_environment(
            client,
            manifest=Manifest(root=str(peer.root)),
            options=Options(),
            connect=connect,
            skills=Skills(
                skills=[
                    Skill(
                        name="report", description="INLINE_REPORT_INDEX", content="plain document"
                    )
                ]
            ),
        )
        assert source.skill_tools == ()
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Inspect.", model="gpt-5.5", provider=provider(responses)
        ).session(environment=source) as session:
            assert (
                await (await session.submit("Inspect skills")).get_result()
            ).final_output == "done"
        assert "INLINE_REPORT_INDEX" in str(responses.requests[-1])
        assert (peer.root / ".agents/report/SKILL.md").read_bytes() == b"plain document"


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="POSIX sandbox resource fixture"
)
async def test_lazy_native_callback_uses_current_source(tmp_path, monkeypatch, responses):
    async with ExecutorPeer(tmp_path / "executor").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        resource = Source(effects=[])
        effects = resource.effects

        @asynccontextmanager
        async def connect(session):
            yield Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=(),
                capability_directories=[str(peer.root / ".agents")],
            )

        source = sandbox_environment(
            Client([]),
            manifest=Manifest(root=str(peer.root)),
            options=Options(),
            connect=connect,
            skills=Skills(lazy_from=resource),
        )
        assert not (peer.root / ".agents/report").exists()
        responses.respond(function_call("load_skill", {"skill_name": "report"}, "call_skill"))
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Inspect.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=source.skill_tools,
        ).session(environment=source) as session:
            assert (await (await session.submit("Load report")).get_result()).final_output == "done"
        assert effects == ["report"]
        assert "LAZY_REPORT_INDEX" in str(responses.requests[0])
        assert (peer.root / ".agents/report/SKILL.md").read_bytes() == b"lazy body"
        assert str(peer.root / ".agents/report") in str(
            tool_output(responses.requests[-1], "call_skill")
        )


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="UnixLocal resource")
async def test_native_local_skill_load_reuses_the_captured_index(tmp_path, monkeypatch, responses):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    class LocalSource(LocalDirLazySkillSource):
        pass

    monkeypatch.chdir(tmp_path)
    selected = tmp_path / "skills/report/SKILL.md"
    unrelated = tmp_path / "skills/unrelated/SKILL.md"
    selected.parent.mkdir(parents=True)
    unrelated.parent.mkdir(parents=True)
    document = b"---\nname: report\ndescription: CAPTURED_NATIVE_INDEX\n---\n" + b"x" * (
        1024 * 1024
    )
    selected.write_bytes(document)
    unrelated.write_text("---\nname: unrelated\n---\n", encoding="utf-8")
    roots: list[Path] = []

    @asynccontextmanager
    async def connect(sandbox):
        root = Path(sandbox.state.manifest.root)
        roots.append(root)
        async with ExecutorPeer(tmp_path / "executor").serve() as executor:
            trust(executor, tmp_path, monkeypatch)
            yield Environment(
                url=executor.url,
                headers={"Authorization": "Bearer " + executor.token},
                cwd=str(root),
                tools=(),
                capability_directories=[str(root / ".agents")],
            )

    source = sandbox_environment(
        UnixLocalSandboxClient(),
        manifest=Manifest(),
        options=UnixLocalSandboxClientOptions(),
        connect=connect,
        skills=Skills(lazy_from=LocalSource(source=LocalDir(src=Path("skills")))),
    )
    responses.respond(function_call("load_skill", {"skill_name": "report"}, "load-report"))
    responses.respond(function_call("load_skill", {"skill_name": "report"}, "load-again"))
    responses.respond(assistant_output("done"))
    async with Agent(
        instructions="Load the report.",
        model="gpt-5.5",
        provider=provider(responses),
        tools=source.skill_tools,
    ).session(environment=source) as session:
        unrelated.write_bytes(b"\xff")
        turn = await session.submit("Load the report.")
        assert (await turn.get_result()).final_output == "done"
        assert (roots[0] / ".agents/report/SKILL.md").read_bytes() == document
        assert not (roots[0] / ".agents/unrelated").exists()
    assert "CAPTURED_NATIVE_INDEX" in str(responses.requests[0])
    assert "loaded" in str(tool_output(responses.requests[1], "load-report"))
    assert "already_loaded" in str(tool_output(responses.requests[-1], "load-again"))


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="POSIX sandbox resource fixture"
)
async def test_foreign_source_cannot_admit_a_loader_even_with_same_environment(
    tmp_path, monkeypatch, responses
):
    async with ExecutorPeer(tmp_path / "executor").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        resource = Source(effects=[])
        effects = resource.effects
        environment = Environment(
            url=peer.url,
            headers={"Authorization": "Bearer " + peer.token},
            cwd=str(peer.root),
            tools=(),
            capability_directories=[str(peer.root / ".agents")],
        )

        @asynccontextmanager
        async def connect(session):
            yield environment

        first = sandbox_environment(
            Client([]),
            manifest=Manifest(root=str(peer.root)),
            options=Options(),
            connect=connect,
            skills=Skills(lazy_from=resource),
        )
        second = sandbox_environment(
            Client([]), manifest=Manifest(root=str(peer.root)), options=Options(), connect=connect
        )
        responses.respond(function_call("load_skill", {"skill_name": "report"}, "call_foreign"))
        async with first:
            agent = Agent(
                instructions="Inspect.",
                model="gpt-5.5",
                provider=provider(responses),
                tools=first.skill_tools,
            )
            async with agent.session(environment=second) as session:
                with pytest.raises(RuntimeError, match="current sandbox environment"):
                    await (await session.submit("Load report")).get_result()
        assert effects == []
        assert not (peer.root / ".agents/report/SKILL.md").exists()
        assert "LAZY_REPORT_INDEX" not in str(responses.requests)


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="POSIX sandbox resource fixture"
)
async def test_unselected_namespace_precedes_index_callback(tmp_path, monkeypatch, responses):
    async with ExecutorPeer(tmp_path / "executor").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        resource = Source(effects=[])

        @asynccontextmanager
        async def connect(session):
            yield Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=(),
                capability_directories=[str(peer.root / "unselected")],
            )

        source = sandbox_environment(
            Client([]),
            manifest=Manifest(root=str(peer.root)),
            options=Options(),
            connect=connect,
            skills=Skills(lazy_from=resource),
        )
        agent = Agent(
            instructions="Inspect.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=source.skill_tools,
        )
        with pytest.raises(ValueError, match="selected current Environment root"):
            async with agent.session(environment=source):
                pytest.fail("Unselected skill root entered a native Session")
        assert resource.index_reads == 0
        assert resource.effects == []
        assert not responses.requests


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="POSIX sandbox resource fixture"
)
async def test_current_manifest_rotation_revokes_loader(tmp_path, monkeypatch, responses):
    async with ExecutorPeer(tmp_path / "executor").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        client = Client([])
        resource = Source(effects=[])

        @asynccontextmanager
        async def connect(session):
            yield Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=(),
                capability_directories=[str(peer.root / ".agents")],
            )

        source = sandbox_environment(
            client,
            manifest=Manifest(root=str(peer.root)),
            options=Options(),
            connect=connect,
            skills=Skills(lazy_from=resource),
        )
        responses.respond(function_call("load_skill", {"skill_name": "report"}, "call_rotated"))
        agent = Agent(
            instructions="Inspect.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=source.skill_tools,
        )
        async with agent.session(environment=source) as session:
            client.session.state.manifest = client.session.state.manifest.model_copy(
                update={"root": str(peer.root / "rotated")}
            )
            with pytest.raises(RuntimeError, match="current sandbox environment"):
                await (await session.submit("Load report")).get_result()
        assert resource.effects == []
        assert not (peer.root / ".agents/report/SKILL.md").exists()


@pytest.mark.parametrize("mode", ["matching", "read_only", "host_path", "removed"])
async def test_borrowed_skill_grants_require_current_exact_configuration(
    tmp_path, monkeypatch, responses, mode
):
    from agents.classic.sandbox.workspace_paths import SandboxPathGrant

    async with ExecutorPeer(tmp_path / "executor").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        client = Client([])
        grant = SandboxPathGrant(path=str(tmp_path / "shared"), read_only=True)
        manifest = Manifest(root=str(peer.root), extra_path_grants=(grant,))
        borrowed = await client.create(manifest=manifest.model_copy(deep=True), options=Options())
        client.backend_id = borrowed.state.type
        resource = Source(effects=[])

        @asynccontextmanager
        async def connect(selected):
            assert selected is borrowed
            yield Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=(),
                capability_directories=[str(peer.root / ".agents")],
            )

        source = sandbox_environment(
            client,
            manifest=manifest,
            options=Options(),
            session=borrowed,
            connect=connect,
            skills=Skills(lazy_from=resource),
        )
        if mode == "read_only":
            borrowed.state.manifest.extra_path_grants = (
                grant.model_copy(update={"read_only": False}),
            )
        elif mode == "host_path":
            borrowed.state.manifest.extra_path_grants = (
                grant.model_copy(update={"host_path": str(tmp_path / "foreign")}),
            )
        elif mode == "removed":
            borrowed.state.manifest.extra_path_grants = ()
        agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=provider(responses))
        try:
            if mode == "matching":
                responses.respond(assistant_output("done"))
                async with agent.session(environment=source) as session:
                    assert (
                        await (await session.submit("Inspect skills")).get_result()
                    ).final_output == "done"
                assert resource.index_reads == 1
                assert "LAZY_REPORT_INDEX" in str(responses.requests)
            else:
                with pytest.raises(RuntimeError, match="current sandbox.*grants"):
                    async with agent.session(environment=source):
                        pytest.fail("Changed borrowed grants published a skill index")
                assert resource.index_reads == 0
                assert not responses.requests
        finally:
            await borrowed.aclose()


async def test_native_skill_index_shares_workspace_utf8_budget(tmp_path, monkeypatch, responses):
    from tests.test_native_workspace_guidance import _latest_user_instructions

    async with ExecutorPeer(tmp_path / "executor").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        resource = Source(effects=[], description="界" * 20_000)

        @asynccontextmanager
        async def connect(selected):
            yield Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(peer.root),
                tools=(),
                capability_directories=[str(peer.root / ".agents")],
            )

        source = sandbox_environment(
            Client([]),
            manifest=Manifest(root=str(peer.root)),
            options=Options(),
            connect=connect,
            skills=Skills(lazy_from=resource),
        )
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Inspect.", model="gpt-5.5", provider=provider(responses)
        ).session(
            environment=source,
            discover_agents_md=False,
        ) as session:
            assert (
                await (await session.submit("Inspect skills")).get_result()
            ).final_output == "done"
        text = _latest_user_instructions(responses.requests[0])
        assert "# Current SDK workspace snapshot" in text
        assert "Executor read-only" in text
        assert "skill index has been exceeding" in text
        assert "界" in text
        assert resource.index_reads == 1


@pytest.mark.parametrize("kind", ["inline", "from"])
async def test_eager_skills_reject_borrowed_resource_before_index(tmp_path, kind):
    from agents.classic.sandbox.entries import Dir

    client = Client([])
    manifest = Manifest(root=str(tmp_path))
    borrowed = await client.create(manifest=manifest, options=Options())
    client.backend_id = borrowed.state.type
    skills = (
        Skills(skills=[Skill(name="report", description="index", content="not staged")])
        if kind == "inline"
        else Skills(from_=Dir())
    )

    @asynccontextmanager
    async def connect(selected):
        pytest.fail("Eager borrowed skills reached environment acquisition")
        yield  # pragma: no cover

    try:
        with pytest.raises(ValueError, match="Eager skills require a new sandbox resource"):
            sandbox_environment(
                client,
                manifest=manifest,
                options=Options(),
                session=borrowed,
                connect=connect,
                skills=skills,
            )
        assert not (tmp_path / ".agents").exists()
    finally:
        await borrowed.aclose()


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="UnixLocal resource")
async def test_owned_unixlocal_skill_stages_before_relocated_subdirectory_index(
    tmp_path, monkeypatch, responses
):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    observed = []

    @asynccontextmanager
    async def connect(selected):
        root = Path(selected.state.manifest.root)
        observed.append(root)
        assert str(root) != Manifest().root
        assert (root / ".agents/report/SKILL.md").read_bytes() == b"exact inline document"
        (root / "nested").mkdir()
        async with ExecutorPeer(tmp_path / "executor").serve() as peer:
            trust(peer, tmp_path, monkeypatch)
            yield Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(root / "nested"),
                tools=(),
                capability_directories=[str(root / ".agents")],
            )

    source = sandbox_environment(
        UnixLocalSandboxClient(),
        manifest=Manifest(),
        options=UnixLocalSandboxClientOptions(),
        connect=connect,
        skills=Skills(
            skills=[
                Skill(
                    name="report", description="REAL_INLINE_INDEX", content="exact inline document"
                )
            ]
        ),
    )
    responses.respond(assistant_output("done"))
    async with Agent(
        instructions="Inspect.", model="gpt-5.5", provider=provider(responses)
    ).session(
        environment=source,
        discover_agents_md=False,
    ) as session:
        assert (await (await session.submit("Inspect skills")).get_result()).final_output == "done"
        text = str(responses.requests[0])
        assert "REAL_INLINE_INDEX" in text
        assert str(observed[0] / ".agents/report") in text
        assert "nested/.agents/report" not in text
    assert len(observed) == 1
    assert not observed[0].exists()


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="UnixLocal snapshot")
@pytest.mark.parametrize("kind", ["inline", "from"])
async def test_owned_cold_resume_stages_current_eager_skills_only(
    tmp_path, monkeypatch, responses, kind
):
    from agents.classic.sandbox.entries import Dir, File
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )
    from agents.sandbox import LocalSnapshotSpec

    client = UnixLocalSandboxClient()
    options = UnixLocalSandboxClientOptions()
    original = await client.create(
        manifest=Manifest(entries={Path("keep.txt"): File(content=b"original snapshot data")}),
        options=options,
        snapshot=LocalSnapshotSpec(base_path=tmp_path / "snapshots"),
    )
    await original.start()
    root = Path(original.state.manifest.root)
    await original.aclose()
    saved = original.state.model_copy(deep=True)
    await client.delete(original)
    assert not root.exists()
    document_bytes = b"new current document"
    if kind == "inline":
        skills = Skills(
            skills=[
                Skill(
                    name="report",
                    description="COLD_RESUME_INDEX",
                    content=document_bytes,
                )
            ]
        )
    else:
        document_bytes = b"---\nname: report\ndescription: COLD_RESUME_INDEX\n---\n" + b"x" * (
            1024 * 1024
        )
        skills = Skills(
            from_=Dir(
                children={
                    "report": Dir(children={"SKILL.md": File(content=document_bytes)}),
                    "oversized": Dir(
                        children={
                            "SKILL.md": File(
                                content=b"---\nname: oversized\ndescription: " + b"x" * (64 * 1024)
                            )
                        }
                    ),
                }
            )
        )

    @asynccontextmanager
    async def connect(selected):
        assert selected is not original
        assert (root / "keep.txt").read_bytes() == b"original snapshot data"
        assert (root / ".agents/report/SKILL.md").read_bytes() == document_bytes
        async with ExecutorPeer(tmp_path / "executor").serve() as peer:
            trust(peer, tmp_path, monkeypatch)
            yield Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(root),
                tools=(),
                capability_directories=[str(root / ".agents")],
            )

    source = sandbox_environment(
        UnixLocalSandboxClient(),
        manifest=Manifest(
            root=str(root),
            entries={Path("keep.txt"): File(content=b"unrelated current manifest data")},
        ),
        options=options,
        authorized_resume_state=saved,
        connect=connect,
        skills=skills,
    )
    responses.respond(assistant_output("done"))
    async with Agent(
        instructions="Inspect.", model="gpt-5.5", provider=provider(responses)
    ).session(
        environment=source,
        discover_agents_md=False,
    ) as session:
        assert (
            await (await session.submit("Inspect restored skills")).get_result()
        ).final_output == "done"
        instructions = str(responses.requests)
        assert ".agents/report" in instructions
        assert "COLD_RESUME_INDEX" in instructions
        assert "- oversized:" not in instructions
        assert (root / "keep.txt").read_bytes() == b"original snapshot data"
        document = root / ".agents/report/SKILL.md"
        assert document.is_file(), "Published eager skill index without restored skill files"
        assert document.read_bytes() == document_bytes
