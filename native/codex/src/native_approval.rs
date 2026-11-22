//! Resume one native call after the SDK has atomically claimed its saved approval.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, Weak};

use codex_core::restore_tool_call::{ApprovalCheck, FirstApproval, NativeApproval};
use codex_core_api::{CodexThread, StartIfIdleSubmission, TurnInputRequest, WithTurnExtensionData};
use codex_exec_server::EnvironmentAccess;
use codex_extension_api::ExtensionDataInit;
use codex_protocol::approvals::ExecApprovalKind;
use codex_protocol::error::{CodexErr, Result as CodexResult};
use codex_protocol::models::ResponseItem;
use codex_protocol::protocol::{Event, EventMsg, Op, ReviewDecision};
use futures::future::BoxFuture;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::{NativeResult, approval_files};

struct Pending {
    live: Weak<()>,
    request: Option<Value>,
    checkpoint: Value,
}

enum Checks {
    Capture(Mutex<HashMap<String, Option<Pending>>>),
    Restore {
        expected: Box<EventMsg>,
        checkpoint: Value,
    },
}

fn call_id(event: &EventMsg) -> Option<&str> {
    match event {
        EventMsg::ExecApprovalRequest(e)
            if e.kind == ExecApprovalKind::Command
                && e.approval_id.is_none()
                && e.network_approval_context.is_none() =>
        {
            Some(&e.call_id)
        }
        EventMsg::ApplyPatchApprovalRequest(e) if e.grant_root.is_none() => Some(&e.call_id),
        _ => None,
    }
}

// Only these two fields change when Codex prepares the same action on a new turn.
fn action(event: &EventMsg) -> Option<Value> {
    let mut value = serde_json::to_value(event).ok()?;
    let fields = value.as_object_mut()?;
    fields.remove("turn_id");
    fields.remove("started_at_ms");
    Some(value)
}

impl ApprovalCheck for Checks {
    fn check<'a>(
        &'a self,
        mut policy: Value,
        fs: &'a dyn EnvironmentAccess,
        first: Option<FirstApproval<'a>>,
    ) -> BoxFuture<'a, CodexResult<()>> {
        Box::pin(async move {
            policy.sort_all_objects();
            let permissions = format!("{:x}", Sha256::digest(policy.to_string().as_bytes()));
            match (self, first) {
                (Self::Capture(pending), Some(first)) => {
                    let patch = match first.changes {
                        Some(changes) => approval_files::capture(fs, changes).await?,
                        None => Value::Null,
                    };
                    let checkpoint = json!({
                        "version": 1, "first_approval": true, "permissions": permissions, "patch": patch,
                    });
                    // A repeated ID invalidates both requests for the rest of the turn.
                    pending
                        .lock()
                        .unwrap()
                        .entry(first.call_id.to_owned())
                        .and_modify(|old| *old = None)
                        .or_insert(Some(Pending {
                            live: first.live,
                            request: None,
                            checkpoint,
                        }));
                }
                (
                    Self::Restore {
                        expected,
                        checkpoint,
                    },
                    None,
                ) if checkpoint["permissions"] == permissions => {
                    if let EventMsg::ApplyPatchApprovalRequest(request) = expected.as_ref() {
                        approval_files::validate(fs, &request.changes, &checkpoint["patch"])
                            .await?;
                    }
                }
                _ => {
                    return Err(CodexErr::InvalidRequest(
                        "Cannot verify saved permissions.".into(),
                    ));
                }
            }
            Ok(())
        })
    }

    fn review(&self, event: &mut EventMsg) -> bool {
        match self {
            Self::Capture(pending) => {
                let mut pending = pending.lock().unwrap();
                if let Some(slot) = call_id(event).and_then(|id| pending.get_mut(id))
                    && let Some(mut saved) = slot.take()
                    && saved.request.is_none()
                {
                    if let EventMsg::ApplyPatchApprovalRequest(edit) = event {
                        let patch = &mut saved.checkpoint["patch"];
                        match approval_files::compact(&mut edit.changes, patch) {
                            Ok(true) => patch["version"] = json!(2),
                            Ok(false) => {}
                            Err(_) => return true, // No checkpoint. The SDK refuses this request.
                        }
                    }
                    saved.request = serde_json::to_value(event).ok();
                    *slot = Some(saved);
                }
                true
            }
            Self::Restore {
                expected,
                checkpoint,
            } => {
                if call_id(event) != call_id(expected) {
                    return false;
                }
                // Older records contain the literal prepared request, even for hash-like text.
                if checkpoint["patch"]["version"] == 2 {
                    let EventMsg::ApplyPatchApprovalRequest(edit) = event else {
                        return false;
                    };
                    if !matches!(
                        approval_files::compact(&mut edit.changes, &checkpoint["patch"]),
                        Ok(true)
                    ) {
                        return false;
                    }
                }
                matches!((action(event), action(expected)), (Some(a), Some(b)) if a == b)
            }
        }
    }
}

