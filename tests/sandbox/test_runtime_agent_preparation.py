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
from agents.tool import CustomTool, FunctionTool


class _Capability:
    def __init__(
        self,
        fragment: str | None,
        *,
        type: str = "test",
        tools: list[object] | None = None,
    ) -> None:
        self.type = type
        self.fragment = fragment
        self.manifests: list[Manifest] = []
        self.sampling_params_calls: list[dict[str, object]] = []
        self._tools = tools or []

    def tools(self) -> list[object]:
        return list(self._tools)

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


def _function_tool(name: str) -> FunctionTool:
    async def _invoke(_ctx: object, _input: str) -> str:
        return "ok"

    return FunctionTool(
        name=name,
        description=name,
        params_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        on_invoke_tool=_invoke,
    )


def _custom_tool(name: str) -> CustomTool:
    async def _invoke(_ctx: object, _input: str) -> str:
        return "ok"

    return CustomTool(name=name, description=name, on_invoke_tool=_invoke)


def _prepare_with_tools(
    tools: list[object],
    *,
    disabled_tools: set[str] | None = None,
    agent_tools: list[object] | None = None,
) -> list[str]:
    manifest = Manifest(root="/workspace")
    agent = SandboxAgent(
        name="sandbox",
        instructions="base instructions",
        tools=cast(Any, agent_tools or []),
        disabled_tools=disabled_tools or set(),
    )
    prepared = sandbox_prep.prepare_sandbox_agent(
        agent=agent,
        session=cast(BaseSandboxSession, _session_with_manifest(manifest)),
        capabilities=cast(list[Capability], [_Capability(None, tools=tools)]),
    )
    return [cast(Any, tool).name for tool in prepared.tools]


def test_disabled_tools_defaults_to_empty_set() -> None:
    assert SandboxAgent(name="sandbox").disabled_tools == set()


def test_disabled_tools_instances_do_not_share_default() -> None:
    first = SandboxAgent(name="first")
    second = SandboxAgent(name="second")
    first.disabled_tools.add("exec_command")
    assert "exec_command" not in second.disabled_tools


def test_disabled_tools_removes_named_function_tool() -> None:
    names = _prepare_with_tools(
        [_function_tool("exec_command"), _function_tool("view_image")],
        disabled_tools={"view_image"},
    )
    assert "exec_command" in names
    assert "view_image" not in names


def test_disabled_tools_removes_named_custom_tool() -> None:
    # apply_patch is a CustomTool, which the runtime tool-enablement check skips. Filtering by
    # name at the aggregation point is the only place it can be removed.
    names = _prepare_with_tools(
        [_function_tool("view_image"), _custom_tool("apply_patch")],
        disabled_tools={"apply_patch"},
    )
    assert "view_image" in names
    assert "apply_patch" not in names


def test_disabled_tools_filters_uniformly_across_capabilities() -> None:
    manifest = Manifest(root="/workspace")
    shell = _Capability(None, type="shell", tools=[_function_tool("write_stdin")])
    filesystem = _Capability(None, type="filesystem", tools=[_custom_tool("apply_patch")])
    prepared = sandbox_prep.prepare_sandbox_agent(
        agent=SandboxAgent(
            name="sandbox",
            instructions="base instructions",
            disabled_tools={"write_stdin", "apply_patch"},
        ),
        session=cast(BaseSandboxSession, _session_with_manifest(manifest)),
        capabilities=cast(list[Capability], [shell, filesystem]),
    )
    assert prepared.tools == []


def test_disabled_tools_unknown_name_is_ignored() -> None:
    names = _prepare_with_tools(
        [_function_tool("exec_command")],
        disabled_tools={"does_not_exist"},
    )
    assert names == ["exec_command"]


def test_disabled_tools_keeps_untargeted_tools() -> None:
    names = _prepare_with_tools(
        [_function_tool("exec_command"), _function_tool("view_image")],
        disabled_tools={"exec_command"},
    )
    assert names == ["view_image"]


def test_disabled_tools_empty_set_passes_all_tools() -> None:
    names = _prepare_with_tools([_function_tool("exec_command"), _custom_tool("apply_patch")])
    assert names == ["exec_command", "apply_patch"]


def test_disabled_tools_does_not_filter_agent_attached_tools() -> None:
    # disabled_tools targets capability-contributed tools, not tools attached directly to the
    # agent. An agent tool that happens to share a disabled name is left untouched.
    names = _prepare_with_tools(
        [_function_tool("exec_command")],
        disabled_tools={"my_tool"},
        agent_tools=[_function_tool("my_tool")],
    )
    assert "my_tool" in names
    assert "exec_command" in names


def test_disabled_tools_prepares_agent_without_raising() -> None:
    prepared_names = _prepare_with_tools(
        [_function_tool("exec_command"), _custom_tool("apply_patch")],
        disabled_tools={"apply_patch"},
    )
    assert prepared_names == ["exec_command"]


def test_disabled_tools_compose_with_configure_tools(tmp_path: Path) -> None:
    # configure_tools runs while a capability builds its tools; disabled_tools filters afterwards.
    # Both must compose: a customized-but-not-disabled tool keeps its customization, while a
    # disabled tool is removed.
    from agents.sandbox.capabilities import Filesystem, FilesystemToolSet
    from agents.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )
    from agents.sandbox.snapshot import NoopSnapshot

    def configure_tools(toolset: FilesystemToolSet) -> None:
        toolset.view_image.needs_approval = True

    session = UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(tmp_path / "workspace")),
            snapshot=NoopSnapshot(id="00000000-0000-0000-0000-000000000000"),
            workspace_root_owned=False,
        )
    )
    capability = Filesystem(configure_tools=configure_tools)
    capability.bind(session)

    prepared = sandbox_prep.prepare_sandbox_agent(
        agent=SandboxAgent(
            name="sandbox",
            instructions="base instructions",
            disabled_tools={"apply_patch"},
        ),
        session=cast(BaseSandboxSession, session),
        capabilities=cast(list[Capability], [capability]),
    )

    tools_by_name = {cast(Any, tool).name: tool for tool in prepared.tools}
    assert "apply_patch" not in tools_by_name
    assert "view_image" in tools_by_name
    assert cast(Any, tools_by_name["view_image"]).needs_approval is True
