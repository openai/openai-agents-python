from __future__ import annotations

import json
import inspect
from collections.abc import Sequence
from typing import Any, Dict # Removed Set, get_type_hints, get_origin, get_args, Annotated

from ...exceptions import AgentsException, UserError
from ...logger import logger
from ...run_context import RunContextWrapper
from ...tool import (
    FunctionTool,
    Tool,
)
from .model import RealtimeEventToolCall


class ToolExecutor:
    """Executes tools based on RealtimeEventToolCall events."""

    def __init__(self, tools: Sequence[Tool], shared_context: Any | None = None):
        self._tool_map: Dict[str, FunctionTool] = {}
        self._shared_context = shared_context
        # self._context_aware_tools: Set[str] = set() # Removed

        for tool in tools:
            if isinstance(tool, FunctionTool):
                self._tool_map[tool.name] = tool
                # Removed context-awareness detection logic
            else: # Tool is not a FunctionTool
                logger.warning(
                    f"Tool '{tool.name}' is not a FunctionTool and will be ignored by ToolExecutor."
                )

        # logger.info(f"Final list of context-aware tools: {self._context_aware_tools}") # Removed

    async def execute(self, tool_call_event: RealtimeEventToolCall) -> str:
        """Executes the specified tool and returns its string output.

        Args:
            tool_call_event: The RealtimeEventToolCall describing the tool to execute.

        Returns:
            A string representation of the tool's output (typically JSON).
        """
        tool_name = tool_call_event.tool_name
        tool = self._tool_map.get(tool_name)

        if not tool:
            err_msg = f"Tool '{tool_name}' not found in ToolExecutor."
            logger.error(err_msg)
            return json.dumps({"error": err_msg, "tool_name": tool_name})

        try:
            arguments_json = json.dumps(tool_call_event.arguments)
        except TypeError as e:  # pragma: no cover
            err_msg = f"Failed to serialize arguments for tool '{tool_name}': {e}"
            logger.error(f"{err_msg} Arguments: {tool_call_event.arguments}")
            return json.dumps({"error": err_msg, "tool_name": tool_name})
        
        current_context_wrapper = RunContextWrapper(context=self._shared_context)
        logger.info(
            f"Executing tool: {tool_name} with args: {arguments_json}, providing RunContextWrapper."
        )

        try:
            # Always pass RunContextWrapper, consistent with _run_impl.py
            # FunctionTool itself is responsible for handling context for the user's function
            tool_output = await tool.on_invoke_tool(
                current_context_wrapper, arguments_json
            )

            if not isinstance(tool_output, str):
                if isinstance(tool_output, (dict, list)):
                    tool_output_str = json.dumps(tool_output)
                else:
                    tool_output_str = str(tool_output)
            else:
                tool_output_str = tool_output

            logger.info(
                f"Tool {tool_name} executed successfully. Output length: {len(tool_output_str)}"
            )
            return tool_output_str
        except UserError as ue: # Specific error handling
            logger.error(f"User error executing tool '{tool_name}': {ue}")
            return json.dumps({"error": str(ue), "tool_name": tool_name, "error_type": "UserError"})
        except AgentsException as ae: # Specific error handling
            logger.error(f"Agents framework error executing tool '{tool_name}': {ae}", exc_info=True)
            return json.dumps({"error": str(ae), "tool_name": tool_name, "error_type": "AgentsException"})
        except Exception as e:  # pragma: no cover
            logger.error(f"Error executing tool '{tool_name}': {e}", exc_info=True)
            return json.dumps({"error": str(e), "tool_name": tool_name, "error_type": "UnhandledException"})
