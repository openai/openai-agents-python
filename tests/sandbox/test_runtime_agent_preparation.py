from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agents import UserError
from agents.models.default_models import get_default_model
from agents.run_context import RunContextWrapper
from agents.sandbox import MemoryReadConfig, runtime_agent_preparation as sandbox_prep
from agents.sandbox.capabilities import Capability, Compaction, Memory
from agents.sandbox.entries import BaseEntry, File
from agents.sandbox.manifest import Manifest
from agents.sandbox.sandbox_agent import SandboxAgent
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession


class _Capability:
    def __init__(self, fragment: str | None, *, type: str = "test") -> None:
        self.type = type
        self.fragment = fragment
        self.manifests: list[Manifest] = []
        self.sampling_params_calls: list[dict[str, object]] = []

    def tools(self) -> list[object]:
        return []

    def sampling_params(self, sampling_params: dict[str, object]) -> dict[str, object]:
        self.sampling_params_calls.append(dict(sampling_params))
        return {}

    def required_capability_types(self) -> set[str]:
        return set()

    async def instructions(self, manifest: Manifest) -> str | None:
        self.manifests.append(manifest)
        return self.fragment


def _session_with_manifest(manifest: Manifest | None) -> object:
    return SimpleNamespace(state=SimpleNamespace(manifest=manifest))


def test_prepare_sandbox_agent_passes_session_manifest_to_capability_instructions():
    manifest = Manifest(root="/workspace")
    capability = _Capability("capability fragment")
    prepared = sandbox_prep.prepare_sandbox_agent(
        agent=SandboxAgent(
            name="sandbox",
            base_instructions="base instructions",
            instructions="additional instructions",
        ),
        session=cast(BaseSandboxSession, _session_with_manifest(manifest)),
        capabilities=cast(list[Capability], [capability]),
    )
    instructions = cast(
        Callable[[RunContextWrapper[object], SandboxAgent[object]], Awaitable[str | None]],
        prepared.instructions,
    )

    result: str | None = asyncio.run(
        cast(
            Coroutine[Any, Any, str | None],
            instructions(
                cast(RunContextWrapper[object], None),
                cast(SandboxAgent[object], prepared),
            ),
        )
    )

    assert result == (
        "base instructions\n\n"
        "# Agent instructions\n\n"
        "additional instructions\n\n"
        "# Sandbox capability instructions\n\n"
        "capability fragment\n\n"
        f"{sandbox_prep._filesystem_instructions(manifest)}"
    )
    assert capability.manifests == [manifest]


def test_prepare_sandbox_agent_wraps_capabilities_without_agent_instructions():
    manifest = Manifest(root="/workspace")
    capability = _Capability("capability fragment")
    prepared = sandbox_prep.prepare_sandbox_agent(
        agent=SandboxAgent(
            name="sandbox",
            base_instructions="base instructions",
        ),
        session=cast(BaseSandboxSession, _session_with_manifest(manifest)),
        capabilities=cast(list[Capability], [capability]),
    )
    instructions = cast(
        Callable[[RunContextWrapper[object], SandboxAgent[object]], Awaitable[str | None]],
        prepared.instructions,
    )

    result: str | None = asyncio.run(
        cast(
            Coroutine[Any, Any, str | None],
            instructions(
                cast(RunContextWrapper[object], None),
                cast(SandboxAgent[object], prepared),
            ),
        )
    )

    assert result == (
        "base instructions\n\n"
        "# Sandbox capability instructions\n\n"
        "capability fragment\n\n"
        f"{sandbox_prep._filesystem_instructions(manifest)}"
    )
    assert capability.manifests == [manifest]


def test_prepare_sandbox_agent_passes_default_model_to_capability_sampling_params() -> None:
    manifest = Manifest(root="/workspace")
    capability = _Capability(None)

    sandbox_prep.prepare_sandbox_agent(
        agent=SandboxAgent(
            name="sandbox",
            instructions="base instructions",
        ),
        session=cast(BaseSandboxSession, _session_with_manifest(manifest)),
        capabilities=cast(list[Capability], [capability]),
    )

    assert capability.sampling_params_calls == [{"model": get_default_model()}]