pub(super) fn capture(request: TurnInputRequest) -> WithTurnExtensionData<TurnInputRequest> {
    let mut init = ExtensionDataInit::new();
    init.insert(Checks::Capture(Mutex::default()));
    let checks = init.get::<Checks>().unwrap();
    init.insert(NativeApproval::new(checks, None));
    WithTurnExtensionData::new(request, init)
}

pub(super) async fn checkpoint(thread: &CodexThread, event: &Event) -> Option<Value> {
    let checks = thread
        .current_turn_extension_data::<Checks>(&event.id)
        .await?;
    let Checks::Capture(pending) = checks.as_ref() else {
        return None;
    };
    let saved = pending
        .lock()
        .unwrap()
        .get_mut(call_id(&event.msg)?)?
        .take()?;
    let _live = saved.live.upgrade()?;
    (saved.request? == serde_json::to_value(&event.msg).ok()?).then_some(saved.checkpoint)
}

pub(super) async fn resolve(
    thread: &CodexThread,
    call: ResponseItem,
    expected: EventMsg,
    checkpoint: Value,
    operations: &crate::operation_tracing::Root,
) -> NativeResult<String> {
    let (id, patch) = match (&call, &expected) {
        (ResponseItem::FunctionCall { call_id, .. }, EventMsg::ExecApprovalRequest(_)) => {
            (call_id, false)
        }
        (ResponseItem::CustomToolCall { call_id, .. }, EventMsg::ApplyPatchApprovalRequest(_)) => {
            (call_id, true)
        }
        _ => return Err("Unsupported Codex approval request.".into()),
    };
    if call_id(&expected) != Some(id)
        || checkpoint["version"] != 1
        || checkpoint["first_approval"] != true
    {
        return Err("Invalid saved Codex approval.".into());
    }
    let call_id = id.clone();
    let mut init = thread.config_snapshot().await.turn_extension_init;
    let checks = Arc::new(Checks::Restore {
        expected: Box::new(expected),
        checkpoint,
    });
    init.insert(NativeApproval::new(checks, Some(call)));
    let request = WithTurnExtensionData::new(TurnInputRequest::user_input(Vec::new()), init);
    let StartIfIdleSubmission::Started { turn_id, .. } =
        Box::pin(thread.start_turn_if_idle(request))
            .await
            .map_err(|_| "Could not restore the pending Codex approval.")?
    else {
        return Err("The Codex thread is busy.".into());
    };

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
            EventMsg::PatchApplyEnd(end)
                if answered && patch && end.call_id == call_id && end.turn_id == turn_id =>
            {
                finished = true;
                continue;
            }
            EventMsg::RawResponseItem(item) => {
                operations.observe_item(&item.item);
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
                if !patch {
                    // Core only returns a successful restored command result after exit.
                    // Its asynchronous ExecCommandEnd event may follow TurnComplete.
                    finished = output.success == Some(true);
                }
                result = output.body.to_text();
                continue;
            }
            EventMsg::TurnComplete(_) => {
                if !finished {
                    return Err("Codex could not confirm that the approved call finished.".into());
                }
                return result.ok_or_else(|| "Codex did not complete the approved call.".into());
            }
            EventMsg::TurnAborted(_) | EventMsg::Error(_) | EventMsg::ShutdownComplete => {
                if answered {
                    return Err("Codex could not confirm that the approved call finished.".into());
                }
                return Err(
                    "Codex could not complete the restored approval under current policy.".into(),
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
