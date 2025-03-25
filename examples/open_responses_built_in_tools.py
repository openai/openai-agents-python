from dataclasses import dataclass
from agents.tool import FunctionTool
from agents.run_context import RunContextWrapper
"""
A generic, reusable tool class for Open Responses-based agents. All built-in tools can be passe in the request with tool_name.
"""

@dataclass(init=False)
class OpenResponsesBuiltInTools(FunctionTool):

    tool_name: str
    precomputed_result: str

    def __init__(self, tool_name: str):
        # Store the provided tool name.
        self.tool_name = tool_name
        self.name = tool_name
        self.description = tool_name
        # Leave the parameters schema empty.
        self.params_json_schema = {}
        # Set a fixed, precomputed result.
        self.precomputed_result = "Nothing to return"
        # Set the on_invoke_tool callback to always return the fixed result.
        self.on_invoke_tool = lambda ctx, input: self.precomputed_result
        self.strict_json_schema = True