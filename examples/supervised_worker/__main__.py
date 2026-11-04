"""Run a local coding worker and inspect its application-owned records."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import stat
from contextlib import AsyncExitStack, aclosing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
from typing import Any, Literal, TypeVar
from urllib.parse import unquote_to_bytes, urlsplit

from pydantic import BaseModel, ConfigDict

from agents import (
    Agent,
    ApprovalRequest,
    Completed,
    Environment,
    HttpMCP,
    LocalRecoveryRequired,
    LocalStorage,
    NativeProvider,
    OutputValidationError,
    PendingRequests,
    ReconciledCall,
    Session,
    Tool,
    ToolContext,
    Turn,
    TurnNeedsAttention,
    function_tool,
)
from agents.observation import (
    ExecutionItemCompleted,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
)

OutputT = TypeVar("OutputT")
logger = logging.getLogger(__name__)

ARTIFACT = "summary.txt"
VERIFY_COMMAND = "cat -- summary.txt"
EFFECT_RESULT = "The application recorded the handoff once."
CORRECTIONS = (
    "Use the exact text supplied by the brief tool.",
    f"After writing the file, verify it with this exact exec_command: {VERIFY_COMMAND}",
)


class ArtifactResult(BaseModel):
    """The output schema checks the claim. The application checks the file."""

    model_config = ConfigDict(extra="forbid", strict=True)

    artifact: Literal["summary.txt"]
    check: Literal["artifact checked"]


def read_record(directory: Path, name: str) -> dict[str, Any] | None:
    try:
        record = json.loads((directory / f"{name}.json").read_bytes())
    except FileNotFoundError:
        return None
    if not isinstance(record, dict):
        raise ValueError(f"The {name} record is invalid.")
    return record


def record_once(directory: Path, name: str, value: dict[str, Any]) -> bool:
    """Install one complete immutable record even if the process dies during a write."""
    target = directory / f"{name}.json"
    with NamedTemporaryFile(dir=directory) as staged:
        staged.write(json.dumps(value, sort_keys=True).encode())
        staged.flush()
        os.fsync(staged.fileno())
        try:
            os.link(staged.name, target)
        except FileExistsError:
            if read_record(directory, name) != value:
                raise ValueError(f"The existing {name} record differs.") from None
            return False
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return True


@dataclass
class Worker:
    directory: Path
    pause_after_effect: bool
    brief_release: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def workspace(self) -> Path:
        return self.directory / "workspace"

    @property
    def expected_text(self) -> str:
        return f"{self.directory.name}: verified\n"

    def read_artifact(self) -> str | None:
        # Use a fixed file name. A model cannot ask the worker to read another path.
        try:
            fd = os.open(self.workspace / ARTIFACT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as artifact:
            if not stat.S_ISREG(os.fstat(artifact.fileno()).st_mode):
                return None
            expected_text = self.expected_text
            expected_bytes = expected_text.encode()
            if artifact.read(len(expected_bytes) + 1) != expected_bytes:
                return None
        return expected_text


@function_tool
async def brief(context: ToolContext[Worker]) -> str:
    """Get the exact expected artifact text after the application confirms its brief."""
    await context.context.brief_release.wait()
    return context.context.expected_text


@function_tool
async def record_effect(context: ToolContext[Worker]) -> str:
    """Record one simulated handoff in the application journal."""
    turn_id = context.submission.turn_id
    if turn_id is None:
        raise RuntimeError("The handoff requires an admitted native turn.")
    record_once(
        context.context.directory,
        "effect",
        {
            "session_id": context.submission.session_id,
            "turn_id": turn_id,
            "call_id": context.call_id,
            "result": EFFECT_RESULT,
        },
    )
    if context.context.pause_after_effect:
        # The test kills this process after the file effect and before the SDK receipt.
        await asyncio.Event().wait()
    return EFFECT_RESULT


@dataclass
class TurnTrace:
    started: float
    first_tool_result_ms: float | None = None
    completion_ms: float | None = None
    steering: list[dict[str, str]] = field(default_factory=list)
    execution_ids: list[str] = field(default_factory=list)
    denied_approvals: int = 0

    def elapsed_ms(self) -> float:
        return (monotonic() - self.started) * 1000


async def observe(worker: Worker, turn: Turn[Any], trace: TurnTrace, *, interrupt: bool) -> None:
    active_id: str | None = None
    async with aclosing(turn.events()) as events:
        async for event in events:
            if isinstance(event, TurnStarted):
                active_id = event.turn_id
            elif (
                isinstance(event, ToolStarted)
                and event.name == "brief"
                and not worker.brief_release.is_set()
            ):
                if interrupt:
                    await turn.cancel()
                    continue
                if active_id is None:
                    raise RuntimeError("No native turn ID was observed before the brief.")
                for correction in CORRECTIONS:
                    receipt = await turn.steer_exact(correction, expected_turn_id=active_id)
                    trace.steering.append(
                        {"turn_id": receipt.turn_id, "input_id": receipt.input_id}
                    )
                worker.brief_release.set()
            elif isinstance(event, (ExecutionItemCompleted, ToolFinished)):
                if trace.first_tool_result_ms is None:
                    trace.first_tool_result_ms = trace.elapsed_ms()
                if isinstance(event, ExecutionItemCompleted):
                    trace.execution_ids.append(event.item["id"])
            elif isinstance(event, TurnFinished):
                return


async def wait_for_result(
    turn: Turn[OutputT], trace: TurnTrace
) -> Completed[OutputT] | TurnNeedsAttention:
    while True:
        outcome = await turn.wait_for_attention()
        if not isinstance(outcome, PendingRequests):
            return outcome
        for request in outcome.requests:
            if not isinstance(request, ApprovalRequest):
                raise RuntimeError("An unattended worker needs an application decision.")
            await request.respond("deny")
            trace.denied_approvals += 1


async def supervise(
    worker: Worker,
    turn: Turn[OutputT],
    trace: TurnTrace,
    *,
    timeout: float,
    interrupt: bool = False,
) -> Completed[OutputT] | TurnNeedsAttention:
    observer = asyncio.create_task(observe(worker, turn, trace, interrupt=interrupt))
    waiter = asyncio.create_task(wait_for_result(turn, trace))
    try:
        pair: tuple[None, Completed[OutputT] | TurnNeedsAttention] = await asyncio.wait_for(
            asyncio.gather(observer, waiter), timeout
        )
        return pair[1]
    except BaseException:
        # Ending a passive wait does not stop native work. Join this owned turn.
        try:
            await turn.cancel()
        except BaseException:
            logger.exception("Could not cancel the worker turn.")
        raise
    finally:
        trace.completion_ms = trace.elapsed_ms()
        observer.cancel()
        waiter.cancel()
        await asyncio.gather(observer, waiter, return_exceptions=True)


async def work(
    worker: Worker,
    session: Session[Worker, ArtifactResult],
    environment: Environment[Worker],
    notes: HttpMCP[Worker] | None,
    timeout: float,
    startup_ms: float,
    *,
    interrupt: bool,
) -> dict[str, Any]:
    prompt = "Call brief, write its exact text to summary.txt, and verify that file."
    rejections: list[str] = []
    traces: list[TurnTrace] = []
    tools: list[Tool[Worker] | Environment[Worker]] = [brief, environment]
    if notes is not None:
        tools.append(notes)
    for _ in range(3):
        trace = TurnTrace(monotonic())
        traces.append(trace)
        turn = await asyncio.wait_for(session.submit(prompt, tools=tools), timeout)
        try:
            result = await supervise(
                worker,
                turn,
                trace,
                timeout=timeout + trace.started - monotonic(),
                interrupt=interrupt,
            )
        except OutputValidationError:
            reason = "The output did not match the required result schema."
        else:
            if isinstance(result, TurnNeedsAttention):
                return {"status": result.reason, "stage": "turn"}
            artifact = worker.read_artifact()
            commands = [
                item
                async for item in session.items()
                if item["type"] == "command_execution"
                and item["turn_id"] == turn.turn_id
                and item["status"] == "completed"
                and item["exit_code"] == 0
                and item["cwd"] is not None
                and (cwd := urlsplit(item["cwd"])).scheme == "file"
                and not (cwd.netloc or cwd.query or cwd.fragment)
                and unquote_to_bytes(cwd.path).rstrip(b"/") == bytes(worker.workspace)
                and item.get("argv", [])[-1:] == [VERIFY_COMMAND]
                and (item["output"] or "").rstrip("\n") == worker.expected_text.rstrip("\n")
            ]
            steering = [receipt for draft in traces for receipt in draft.steering]
            if artifact is not None and commands and len(steering) == len(CORRECTIONS):
                receipts = [
                    recorded.get("receipt")
                    async for recorded in session.turns()
                    if recorded["id"] == turn.turn_id
                ]
                if not receipts or receipts[0] is None:
                    raise RuntimeError("The accepted turn has no recorded sampling receipt.")
                requests = session.rate_limits().requests
                accepted = {
                    "session_id": session.id,
                    "turn_id": turn.turn_id,
                    "artifact": result.final_output.model_dump(),
                    "text": artifact,
                    "sha256": hashlib.sha256(artifact.encode()).hexdigest(),
                    "command_id": commands[-1]["id"],
                    "execution_ids": [item for draft in traces for item in draft.execution_ids],
                    "rejections": rejections,
                    "denied_approvals": sum(draft.denied_approvals for draft in traces),
                    "steering": steering,
                    "turn_receipt": receipts[0],
                    "remaining_requests": requests.remaining if requests else None,
                    "startup_ms": startup_ms,
                    "first_tool_result_ms": next(
                        (
                            draft.first_tool_result_ms
                            for draft in traces
                            if draft.first_tool_result_ms is not None
                        ),
                        None,
                    ),
                    "completion_ms": trace.completion_ms,
                    "usage": asdict(result.usage) if result.usage is not None else None,
                    "follow_up_ms": [draft.completion_ms for draft in traces[1:]],
                }
                record_once(worker.directory, "accepted", accepted)
                return {"status": "accepted", **accepted}
            reason = (
                "The file or successful check is missing."
                if len(steering) == len(CORRECTIONS)
                else "Call brief so the application can confirm the two corrections."
            )
        rejections.append(reason)
        prompt = (
            f"{reason} Revise the same task. Write the file and run the check before reporting."
        )
    return {"status": "rejected", "session_id": session.id, "rejections": rejections}


async def run_worker(args: argparse.Namespace) -> dict[str, Any]:
    directory = args.directory.resolve()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", directory.name):
        raise ValueError("Use a worker directory name like alpha or worker-2.")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    worker = Worker(directory, args.pause_after_effect)
    worker.workspace.mkdir(mode=0o700, exist_ok=True)
    identity = read_record(directory, "identity")
    if args.command in {"work", "interrupt"} and identity is not None:
        raise ValueError("Use a fresh worker directory for a new task.")
    if args.command in {"effect", "recover"} and identity is None:
        raise ValueError("An existing worker identity is required.")

    provider = NativeProvider(base_url=os.environ.get("OPENAI_BASE_URL"), supports_websockets=False)
    notes: HttpMCP[Worker] | None = None
    if args.mcp_url is not None:
        notes = HttpMCP(name="job_notes", url=args.mcp_url, tools=["lookup"])
    agent = Agent[Worker, ArtifactResult](
        model=os.environ.get("WORKER_MODEL"),
        instructions=(
            "Work only in the assigned workspace. Use exec_command to write summary.txt, "
            f"then run {VERIFY_COMMAND} as a separate exec_command. "
            "Report the required structured output for artifact tasks. "
            "Call record_effect only when the application asks for the simulated handoff."
        ),
        provider=provider,
        output_type=ArtifactResult,
        tools=[brief, record_effect] if notes is None else [brief, record_effect, notes],
    )
    environment: Environment[Worker] = Environment.local(
        cwd=str(worker.workspace),
        tools=("exec_command",),
        writable_roots=(str(worker.workspace),),
        capability_directories=(),
    )
    stage = "startup"
    try:
        async with AsyncExitStack() as scope:
            started = monotonic()
            await scope.enter_async_context(provider)

            async def open_session(
                reconciled_calls: tuple[ReconciledCall, ...] = (),
            ) -> Session[Worker, ArtifactResult]:
                return await asyncio.wait_for(
                    scope.enter_async_context(
                        agent.session(
                            context=worker,
                            environment=environment,
                            storage=LocalStorage(directory / "history"),
                            resume=identity["session_id"] if identity else None,
                            reconciled_calls=reconciled_calls,
                            instruction_files=args.instruction_file,
                        )
                    ),
                    args.startup_timeout + started - monotonic(),
                )

            effect = read_record(directory, "effect") if args.command == "recover" else None
            if args.command == "recover" and effect is None:
                raise RuntimeError("There is no confirmed handoff to reconcile.")
            try:
                session = await open_session()
            except LocalRecoveryRequired as error:
                if effect is None or len(error.calls) != 1:
                    raise
                call = error.calls[0]
                if (
                    call.thread_id != effect["session_id"]
                    or call.turn_id != effect["turn_id"]
                    or call.call_id != effect["call_id"]
                    or call.name.rsplit(".", 1)[-1] != "record_effect"
                    or call.arguments != {}
                    or effect["result"] != EFFECT_RESULT
                ):
                    raise
                session = await open_session(
                    (call.reconcile(outcome="applied", result=effect["result"]),)
                )
            startup_ms = (monotonic() - started) * 1000
            record_once(directory, "identity", {"session_id": session.id})
            stage = "turn"
            if args.command in {"work", "interrupt"}:
                return await work(
                    worker,
                    session,
                    environment,
                    notes,
                    args.turn_timeout,
                    startup_ms,
                    interrupt=args.command == "interrupt",
                )
            prompt = (
                "Call record_effect once to record the simulated handoff, then report its result."
                if args.command == "effect"
                else "Report the reconciled handoff result from history. Do not call record_effect."
            )
            trace = TurnTrace(monotonic())
            turn = await asyncio.wait_for(
                session.submit(
                    prompt,
                    output_type=str,
                    tools=[record_effect] if args.command == "effect" else [],
                ),
                args.turn_timeout,
            )
            result = await supervise(
                worker, turn, trace, timeout=args.turn_timeout + trace.started - monotonic()
            )
            if isinstance(result, TurnNeedsAttention):
                return {"status": result.reason, "stage": stage}
            if args.command == "effect":
                effect = read_record(directory, "effect")
                turn_id = result.turn.turn_id
                if turn_id is None or (effect is not None and effect.get("turn_id") != turn_id):
                    effect = None
            if (
                effect is None
                or effect.get("session_id") != session.id
                or effect.get("result") != EFFECT_RESULT
            ):
                return {
                    "status": "rejected",
                    "stage": stage,
                    "reason": "The application journal did not confirm this turn's handoff.",
                }
            return {"status": "completed", "session_id": session.id, "result": effect["result"]}
    except LocalRecoveryRequired:
        return {"status": "recovery_required", "stage": stage}
    except (TimeoutError, asyncio.TimeoutError):
        return {"status": "timed_out", "stage": stage}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("work", "effect", "recover", "interrupt", "status", "publish")
    )
    parser.add_argument("directory", type=Path)
    parser.add_argument("--startup-timeout", type=float, default=60)
    parser.add_argument("--turn-timeout", type=float, default=60)
    parser.add_argument("--instruction-file", action="append", default=[], type=Path)
    parser.add_argument("--mcp-url", help="Trusted endpoint for the optional job_notes lookup.")
    parser.add_argument("--pause-after-effect", action="store_true")
    args = parser.parse_args()
    if not all(
        math.isfinite(value) and value > 0 for value in (args.startup_timeout, args.turn_timeout)
    ):
        parser.error("Deadlines must be finite positive numbers of seconds.")

    status: dict[str, Any]
    if args.command == "status":
        status = {
            name: read_record(args.directory, name)
            for name in ("identity", "effect", "accepted", "published")
        }
    elif args.command == "publish":
        accepted = read_record(args.directory, "accepted")
        if accepted is None:
            parser.error("This worker has no accepted artifact.")
        created = record_once(args.directory, "published", accepted)
        status = {"status": "published" if created else "already_published"}
    else:
        status = asyncio.run(run_worker(args))
    print(json.dumps(status), flush=True)


if __name__ == "__main__":
    main()
