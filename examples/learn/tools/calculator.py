"""Basic arithmetic tools - demonstrates @function_tool decorator + type hints + docstring auto schema."""

from agents import function_tool


@function_tool
def add(a: int, b: int) -> int:
    """Add two integers.

    Args:
        a: The first integer
        b: The second integer
    """
    return a + b


@function_tool
def subtract(a: int, b: int) -> int:
    """Calculate a minus b."""
    return a - b


@function_tool
def multiply(a: int, b: int) -> int:
    """Multiply two integers."""
    return a * b


@function_tool
def divide(a: float, b: float) -> float | str:
    """Calculate a divided by b. If b is 0, return an error string instead of raising an exception."""
    if b == 0:
        return "error: division by zero"
    return a / b


CALCULATOR_TOOLS = [add, subtract, multiply, divide]
