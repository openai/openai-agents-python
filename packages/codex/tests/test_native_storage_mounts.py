"""Application-owned storage mounts through the public native boundary."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from uuid import uuid4

import pytest

from agents import (
    Agent,
    CapabilityError,
    Environment,
    LocalStorage,
    LocalStorageMount,
    NativeProvider,
)
from agents.executor import LocalStorageMount as FacadeMount
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_executor_local import _command_is_running, _owned_command


def mount_at(path: Path) -> LocalStorageMount:
    return LocalStorageMount(path, path, strategy="local_bind")


def test_mount_export_and_frozen_declaration(tmp_path: Path) -> None:
    assert FacadeMount is LocalStorageMount
    mount = mount_at(tmp_path)
    assert mount.source == str(tmp_path)
    assert mount.read_only is True
    with pytest.raises(FrozenInstanceError):
        mount.source = "/other"
    with pytest.raises(TypeError, match="strategy"):
        LocalStorageMount(tmp_path, tmp_path)  # type: ignore[call-arg]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux mount declaration")
def test_typed_and_dictionary_mounts_have_the_same_wire_shape(tmp_path: Path) -> None:
    config = {"source": str(tmp_path), "destination": str(tmp_path), "strategy": "local_bind"}
    typed = Environment.local(cwd=tmp_path.parent, mounts=[mount_at(tmp_path)])
    dictionary = Environment.local(cwd=tmp_path.parent, mounts=[config])
    assert dictionary.mounts == typed.mounts
    assert typed._to_native()["mounts"] == [{**config, "read_only": True}]


@pytest.mark.parametrize(
    "change",
    [
        {"strategy": "copy"},
        {"read_only": False},
        {"read_only": 1},
        {"provider": "s3"},
        {"credential": "do-not-reflect-this-value"},
        {"source": "relative"},
        {"destination": "/relocated"},
    ],
)
def test_unsupported_mount_declarations_fail_without_effects(tmp_path: Path, change: dict) -> None:
    config = {"source": str(tmp_path), "destination": str(tmp_path), "strategy": "local_bind"}
    with pytest.raises((TypeError, ValueError)) as error:
        Environment.local(cwd=tmp_path, mounts=[{**config, **change}])
    assert "do-not-reflect-this-value" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_mount_paths_reject_missing_file_and_symlink(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.write_text("preserved")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    for path in (file, alias, tmp_path / "missing", tmp_path / "alias" / "alias"):
        with pytest.raises(ValueError, match="directories|symlinks"):
            mount_at(path)
    assert file.read_text() == "preserved"


def test_remote_and_non_linux_mounts_fail_at_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = mount_at(tmp_path)
    with pytest.raises(ValueError, match="local on Linux"):
        Environment(url="wss://executor.invalid", cwd="/job", mounts=[mount])
    monkeypatch.setattr("_agents_codex.executor.sys.platform", "darwin")
    with pytest.raises(ValueError, match="local on Linux"):
        Environment.local(cwd=tmp_path, mounts=[mount])


@pytest.mark.skipif(sys.platform != "linux", reason="Linux native preflight")
@pytest.mark.parametrize(
    "kind", ["cwd", "writable_alias", "nested", "private", "removed", "symlink"]
)
async def test_native_preflight_rejects_mounts_before_helpers_or_model(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    work, source = tmp_path / "work", tmp_path / "source"
    work.mkdir()
    source.mkdir()
    preserved = source / "preserved"
    preserved.write_text("caller data")
    mounts = [mount_at(source)]
    roots = []
    storage = None
    if kind == "cwd":
        work = source
    elif kind == "writable_alias":
        alias = tmp_path / "alias"
        alias.symlink_to(source, target_is_directory=True)
        roots = [alias]
    elif kind == "nested":
        nested = source / "nested"
        nested.mkdir()
        mounts.append(mount_at(nested))
    elif kind == "private":
        storage = LocalStorage(source / "private")
    elif kind == "removed":
        selected = Environment.local(cwd=work, mounts=mounts)
        preserved.unlink()
        source.rmdir()
    else:
        selected = Environment.local(cwd=work, mounts=mounts)
        source.rename(tmp_path / "original")
        source.symlink_to(tmp_path / "original", target_is_directory=True)
    if kind not in ("removed", "symlink"):
        selected = Environment.local(cwd=work, mounts=mounts, writable_roots=roots)
    # A helper with an observable effect must never run on rejected grants.
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    marker = tmp_path / "helper-ran"
    helper = helpers / "codex-sdk-runtime"
    helper.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
    helper.chmod(0o755)
    monkeypatch.setattr("_agents_codex.session._package_files", lambda _: tmp_path)
    helpers.rename(tmp_path / "bin")
    agent = Agent(
        instructions="Use the selected tools.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-key"),
    )
    with pytest.raises(RuntimeError, match="storage mounts|native SDK storage"):
        async with agent.session(environment=selected, storage=storage):
            pytest.fail("Invalid mounts opened a session")
    assert not marker.exists()
    assert responses.requests == []
    if kind != "removed":
        assert preserved.read_text() == "caller data"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux storage entry preflight")
@pytest.mark.parametrize("kind", ["fifo", "socket"])
async def test_mount_special_nodes_rejected_before_effects(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    work, source = tmp_path / "work", tmp_path / "source"
    work.mkdir()
    source.mkdir()
    nested = source / "nested"
    nested.mkdir()
    data = source / "data"
    data.write_bytes(b"caller data\n")
    data.chmod(0o640)
    endpoint = nested / "endpoint"
    endpoint_socket = None
    if kind == "fifo":
        os.mkfifo(endpoint, 0o600)
    else:
        endpoint_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        endpoint_socket.bind(str(endpoint))
    original = {
        path: (path.lstat().st_mode, path.lstat().st_dev, path.lstat().st_ino)
        for path in (source, nested, data, endpoint)
    }
    package = tmp_path / "sdk" / "agents"
    helpers = package / "bin"
    helpers.mkdir(parents=True)
    marker = tmp_path / "helper-ran"
    for name in ("codex-sdk-runtime", "codex-linux-sandbox"):
        helper = helpers / name
        helper.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
        helper.chmod(0o755)
    monkeypatch.setattr("_agents_codex.session._package_files", lambda _: package)
    selected = Environment.local(cwd=work, mounts=[mount_at(source)])
    try:
        async with timeout(30):
            with pytest.raises(
                RuntimeError,
                match="storage mounts must contain only regular files, directories, or symlinks",
            ):
                async with Agent(
                    instructions="Answer.",
                    provider=NativeProvider(base_url=responses.url, api_key="synthetic-key"),
                ).session(environment=selected):
                    pytest.fail("A mounted special endpoint opened a session")
    finally:
        if endpoint_socket is not None:
            endpoint_socket.close()
    assert not marker.exists()
    assert responses.requests == []
    assert data.read_bytes() == b"caller data\n"
    assert sorted(path.name for path in source.iterdir()) == ["data", "nested"]
    assert list(nested.iterdir()) == [endpoint]
    for path, identity in original.items():
        metadata = path.lstat()
        assert (metadata.st_mode, metadata.st_dev, metadata.st_ino) == identity


@pytest.mark.skipif(sys.platform != "linux", reason="Linux hardlink preflight")
@pytest.mark.parametrize("location", ["cwd", "additional"])
async def test_mount_hardlink_alias_rejected_before_effects(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    work, source, extra = (tmp_path / name for name in ("work", "source", "extra"))
    for directory in (work, source, extra):
        directory.mkdir()
    data = source / "data"
    data.write_bytes(b"caller data\n")
    data.chmod(0o640)
    alias = (work if location == "cwd" else extra) / "alias"
    alias.hardlink_to(data)
    original = {path: (path.read_bytes(), path.stat().st_mode) for path in (data, alias)}
    source_mode = source.stat().st_mode
    helpers = tmp_path / "bin"
    helpers.mkdir()
    marker = tmp_path / "helper-ran"
    for name in ("codex-sdk-runtime", "codex-linux-sandbox"):
        helper = helpers / name
        helper.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
        helper.chmod(0o755)
    monkeypatch.setattr("_agents_codex.session._package_files", lambda _: tmp_path)
    selected = Environment.local(
        cwd=work,
        writable_roots=[extra] if location == "additional" else [],
        mounts=[mount_at(source)],
    )
    with pytest.raises(
        RuntimeError, match="storage mounts share a hard-linked file with cwd or writable roots"
    ):
        async with Agent(
            instructions="Answer.",
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-key"),
        ).session(environment=selected):
            pytest.fail("A writable alias to mounted data opened a session")
    assert not marker.exists()
    assert responses.requests == []
    assert source.stat().st_mode == source_mode
    for path, (contents, mode) in original.items():
        assert path.read_bytes() == contents
        assert path.stat().st_mode == mode


@pytest.mark.skipif(sys.platform != "linux", reason="Linux hardlink preflight")
@pytest.mark.parametrize("location", ["source", "writable"])
async def test_mount_hardlink_inspection_failure_is_closed(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    work, source, outside = (tmp_path / name for name in ("work", "source", "outside"))
    for directory in (work, source, outside):
        directory.mkdir()
    data = source / "data"
    data.write_text("caller data")
    (outside / "alias").hardlink_to(data)
    mode = data.stat().st_mode
    blocked = (source if location == "source" else work) / "blocked"
    blocked.mkdir()
    helpers = tmp_path / "bin"
    helpers.mkdir()
    marker = tmp_path / "helper-ran"
    for name in ("codex-sdk-runtime", "codex-linux-sandbox"):
        helper = helpers / name
        helper.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
        helper.chmod(0o755)
    monkeypatch.setattr("_agents_codex.session._package_files", lambda _: tmp_path)
    selected = Environment.local(cwd=work, mounts=[mount_at(source)])
    blocked.chmod(0)
    try:
        with pytest.raises(
            RuntimeError, match="could not inspect storage mounts and writable roots for hard links"
        ):
            async with Agent(
                instructions="Answer.",
                provider=NativeProvider(base_url=responses.url, api_key="synthetic-key"),
            ).session(environment=selected):
                pytest.fail("An incomplete hardlink inspection opened a session")
    finally:
        blocked.chmod(0o700)
    assert not marker.exists()
    assert responses.requests == []
    assert data.read_text() == "caller data"
    assert data.stat().st_mode == mode


@pytest.mark.skipif(sys.platform != "linux", reason="Linux native bind mounts")
@pytest.mark.requires_native_linux_sandbox
async def test_real_mount_read_mutation_denial_updates_and_restore(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    work, source, other = (tmp_path / name for name in ("work", "source", "other"))
    for directory in (work, source, other):
        directory.mkdir()
    data = source / "data"
    data.write_text("caller data")
    original_mode = source.stat().st_mode
    qdata, qsource = shlex.quote(str(data)), shlex.quote(str(source))
    command = (
        f"set -e; test \"$(cat {qdata})\" = 'caller data'; "
        f"if (printf changed >> {qdata}) 2>/dev/null; then exit 11; fi; "
        f"if rm {qdata} 2>/dev/null; then exit 12; fi; "
        f"if touch {qsource}/new 2>/dev/null; then exit 13; fi; "
        f"if chmod 777 {qdata} 2>/dev/null; then exit 14; fi; "
        "printf verified > result"
    )
    selected = Environment.local(
        cwd=work, mounts=[mount_at(source), mount_at(other)], tools=["exec_command", "apply_patch"]
    )
    responses.respond(function_call("exec_command", {"cmd": command, "login": False}, "read"))
    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "patch",
                "input": (
                    f"*** Begin Patch\n*** Update File: {data}\n"
                    "@@\n-caller data\n+changed\n*** End Patch"
                ),
            },
        }
    )
    responses.respond(assistant_output("done"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-key")
    agent = Agent(instructions="Use the selected tools.", provider=provider)
    async with timeout(60), provider:
        async with agent.session(environment=selected) as session:
            with pytest.raises(CapabilityError, match="storage mounts are active"):
                await session.submit("Replace grants", writable_roots=[])
            native_refusal = json.loads(
                await session._native._submit(
                    json.dumps(
                        {
                            "mode": "start_if_idle",
                            "input": [],
                            "writable_roots": [],
                        }
                    )
                )
            )
            assert native_refusal["status"] == "not_submitted"
            assert "storage mounts are active" in native_refusal["reason"]["message"]
            assert responses.requests == []
            turn = await session.submit("Read and try mutations")
            assert (await turn.get_result()).final_output == "done"
            assert "Process exited with code 0" in tool_output(responses.requests[1], "read")
            saved = await session.save_state()
        assert saved.executor_policy_digest is not None
        for changed in (replace(selected, mounts=[]), replace(selected, mounts=[mount_at(other)])):
            with pytest.raises(ValueError, match="agree on the applied executor policy"):
                async with agent.session(environment=changed, state=saved):
                    pytest.fail("Restore accepted changed read authority")
        async with agent.session(
            environment=replace(selected, mounts=[mount_at(other), mount_at(source)]), state=saved
        ) as reordered:
            reordered_state = await reordered.save_state()
            assert reordered_state.executor_policy_digest == saved.executor_policy_digest
        async with agent.session(environment=selected, state=saved):
            pass
    assert data.read_text() == "caller data"
    assert source.stat().st_mode == original_mode
    assert list(source.iterdir()) == [data]
    assert (work / "result").read_text() == "verified"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux native bind mounts")
@pytest.mark.requires_native_linux_sandbox
async def test_cancellation_preserves_caller_source(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    work, source = tmp_path / "work", tmp_path / "source"
    work.mkdir()
    source.mkdir()
    (source / "data").write_text("caller data")
    entered = asyncio.Event()

    async def hold_result(ctx, result):
        entered.set()
        await asyncio.Future()

    selected = Environment.local(
        cwd=work, mounts=[mount_at(source)], tools=["exec_command"], transform_result=hold_result
    )
    command_name = "agents-storage-mount-" + uuid4().hex
    script = shlex.quote(f"exec -a {command_name} /usr/bin/sleep 60")
    responses.respond(
        function_call(
            "exec_command",
            {"cmd": f"/bin/bash -c {script}", "yield_time_ms": 250, "login": False},
            "held",
        )
    )
    async with (
        timeout(40),
        Agent(
            instructions="Use the selected tools.",
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-key"),
        ).session(environment=selected) as session,
    ):
        turn = await session.submit("Start a command")
        await entered.wait()
        command = await _owned_command(command_name)
        assert _command_is_running(command, command_name)
        await turn.cancel()
        assert not _command_is_running(command, command_name)
    assert not _command_is_running(command, command_name)
    assert (source / "data").read_text() == "caller data"
    assert sorted(path.name for path in source.iterdir()) == ["data"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux native startup")
@pytest.mark.parametrize(
    "links", ["none", "readonly", "outside", "single_link_unreadable_writable", "ordinary_tree"]
)
async def test_helper_startup_failure_preserves_source(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, links: str
) -> None:
    work, source = tmp_path / "work", tmp_path / "source"
    work.mkdir()
    source.mkdir()
    data = source / "data"
    data.write_text("caller data")
    mode = source.stat().st_mode
    blocked = None
    if links == "ordinary_tree":
        nested = source / "nested"
        nested.mkdir()
        (nested / "data").write_text("nested caller data")
        (source / "file-link").symlink_to(data)
        (source / "directory-link").symlink_to(nested, target_is_directory=True)
        (source / "dangling-link").symlink_to(source / "absent")
    elif links == "readonly":
        (source / "alias").hardlink_to(data)
    elif links == "outside":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "alias").hardlink_to(data)
        (source / "unfollowed").symlink_to(outside, target_is_directory=True)
        (work / "unfollowed").symlink_to(outside, target_is_directory=True)
        blocked = outside
    elif links == "single_link_unreadable_writable":
        blocked = work / "blocked"
        blocked.mkdir()
    expected_entries = sorted(path.name for path in source.iterdir())
    package = tmp_path / "sdk" / "agents"
    helpers = package / "bin"
    helpers.mkdir(parents=True)
    marker = tmp_path / "helper-ran"
    for name in ("codex-sdk-runtime", "codex-linux-sandbox"):
        executable = helpers / name
        executable.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$0" > {shlex.quote(str(marker))}\nexit 1\n'
        )
        executable.chmod(0o755)
    monkeypatch.setattr("_agents_codex.session._package_files", lambda _: package)
    selected = Environment.local(cwd=work, mounts=[mount_at(source)])
    if blocked is not None:
        blocked.chmod(0)
    try:
        async with timeout(30):
            with pytest.raises(RuntimeError, match="local executor") as startup_error:
                async with Agent(
                    instructions="Use the selected tools.",
                    provider=NativeProvider(base_url=responses.url, api_key="synthetic-key"),
                ).session(environment=selected):
                    pytest.fail("A failed helper opened a session")
    finally:
        if blocked is not None:
            blocked.chmod(0o700)
    assert marker.exists(), str(startup_error.value)
    assert Path(marker.read_text().strip()) in {
        helpers / "codex-sdk-runtime",
        helpers / "codex-linux-sandbox",
    }
    assert responses.requests == []
    assert data.read_text() == "caller data"
    assert source.stat().st_mode == mode
    assert sorted(path.name for path in source.iterdir()) == expected_entries
    if links == "ordinary_tree":
        assert (source / "nested" / "data").read_text() == "nested caller data"
        for name in ("file-link", "directory-link", "dangling-link"):
            assert (source / name).is_symlink()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux native validation")
@pytest.mark.parametrize(
    "kind",
    [
        "strategy",
        "writable",
        "unknown",
        "unknown_secret_key",
        "bad_type",
        "remote",
        "missing_strategy",
        "destination_spelling",
    ],
)
async def test_native_wire_rejects_unsupported_mount_authority(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    work, source = tmp_path / "work", tmp_path / "source"
    work.mkdir()
    source.mkdir()
    selected = Environment.local(cwd=work, mounts=[mount_at(source)])
    to_native = Environment._to_native

    def untrusted_wire(self):
        result = to_native(self)
        mount = result["mounts"][0]
        if kind == "strategy":
            mount["strategy"] = "copy"
        elif kind == "writable":
            mount["read_only"] = False
        elif kind == "unknown":
            mount["credential"] = "do-not-reflect-this-value"
        elif kind == "unknown_secret_key":
            mount["do-not-reflect-this-value"] = "private field"
        elif kind == "bad_type":
            mount["read_only"] = "do-not-reflect-this-value"
        elif kind == "missing_strategy":
            del mount["strategy"]
        elif kind == "destination_spelling":
            mount["destination"] = str(source) + "/."
        else:
            result["local"] = False
            result["url"] = "wss://executor.invalid"
        return result

    monkeypatch.setattr(Environment, "_to_native", untrusted_wire)
    with pytest.raises(RuntimeError, match="storage mounts|strategy|unknown field") as error:
        async with Agent(
            instructions="Use the selected tools.",
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-key"),
        ).session(environment=selected):
            pytest.fail("Unsupported native mount authority opened a session")
    assert "do-not-reflect-this-value" not in str(error.value)
    assert responses.requests == []
    assert list(source.iterdir()) == []
