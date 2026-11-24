"""Keep native approval state and execution separate from SDK tool dispatch."""

from __future__ import annotations

from typing import Any, cast

from .._redaction import redact_payload
from ..exceptions import UserError
from ..items import ToolCallOutputItem, ToolCallOutputTypes
from ..memory.resumable_session import _ResumableSessionBinding
from ..models._codex_items import native_approval_checkpoint
from ..models._run_context import ModelToolHooks, model_run_context
from ..run_context import RunContextWrapper
from .run_steps import NextStepInterruption, NextStepRunAgain, SingleStepResult
from .tool_execution import pending_tool_span
from .turn_preparation import get_all_tools, get_model


def native_receipts_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Select the immutable native proposals while leaving decisions mutable."""
    processed = snapshot.get("last_processed_response") or {}
    approvals = processed.get("native_approvals", [])
    if not approvals:
        return None
    call_ids = {item["raw_item"]["call_id"] for item in approvals}
    step = snapshot.get("current_step") or {}
    pending = (step.get("data") or {}).get("interruptions", [])
    return {
        "plans": approvals,
        "pending": [
            item for item in pending if item.get("raw_item", {}).get("call_id") in call_ids
        ],
        "generated": [
            item
            for item in snapshot.get("generated_items", [])
            if item.get("raw_item", {}).get("call_id") in call_ids
        ],
    }


def _validate_native_plan(processed: Any) -> None:
    if len(processed.native_approvals) != 1 or any(
        (
            processed.functions,
            processed.custom_tool_calls,
            processed.handoffs,
            processed.computer_actions,
            processed.local_shell_calls,
            processed.shell_calls,
            processed.apply_patch_calls,
            processed.function_tools_not_found,
            processed.mcp_approval_requests,
        )
    ):
        raise UserError(
            "Native approvals require a single pending native call without SDK actions."
        )
    item = processed.native_approvals[0]
    if native_approval_checkpoint(item.raw_item) is None or item.tool_name != item.raw_item.name:
        raise UserError("Invalid native approval plan.")


def _native_decision(context_wrapper: RunContextWrapper[Any], pending: Any) -> bool | None:
    key = context_wrapper._resolve_approval_key(pending)
    decision = context_wrapper._approvals.get(
        context_wrapper._function_approval_key(key, pending) or key
    )
    if decision is not None and (decision.approved is True or decision.rejected is True):
        raise UserError("Native approvals require a decision for the original call only.")
    return context_wrapper.get_approval_status(
        pending.tool_name,
        pending.raw_item.call_id,
        existing_pending=pending,
        current_invocation=pending,
        tool_lookup_key=pending.tool_lookup_key,
        tool_namespace=pending.tool_namespace,
    )


def validate_native_restore_hooks(processed: Any, bindings: ModelToolHooks) -> None:
    from ..models.codex import _CodexModel, _validate_native_restore

    _validate_native_plan(processed)
    if _native_decision(bindings.context, processed.native_approvals[0]):
        # Check hooks before claiming the session. Discover tools and recheck at dispatch.
        checkpoint = native_approval_checkpoint(processed.native_approvals[0].raw_item)
        assert checkpoint is not None
        _validate_native_restore([], bindings, checkpoint)
        assert bindings.run_config is not None
        model = get_model(bindings.agent, bindings.run_config)
        if isinstance(model, _CodexModel):
            model._provider._check_workspace_approval()


def native_pending_step(
    *,
    bindings: Any,
    original_input: Any,
    pre_step_items: list[Any],
    new_response: Any,
    processed_response: Any,
    run_state: Any,
    run_config: Any,
) -> SingleStepResult:
    _validate_native_plan(processed_response)
    binding = run_state._resumable_binding if run_state is not None else None
    if (
        not isinstance(binding, _ResumableSessionBinding)
        or binding.session._checkpoint.status != "active"
    ):
        raise UserError(
            "Start the run with session=ResumableSession to recover native tool approvals."
        )
    pending = list(processed_response.native_approvals)
    processed_response.interruptions = pending
    with pending_tool_span(run_config, pending[0].tool_name, pending[0].raw_item.call_id) as span:
        span.span_data.operation.outcome = "paused"
    return SingleStepResult(
        original_input=original_input,
        model_response=new_response,
        pre_step_items=pre_step_items,
        new_step_items=[*processed_response.new_items, *pending],
        next_step=NextStepInterruption(interruptions=pending),
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        processed_response=processed_response,
    )


async def resolve_native_approval_step(
    *,
    bindings: Any,
    original_input: Any,
    original_pre_step_items: list[Any],
    new_response: Any,
    processed_response: Any,
    context_wrapper: Any,
    hooks: Any,
    run_config: Any,
    run_state: Any,
) -> SingleStepResult:
    from ..models.codex import _CodexModel

    _validate_native_plan(processed_response)
    binding = run_state._resumable_binding if run_state is not None else None
    if (
        not isinstance(binding, _ResumableSessionBinding)
        or binding.session._checkpoint.status != "active"
    ):
        raise UserError("Native approval recovery requires a claimed ResumableSession.")
    pending = processed_response.native_approvals[0]
    checkpoint = native_approval_checkpoint(pending.raw_item)
    assert checkpoint is not None
    call = pending.raw_item
    approved = _native_decision(context_wrapper, pending)
    if approved is None:
        with pending_tool_span(run_config, pending.tool_name, call.call_id) as span:
            span.span_data.operation.outcome = "paused"
        return SingleStepResult(
            original_input=original_input,
            model_response=new_response,
            pre_step_items=original_pre_step_items,
            new_step_items=[],
            next_step=NextStepInterruption(interruptions=[pending]),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
            processed_response=processed_response,
        )
    # A new checkpoint can carry the same model call ID. Claiming that checkpoint
    # doesn't authorize repeating an invocation that already used its decision.
    status = context_wrapper._tool_invocation_status(
        call, tool_lookup_key=pending.tool_lookup_key, tool_name=pending.tool_name
    )
    if status is None:
        raise UserError("Cannot match the native approval to a pending invocation.")
    _, completed, executed = status
    if executed or completed:
        raise UserError(
            "Cannot reuse a native call ID after the call has executed or completed. "
            "Start a new invocation with a new call ID."
        )
    if approved:
        from ..tracing.model_tracing import get_model_tracing_impl

        model = bindings.resolved_model or get_model(bindings.execution_agent, run_config)
        if not isinstance(model, _CodexModel):
            raise UserError("A pending native approval requires its current Codex model.")
        tools = await get_all_tools(bindings.execution_agent, context_wrapper)
        with model_run_context(
            run_state,
            ModelToolHooks(
                bindings.public_agent, context_wrapper, hooks, run_config.tool_execution
            ),
        ):
            output = await model._run_approved_native_call(
                checkpoint,
                tools=tools,
                tracing=get_model_tracing_impl(
                    run_config.tracing_disabled, run_config.trace_include_sensitive_data
                ),
            )
    else:
        from .tool_execution import resolve_approval_rejection_message

        with pending_tool_span(run_config, pending.tool_name, call.call_id):
            output = await resolve_approval_rejection_message(
                context_wrapper=context_wrapper,
                run_config=run_config,
                tool_type="shell" if call.name == "exec_command" else "apply_patch",
                tool_name=call.name,
                call_id=call.call_id,
                tool_call=call,
                existing_pending=pending,
                tool_lookup_key=pending.tool_lookup_key,
                tool_namespace=pending.tool_namespace,
            )
    output = redact_payload(output)
    result_item = ToolCallOutputItem(
        agent=bindings.public_agent,
        raw_item=cast(
            ToolCallOutputTypes,
            {
                "type": "function_call_output"
                if call.type == "function_call"
                else "custom_tool_call_output",
                "call_id": call.call_id,
                "output": output,
            },
        ),
        output=output,
    )
    context_wrapper._mark_tool_call_completed(result_item.raw_item)
    processed_response.interruptions = []
    return SingleStepResult(
        original_input=original_input,
        model_response=new_response,
        pre_step_items=original_pre_step_items,
        new_step_items=[result_item],
        next_step=NextStepRunAgain(),
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        processed_response=processed_response,
    )
