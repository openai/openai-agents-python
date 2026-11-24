"""Track one sales report across two SDK workers.

Run `uv run python -m examples.memory.resumable_jobs` from the repository
root for an offline demo. Use --openai or --codex for live model calls.
"""

import argparse
import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from agents import Agent, ModelSettings, ResumableSession, RunConfig, Runner
from agents.decorators import tool
from agents.models.codex import CodexProvider
from agents.testing import ScriptedModel, function_call
from examples.memory.resumable_sqlite_session import SQLiteStore


async def run_turn(folder: Path, session_id: str, job_id: str, mode: str, submit: bool) -> str:
    jobs = SQLiteStore(folder / "jobs.db")
    session = await ResumableSession.resume(SQLiteStore(folder / "sessions.db"), session_id)

    @tool(needs_approval=True)
    async def submit_job() -> str:
        """Queue the sample sales report."""
        # Insert only once. A duplicate must leave progress and results intact.
        await jobs.save(job_id, "Queued", expected=None)
        return f"Accepted report {job_id}"

    @tool
    async def job_status() -> str:
        """Read the status or result of the sample sales report."""
        return await jobs.load(job_id) or "Report not found"

    action = submit_job if submit else job_status
    agent = Agent(
        name="Sales reports",
        tools=[action],
        model_settings=ModelSettings(tool_choice="required"),
        tool_use_behavior={"stop_at_tool_names": [action.name]},
    )
    config = RunConfig(tracing_disabled=True)
    if mode == "offline":
        agent.model = ScriptedModel([[function_call(action.name, {}, call_id=action.name)]])
    elif mode == "codex":
        config.model_provider = CodexProvider(
            cwd=folder / "workspace",
            config_overrides=['sandbox_mode="read-only"', 'approval_policy="never"'],
        )

    try:
        prompt = "Queue the sample sales report." if submit else "Read my report."
        result = await Runner.run(agent, prompt, session=session, run_config=config)
        if submit:
            (request,) = result.interruptions
            if request.name != submit_job.name:
                raise ValueError("Expected the sample report request.")
            state = result.to_state()
            state.approve(request)  # Simulate consent to this fixed, local demo job.
            result = await Runner.run(agent, state, run_config=config)
        return str(result.final_output)
    finally:
        await config.model_provider.aclose()


def worker(folder: Path, session_id: str, job_id: str, mode: str, submit: bool) -> str:
    return asyncio.run(run_turn(folder, session_id, job_id, mode, submit))


def demo(folder: Path, mode: str) -> None:
    (folder / "workspace").mkdir()
    session = asyncio.run(ResumableSession.create(SQLiteStore(folder / "sessions.db")))
    jobs = SQLiteStore(folder / "jobs.db")
    job_id = uuid4().hex
    args = (folder, session.session_id, job_id, mode)
    context = multiprocessing.get_context("spawn")

    # The parent process owns the job and outlives both SDK workers.
    with ProcessPoolExecutor(1, mp_context=context) as first:
        print(first.submit(worker, *args, True).result())
        total = sum([12, 18])
        progress = "Running: 1 of 2 batches"
        if not asyncio.run(jobs.save(job_id, progress, expected="Queued")):
            raise RuntimeError("The report was not queued.")

    # Report progress directly, without a model request, while the worker is gone.
    print(asyncio.run(jobs.load(job_id)))
    total += sum([20, 25])
    completed = f"Completed: {total} units sold"
    if not asyncio.run(jobs.save(job_id, completed, expected=progress)):
        raise RuntimeError("The report changed while it was running.")

    with ProcessPoolExecutor(1, mp_context=context) as replacement:
        print(replacement.submit(worker, *args, False).result())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--openai", action="store_true", help="Use the default Python provider")
    modes.add_argument("--codex", action="store_true", help="Use the embedded Codex provider")
    options = parser.parse_args()
    mode = "codex" if options.codex else "openai" if options.openai else "offline"
    with TemporaryDirectory() as directory:
        demo(Path(directory), mode)
