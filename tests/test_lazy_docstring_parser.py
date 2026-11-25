"""Function schemas load the docstring parser only when documentation needs it."""

import subprocess
import sys


def test_function_without_docstring_defers_docstring_parser() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from agents import Agent, Model, RunConfig, Runner
from agents.decorators import tool

async def increment(x: int) -> str:
    return str(x + 1)

function_tool = tool(increment)
assert function_tool.params_json_schema["properties"]["x"]["type"] == "integer"
assert "griffe" not in sys.modules
from agents.function_schema import generate_func_documentation

def documented(x: int) -> str:
    "Describe the increment."
    return str(x + 1)

assert generate_func_documentation(documented).description == "Describe the increment."
assert "griffe" in sys.modules
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
