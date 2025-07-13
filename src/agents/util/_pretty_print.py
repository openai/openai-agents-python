from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ..exceptions import RunErrorDetails
    from ..result import RunResult, RunResultBase, RunResultStreaming


def _indent(text: str, indent_level: int) -> str:
    indent_string = "  " * indent_level
    return "\n".join(f"{indent_string}{line}" for line in text.splitlines())


def _final_output_str(result: "RunResultBase") -> str:
    if result.final_output is None:
        return "None"
    elif isinstance(result.final_output, str):
        return result.final_output
    elif isinstance(result.final_output, BaseModel):
        return result.final_output.model_dump_json(indent=2)
    else:
        return str(result.final_output)


def pretty_print_result(result: "RunResult") -> str:
    output = "RunResult:"
    output += f'\n- Last agent: Agent(name="{result.last_agent.name}", ...)'
    output += (
        f"\n- Final output ({type(result.final_output).__name__}):\n"
        f"{_indent(_final_output_str(result), 2)}"
    )
    output += f"\n- {len(result.new_items)} new item(s)"
    output += f"\n- {len(result.raw_responses)} raw response(s)"
    output += f"\n- {len(result.input_guardrail_results)} input guardrail result(s)"
    output += f"\n- {len(result.output_guardrail_results)} output guardrail result(s)"
    output += "\n(See `RunResult` for more details)"

    return output


def pretty_print_run_error_details(result: "RunErrorDetails") -> str:
    output = "RunErrorDetails:"
    output += f'\n- Last agent: Agent(name="{result.last_agent.name}", ...)'
    output += f"\n- {len(result.new_items)} new item(s)"
    output += f"\n- {len(result.raw_responses)} raw response(s)"
    output += f"\n- {len(result.input_guardrail_results)} input guardrail result(s)"
    output += "\n(See `RunErrorDetails` for more details)"

    return output


def pretty_print_run_result_streaming(result: "RunResultStreaming") -> str:
    output = "RunResultStreaming:"
    output += f'\n- Current agent: Agent(name="{result.current_agent.name}", ...)'
    output += f"\n- Current turn: {result.current_turn}"
    output += f"\n- Max turns: {result.max_turns}"
    output += f"\n- Is complete: {result.is_complete}"
    output += (
        f"\n- Final output ({type(result.final_output).__name__}):\n"
        f"{_indent(_final_output_str(result), 2)}"
    )
    output += f"\n- {len(result.new_items)} new item(s)"
    output += f"\n- {len(result.raw_responses)} raw response(s)"
    output += f"\n- {len(result.input_guardrail_results)} input guardrail result(s)"
    output += f"\n- {len(result.output_guardrail_results)} output guardrail result(s)"
    output += "\n(See `RunResultStreaming` for more details)"
    return output
