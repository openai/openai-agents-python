//! Resume one native call after the SDK has atomically claimed its saved approval.

use codex_core_api::{CodexThread, StartIfIdleSubmission};
use codex_extension_api::ExtensionDataInit;
use codex_protocol::models::ResponseItem;
use codex_protocol::protocol::{EventMsg, Op, ReviewDecision};

use crate::NativeResult;

pub(super) async fn resolve(
    thread: &CodexThread,
    call: ResponseItem,
    expected: EventMsg,
    preconditions: Option<serde_json::Value>,
    capture: ExtensionDataInit,
) -> NativeResult<(Option<String>, String, Option<String>)> {
    let call_id = match (&call, &expected) {
        (ResponseItem::FunctionCall { call_id, .. }, EventMsg::ExecApprovalRequest(request))
            if call_id == &request.call_id =>
        {
            call_id.clone()
        }
        (
            ResponseItem::CustomToolCall { call_id, .. },
            EventMsg::ApplyPatchApprovalRequest(request),
        ) if call_id == &request.call_id => call_id.clone(),
        _ => return Err("The saved Codex approval does not match its tool call.".into()),
    };
    let patch = matches!(expected, EventMsg::ApplyPatchApprovalRequest(_));
    // Restoration dispatches this call through Core. Core checks the saved request
    // against its freshly prepared action before it can ask us for the decision.
    let StartIfIdleSubmission::Started { turn_id, .. } =
        Box::pin(thread.restore_tool_call_if_idle(call, expected, preconditions, Some(capture)))
            .await
            .map_err(|_| "Could not restore the pending Codex approval.")?
    else {
        return Err("The Codex thread is busy.".into());
    };

    let outcome: NativeResult<String> = async {
        let mut answered = false;
        let mut finished = false;
        let mut result = None;
        loop {
            let event = thread
                .next_event()
                .await
                .map_err(|_| "The restored Codex turn ended unexpectedly.")?;
            if event.id != turn_id {
                continue;
            }
            let decision = match event.msg {
                EventMsg::ExecApprovalRequest(request)
                    if !patch
                        && !answered
                        && request.call_id == call_id
                        && request.approval_id.is_none()
                        && request.turn_id == turn_id
                        && request
                            .effective_available_decisions()
                            .contains(&ReviewDecision::Approved) =>
                {
                    Op::ExecApproval {
                        id: request.call_id,
                        turn_id: Some(turn_id.clone()),
                        decision: ReviewDecision::Approved,
                    }
                }
                EventMsg::ApplyPatchApprovalRequest(request)
                    if patch
                        && !answered
                        && request.call_id == call_id
                        && request.turn_id == turn_id =>
                {
                    Op::PatchApproval {
                        id: request.call_id,
                        decision: ReviewDecision::Approved,
                    }
                }
                EventMsg::ExecApprovalRequest(_)
                | EventMsg::ApplyPatchApprovalRequest(_)
                | EventMsg::RequestPermissions(_)
                | EventMsg::RequestUserInput(_)
                | EventMsg::ElicitationRequest(_)
                | EventMsg::DynamicToolCallRequest(_) => {
                    return Err("Codex requested another decision while restoring the call.".into());
                }
                EventMsg::ExecCommandEnd(end)
                    if answered && !patch && end.call_id == call_id && end.turn_id == turn_id =>
                {
                    finished = true;
                    continue;
                }
                EventMsg::PatchApplyEnd(end)
                    if answered && patch && end.call_id == call_id && end.turn_id == turn_id =>
                {
                    finished = true;
                    continue;
                }
                EventMsg::RawResponseItem(item) => {
                    let output = match item.item {
                        ResponseItem::FunctionCallOutput {
                            call_id: Some(id),
                            output,
                            ..
                        } if !patch && id == call_id => output,
                        ResponseItem::CustomToolCallOutput {
                            call_id: id,
                            output,
                            ..
                        } if patch && id == call_id => output,
                        _ => continue,
                    };
                    if !answered || result.is_some() {
                        return Err(
                            "Codex could not confirm the saved approval before dispatch.".into(),
                        );
                    }
                    result = output.body.to_text();
                    continue;
                }
                EventMsg::TurnComplete(_) => {
                    if !finished {
                        // An exec can yield a handle to a still-running process. That handle
                        // dies with this thread. Don't checkpoint its uncertain effect as done.
                        return Err(
                            "Codex could not confirm that the approved call finished.".into()
                        );
                    }
                    return result
                        .ok_or_else(|| "Codex did not complete the approved call.".into());
                }
                EventMsg::TurnAborted(_) | EventMsg::Error(_) | EventMsg::ShutdownComplete => {
                    return Err(
                        "Codex could not complete the restored approval under current policy."
                            .into(),
                    );
                }
                _ => continue,
            };
            thread
                .submit(decision)
                .await
                .map_err(|_| "Could not submit the saved approval to Codex.")?;
            answered = true;
        }
    }
    .await;
    match outcome {
        Ok(output) => Ok((Some(output), turn_id, None)),
        Err(failure) => {
            // Close real Core work before the caller drains its cancelled/failed observations.
            let _ = thread.shutdown_and_wait().await;
            Ok((None, turn_id, Some(failure)))
        }
    }
}