def test_prepare_sandbox_agent_prepares_default_compaction_policy() -> None:
    manifest = Manifest(root="/workspace")

    prepared = sandbox_prep.prepare_sandbox_agent(
        agent=SandboxAgent(
            name="sandbox",
            instructions="base instructions",
        ),
        session=cast(BaseSandboxSession, _session_with_manifest(manifest)),
        capabilities=[Compaction()],
    )

    extra_args = prepared.model_settings.extra_args
    assert extra_args is not None
    assert "context_management" in extra_args
    assert "model" not in extra_args


def test_prepare_sandbox_agent_uses_default_sandbox_instructions_when_base_missing():
    manifest = Manifest(root="/workspace")
    capability = _Capability("capability fragment")
    prepared = sandbox_prep.prepare_sandbox_agent(
        agent=SandboxAgent(
            name="sandbox",
            instructions="additional instructions",
        ),
        session=cast(BaseSandboxSession, _session_with_manifest(manifest)),
        capabilities=cast(list[Capability], [capability]),
    )
    instructions = cast(
        Callable[[RunContextWrapper[object], SandboxAgent[object]], Awaitable[str | None]],
        prepared.instructions,
    )

    result: str | None = asyncio.run(
        cast(
            Coroutine[Any, Any, str | None],
            instructions(
                cast(RunContextWrapper[object], None),
                cast(SandboxAgent[object], prepared),
            ),
        )
    )

    default_instructions = sandbox_prep.get_default_sandbox_instructions()
    assert default_instructions is not None
    assert result == (
        f"{default_instructions}\n\n"
        "# Agent instructions\n\n"
        "additional instructions\n\n"
        "# Sandbox capability instructions\n\n"
        "capability fragment\n\n"
        f"{sandbox_prep._filesystem_instructions(manifest)}"
    )
    assert capability.manifests == [manifest]


def test_filesystem_instructions_tell_model_to_ls_when_manifest_tree_is_truncated() -> None:
    entries: dict[str | Path, BaseEntry] = {
        f"file_{index:03}.txt": File(content=b"", description="x" * 40) for index in range(200)
    }
    manifest = Manifest(root="/workspace", entries=entries)

    result = sandbox_prep._filesystem_instructions(manifest)

    assert "... (truncated " in result
    assert (
        "The filesystem layout above was truncated. "
        "Use `ls` to explore specific directories before relying on omitted paths."
    ) in result


def test_prepare_sandbox_agent_validates_required_capabilities() -> None:
    manifest = Manifest(root="/workspace")

    with pytest.raises(UserError, match="Memory requires missing capabilities: filesystem, shell"):
        sandbox_prep.prepare_sandbox_agent(
            agent=SandboxAgent(
                name="sandbox",
                instructions="base instructions",
                capabilities=[Memory()],
            ),
            session=cast(BaseSandboxSession, _session_with_manifest(manifest)),
            capabilities=[Memory()],
        )

    with pytest.raises(UserError, match="Memory requires missing capabilities: shell"):
        sandbox_prep.prepare_sandbox_agent(
            agent=SandboxAgent(
                name="sandbox",
                instructions="base instructions",
                capabilities=[Memory(read=MemoryReadConfig(live_update=False), generate=None)],
            ),
            session=cast(BaseSandboxSession, _session_with_manifest(manifest)),
            capabilities=[Memory(read=MemoryReadConfig(live_update=False), generate=None)],
        )

    prepared = sandbox_prep.prepare_sandbox_agent(
        agent=SandboxAgent(
            name="sandbox",
            instructions="base instructions",
            capabilities=[Memory()],
        ),
        session=cast(BaseSandboxSession, _session_with_manifest(manifest)),
        capabilities=cast(
            list[Capability],
            [
                Memory(),
                _Capability(None, type="filesystem"),
                _Capability(None, type="shell"),
            ],
        ),
    )

    assert prepared.name == "sandbox"
