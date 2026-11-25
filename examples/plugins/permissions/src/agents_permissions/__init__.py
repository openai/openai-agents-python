"""Application permissions for SDK function tools."""

from typing import Any

from agents.plugins import Deny, Plugin, PluginHooks, RequireApproval, ToolCall

__all__ = ["permissions"]


def _check(call: ToolCall) -> Deny | RequireApproval | None:
    if call.context == {"decision": "allow"}:
        return None
    if call.context == {"decision": "review"}:
        return RequireApproval()
    return Deny("No permission for this call.")


def permissions() -> Plugin[Any]:
    """Require an explicit decision from RunConfig.policy_context for every SDK function."""
    return Plugin(name="permissions", hooks=PluginHooks(check_tool=_check))
