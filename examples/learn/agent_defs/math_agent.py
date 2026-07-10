"""Math Agent - Holds the calculator toolset."""

from config import MODEL  # type: ignore[import-not-found]
from tools.calculator import CALCULATOR_TOOLS  # type: ignore[import-not-found]

from agents import Agent

math_agent = Agent(
    name="MathAgent",
    instructions=(
        "You are a math assistant, only use tools to complete addition, subtraction, multiplication, and division.\n"
        "When the user gives you an expression (e.g., '3 plus 5' or '12 * 7'), you must call the corresponding tool, "
        "then tell the user the result in one sentence. Do not give arithmetic answers out of thin air."
    ),
    model=MODEL,
    tools=CALCULATOR_TOOLS,
)
