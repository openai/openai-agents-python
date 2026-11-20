"""Start a run, decide its pending tool call, then continue in a third process.

Start a trusted Redis server on localhost or set REDIS_URL. From the repository root, run

    uv run --extra redis examples/memory/resumable_redis_session.py
    uv run --extra redis examples/memory/resumable_redis_session.py approve SESSION_ID CALL_ID
    uv run --extra redis examples/memory/resumable_redis_session.py follow-up SESSION_ID

Copy SESSION_ID and CALL_ID from the first invocation. Use reject to decline the lookup.
The scripted model reads the tool result from its input, so Redis is the only service needed.
In a service, authorize the user and select their tenant's store before loading a session.
"""

import argparse
import asyncio
import os

from redis.asyncio import Redis

from agents import Agent, ResumableSession, ResumableSessionError, RunConfig, Runner
from agents.decorators import tool
from agents.extensions.memory import RedisSessionStore
from agents.items import TResponseOutputItem
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message, function_call


@tool(needs_approval=True)
def lookup_order(order_id: str) -> str:
    """Look up a sample order."""
    print(f"Looking up order {order_id}.")
    return f"Order {order_id} has shipped."


def read_result(call: ModelCall) -> list[TResponseOutputItem]:
    (output,) = [
        item["output"] for item in call.input if item.get("type") == "function_call_output"
    ]
    return [assistant_message(output)]


async def run(args: argparse.Namespace) -> None:
    first_step = ModelStep(
        output=[function_call("lookup_order", {"order_id": "42"}, call_id="lookup_42")]
    )
    model = ScriptedModel([first_step if args.action is None else ModelStep(responder=read_result)])
    agent = Agent(name="Order assistant", tools=[lookup_order], model=model)
    config = RunConfig(tracing_disabled=True)
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    async with Redis.from_url(url) as client:
        store = RedisSessionStore(client, key_prefix="example:orders")
        if args.action is None:
            session = await ResumableSession.create(store)
            result = await Runner.run(agent, "Check order 42.", session=session, run_config=config)
            (pending,) = result.interruptions
            print(f"Awaiting approval for {pending.name}({pending.arguments}).")
            print(f"SESSION_ID={session.session_id} CALL_ID={pending.call_id}")
            return

        session = await ResumableSession.resume(store, args.session_id)
        if args.action == "follow-up":
            result = await Runner.run(
                agent, "What was the lookup result?", session=session, run_config=config
            )
        else:
            state = await session.load_state(agent)
            (pending,) = state.get_interruptions()
            if pending.call_id != args.call_id:
                raise SystemExit("The decision must match the saved call ID.")
            if args.action == "approve":
                state.approve(pending)
            else:
                state.reject(pending)
            result = await Runner.run(agent, state, run_config=config)
        print(result.final_output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action")
    for action in ("approve", "reject"):
        command = actions.add_parser(action)
        command.add_argument("session_id")
        command.add_argument("call_id")
    actions.add_parser("follow-up").add_argument("session_id")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
        return
    except ResumableSessionError as exc:
        message = str(exc)
    except Exception:
        # Connection errors can include credentials from REDIS_URL.
        message = "The example failed. Check the Redis connection and configuration."
    raise SystemExit(message)


if __name__ == "__main__":
    main()
