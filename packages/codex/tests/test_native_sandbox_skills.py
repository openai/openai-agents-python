"""Current sandbox ownership for inline and lazy native skills."""

import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agents import Agent, Environment
from agents.classic.sandbox.entries import LocalDir
from agents.classic.sandbox.errors import LocalDirReadError
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


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="UnixLocal resource")
async def test_generic_bounded_listing_preserves_permission_failure(tmp_path, monkeypatch):
    from agents.classic.sandbox.entries import Dir
    from agents.classic.sandbox.errors import ExecNonZeroError
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
        UnixLocalSandboxSession,
    )
    from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession

    if os.geteuid() == 0:
        pytest.skip("Directory permissions require an unprivileged host user")
    monkeypatch.setattr(UnixLocalSandboxSession, "ls", BaseSandboxSession.ls)
    monkeypatch.setattr(UnixLocalSandboxSession, "_ls_bounded", BaseSandboxSession._ls_bounded)
    client = UnixLocalSandboxClient()
    sandbox = await client.create(
        manifest=Manifest(entries={Path("denied"): Dir()}), options=UnixLocalSandboxClientOptions()
    )
    await sandbox.start()
    denied = Path(sandbox.state.manifest.root) / "denied"
    denied.chmod(0)
    try:
        with pytest.raises(ExecNonZeroError):
            await sandbox._ls_bounded(denied, max_entries=1025)
    finally:
        denied.chmod(0o700)
        await sandbox.aclose()
        await client.delete(sandbox)


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="UnixLocal resource")
async def test_eager_metadata_bounds_provider_enumeration(tmp_path, monkeypatch, responses):
    from agents.classic.sandbox.entries import Dir
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
        UnixLocalSandboxSession,
    )

    sizes = []
    method = "_ls_bounded" if hasattr(UnixLocalSandboxSession, "_ls_bounded") else "ls"
    original = getattr(UnixLocalSandboxSession, method)

    async def observed(session, *args, **kwargs):
        entries = await original(session, *args, **kwargs)
        sizes.append(len(entries))
        return entries

    monkeypatch.setattr(UnixLocalSandboxSession, method, observed)

    @asynccontextmanager
    async def connect(sandbox):
        root = Path(sandbox.state.manifest.root)
        for index in range(2048):
            (root / ".agents" / str(index)).mkdir()
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
        manifest=Manifest(),
        options=UnixLocalSandboxClientOptions(),
        connect=connect,
        skills=Skills(from_=Dir()),
    )
    with pytest.raises(ValueError, match="metadata directory exceeds the entry limit"):
        async with Agent(
            instructions="Inspect.", model="gpt-5.5", provider=provider(responses)
        ).session(environment=source):
            pytest.fail("Published an oversized Skills index")
    assert sizes == [1025]
    assert responses.requests == []


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="POSIX sandbox resource fixture"
)
@pytest.mark.parametrize("localhost", [False, True])
async def test_inline_descriptor_reaches_native_wire_without_changing_content(
    tmp_path, monkeypatch, responses, localhost
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
                capability_directories=[
                    (peer.root / ".agents").as_uri().replace("file://", "file://localhost")
                    if localhost
                    else str(peer.root / ".agents")
                ],
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
@pytest.mark.parametrize("approval", ["none", "manual", "automatic"])
async def test_lazy_native_callback_uses_current_source(tmp_path, monkeypatch, responses, approval):
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
            tools=(
                replace(
                    source.skill_tools[0],
                    needs_approval=(lambda _: False) if approval == "automatic" else True,
                ),
            )
            if approval != "none"
            else source.skill_tools,
        ).session(environment=source) as session:
            turn = await session.submit("Load report")
            if approval == "manual":
                request = await anext(session.interactions())
                await request.approve()
            assert (await turn.get_result()).final_output == "done"
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


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="UnixLocal resource")
@pytest.mark.parametrize("limit", ["members", "depth", "bytes", "growth", "fifo"])
async def test_native_lazy_skill_materialization_is_bounded(
    tmp_path, monkeypatch, responses, limit
):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    monkeypatch.chdir(tmp_path)
    selected = tmp_path / "skills/report/SKILL.md"
    selected.parent.mkdir(parents=True)
    selected.write_text("---\nname: report\n---\n", encoding="utf-8")
    if limit == "members":
        for index in range(4096):
            (selected.parent / str(index)).touch()
    elif limit == "depth":
        (selected.parent.joinpath(*["nested"] * 33)).mkdir(parents=True)
    elif limit == "bytes":
        with (selected.parent / "large.bin").open("wb") as handle:
            handle.truncate(64 * 1024 * 1024 + 1)
    elif limit in ("growth", "fifo"):
        original = LocalDir._copy_local_dir_file

        async def grow_before_copy(self, **kwargs):
            if limit == "fifo":
                selected.unlink()
                os.mkfifo(selected)
            else:
                with selected.open("r+b") as handle:
                    handle.truncate(64 * 1024 * 1024 + 1)
            return await original(self, **kwargs)

        monkeypatch.setattr(LocalDir, "_copy_local_dir_file", grow_before_copy)
    roots = []

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
        skills=Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=Path("skills")))),
    )
    responses.respond(function_call("load_skill", {"skill_name": "report"}, "bounded-load"))
    responses.respond(assistant_output("done"))
    async with Agent(
        instructions="Load report.",
        model="gpt-5.5",
        provider=provider(responses),
        tools=source.skill_tools,
    ).session(environment=source) as session:
        expected_limit = "byte" if limit in ("bytes", "growth") else limit.removesuffix("s")
        with pytest.raises(
            LocalDirReadError if limit == "fifo" else ValueError,
            match=None
            if limit == "fifo"
            else f"materialization exceeds the {expected_limit} limit",
        ):
            await (await session.submit("Load report.")).get_result()
        assert not (roots[0] / ".agents/report/SKILL.md").exists()
    assert len(responses.requests) == 1


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


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(
            "matching",
            marks=pytest.mark.skipif(
                sys.platform not in ("linux", "darwin"), reason="Strict native executor policy"
            ),
        ),
        "read_only",
        "host_path",
        "removed",
    ],
)
async def test_borrowed_skill_grants_require_current_exact_configuration(
    tmp_path, monkeypatch, responses, mode
):
    from agents.classic.sandbox.workspace_paths import SandboxPathGrant

    async with ExecutorPeer(tmp_path / "executor").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        client = Client([])
        grant = SandboxPathGrant(path="/shared", read_only=True)
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


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="Strict native executor policy")
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
@pytest.mark.parametrize(
    "kind,seed_old",
    [
        ("inline", True),
        ("from", True),
        ("local_from", True),
        ("lazy", False),
        ("from", False),
    ],
)
async def test_owned_cold_resume_uses_current_skill_sources(
    tmp_path, monkeypatch, responses, kind, seed_old
):
    from agents.classic.sandbox.entries import Dir, File
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )
    from agents.sandbox import LocalSnapshotSpec

    client = UnixLocalSandboxClient()
    options = UnixLocalSandboxClientOptions()
    entries = {Path("keep.txt"): File(content=b"original snapshot data")}
    if seed_old:
        entries.update(
            {
                Path(".agents/keep.txt"): File(content=b"namespace configuration"),
                Path(".agents/config/settings.txt"): File(content=b"unrelated configuration"),
                Path(".agents/report/SKILL.md"): File(
                    content=b"---\nname: report\ndescription: OLD_SKILL\n---\n"
                ),
                Path(".agents/report/scripts/retired.py"): File(content=b"retired script"),
                Path(".agents/retired/SKILL.md"): File(
                    content=b"---\nname: retired\ndescription: RETIRED_SKILL\n---\n"
                ),
            }
        )
    original = await client.create(
        manifest=Manifest(entries=entries),
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
    elif kind in ("from", "local_from"):
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
        if kind == "local_from":
            monkeypatch.chdir(tmp_path)
            current = tmp_path / "current-source/report/SKILL.md"
            current.parent.mkdir(parents=True)
            current.write_bytes(document_bytes)
            skills = Skills(from_=LocalDir(src=current.parent.parent))

    else:
        monkeypatch.chdir(tmp_path)
        saved = client.deserialize_session_state(client.serialize_session_state(saved))
        local = tmp_path / "lazy-source/report/SKILL.md"
        local.parent.mkdir(parents=True)
        document_bytes = b"---\nname: report\ndescription: COLD_RESUME_INDEX\n---\n" + b"x" * (
            1024 * 1024
        )
        local.write_bytes(document_bytes)
        skills = Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=local.parent.parent)))

    @asynccontextmanager
    async def connect(selected):
        assert selected is not original
        assert (root / "keep.txt").read_bytes() == b"original snapshot data"
        if kind == "lazy":
            assert not (root / ".agents/report/SKILL.md").exists()
        else:
            assert (root / ".agents/report/SKILL.md").read_bytes() == document_bytes
            assert (
                (root / ".agents/retired").exists(),
                (root / ".agents/report/scripts/retired.py").exists(),
            ) == (False, False)
            if seed_old:
                assert (root / ".agents/keep.txt").read_bytes() == b"namespace configuration"
                assert (
                    root / ".agents/config/settings.txt"
                ).read_bytes() == b"unrelated configuration"
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
    if kind == "lazy":
        responses.respond(function_call("load_skill", {"skill_name": "report"}, "cold-load"))
    responses.respond(assistant_output("done"))
    async with Agent(
        instructions="Inspect.",
        model="gpt-5.5",
        provider=provider(responses),
        tools=source.skill_tools,
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
        assert "RETIRED_SKILL" not in instructions
        assert (root / "keep.txt").read_bytes() == b"original snapshot data"
        document = root / ".agents/report/SKILL.md"
        assert document.is_file(), "Missing the current skill document after resume and load"
        assert document.read_bytes() == document_bytes


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="UnixLocal resource")
@pytest.mark.parametrize("replacement", ["ancestor", "leaf", "fifo"])
@pytest.mark.parametrize("portable", [False, True])
async def test_native_lazy_index_rejects_swapped_source(
    tmp_path, monkeypatch, responses, replacement, portable
):
    from _agents_shared.sandbox_skills import _MetadataBudget
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    monkeypatch.chdir(tmp_path)
    selected = tmp_path / "skills/report/SKILL.md"
    selected.parent.mkdir(parents=True)
    selected.write_text("---\nname: report\n---\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: report\ndescription: OUTSIDE_INDEX_SECRET\n---\n", encoding="utf-8"
    )
    if portable:
        from agents.classic.sandbox.entries import artifacts

        monkeypatch.setattr(artifacts, "_OPEN_SUPPORTS_DIR_FD", False)
    original = _MetadataBudget.read_limit

    def swap_before_read(budget):
        if replacement == "ancestor":
            selected.parent.rename(tmp_path / "original-report")
            selected.parent.symlink_to(outside, target_is_directory=True)
        else:
            selected.unlink()
            if replacement == "fifo":
                os.mkfifo(selected)
            else:
                selected.symlink_to(outside / "SKILL.md")
        return original(budget)

    monkeypatch.setattr(_MetadataBudget, "read_limit", swap_before_read)

    @asynccontextmanager
    async def connect(sandbox):
        root = Path(sandbox.state.manifest.root)
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
        skills=Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=Path("skills")))),
    )
    responses.respond(assistant_output("done"))
    with pytest.raises(LocalDirReadError):
        async with Agent(
            instructions="Read the current index.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=source.skill_tools,
        ).session(environment=source) as session:
            await (await session.submit("Read the index.")).get_result()
    assert responses.requests == []


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="UnixLocal resource")
@pytest.mark.parametrize("limit", ["bytes", "members"])
@pytest.mark.parametrize(
    "state_mode",
    [
        "shared",
        "resumed",
        "serialized",
        "pydantic_json",
        "pydantic_dict",
        "snapshot_shared",
        "snapshot_serialized",
        "snapshot_repository",
        "snapshot_mismatch",
        "recreated",
    ],
)
async def test_native_skill_budget_survives_environment_lease_retirement(
    tmp_path, monkeypatch, responses, limit, state_mode
):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    monkeypatch.chdir(tmp_path)
    for name in ("alpha", "beta"):
        selected = tmp_path / name / name
        selected.mkdir(parents=True)
        (selected / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
        if limit == "bytes":
            with (selected / "large.bin").open("wb") as handle:
                handle.truncate(
                    1024
                    if state_mode in ("snapshot_repository", "snapshot_mismatch") and name == "beta"
                    else 40 * 1024 * 1024
                )
        else:
            for index in range(
                1
                if state_mode in ("snapshot_repository", "snapshot_mismatch") and name == "beta"
                else 2050
            ):
                (selected / str(index)).touch()

    client = UnixLocalSandboxClient()
    from agents.sandbox import LocalSnapshotSpec

    options = UnixLocalSandboxClientOptions()
    borrowed = await client.create(
        manifest=Manifest(),
        options=options,
        snapshot=LocalSnapshotSpec(base_path=tmp_path / "snapshots")
        if state_mode.startswith("snapshot")
        else None,
    )
    await borrowed.start()
    root = Path(borrowed.state.manifest.root)
    skills_path = "stored-skills" if state_mode == "snapshot_serialized" else ".agents"
    if state_mode == "snapshot_repository":
        with (root / "unrelated-repository.bin").open("wb") as handle:
            handle.truncate(80 * 1024 * 1024)
    saved = (
        client.serialize_session_state(borrowed.state)
        if state_mode == "serialized"
        else borrowed.state.model_copy(deep=True)
    )
    if state_mode == "pydantic_json":
        saved = type(borrowed.state).model_validate_json(borrowed.state.model_dump_json())
    elif state_mode == "pydantic_dict":
        saved = type(borrowed.state).model_validate(borrowed.state.model_dump())
    if state_mode == "serialized":
        assert "_native_skill_copy_budget" not in saved
        saved["_native_skill_copy_budget"] = {"trusted": True, "bytes": 0, "members": 0}
    alias = None
    current = borrowed
    try:
        async with ExecutorPeer(tmp_path / "executor").serve() as peer:
            trust(peer, tmp_path, monkeypatch)

            @asynccontextmanager
            async def connect(selected):
                assert selected is current
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(root),
                    tools=(),
                    capability_directories=[str(root / skills_path)],
                )

            for name in ("alpha", "beta"):
                if name == "beta" and state_mode != "shared":
                    if state_mode.startswith("snapshot") or state_mode == "recreated":
                        await borrowed.aclose()
                        saved = borrowed.state.model_copy(deep=True)
                        if state_mode == "snapshot_mismatch":
                            import tarfile

                            with tarfile.open(saved.snapshot._path(), "a") as archive:
                                if limit == "bytes":
                                    member = tarfile.TarInfo("unaccounted.bin")
                                    member.size = 40 * 1024 * 1024
                                    with (tmp_path / "alpha/alpha/large.bin").open("rb") as data:
                                        archive.addfile(member, data)
                                else:
                                    for index in range(2050):
                                        archive.addfile(tarfile.TarInfo(f"unaccounted-{index}"))
                        if state_mode == "snapshot_serialized":
                            saved = client.deserialize_session_state(
                                client.serialize_session_state(saved)
                            )
                        await client.delete(borrowed)
                        assert not root.exists()
                    restored = (
                        client.deserialize_session_state(saved)
                        if state_mode == "serialized"
                        else saved
                    )
                    alias = await client.resume(restored)
                    await alias.start()
                    current = alias
                source = sandbox_environment(
                    client,
                    manifest=current.state.manifest,
                    options=options,
                    session=current,
                    connect=connect,
                    skills=Skills(
                        skills_path=skills_path,
                        lazy_from=LocalDirLazySkillSource(source=LocalDir(src=Path(name))),
                    ),
                )
                responses.respond(function_call("load_skill", {"skill_name": name}, name))
                responses.respond(assistant_output("done"))
                async with Agent(
                    instructions="Load the current skill.",
                    model="gpt-5.5",
                    provider=provider(responses),
                    tools=source.skill_tools,
                ).session(environment=source) as session:
                    if name == "alpha" or state_mode in ("snapshot_repository", "recreated"):
                        assert (
                            await (await session.submit(f"Load {name}.")).get_result()
                        ).final_output == "done"
                    else:
                        error = "byte" if limit == "bytes" else "member"
                        message = (
                            "quota continuity"
                            if state_mode in ("serialized", "pydantic_json", "pydantic_dict")
                            else f"sandbox {error} limit"
                        )
                        with pytest.raises(ValueError, match=message):
                            await (await session.submit("Load beta.")).get_result()
            assert (root / skills_path / "alpha/SKILL.md").exists() is (state_mode != "recreated")
            assert (root / skills_path / "beta").exists() is (
                state_mode in ("snapshot_repository", "recreated")
            )
    finally:
        if alias is not None:
            await alias.aclose()
        await borrowed.aclose()
        await client.delete(borrowed)


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="UnixLocal resource")
async def test_native_duplicate_lazy_load_copies_and_charges_once(tmp_path, monkeypatch, responses):
    import asyncio
    import json

    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    monkeypatch.chdir(tmp_path)
    selected = tmp_path / "skills/report"
    selected.mkdir(parents=True)
    (selected / "SKILL.md").write_bytes(b"---\nname: report\n---\n")
    with (selected / "large.bin").open("wb") as handle:
        handle.truncate(40 * 1024 * 1024)
    both_calls = asyncio.Event()
    arrivals = 0
    original = LocalDir._apply_bounded

    async def concurrent_copy(source, *args, **kwargs):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            both_calls.set()
        await asyncio.wait_for(both_calls.wait(), timeout=10)
        return await original(source, *args, **kwargs)

    monkeypatch.setattr(LocalDir, "_apply_bounded", concurrent_copy)

    @asynccontextmanager
    async def connect(sandbox):
        root = Path(sandbox.state.manifest.root)
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
        manifest=Manifest(),
        options=UnixLocalSandboxClientOptions(),
        connect=connect,
        skills=Skills(lazy_from=LocalDirLazySkillSource(source=LocalDir(src=Path("skills")))),
    )
    responses.respond(
        function_call("load_skill", {"skill_name": "report"}, "first-load"),
        function_call("load_skill", {"skill_name": "report"}, "second-load"),
    )
    responses.respond(assistant_output("done"))
    async with Agent(
        instructions="Load report twice concurrently.",
        model="gpt-5.5",
        provider=provider(responses),
        tools=source.skill_tools,
    ).session(environment=source) as session:
        assert (
            await (await session.submit("Load report twice.")).get_result()
        ).final_output == "done"
    assert arrivals == 2
    assert sorted(
        json.loads(tool_output(responses.requests[1], call_id))["status"]
        for call_id in ("first-load", "second-load")
    ) == ["already_loaded", "loaded"]


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("backend", ["unix_local", "docker"])
async def test_native_skill_quota_tracks_fresh_resource(
    tmp_path, monkeypatch, responses, replacement, backend
):
    import shutil

    workspace = tmp_path / "workspace"
    transport = None
    if backend == "docker":
        docker = pytest.importorskip("docker")
        from agents.classic.sandbox.sandboxes.docker import (
            DockerSandboxClient,
            DockerSandboxClientOptions,
        )

        try:
            transport = docker.from_env()
            transport.ping()
            transport.images.get("python:3.14-slim")
        except docker.errors.DockerException:
            pytest.skip("Cached Docker skill fixture unavailable")
        client = DockerSandboxClient(transport)
        options = DockerSandboxClientOptions(image="python:3.14-slim", network_mode="none")
        # The loopback executor has been resolving this host path independently of Docker.
        workspace.mkdir()
    else:
        if sys.platform not in ("linux", "darwin"):
            pytest.skip("UnixLocal resource")
        from agents.classic.sandbox.sandboxes.unix_local import (
            UnixLocalSandboxClient,
            UnixLocalSandboxClientOptions,
        )

        client = UnixLocalSandboxClient()
        options = UnixLocalSandboxClientOptions()
    original = await client.create(manifest=Manifest(root=str(workspace)), options=options)
    resumed = None
    try:
        await original.start()
        saved = client.deserialize_session_state(client.serialize_session_state(original.state))
        if replacement:
            await original.aclose()
            await client.delete(original)
            if backend == "unix_local":
                shutil.rmtree(workspace)
        resumed = await client.resume(saved)
        if backend == "docker":
            assert (resumed.state.container_id != original.state.container_id) is replacement
        await resumed.start()
        monkeypatch.chdir(tmp_path)
        selected = tmp_path / "skills/report"
        selected.mkdir(parents=True)
        (selected / "SKILL.md").write_text("---\nname: report\n---\nFULL_DOC", encoding="utf-8")
        async with ExecutorPeer(tmp_path / "executor").serve() as peer:
            trust(peer, tmp_path, monkeypatch)

            @asynccontextmanager
            async def connect(selected_session):
                assert selected_session is resumed
                root = resumed.state.manifest.root
                yield Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(root),
                    tools=(),
                    capability_directories=[str(Path(root) / ".agents")],
                )

            source = sandbox_environment(
                client,
                manifest=resumed.state.manifest,
                options=options,
                session=resumed,
                connect=connect,
                skills=Skills(
                    lazy_from=LocalDirLazySkillSource(source=LocalDir(src=Path("skills")))
                ),
            )
            responses.respond(function_call("load_skill", {"skill_name": "report"}, "docker-load"))
            responses.respond(assistant_output("done"))
            async with Agent(
                instructions="Load the current skill.",
                provider=provider(responses),
                tools=source.skill_tools,
            ).session(environment=source) as session:
                if replacement:
                    assert (
                        await (await session.submit("Load report.")).get_result()
                    ).final_output == "done"
                else:
                    with pytest.raises(ValueError, match="quota continuity"):
                        await (await session.submit("Load report.")).get_result()
            if replacement:
                assert b"FULL_DOC" in await resumed.read(Path(".agents/report/SKILL.md"))
    finally:
        if resumed is not None:
            await resumed.aclose()
            await client.delete(resumed)
        await original.aclose()
        await client.delete(original)
        if transport is not None:
            transport.close()


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="UnixLocal snapshot")
async def test_nonseekable_snapshot_persist_retires_private_quota_receipt(tmp_path, monkeypatch):
    import io

    from agents.classic.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
    from agents.sandbox import LocalSnapshotSpec

    client = UnixLocalSandboxClient()
    resource = await client.create(snapshot=LocalSnapshotSpec(base_path=tmp_path / "snapshots"))
    try:
        await resource.start()
        await resource.stop()
        budget = resource.state._native_skill_copy_budget
        assert budget.snapshot_digest is not None
        budget.restore_candidate = (budget.members, budget.bytes, budget.snapshot_digest)
        original = resource._inner.persist_workspace

        class Stream(io.IOBase):
            def __init__(self, source):
                self.source = source

            def read(self, size=-1):
                return self.source.read(size)

            def close(self):
                self.source.close()
                super().close()

        async def nonseekable():
            return Stream(await original())

        monkeypatch.setattr(resource._inner, "persist_workspace", nonseekable)
        await resource._inner._persist_snapshot()
        assert budget.snapshot_digest is None
        assert budget.restore_candidate is None
    finally:
        await resource.aclose()
        await client.delete(resource)


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="UnixLocal snapshot")
@pytest.mark.parametrize("failure", ["list", "read"])
async def test_eager_restore_metadata_failure_precedes_environment_publication(
    tmp_path, monkeypatch, responses, failure
):
    from agents.classic.sandbox.entries import Dir, File
    from agents.classic.sandbox.errors import WorkspaceArchiveReadError
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
        UnixLocalSandboxSession,
    )
    from agents.sandbox import LocalSnapshotSpec

    client = UnixLocalSandboxClient()
    options = UnixLocalSandboxClientOptions()
    original = await client.create(
        manifest=Manifest(
            entries={
                Path("keep.txt"): File(content=b"unrelated workspace data"),
                Path(".agents/retired/SKILL.md"): File(
                    content=b"---\nname: retired\ndescription: RETIRED_SKILL\n---\n"
                ),
            }
        ),
        options=options,
        snapshot=LocalSnapshotSpec(base_path=tmp_path / "snapshots"),
    )
    await original.start()
    root = Path(original.state.manifest.root)
    await original.aclose()
    saved = original.state.model_copy(deep=True)
    await client.delete(original)

    attempted = False
    connected = 0
    method = "_ls_bounded" if failure == "list" else "_read_bounded"
    previous = getattr(UnixLocalSandboxSession, method)

    async def fail_once(self, path, **kwargs):
        nonlocal attempted
        target = root / ".agents" if failure == "list" else root / ".agents/retired/SKILL.md"
        if Path(path) == target and not attempted:
            attempted = True
            raise OSError("synthetic one-shot skill metadata failure")
        return await previous(self, path, **kwargs)

    monkeypatch.setattr(UnixLocalSandboxSession, method, fail_once)

    @asynccontextmanager
    async def connect(selected):
        nonlocal connected
        connected += 1
        assert not (root / ".agents/retired").exists()
        assert (root / "keep.txt").read_bytes() == b"unrelated workspace data"
        async with ExecutorPeer(tmp_path / "executor").serve() as peer:
            trust(peer, tmp_path, monkeypatch)
            yield Environment(
                url=peer.url,
                headers={"Authorization": "Bearer " + peer.token},
                cwd=str(root),
                tools=(),
                capability_directories=[str(root / ".agents")],
            )

    def source():
        return sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(root=str(root)),
            options=options,
            authorized_resume_state=saved,
            connect=connect,
            skills=Skills(
                from_=Dir(
                    children={
                        "replacement": Dir(
                            children={
                                "SKILL.md": File(
                                    content=(
                                        b"---\nname: replacement\ndescription: CURRENT_SKILL\n---\n"
                                    )
                                )
                            }
                        ),
                    }
                )
            ),
        )

    agent = Agent(instructions="Inspect current skills.", provider=provider(responses))
    expected = OSError if failure == "list" else WorkspaceArchiveReadError
    with pytest.raises(expected):
        async with agent.session(environment=source(), discover_agents_md=False):
            pytest.fail("An unreadable prior skill index reached environment publication")
    assert attempted and connected == 0 and not responses.requests

    responses.respond(assistant_output("done"))
    async with agent.session(environment=source(), discover_agents_md=False) as session:
        assert (await (await session.submit("Inspect")).get_result()).final_output == "done"
    assert connected == 1
    assert "CURRENT_SKILL" in str(responses.requests) and "RETIRED_SKILL" not in str(
        responses.requests
    )
