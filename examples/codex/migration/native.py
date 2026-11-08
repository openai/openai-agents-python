"""Port the read-only invoice lookup to the native API. See README.md for setup."""

import asyncio

from agents import Agent, NativeProvider, ToolContext, function_tool

from .business import PROMPT, Application, Invoice, read_invoice


@function_tool
def lookup_invoice(context: ToolContext[Application], invoice_id: str) -> Invoice:
    """Read the invoice supplied by the application."""
    return read_invoice(context.context, invoice_id)


def build_agent(provider: NativeProvider | None = None) -> Agent[Application, Invoice]:
    return Agent(
        name="Invoice lookup",
        instructions="Use lookup_invoice and return the invoice without changing it.",
        tools=[lookup_invoice],
        provider=provider,
        output_type=Invoice,
    )


async def main() -> None:
    result = await build_agent().run(PROMPT, context=Application())
    print(result.final_output.model_dump_json())


if __name__ == "__main__":
    asyncio.run(main())
