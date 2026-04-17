# Sandbox resume

This example shows a small sandbox resume flow with `AGENTS.md`
mounted in the sandbox and loaded into the agent instructions. It runs in two
steps: first it builds the app and smoke tests it, then it serializes the
sandbox session state, resumes the sandbox, and adds pytest coverage.

By default the agent builds a tiny warehouse-robot status API, smoke-tests it,
then resumes the same sandbox to add tests. The sandbox workspace starts with
one instruction file:

- `AGENTS.md` with instructions to build FastAPI apps, use type hints and
  Pydantic, install dependencies with `uv`, run Python commands through
  `uv run python`, and test locally before finishing.

Run the example from the repository root:

```bash
uv run python examples/sandbox/tutorials/sandbox_resume/main.py
```

This demo exits after the scripted resume flow so the serialized session state
and resume step stay easy to follow.

You can override the model or prompt:

```bash
uv run python examples/sandbox/tutorials/sandbox_resume/main.py --model gpt-5.4 --question "Build a FastAPI service that exposes a warehouse robot's maintenance status."
```

To run the same flow in Docker, build the shared tutorial image once and pass
`--docker`:

```bash
docker build --tag sandbox-tutorials:latest examples/sandbox/tutorials
uv run python examples/sandbox/tutorials/sandbox_resume/main.py --docker
```
