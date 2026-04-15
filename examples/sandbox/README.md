# Sandbox examples

These examples show how to run agents with an isolated workspace. Start with the
small API examples when you want the smallest surface area, or use the tutorial
scaffold when you want the shared layout for guided sandbox tutorials.

Most examples call a model through `Runner`, so set `OPENAI_API_KEY` in the
repository-root `.env` file, in the example's `.env` file when it has one, or
in your shell environment.

## Small API examples

| Example | Run | What it shows |
| --- | --- | --- |
| [`basic.py`](./basic.py) | `uv run python examples/sandbox/basic.py` | Creates a sandbox session from a manifest, runs a `SandboxAgent`, and streams the result. |
| [`handoffs.py`](./handoffs.py) | `uv run python examples/sandbox/handoffs.py` | Uses handoffs with sandbox-backed agents. |
| [`sandbox_agent_capabilities.py`](./sandbox_agent_capabilities.py) | `uv run python examples/sandbox/sandbox_agent_capabilities.py` | Configures a sandbox agent with workspace capabilities. |
| [`sandbox_agent_with_tools.py`](./sandbox_agent_with_tools.py) | `uv run python examples/sandbox/sandbox_agent_with_tools.py` | Combines sandbox capabilities with host-defined tools. |
| [`sandbox_agents_as_tools.py`](./sandbox_agents_as_tools.py) | `uv run python examples/sandbox/sandbox_agents_as_tools.py` | Exposes sandbox agents as tools for another agent. |
| [`sandbox_agent_with_remote_snapshot.py`](./sandbox_agent_with_remote_snapshot.py) | `uv run python examples/sandbox/sandbox_agent_with_remote_snapshot.py` | Starts from a remote sandbox snapshot. |
| [`memory.py`](./memory.py) | `uv run python examples/sandbox/memory.py` | Runs one sandbox agent twice across a snapshot resume so it can read and write its own memory. |
| [`memory_s3.py`](./memory_s3.py) | `source ~/.s3.env && uv run python examples/sandbox/memory_s3.py` | Runs sandbox memory across two fresh Docker sandboxes with S3-backed memory storage. |
| [`memory_multi_agent_multiturn.py`](./memory_multi_agent_multiturn.py) | `uv run python examples/sandbox/memory_multi_agent_multiturn.py` | Shows separate memory layouts for two agents sharing one sandbox workspace. |
| [`unix_local_pty.py`](./unix_local_pty.py) | `uv run python examples/sandbox/unix_local_pty.py` | Exercises an interactive pseudo-terminal in a Unix-local sandbox. |
| [`unix_local_runner.py`](./unix_local_runner.py) | `uv run python examples/sandbox/unix_local_runner.py` | Runs against the Unix-local sandbox backend directly. |

## Cloud backend examples

Cloud-provider examples live under [`extensions/`](./extensions/). They cover
E2B, Modal, and Daytona sandbox backends and require provider-specific
credentials in addition to `OPENAI_API_KEY`.

## Tutorial scaffold

[`tutorials/`](./tutorials/) contains the shared helper code, Docker image, and folder
conventions for guided sandbox tutorials. Tutorial folders are added in separate
focused changes.

## Tutorials

| Example | What it does |
| --- | --- |
| [`sandbox_resume`](./tutorials/sandbox_resume/) | Edits a workspace app and reuses a sandbox snapshot. |
| [`dataroom_qa`](./tutorials/dataroom_qa/) | Answers questions over a mounted dataroom with source-backed responses. |
| [`dataroom_metric_extract`](./tutorials/dataroom_metric_extract/) | Extracts structured financial metrics to CSV/JSONL. |
| [`repo_code_review`](./tutorials/repo_code_review/) | Reviews a sample repo and writes finding, report, and patch artifacts. |
| [`vision_website_clone`](./tutorials/vision_website_clone/) | Uses vision and a browser-review loop to clone a reference static website. |

## Workflow examples

| Example | What it does |
| --- | --- |
| [`healthcare_support`](./healthcare_support/) | Runs a synthetic healthcare support workflow with a standard orchestrator, sandbox policy agent, memory, and human approvals. |

## Shared files

- [`docker/`](./docker/) contains Docker-specific helper examples.
- [`misc/`](./misc/) contains reusable support code and tiny reference tools
  used by several sandbox examples.
