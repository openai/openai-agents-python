"""Optional classic import-only staging step. See README.md for setup."""

import asyncio

from agents.classic import Agent, Model, RunContextWrapper, Runner, function_tool

from .business import PROMPT, Application, Invoice, read_invoice


@function_tool
def lookup_invoice(context: RunContextWrapper[Application], invoice_id: str) -> Invoice:
    """Read the invoice supplied by the application."""
    return read_invoice(context.context, invoice_id)


def build_agent(model: str | Model | None = None) -> Agent[Application]:
    return Agent(
        name="Invoice lookup",
        instructions="Use lookup_invoice and return the invoice without changing it.",
        tools=[lookup_invoice],
        model=model,
        output_type=Invoice,
    )


async def main() -> None:
    result = await Runner.run(build_agent(), PROMPT, context=Application())
    print(result.final_output.model_dump_json())


if __name__ == "__main__":
    asyncio.run(main())
