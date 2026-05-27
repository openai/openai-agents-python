from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from textwrap import dedent
from typing import Literal

from pydantic import Field

from ...tool import Tool
from ..manifest import Manifest
from .capability import Capability
from .tools import ExecCommandTool, WriteStdinTool

_SHELL_INSTRUCTIONS = dedent(
    """
    When using the shell:
    - Use `exec_command` for shell execution.
    - If available, use `write_stdin` to interact with or poll running sessions.
    - To interrupt a long-running process via `write_stdin`, start it with `tty=true` and send \
Ctrl-C (`\\u0003`).
    - Prefer `rg` and `rg --files` for text/file discovery when available.
    - Avoid using Python scripts just to print large file chunks.
    """
).strip()


@dataclass
class ShellToolSet:
    """Mutable bundle of tools exposed by the shell capability."""

    exec_command: ExecCommandTool
    write_stdin: WriteStdinTool | None


ShellToolConfigurator = Callable[[ShellToolSet], None]


class Shell(Capability):
    type: Literal["shell"] = "shell"
    configure_tools: ShellToolConfigurator | None = Field(default=None, exclude=True)
    """Optional callback that can customize or replace bundled shell tools."""

    def tools(self) -> list[Tool]:
        if self.session is None:
            raise ValueError("Shell capability is not bound to a SandboxSession")
        toolset = ShellToolSet(
            exec_command=ExecCommandTool(session=self.session, user=self.run_as),
            write_stdin=WriteStdinTool(session=self.session)
            if self.session.supports_pty()
            else None,
        )
        if self.configure_tools is not None:
            self.configure_tools(toolset)
        tools: list[Tool] = [toolset.exec_command]
        if toolset.write_stdin is not None:
            tools.append(toolset.write_stdin)
        return tools

    async def instructions(self, manifest: Manifest) -> str | None:
        _ = manifest
        return _SHELL_INSTRUCTIONS
