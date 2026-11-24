from .exceptions import UserError
from .memory.resumable_session import Plan, PlanStep
from .tool import Tool, function_tool
from .tool_context import ToolContext

_MISSING_SESSION = "update_plan requires a ResumableSession passed to Runner as session."


async def _update_plan(
    ctx: ToolContext, steps: list[PlanStep], explanation: str | None = None
) -> str:
    """Replace the plan with ordered steps and their statuses. Empty steps clear the plan."""
    propose = ctx._propose_plan
    if propose is None:
        raise UserError(_MISSING_SESSION)
    ctx._propose_plan = None
    propose(Plan(tool_call_id=ctx.tool_call_id, steps=tuple(steps), explanation=explanation))
    return "Plan updated."


update_plan = function_tool(_update_plan, name_override="update_plan", failure_error_function=None)


def is_plan_tool(tool: Tool) -> bool:
    return getattr(tool, "__wrapped__", None) is _update_plan


def validate_plan_session(tools: list[Tool]) -> None:
    from .run_internal.agent_tool_configuration import current_plan_session

    if current_plan_session() is None and any(is_plan_tool(tool) for tool in tools):
        raise UserError(_MISSING_SESSION)
