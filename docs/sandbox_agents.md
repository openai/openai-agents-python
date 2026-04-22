# Quickstart

!!! warning "Beta feature"

    Sandbox agents are in beta. Expect details of the API, defaults, and supported capabilities to change before general availability, and expect more advanced features over time.

Modern agents work best when they can operate on real files in a filesystem. **Sandbox Agents** in the Agents SDK give the model a persistent workspace where it can search large document sets, edit files, run commands, generate artifacts, and pick work back up from saved sandbox state.

The SDK gives you that execution harness without making you wire together file staging, filesystem tools, shell access, sandbox lifecycle, snapshots, and provider-specific glue yourself. You keep the normal `Agent` and `Runner` flow, then add a `Manifest` for the workspace, capabilities for sandbox-native tools, and `SandboxRunConfig` for where the work runs.

## Prerequisites

- Python 3.10 or higher
- Basic familiarity with the OpenAI Agents SDK
- A sandbox client. For local development, start with `UnixLocalSandboxClient`.

## Installation

If you have not already installed the SDK:

```bash
pip install openai-agents
```

For Docker-backed sandboxes:

```bash
pip install "openai-agents[docker]"
```

## Create a local sandbox agent

This example stages a local repo under `repo/`, loads local skills lazily, and lets the runner create a Unix-local sandbox session for the run.

```python
import asyncio
from pathlib import Path

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Capabilities, LocalDirLazySkillSource, Skills
from agents.sandbox.entries import LocalDir
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

EXAMPLE_DIR = Path(__file__).resolve().parent
HOST_REPO_DIR = EXAMPLE_DIR / "repo"
HOST_SKILLS_DIR = EXAMPLE_DIR / "skills"


def build_agent(model: str) -> SandboxAgent[None]:
    return SandboxAgent(
        name="Sandbox engineer",
        model=model,
        instructions=(
            "Read `repo/task.md` before editing files. Stay grounded in the repository, preserve "
            "existing behavior, and mention the exact verification command you ran. "
            "If you edit files with apply_patch, paths are relative to the sandbox workspace root."
        ),
        default_manifest=Manifest(
            entries={
                "repo": LocalDir(src=HOST_REPO_DIR),
            }
        ),
        capabilities=Capabilities.default() + [
            Skills(
                lazy_from=LocalDirLazySkillSource(
                    # This is a host path read by the SDK process.
                    # Requested skills are copied into `skills_path` in the sandbox.
                    source=LocalDir(src=HOST_SKILLS_DIR),
                )
            ),
        ],
    )


async def main() -> None:
    result = await Runner.run(
        build_agent("gpt-5.4"),
        "Open `repo/task.md`, fix the issue, run the targeted test, and summarize the change.",
        run_config=RunConfig(
            sandbox=SandboxRunConfig(client=UnixLocalSandboxClient()),
            workflow_name="Sandbox coding example",
        ),
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

See [examples/sandbox/docs/coding_task.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/docs/coding_task.py). It uses a tiny shell-based repo so the example can be verified deterministically across Unix-local runs.

## Key choices

Once the basic run works, the choices most people reach for next are:

- `default_manifest`: the files, repos, directories, and mounts for fresh sandbox sessions
- `instructions`: short workflow rules that should apply across prompts
- `base_instructions`: an advanced escape hatch for replacing the SDK sandbox prompt
- `capabilities`: sandbox-native tools such as filesystem editing/image inspection, shell, skills, memory, and compaction
- `run_as`: the sandbox user identity for model-facing tools
- `SandboxRunConfig.client`: the sandbox backend
- `SandboxRunConfig.session`, `session_state`, or `snapshot`: how later runs reconnect to prior work

## Where to go next

- [Concepts](sandbox/guide.md): understand manifests, capabilities, permissions, snapshots, run config, and composition patterns.
- [Sandbox clients](sandbox/clients.md): choose Unix-local, Docker, hosted providers, and mount strategies.
- [Agent memory](sandbox/memory.md): preserve and reuse lessons from previous sandbox runs.

If shell access is only one occasional tool, start with hosted shell in the [tools guide](tools.md). Reach for sandbox agents when workspace isolation, sandbox client choice, or sandbox-session resume behavior are part of the design.
