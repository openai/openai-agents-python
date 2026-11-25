import argparse
import asyncio
import json

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, function_call
from agents_ticket_desk import ticket_desk


class ExampleClient:
    def __init__(self) -> None:
        self.signed_in: str | None = "alice"
        self.grants = {("alice", "T-1")}
        self.closed: set[str] = set()

    async def close_if_allowed(self, user: str, ticket_id: str) -> bool:
        if user != self.signed_in or (user, ticket_id) not in self.grants:
            return False
        self.closed.add(ticket_id)
        return True


async def review(config: RunConfig) -> None:
    client = ExampleClient()
    owner = "alice"
    desk = Agent(
        "Desk",
        plugins=[ticket_desk(client, owner=owner)],
        tool_use_behavior="stop_on_first_tool",
    )
    result = await Runner.run(
        desk, "Use close_ticket to close T-1.", context=owner, run_config=config
    )
    if len(result.interruptions) != 1:
        raise ValueError("Expected exactly one proposal")
    item = result.interruptions[0]
    if item.tool_name != "close_ticket" or json.loads(item.arguments) != {"ticket_id": "T-1"}:
        raise ValueError("Unexpected proposal")

    state = result.to_state()
    answer = input(f"Close T-1 as {owner}? Type yes to approve. ")
    if client.signed_in != owner:
        raise ValueError("The signed-in user changed")
    if answer == "yes":
        state.approve(item)
    else:
        state.reject(item)
    await Runner.run(desk, state, run_config=config)
    print("T-1 closed" if "T-1" in client.closed else "T-1 still open")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Review a synthetic ticket closure.")
    providers = parser.add_mutually_exclusive_group()
    providers.add_argument(
        "--openai", action="store_true", help="Send a live API request. Charges may apply."
    )
    providers.add_argument(
        "--codex", action="store_true", help="Send a live Codex request. Charges may apply."
    )
    args = parser.parse_args()
    if args.codex:
        from agents.models.codex import CodexProvider

        provider = CodexProvider(
            config_overrides=('sandbox_mode="read-only"', 'approval_policy="never"')
        )
        try:
            await review(RunConfig(model_provider=provider, tracing_disabled=True))
        finally:
            await provider.aclose()
    elif args.openai:
        await review(RunConfig(tracing_disabled=True))
    else:
        call = function_call("close_ticket", {"ticket_id": "T-1"}, call_id="close-1")
        await review(RunConfig(model=ScriptedModel([[call]]), tracing_disabled=True))


if __name__ == "__main__":
    asyncio.run(main())
