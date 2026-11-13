from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents import Agent, Environment
from agents.classic.sandbox.entries import File, LocalFile
from agents.classic.sandbox.errors import LocalFileReadError
from agents.classic.sandbox.workspace_paths import SandboxPathGrant
from agents.classic.tracing import set_tracing_disabled
from agents.sandbox import Manifest, SandboxConcurrencyLimits, sandbox_environment
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider

if sys.platform in ("linux", "darwin"):
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="UnixLocal resource"
)


@asynccontextmanager
async def live_provider(tmp_path, monkeypatch, grants=()):
    set_tracing_disabled(True)
    client = UnixLocalSandboxClient()
    selected = await client.create(
        manifest=Manifest(entries={"old.txt": File(content=b"old")}, extra_path_grants=grants)
    )
    await selected.start()
    root = Path(selected.state.manifest.root)
    peer = ExecutorPeer(tmp_path / "peer")
    try:
        async with peer.serve():
            trust(peer, tmp_path, monkeypatch)

            @asynccontextmanager
            async def connect(sandbox):
                assert sandbox is selected
                yield Environment(
                    url=peer.url,
                    cwd=str(root),
                    headers={"Authorization": "Bearer " + peer.token},
                    tools=[],
                )
                await peer.wait_disconnected()

            yield client, selected, connect
    finally:
        await selected.aclose()


@asynccontextmanager
async def owned_provider(tmp_path, monkeypatch, *, initial_limits=None):
    set_tracing_disabled(True)
    client = UnixLocalSandboxClient()
    created = []
    original_create = client.create

    async def create(**kwargs):
        selected = await original_create(**kwargs)
        if initial_limits is not None:
            selected._set_concurrency_limits(initial_limits)
        created.append(selected)
        return selected

    monkeypatch.setattr(client, "create", create)
    peer = ExecutorPeer(tmp_path / "peer")
    async with peer.serve():
        trust(peer, tmp_path, monkeypatch)

        @asynccontextmanager
        async def connect(sandbox):
            assert sandbox is created[-1]
            yield Environment(
                url=peer.url,
                cwd=str(sandbox.state.manifest.root),
                headers={"Authorization": "Bearer " + peer.token},
                tools=[],
            )
            await peer.wait_disconnected()

        yield client, created, connect


def source_for(client, selected, target, connect, **kwargs):
    return sandbox_environment(
        client,
        options=UnixLocalSandboxClientOptions(),
        manifest=target,
        session=selected,
        connect=connect,
        **kwargs,
    )


async def run_source(source, responses):
    async with Agent(instructions="Use the workspace.", provider=provider(responses)).session(
        environment=source
    ) as session:
        return await (await session.submit("start")).get_result()


@pytest.mark.parametrize("granted", [False, True])
async def test_native_has_been_applying_current_external_grant_on_owned_materialization(
    tmp_path, monkeypatch, responses, granted
):
    external = tmp_path / "external"
    external.mkdir()
    (external / "source.txt").write_text("current grant")
    grant = SandboxPathGrant(path=str(external), read_only=True)
    async with owned_provider(tmp_path, monkeypatch) as (client, created, connect):
        target = Manifest(
            entries={"imported.txt": LocalFile(src=external / "source.txt")},
            extra_path_grants=(grant,) if granted else (),
        )
        source = sandbox_environment(
            client, options=UnixLocalSandboxClientOptions(), manifest=target, connect=connect
        )
        agent = Agent(instructions="Use the workspace.", provider=provider(responses))
        if granted:
            responses.respond(assistant_output("done"))
            async with agent.session(environment=source) as session:
                assert (await (await session.submit("start")).get_result()).final_output == "done"
                selected = created[0]
                assert (
                    Path(selected.state.manifest.root) / "imported.txt"
                ).read_text() == "current grant"
                assert selected.state.manifest.extra_path_grants == (grant,)
        else:
            with pytest.raises(LocalFileReadError):
                async with agent.session(environment=source):
                    raise AssertionError("Unexpected session")
            assert not responses.requests
        assert len(created) == 1
        assert not Path(created[0].state.manifest.root).exists()


