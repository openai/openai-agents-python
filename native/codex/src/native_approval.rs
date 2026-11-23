//! Resume one native call after the SDK has atomically claimed its saved approval.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock, Weak};

use codex_core::restore_tool_call::{ApprovalCheck, ApprovalStage, NativeApproval};
use codex_core_api::{CodexThread, StartIfIdleSubmission, TurnInputRequest, WithTurnExtensionData};
use codex_exec_server::EnvironmentAccess;
use codex_extension_api::{ExtensionDataInit, ToolPayload, ToolStartInput};
use codex_hooks::HookListEntry;
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
    checkpoint: Value,
}

enum Checks {
    Capture(Mutex<HashMap<String, Option<Pending>>>),
    Restore {
        expected: Box<EventMsg>,
        approved_turn: OnceLock<String>,
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
        stage: ApprovalStage<'a>,
    ) -> BoxFuture<'a, CodexResult<()>> {
        Box::pin(async move {
            policy.sort_all_objects();
            let permissions = format!("{:x}", Sha256::digest(policy.to_string().as_bytes()));
            match (self, stage) {
                (
                    Self::Capture(pending),
                    ApprovalStage::Capture {
                        call_id,
                        changes,
                        live,
                    },
                ) => {
                    let patch = match changes {
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
                        .entry(call_id.to_owned())
                        .and_modify(|old| *old = None)
                        .or_insert(Some(Pending { live, checkpoint }));
                }
                (
                    Self::Restore {
                        expected,
                        checkpoint,
                        approved_turn,
                    },
                    stage,
                ) if checkpoint["permissions"] == permissions => {
                    match stage {
                        ApprovalStage::Prepare if approved_turn.get().is_none() => {}
                        ApprovalStage::Execute {
                            turn_id,
                            call_id: id,
                        } if approved_turn.get().map(String::as_str) == Some(turn_id)
                            && call_id(expected) == Some(id) => {}
                        _ => {
                            return Err(CodexErr::InvalidRequest(
                                "A restored call requires a fresh matching approval.".into(),
                            ));
                        }
                    }
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
}

impl Checks {
    fn decision(&self, event: &mut EventMsg, turn_id: &str) -> Option<Op> {
        let Self::Restore {
            expected,
            checkpoint,
            approved_turn,
        } = self
        else {
            return None;
        };
        if approved_turn.get().is_some() || call_id(event)? != call_id(expected)? {
            return None;
        }
        // Legacy records use literal changes, even when a file looks like a digest.
        if checkpoint["patch"]["version"] == 2 {
            let EventMsg::ApplyPatchApprovalRequest(edit) = event else {
                return None;
            };
            if !approval_files::compact(&mut edit.changes, &checkpoint["patch"]).ok()? {
                return None;
            }
        }
        if action(event)? != action(expected)? {
            return None;
        }
        let decision = match event {
            EventMsg::ExecApprovalRequest(e)
                if e.turn_id == turn_id
                    && e.effective_available_decisions()
                        .contains(&ReviewDecision::Approved) =>
            {
                Op::ExecApproval {
                    id: e.call_id.clone(),
                    turn_id: Some(turn_id.to_owned()),
                    decision: ReviewDecision::Approved,
                }
            }
            EventMsg::ApplyPatchApprovalRequest(e) if e.turn_id == turn_id => Op::PatchApproval {
                id: e.call_id.clone(),
                decision: ReviewDecision::Approved,
            },
            _ => return None,
        };
        // A cached approval or a policy skip never passes through this gate.
        approved_turn.set(turn_id.to_owned()).ok()?;
        Some(decision)
    }
}

// A paused call never reaches PostToolUse. Confirm the command Core accepted
// after PreToolUse, independently of tracing. Only one call can be pending.
#[derive(Default)]
struct HookedInput(Mutex<Option<(String, String)>>);

pub(super) fn observe_input(input: &ToolStartInput<'_>) {
    let Some(seen) = input.turn_store.get::<HookedInput>() else {
        return;
    };
    let args;
    let command = match (input.tool_name.name.as_str(), input.payload) {
        ("exec_command", ToolPayload::Function { arguments }) => {
            args = serde_json::from_str::<Value>(arguments).unwrap_or_default();
            args["cmd"].as_str()
        }
        ("apply_patch", ToolPayload::Custom { input }) => Some(input.as_str()),
        _ => return,
    };
    *seen.0.lock().unwrap() = command.map(|s| {
        (
            input.call_id.to_owned(),
            format!("{:x}", Sha256::digest(s.as_bytes())),
        )
    });
}

pub(super) fn capture(
    request: TurnInputRequest,
    hooks: Vec<HookListEntry>,
) -> WithTurnExtensionData<TurnInputRequest> {
    let mut init = ExtensionDataInit::new();
    if !hooks.is_empty() {
        init.insert(HookedInput::default());
    }
    init.insert(Checks::Capture(Mutex::default()));
    let checks = init.get::<Checks>().unwrap();
    init.insert(NativeApproval {
        check: checks,
        call: None,
        hooks,
    });
    WithTurnExtensionData::new(request, init)
}

pub(super) async fn checkpoint(
    thread: &CodexThread,
    event: &mut Event,
) -> Option<(Value, Option<String>)> {
    let mut input_hash = None;
    if let Some(seen) = thread
        .current_turn_extension_data::<HookedInput>(&event.id)
        .await
    {
        let (id, hash) = seen.0.lock().unwrap().take()?;
        if call_id(&event.msg)? != id {
            return None;
        }
        input_hash = Some(hash);
    }
    let checks = thread
        .current_turn_extension_data::<Checks>(&event.id)
        .await?;
    let Checks::Capture(pending) = checks.as_ref() else {
        return None;
    };
    let mut saved = pending
        .lock()
        .unwrap()
        .get_mut(call_id(&event.msg)?)?
        .take()?;
    let _live = saved.live.upgrade()?;
    let patch = &mut saved.checkpoint["patch"];
    match &mut event.msg {
        EventMsg::ApplyPatchApprovalRequest(edit) if edit.turn_id == event.id => {
            if approval_files::compact(&mut edit.changes, patch).ok()? {
                patch["version"] = json!(2);
            }
        }
        EventMsg::ExecApprovalRequest(e) if e.turn_id == event.id && patch.is_null() => {}
        _ => return None,
    }
    Some((saved.checkpoint, input_hash))
}

pub(super) async fn resolve(
    thread: &CodexThread,
    call: ResponseItem,
    expected: EventMsg,
    checkpoint: Value,
    operations: &crate::operation_tracing::Root,
    hooks: Vec<HookListEntry>,
) -> NativeResult<String> {
    let (id, patch) = match (&call, &expected) {
        (
            ResponseItem::FunctionCall {
                call_id,
                name,
                namespace,
                ..
            },
            EventMsg::ExecApprovalRequest(_),
        ) if name == "exec_command" && matches!(namespace.as_deref(), None | Some("functions")) => {
            (call_id, false)
        }
        (
            ResponseItem::CustomToolCall {
                call_id,
                name,
                namespace,
                ..
            },
            EventMsg::ApplyPatchApprovalRequest(_),
        ) if name == "apply_patch" && matches!(namespace.as_deref(), None | Some("functions")) => {
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
        approved_turn: OnceLock::new(),
        checkpoint,
    });
    init.insert(NativeApproval {
        check: checks.clone(),
        call: Some(call),
        hooks,
    });
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
            mut review @ (EventMsg::ExecApprovalRequest(_)
            | EventMsg::ApplyPatchApprovalRequest(_)) => checks
                .decision(&mut review, &turn_id)
                .ok_or("The fresh Codex approval does not match the saved call.")?,
            EventMsg::RequestPermissions(_)
            | EventMsg::RequestUserInput(_)
            | EventMsg::ElicitationRequest(_)
            | EventMsg::DynamicToolCallRequest(_) => {
                return Err("Codex requested another decision while restoring the call.".into());
            }
            EventMsg::PatchApplyEnd(end)
                if answered && patch && end.call_id == call_id && end.turn_id == turn_id =>
            {
                finished = end.success;
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
