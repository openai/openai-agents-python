import argparse
import asyncio
import random

from agents import Agent, GenerateDynamicPromptData, RunConfig, RunContextWrapper, Runner

"""
NOTE: This example will not work out of the box, because the default prompt ID will not be available
in your project.

To use it, please:
1. Go to https://platform.openai.com/playground/prompts
2. Create a new prompt variable, `poem_style`.
3. Create a system prompt with the content:
```
Write a poem in {{poem_style}}
```
4. Run the example with the `--prompt-id` flag.

For an application-owned template that works with either model provider, use
`--local` (optionally with `--dynamic`). Add `--codex` to use stock Codex.
This does not look up a hosted prompt ID; hosted IDs require the default provider.
See https://developers.openai.com/api/docs/guides/prompting/migrate-from-prompt-object
"""

DEFAULT_PROMPT_ID = "pmpt_6965a984c7ac8194a8f4e79b00f838840118c1e58beb3332"


class DynamicContext:
    def __init__(self, prompt_id: str):
        self.prompt_id = prompt_id
        self.poem_style = random.choice(["limerick", "haiku", "ballad"])
        print(f"[debug] DynamicContext initialized with poem_style: {self.poem_style}")


async def _get_dynamic_prompt(data: GenerateDynamicPromptData):
    ctx: DynamicContext = data.context.context
    return {
        "id": ctx.prompt_id,
        "version": "1",
        "variables": {
            "poem_style": ctx.poem_style,
        },
    }


class LocalPoemContext:
    def __init__(self, dynamic: bool):
        self.poem_style = random.choice(["limerick", "haiku", "ballad"]) if dynamic else "limerick"


def local_instructions(
    context: RunContextWrapper[LocalPoemContext], agent: Agent[LocalPoemContext]
):
    return poem_instructions(context.context.poem_style)


def poem_instructions(style: str) -> str:
    lines = {"limerick": 5, "haiku": 3, "ballad": 8}[style]
    return (
        f"Reply only with a {style} poem about the user's topic in exactly {lines} lines, "
        "without an explanation or heading."
    )


async def local_prompt(dynamic: bool, config: RunConfig | None = None):
    context = LocalPoemContext(dynamic)
    print(f"[debug] Local poem_style: {context.poem_style}")
    agent = Agent(
        name="Assistant",
        instructions=local_instructions if dynamic else poem_instructions(context.poem_style),
    )
    result = await Runner.run(
        agent, "Tell me about recursion in programming.", context=context, run_config=config
    )
    print(result.final_output)


async def dynamic_prompt(prompt_id: str, config: RunConfig | None = None):
    context = DynamicContext(prompt_id)

    agent = Agent(
        name="Assistant",
        prompt=_get_dynamic_prompt,
    )

    result = await Runner.run(
        agent, "Tell me about recursion in programming.", context=context, run_config=config
    )
    print(result.final_output)


async def static_prompt(prompt_id: str, config: RunConfig | None = None):
    agent = Agent(
        name="Assistant",
        prompt={
            "id": prompt_id,
            "version": "1",
            "variables": {
                "poem_style": "limerick",
            },
        },
    )

    result = await Runner.run(agent, "Tell me about recursion in programming.", run_config=config)
    print(result.final_output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dynamic", action="store_true")
    parser.add_argument("--prompt-id", type=str)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--codex", action="store_true")
    args = parser.parse_args()

    if args.local and args.prompt_id is not None:
        parser.error("--local cannot be combined with --prompt-id.")

    config = None
    if args.codex:
        from agents.models.codex import CodexProvider

        config = RunConfig(model_provider=CodexProvider())

    prompt_id = args.prompt_id if args.prompt_id is not None else DEFAULT_PROMPT_ID
    if args.local:
        asyncio.run(local_prompt(args.dynamic, config))
    elif args.dynamic:
        asyncio.run(dynamic_prompt(prompt_id, config))
    else:
        asyncio.run(static_prompt(prompt_id, config))
