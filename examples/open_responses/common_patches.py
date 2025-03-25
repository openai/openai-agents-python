from agents.models.openai_responses import Converter
from examples.open_responses_built_in_tools import OpenResponsesBuiltInTools

"""
Common converter to convert OpenResponsesBuiltInTools to the correct format just like web_search, file_search tool with type as tool_name.
"""
_original_convert_tool = Converter._convert_tool

def patched_convert_tool(tool):
    if isinstance(tool, OpenResponsesBuiltInTools):
        converted_tool = {
            "name": '',
            "description": '',
            "parameters": tool.params_json_schema if tool.params_json_schema else {"additionalProperties": False},
            "strict": False,
            "type": tool.tool_name  # Our custom type.
        }
        return converted_tool, None
    return _original_convert_tool(tool)

# Apply the patch
Converter._convert_tool = patched_convert_tool