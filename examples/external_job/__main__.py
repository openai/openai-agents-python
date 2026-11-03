"""Render a thumbnail in a separate worker and resume the original tool call.

Run these commands from a checkout containing the experimental helper. Supply the
port of your local Responses endpoint. Set THUMBNAIL_STATE_DIR to the absolute
path of a private local POSIX directory outside every model-visible mount. Its
parent must already exist and be trusted by the host. Keep its
database, SQLite sidecars, locks, and private receipts outside those mounts for
the entire job lifetime. The host trusts the directory's parent and same-UID
processes. The worker needs no model connection.

    PYTHONPATH=. python -m examples.external_job run sample-01 --model-port 8080 --directory "$THUMBNAIL_STATE_DIR"
    PYTHONPATH=. python -m examples.external_job worker sample-01 --directory "$THUMBNAIL_STATE_DIR"
    PYTHONPATH=. python -m examples.external_job run sample-01 --model-port 8080 --directory "$THUMBNAIL_STATE_DIR"

The first run prints pending. The worker stores a 32 by 32 image in the private
application database. The next run gives the model its byte count and prints
completed. Reuse the order ID for a retry, or choose a new ID to order another
image. This example requires a local POSIX filesystem and a Unix SQLite VFS
that creates private files using the selected state directory's permissions.
For a complete demo
with a synthetic model, run pytest -q tests/test_deferred_jobs_example.py.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agents import ActionRequested, Agent, Completed, FunctionTool, NativeProvider, Turn
from agents.experimental.deferred import DeferredTools, JobClaim, JobProvider, JobUpdate
from agents.state import SessionState

from .journal import Busy, Journal
from .store import ThumbnailStore


class Thumbnail(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    color: Literal["red", "blue"]


class ImageSize(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    size_bytes: int = Field(gt=0, le=65536)


render = FunctionTool(
    name="render_thumbnail",
    description="Render one thumbnail.",
    input_type=Thumbnail,
    output_type=ImageSize,
)


def public_size(value: object) -> ImageSize:
    """The model can see the byte count. The image and provider receipt stay private."""
    if not isinstance(value, dict):
        raise ValueError("No verified thumbnail size.")
    return ImageSize.model_validate({"size_bytes": value.get("size_bytes")})


class ThumbnailProvider:
    def __init__(self, journal: Journal) -> None:
        self.journal = journal

    async def start(self, claim: JobClaim, arguments: Thumbnail) -> JobUpdate:
        with self.journal.authorized(claim) as db:
            db.execute(
                """INSERT INTO thumbnails (tenant,order_id,color,receipt) VALUES (?,?,?,?)
                   ON CONFLICT (tenant,order_id) DO NOTHING""",
                (*self.journal.scope, arguments.color, uuid4().hex),
            )
            color, receipt = db.execute(
                "SELECT color,receipt FROM thumbnails WHERE tenant=? AND order_id=?",
                self.journal.scope,
            ).fetchone()
            if color != arguments.color:
                raise ValueError("This order already names a different thumbnail.")
            return JobUpdate(status="pending", receipt=receipt)

    # The same transaction proves absence before inserting or returns the existing bound receipt.
    reconcile_creation = start

    async def observe(self, claim: JobClaim) -> JobUpdate:
        with self.journal.authorized(claim) as db:
            receipt = claim.update.receipt if claim.update else None
            row = db.execute(
                "SELECT length(image) FROM thumbnails WHERE tenant=? AND order_id=? AND receipt=?",
                (*self.journal.scope, receipt),
            ).fetchone()
            if row is None:
                return JobUpdate(status="unknown")
            if row[0] is None:
                return JobUpdate(status="pending", receipt=receipt)
            return JobUpdate(status="completed", receipt=receipt, value={"size_bytes": row[0]})


async def request_from(
    turn: Turn[str], tool: FunctionTool[Thumbnail, ImageSize]
) -> ActionRequested[Thumbnail, ImageSize]:
    outcome = await turn.wait_for_request_or_result()
    if isinstance(outcome, Completed):
        raise RuntimeError("The model finished without requesting a thumbnail.")
    for interaction in outcome.requests:
        if request := tool.match(interaction):
            return request
    raise RuntimeError("The model requested an unexpected interaction.")


async def run_order(
    journal: Journal,
    provider: JobProvider[Thumbnail],
    model: NativeProvider,
    *,
    provider_id: str = "sqlite-thumbnail-v1",
    wait_seconds: float = 0,
) -> str:
    """Call again with the same authorized order after the worker publishes its result."""
    with journal.owner():
        checkpoint, finished = journal.load()
        if finished:
            return "completed"
        jobs = DeferredTools(journal=journal, tenant=journal.store.tenant, run_id=journal.order)
        jobs.register(
            render,
            provider_id=provider_id,
            provider=provider,
            project=public_size,
            max_output_bytes=64,
            require_reconciliation=True,
        )
        agent = Agent[None, str](
            instructions="Request one thumbnail. Then report its verified byte count.",
            tools=[render],
            provider=model,
        )
        state = SessionState.from_json(checkpoint) if checkpoint is not None else None
        async with AsyncExitStack() as stack:
            session = await stack.enter_async_context(agent.session(state=state))
            if state is None:
                turn = await session.submit("Render a blue thumbnail.")
            else:
                turn = session.restored_turn
                if turn is None:
                    raise RuntimeError("The saved order has no pending thumbnail request.")
            current = await request_from(turn, render)
            if state is None:
                # The helper seals, stores, and reopens the original call before starting work.
                session, outcome = await stack.enter_async_context(
                    jobs.start(session, current, wait_seconds=wait_seconds)
                )
            else:
                outcome = await jobs.advance(session, current, wait_seconds=wait_seconds)
            if isinstance(outcome, Turn):
                await outcome.get_result()
                journal.finish((await session.save_state()).to_json())
                return "completed"
            return outcome


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["run", "worker"])
    parser.add_argument("order", help="A stable application order ID. Use a new ID for new work.")
    parser.add_argument(
        "--directory",
        type=Path,
        required=True,
        help="Absolute private POSIX state path with an existing trusted parent, outside all model-visible mounts.",
    )
    parser.add_argument("--model-port", type=int, help="Port of a local Responses model fixture.")
    args = parser.parse_args()
    store = ThumbnailStore(args.directory, tenant="local-studio")
    journal = Journal(store, args.order)
    if args.command == "worker":
        print("Rendered." if store.work(args.order) else "No queued thumbnail.")
        return
    if args.model_port is None or not 1 <= args.model_port <= 65535:
        parser.error("run requires --model-port for a local Responses endpoint")
    model = NativeProvider(base_url=f"http://127.0.0.1:{args.model_port}/v1", api_key="local-only")
    try:
        print(await asyncio.wait_for(run_order(journal, ThumbnailProvider(journal), model), 30))
    except Busy:
        print("Another host owns this order.")
    finally:
        await model.aclose()


if __name__ == "__main__":
    asyncio.run(main())
