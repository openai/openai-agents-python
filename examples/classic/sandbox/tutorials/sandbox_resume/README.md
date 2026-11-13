# Sandbox resume

The [sandbox setup guide](../../README.md) covers source installation, model credentials, and the Unix-local isolation limits. These tutorials also import `rich`; use `uv run --with rich` in place of `uv run` in the commands below when the environment does not install it.

This example runs two tasks in the same sandbox workspace: build and smoke-test a warehouse-robot status API, then resume the workspace and add pytest coverage.

The script mounts `AGENTS.md` and also uses its content as agent instructions. Those instructions call for FastAPI, type hints, Pydantic, dependency installation with `uv`, and local testing.

Run the example from the repository root:

```bash
uv run python examples/classic/sandbox/tutorials/sandbox_resume/main.py
```

Both runs happen in one process. The script serializes and deserializes sandbox state in memory, carries forward the conversation with `result.to_input_list()`, and resumes the workspace for the second task. It deletes both sessions on exit; it does not save a checkpoint file or retain the generated application for later use. Inspect the streamed tool events and final answers for the build and test results.

You can override the model or prompt:

```bash
uv run python examples/classic/sandbox/tutorials/sandbox_resume/main.py --model gpt-5.6-sol --question "Build a FastAPI service that exposes a warehouse robot's maintenance status."
```

To run the same flow in Docker, build the shared tutorial image once and pass `--docker`:

```bash
docker build --tag sandbox-tutorials:latest examples/classic/sandbox/tutorials
uv run python examples/classic/sandbox/tutorials/sandbox_resume/main.py --docker
```