async def test_native_has_been_rejecting_removed_historical_external_grant(
    tmp_path, monkeypatch, responses
):
    external = tmp_path / "external"
    external.mkdir()
    (external / "source.txt").write_text("historical permission")
    grant = SandboxPathGrant(path=str(external), read_only=True)
    async with live_provider(tmp_path, monkeypatch, (grant,)) as (client, selected, connect):
        original = selected.state.manifest
        target = original.model_copy(
            update={
                "entries": {
                    **original.entries,
                    "imported.txt": LocalFile(src=external / "source.txt"),
                },
                "extra_path_grants": (),
            },
            deep=True,
        )
        rejected = False
        try:
            async with Agent(
                instructions="Use the workspace.", provider=provider(responses)
            ).session(environment=source_for(client, selected, target, connect)):
                pass
        except ValueError as error:
            assert "atomic provider transaction" in str(error)
            rejected = True
        imported = Path(target.root) / "imported.txt"
        assert rejected, f"historical grant materialization: {imported.exists()}"
        assert not imported.exists()
        assert selected.state.manifest is original
        assert not responses.requests


async def test_native_has_been_limiting_owned_manifest_bodies_before_gateway(
    tmp_path, monkeypatch, responses
):
    async with owned_provider(
        tmp_path,
        monkeypatch,
        initial_limits=SandboxConcurrencyLimits(manifest_entries=7),
    ) as (client, created, connect):
        target = Manifest(
            entries={"first.txt": File(content=b"first"), "second.txt": File(content=b"second")}
        )
        original_apply = File.apply
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        peak = 0

        async def held_apply(self, session, dest, base_dir):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            assert created[0]._max_manifest_entry_concurrency == 1
            assert created[0]._inner._max_manifest_entry_concurrency == 1
            entered.set()
            try:
                await release.wait()
                return await original_apply(self, session, dest, base_dir)
            finally:
                active -= 1

        monkeypatch.setattr(File, "apply", held_apply)
        responses.respond(assistant_output("done"))
        pending = asyncio.create_task(
            run_source(
                sandbox_environment(
                    client,
                    options=UnixLocalSandboxClientOptions(),
                    manifest=target,
                    connect=connect,
                    concurrency_limits=SandboxConcurrencyLimits(manifest_entries=1),
                ),
                responses,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.sleep(0.04)
        finally:
            release.set()
        assert (await pending).final_output == "done"
        assert peak == 1
        assert created[0]._max_manifest_entry_concurrency == 1
        assert created[0]._inner._max_manifest_entry_concurrency == 1
        assert not Path(created[0].state.manifest.root).exists()


@pytest.mark.parametrize("custom", [False, True])
async def test_native_has_been_preserving_borrowed_grants_and_limits_before_rejected_batch(
    tmp_path, monkeypatch, responses, custom
):
    external = tmp_path / "external"
    external.mkdir()
    grant = SandboxPathGrant(path=str(external), read_only=True)
    async with live_provider(tmp_path, monkeypatch) as (client, selected, connect):
        if custom:
            selected._set_concurrency_limits(
                SandboxConcurrencyLimits(manifest_entries=7, local_dir_files=11)
            )
        old_limits = [
            (owner._max_manifest_entry_concurrency, owner._max_local_dir_file_concurrency)
            for owner in (selected, selected._inner)
        ]
        original = selected.state.manifest
        ready = selected.state.workspace_root_ready
        target = original.model_copy(
            update={
                "entries": {**original.entries, "added.txt": File(content=b"added")},
                "extra_path_grants": (grant,),
            },
            deep=True,
        )
        apply = AsyncMock(side_effect=AssertionError("Borrowed batch entered"))
        monkeypatch.setattr(selected, "_apply_entry_batch", apply)
        with pytest.raises(ValueError, match="atomic provider transaction"):
            async with Agent(
                instructions="Use the workspace.", provider=provider(responses)
            ).session(
                environment=source_for(
                    client,
                    selected,
                    target,
                    connect,
                    concurrency_limits=SandboxConcurrencyLimits(manifest_entries=1),
                )
            ):
                raise AssertionError("Unexpected session")
        apply.assert_not_awaited()
        assert selected.state.manifest is original
        assert selected.state.workspace_root_ready is ready
        assert [
            (owner._max_manifest_entry_concurrency, owner._max_local_dir_file_concurrency)
            for owner in (selected, selected._inner)
        ] == old_limits
        assert (Path(original.root) / "old.txt").read_bytes() == b"old"
        assert not (Path(original.root) / "added.txt").exists()
        assert not responses.requests
