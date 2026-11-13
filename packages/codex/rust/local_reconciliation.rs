//! Import application-confirmed outcomes for original local calls without replay.

use std::collections::{HashMap, HashSet};

use anyhow::{Context, Result, ensure};
use codex_exec_server::REMOTE_ENVIRONMENT_ID;
use codex_history::{ResponseItemEnvelope, RolloutItem};
use codex_protocol::ThreadId;
use codex_protocol::models::{
    ContentItemKind, FunctionCallOutputBody, FunctionCallOutputPayload,
    InternalChatMessageMetadataPassthrough, PermissionProfile, ResponseItem,
};
use codex_protocol::permissions::{FileSystemAccessMode, FileSystemPath};
use codex_protocol::protocol::{
    EventMsg, TurnAbortReason, TurnAbortedEvent, sdk_executor_policy_receipt,
};
use codex_utils_path_uri::PathUri;
use serde::Deserialize;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::engine::ThreadState;

const APPLIED: &str = "sdk_reconciliation.applied";
const NOT_APPLIED: &str = "sdk_reconciliation.not_applied";

pub(crate) fn shell_call_id(fields: &Map<String, Value>) -> Option<&String> {
    match fields.get("call_id") {
        Some(Value::String(id)) => Some(id),
        _ => None,
    }
}

pub(crate) fn output_identity(item: &ResponseItem) -> Option<(&str, Option<&str>)> {
    let call: &str = match item {
        ResponseItem::FunctionCallOutput {
            call_id: Some(call),
            ..
        }
        | ResponseItem::CustomToolCallOutput { call_id: call, .. }
        | ResponseItem::ApplyPatchCallOutput { call_id: call, .. }
        | ResponseItem::ComputerCallOutput { call_id: call, .. } => call,
        ResponseItem::McpApprovalResponse {
            approval_request_id,
            ..
        } => approval_request_id.as_str(),
        ResponseItem::ShellCallOutput {
            provider_fields, ..
        } => shell_call_id(provider_fields)?,
        _ => return None,
    };
    Some((call, item.turn_id()))
}

pub(crate) fn same_acknowledged_output(left: &ResponseItem, right: &ResponseItem) -> bool {
    if !same_output_payload(left, right) {
        return false;
    }
    let normalize = |item: &ResponseItem| -> Option<serde_json::Value> {
        let mut value = serde_json::to_value(item).ok()?;
        let object = value.as_object_mut()?;
        object.remove("id");
        if let Some(metadata) = object
            .get_mut("internal_chat_message_metadata_passthrough")
            .and_then(serde_json::Value::as_object_mut)
        {
            metadata.remove("create_time");
        }
        Some(value)
    };
    normalize(left)
        .zip(normalize(right))
        .is_some_and(|(left, right)| left == right)
}

#[cfg(test)]
mod acknowledged_original_tests {
    use super::*;
    #[test]
    fn ignores_only_generated_id_and_time() {
        let make = |name: &str, turn: &str, generated: bool| {
            let mut value = serde_json::json!({
                "type":"custom_tool_call_output", "name":name, "call_id":"call",
                "output":"accepted", "internal_chat_message_metadata_passthrough":{"turn_id":turn}
            });
            if generated {
                value["id"] = serde_json::json!("cto_generated");
                value["internal_chat_message_metadata_passthrough"]["create_time"] =
                    serde_json::json!(1.25);
            }
            serde_json::from_value::<ResponseItem>(value).unwrap()
        };
        let accepted = make("original", "turn", false);
        assert!(same_acknowledged_output(
            &accepted,
            &make("original", "turn", true)
        ));
        assert!(!same_acknowledged_output(
            &accepted,
            &make("substitute", "turn", true)
        ));
        assert!(!same_acknowledged_output(
            &accepted,
            &make("original", "foreign", true)
        ));
    }

    #[test]
    fn hosted_mcp_receipts_preserve_the_complete_approval_decision() {
        let make = |approve: bool, request: &str, reason: &str, caller: &str, generated: bool| {
            let mut value = serde_json::json!({
                "type": "mcp_approval_response", "approval_request_id": request,
                "approve": approve, "reason": reason, "caller": {"type": caller},
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn"}
            });
            if generated {
                value["id"] = serde_json::json!("mcp_response_generated");
                value["internal_chat_message_metadata_passthrough"]["create_time"] =
                    serde_json::json!(1.25);
            }
            serde_json::from_value::<ResponseItem>(value).unwrap()
        };
        for approve in [true, false] {
            let accepted = make(approve, "approval", "decision", "direct", false);
            assert!(same_acknowledged_output(
                &accepted,
                &make(approve, "approval", "decision", "direct", true)
            ));
            for changed in [
                make(!approve, "approval", "decision", "direct", false),
                make(approve, "other", "decision", "direct", false),
                make(approve, "approval", "substitute", "direct", false),
                make(approve, "approval", "decision", "programmatic", false),
            ] {
                assert!(!same_output_payload(&accepted, &changed));
                assert!(!same_acknowledged_output(&accepted, &changed));
            }
        }
    }

    #[test]
    fn completed_and_failed_patch_receipts_require_exact_status_and_output() {
        let make = |status: &str, output: &str| {
            serde_json::from_value::<ResponseItem>(serde_json::json!({
                "type":"apply_patch_call_output", "call_id":"call", "status":status, "output":output
            }))
            .unwrap()
        };
        for status in ["completed", "failed"] {
            let accepted = make(status, "accepted output");
            assert!(matches!(
                &accepted,
                ResponseItem::ApplyPatchCallOutput { .. }
            ));
            assert!(same_output_payload(
                &accepted,
                &make(status, "accepted output")
            ));
            assert!(same_acknowledged_output(
                &accepted,
                &make(status, "accepted output")
            ));
            assert!(!same_output_payload(
                &accepted,
                &make(status, "modified output")
            ));
            assert!(!same_output_payload(
                &accepted,
                &make(
                    if status == "completed" {
                        "failed"
                    } else {
                        "completed"
                    },
                    "accepted output"
                )
            ));
        }
    }
}

pub(crate) fn same_output_payload(left: &ResponseItem, right: &ResponseItem) -> bool {
    match (left, right) {
        (
            ResponseItem::FunctionCallOutput { output: left, .. },
            ResponseItem::FunctionCallOutput { output: right, .. },
        )
        | (
            ResponseItem::CustomToolCallOutput { output: left, .. },
            ResponseItem::CustomToolCallOutput { output: right, .. },
        ) => serde_json::to_value(left)
            .ok()
            .zip(serde_json::to_value(right).ok())
            .is_some_and(|(left, right)| left == right),
        (
            ResponseItem::McpApprovalResponse {
                approval_request_id: left_request,
                approve: left_approve,
                reason: left_reason,
                caller: left_caller,
                ..
            },
            ResponseItem::McpApprovalResponse {
                approval_request_id: right_request,
                approve: right_approve,
                reason: right_reason,
                caller: right_caller,
                ..
            },
        ) => {
            left_request == right_request
                && left_approve == right_approve
                && left_reason == right_reason
                && left_caller == right_caller
        }
        (
            ResponseItem::ApplyPatchCallOutput {
                status: left_status,
                output: left_output,
                ..
            },
            ResponseItem::ApplyPatchCallOutput {
                status: right_status,
                output: right_output,
                ..
            },
        ) => left_status == right_status && left_output == right_output,
        (
            ResponseItem::ComputerCallOutput {
                output: left,
                acknowledged_safety_checks: left_checks,
                ..
            },
            ResponseItem::ComputerCallOutput {
                output: right,
                acknowledged_safety_checks: right_checks,
                ..
            },
        ) => left == right && left_checks == right_checks,
        (
            ResponseItem::ShellCallOutput {
                provider_fields: left,
                ..
            },
            ResponseItem::ShellCallOutput {
                provider_fields: right,
                ..
            },
        ) => {
            left.get("output")
                .zip(right.get("output"))
                .is_some_and(|(left, right)| left == right)
                && left.get("max_output_length") == right.get("max_output_length")
        }
        _ => false,
    }
}

pub(crate) fn output_matches_invocation(call: &ResponseItem, result: &ResponseItem) -> bool {
    match (call, result) {
        (
            ResponseItem::FunctionCall {
                call_id: expected, ..
            },
            ResponseItem::FunctionCallOutput {
                call_id: Some(actual),
                ..
            },
        )
        | (
            ResponseItem::CustomToolCall {
                call_id: expected, ..
            },
            ResponseItem::CustomToolCallOutput {
                call_id: actual, ..
            },
        )
        | (
            ResponseItem::ApplyPatchCall {
                call_id: expected, ..
            },
            ResponseItem::ApplyPatchCallOutput {
                call_id: actual, ..
            },
        )
        | (
            ResponseItem::ComputerCall {
                call_id: expected, ..
            },
            ResponseItem::ComputerCallOutput {
                call_id: actual, ..
            },
        ) => expected == actual,
        (
            ResponseItem::McpApprovalRequest {
                id: Some(expected),
                provider_fields,
                ..
            },
            ResponseItem::McpApprovalResponse {
                approval_request_id: actual,
                caller,
                ..
            },
        ) => {
            expected.as_str() == actual.as_str()
                && provider_fields
                    .get("caller")
                    .filter(|value| !value.is_null())
                    == caller.as_ref().filter(|value| !value.is_null())
        }
        (
            ResponseItem::ShellCall {
                provider_fields: expected,
                ..
            },
            ResponseItem::ShellCallOutput {
                provider_fields: actual,
                ..
            },
        ) => shell_call_id(expected).is_some_and(|id| shell_call_id(actual) == Some(id)),
        _ => false,
    }
}

pub(crate) fn owns_application_patch(envelope: &codex_history::ResponseItemEnvelope) -> bool {
    matches!(envelope.item, ResponseItem::ApplyPatchCall { .. })
        && envelope
            .metadata
            .as_ref()
            .and_then(|metadata| metadata.sdk_application_tool.as_deref())
            == Some("apply_patch")
}

pub(crate) fn application_patch_receipt(
    invocation: &ResponseItem,
    response: codex_protocol::dynamic_tools::DynamicToolResponse,
    turn: &str,
) -> Result<ResponseItem> {
    use codex_tools::ToolOutput as _;
    let ResponseItem::ApplyPatchCall { call_id, .. } = invocation else {
        anyhow::bail!("Application patch receipt requires its original call");
    };
    let arguments = serde_json::to_string(invocation)?;
    let output = codex_tools::ApplyPatchToolOutput::from_dynamic_response(&arguments, response)
        .map_err(|_| anyhow::anyhow!("Invalid application patch receipt"))?;
    let mut item: ResponseItem = output
        .to_response_item(call_id, &codex_tools::ToolPayload::ApplyPatch { arguments })
        .into();
    item.set_turn_id_if_missing(turn);
    Ok(item)
}

pub(crate) fn computer_receipt(
    invocation: &ResponseItem,
    response: codex_protocol::dynamic_tools::DynamicToolResponse,
    turn: &str,
) -> Result<ResponseItem> {
    use codex_tools::ToolOutput as _;
    let ResponseItem::ComputerCall { call_id, .. } = invocation else {
        anyhow::bail!("Computer reconciliation requires its original call");
    };
    let arguments = serde_json::to_string(invocation)
        .map_err(|_| anyhow::anyhow!("Invalid original computer call"))?;
    let output = codex_tools::ComputerToolOutput::from_dynamic_response(&arguments, response)
        .map_err(|_| {
            anyhow::anyhow!(
                "Computer reconciliation requires a valid screenshot and safety acknowledgements"
            )
        })?;
    let mut item: ResponseItem = output
        .to_response_item(call_id, &codex_tools::ToolPayload::Computer { arguments })
        .into();
    let ResponseItem::ComputerCallOutput {
        internal_chat_message_metadata_passthrough,
        ..
    } = &mut item
    else {
        unreachable!("validated computer output has the native computer shape");
    };
    *internal_chat_message_metadata_passthrough = Some(InternalChatMessageMetadataPassthrough {
        turn_id: Some(turn.to_owned()),
        ..Default::default()
    });
    Ok(item)
}

pub(crate) fn shell_receipt(
    arguments: &str,
    response: codex_protocol::dynamic_tools::DynamicToolResponse,
    turn: &str,
    call_id: &str,
) -> Result<ResponseItem> {
    use codex_tools::ToolOutput as _;
    let output = codex_tools::ApplicationShellOutput::from_dynamic_response(arguments, response)
        .map_err(|_| anyhow::anyhow!("Invalid native shell response"))?;
    let mut item = ResponseItem::from(output.to_response_item(
        call_id,
        &codex_tools::ToolPayload::Shell {
            arguments: arguments.to_owned(),
        },
    ));
    let ResponseItem::ShellCallOutput {
        internal_chat_message_metadata_passthrough,
        ..
    } = &mut item
    else {
        unreachable!("validated Shell output has the native Shell shape");
    };
    *internal_chat_message_metadata_passthrough = Some(InternalChatMessageMetadataPassthrough {
        turn_id: Some(turn.to_owned()),
        ..Default::default()
    });
    Ok(item)
}

fn computer_reconciliation(
    invocation: &ResponseItem,
    result: &str,
    turn: &str,
) -> Result<ResponseItem> {
    computer_receipt(
        invocation,
        codex_protocol::dynamic_tools::DynamicToolResponse {
            content_items: vec![
                codex_protocol::dynamic_tools::DynamicToolCallOutputContentItem::InputText {
                    text: result.to_owned(),
                },
            ],
            success: true,
        },
        turn,
    )
}

fn shell_reconciliation(
    invocation: &ResponseItem,
    receipt: &ReconciledCall,
) -> Result<ResponseItem> {
    shell_receipt(
        &serde_json::to_string(invocation)?,
        codex_protocol::dynamic_tools::DynamicToolResponse {
            content_items: vec![
                codex_protocol::dynamic_tools::DynamicToolCallOutputContentItem::InputText {
                    text: receipt.result.clone(),
                },
            ],
            success: true,
        },
        &receipt.turn_id,
        &receipt.call_id,
    )
}

fn computer_result_matches(item: &ResponseItem, result: &str) -> bool {
    let ResponseItem::ComputerCallOutput {
        output,
        acknowledged_safety_checks,
        ..
    } = item
    else {
        return false;
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(result) else {
        return false;
    };
    value.get("output") == Some(output)
        && match value.get("acknowledged_safety_checks") {
            Some(serde_json::Value::Array(checks)) => checks == acknowledged_safety_checks,
            None => acknowledged_safety_checks.is_empty(),
            _ => false,
        }
}

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Outcome {
    Applied,
    NotApplied,
}

impl Outcome {
    fn tag(self) -> &'static str {
        match self {
            Self::Applied => APPLIED,
            Self::NotApplied => NOT_APPLIED,
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ReconciledCall {
    thread_id: ThreadId,
    turn_id: String,
    call_id: String,
    outcome: Outcome,
    result: String,
    #[serde(default)]
    shell: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RecoveryRequest {
    receipts: Vec<ReconciledCall>,
    histories: HashMap<ThreadId, DescendantHistory>,
    #[serde(default)]
    root_recovery_turns: Vec<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DescendantHistory {
    digest: String,
    parent: ThreadId,
    status: String,
    interrupted: Vec<String>,
}

pub(crate) fn encode_history(history: &[RolloutItem]) -> serde_json::Result<String> {
    // Some rollout payloads use hash maps. Compare canonical JSON across reads.
    let mut value = serde_json::to_value(history)?;
    value.sort_all_objects();
    serde_json::to_string(&value)
}

fn output(item: &ResponseItem) -> Option<(&str, &FunctionCallOutputPayload)> {
    match item {
        ResponseItem::FunctionCallOutput {
            call_id: Some(call_id),
            output,
            ..
        }
        | ResponseItem::CustomToolCallOutput {
            call_id, output, ..
        } => Some((call_id, output)),
        _ => None,
    }
}

fn tag(item: &ResponseItem) -> Option<&str> {
    let metadata = match item {
        ResponseItem::FunctionCallOutput {
            internal_chat_message_metadata_passthrough,
            ..
        }
        | ResponseItem::CustomToolCallOutput {
            internal_chat_message_metadata_passthrough,
            ..
        }
        | ResponseItem::ApplyPatchCallOutput {
            internal_chat_message_metadata_passthrough,
            ..
        }
        | ResponseItem::ComputerCallOutput {
            internal_chat_message_metadata_passthrough,
            ..
        }
        | ResponseItem::ShellCallOutput {
            internal_chat_message_metadata_passthrough,
            ..
        } => internal_chat_message_metadata_passthrough.as_ref()?,
        _ => return None,
    };
    let tags = metadata.content_item_kinds.as_deref()?;
    match tags {
        [ContentItemKind(tag)] if tag == APPLIED || tag == NOT_APPLIED => Some(tag),
        _ => None,
    }
}

fn matches_receipt(
    item: &ResponseItem,
    receipt: &ReconciledCall,
    typed: Option<&ResponseItem>,
) -> bool {
    let same_result = match (item, typed) {
        (
            ResponseItem::ApplyPatchCallOutput { status: actual_status, output: actual_output, .. },
            Some(ResponseItem::ApplyPatchCallOutput { status: expected_status, output: expected_output, .. }),
        ) => actual_status == expected_status && actual_output == expected_output,
        (
            ResponseItem::ShellCallOutput { provider_fields: actual, .. },
            Some(ResponseItem::ShellCallOutput { provider_fields: expected, .. }),
        ) => actual == expected, // Core assigns the stored item ID at insertion.
        _ => output(item).is_some_and(|(_, payload)| {
            matches!(&payload.body, FunctionCallOutputBody::Text(text) if text == &receipt.result)
        }) || computer_result_matches(item, &receipt.result),
    };
    same_result
        && item.turn_id() == Some(receipt.turn_id.as_str())
        && tag(item) == Some(receipt.outcome.tag())
        && output_identity(item).is_some_and(|(id, _)| id == receipt.call_id)
}

pub(crate) async fn executor_policies(state: &ThreadState) -> Result<Vec<Value>> {
    let manager = state.engine.manager.environment_manager();
    let local_network = if state
        .thread
        .thread_extension_data()
        .get::<crate::environment::StrictExecutorWrites>()
        .is_some()
    {
        let config = state.thread.config().await;
        crate::environment::local_session_network_policy(&config)
    } else {
        None
    };
    state
        .thread
        .environment_selections()
        .await
        .iter()
        .map(|selection| {
            let environment = manager
                .get_environment(&selection.environment_id)
                .context("Selected executor is unavailable")?;
            sdk_executor_policy_receipt(selection, environment.is_remote(), local_network.as_ref())
                .context("Selected executor policy is unavailable")
        })
        .collect()
}

fn implicit_remote_authority(receipt: &Value) -> Option<Map<String, Value>> {
    let mut authority = receipt.as_object()?.clone();
    if authority.get("transport")?.as_str()? != "remote" {
        return None;
    }
    let cwd: PathUri = serde_json::from_value(authority.remove("cwd")?).ok()?;
    let roots = std::slice::from_ref(&cwd);
    let profile = crate::environment::workspace_profile(roots);
    // Ignore the paths only when the entire recorded write policy came from cwd.
    if authority.remove("writable_roots")? != serde_json::to_value(roots).ok()?
        || authority.remove("permission_profile")? != serde_json::to_value(profile).ok()?
    {
        return None;
    }
    Some(authority)
}

fn can_drop_implicit_remote(receipt: &Value) -> bool {
    let Some(authority) = implicit_remote_authority(receipt) else {
        return false;
    };
    if authority.len() != 4
        || authority.get("environment_id").and_then(Value::as_str) != Some(REMOTE_ENVIRONMENT_ID)
        || authority.get("network_sandbox").and_then(Value::as_str) != Some("restricted")
    {
        return false;
    }
    match authority.get("network_policy") {
        Some(Value::Null) => true,
        Some(Value::Object(policy)) => {
            policy.len() == 4
                && policy.get("requires_proxy").and_then(Value::as_bool) == Some(true)
                && policy
                    .get("allow_all_unix_sockets")
                    .is_some_and(Value::is_boolean)
                && policy
                    .get("allow_local_binding")
                    .is_some_and(Value::is_boolean)
                && policy
                    .get("domains")
                    .and_then(Value::as_object)
                    .is_some_and(|domains| {
                        domains
                            .values()
                            .all(|permission| matches!(permission.as_str(), Some("allow" | "deny")))
                    })
        }
        _ => false,
    }
}

fn has_executor_receipt_markers(receipt: &Value) -> bool {
    ["codex_source_tree", "strict_executor_writes", "executors"]
        .into_iter()
        .any(|field| receipt.get(field).is_some())
}

fn validate_sdk_receipt_identities(history: &[RolloutItem], thread_id: &str) -> Result<()> {
    // Core writes sampling receipts into the producing thread's raw rollout.
    // Native forks and subagents may retain identified ancestor receipts.
    let own_root = matches!(history.first(), Some(RolloutItem::SessionMeta(header))
        if header.meta.id.to_string() == thread_id
            && header.meta.source.parent_thread_id().is_none()
            && header.meta.parent_thread_id.is_none()
            && header.meta.forked_from_id.is_none()
            && header.meta.history_base.is_none());
    for item in history {
        let RolloutItem::TurnContext(context) = item else {
            continue;
        };
        let Some(receipt) = context.sdk_receipt.as_ref() else {
            continue;
        };
        if has_executor_receipt_markers(receipt) {
            ensure!(
                receipt["thread_id"].as_str().is_some_and(|id| {
                    codex_protocol::ThreadId::from_string(id).is_ok()
                        && (!own_root || id == thread_id)
                }),
                "Local resume requires the recorded executor write and network policy"
            );
        }
    }
    Ok(())
}

fn has_sampled_model(history: &[RolloutItem]) -> bool {
    history.iter().any(|item| match item {
        RolloutItem::EventMsg(EventMsg::TurnComplete(turn)) => {
            turn.tool_completion.is_some()
                || turn.last_agent_message.is_some()
                || turn.time_to_first_token_ms.is_some()
        }
        RolloutItem::ResponseItem(response) => match &response.item {
            ResponseItem::Message { role, .. } => role == "assistant",
            ResponseItem::Reasoning { .. }
            | ResponseItem::LocalShellCall { .. }
            | ResponseItem::ShellCall { .. }
            | ResponseItem::FunctionCall { .. }
            | ResponseItem::CustomToolCall { .. }
            | ResponseItem::ComputerCall { .. }
            | ResponseItem::ApplyPatchCall { .. }
            | ResponseItem::ToolSearchCall { .. }
            | ResponseItem::WebSearchCall { .. }
            | ResponseItem::ImageGenerationCall { .. } => true,
            _ => false,
        },
        _ => false,
    })
}

fn has_legacy_executor_profiles(history: &[RolloutItem]) -> bool {
    let mut contexts = history
        .iter()
        .filter_map(|item| match item {
            RolloutItem::TurnContext(context) => Some(context),
            _ => None,
        })
        .peekable();
    contexts.peek().is_some()
        && contexts.all(|context| {
            context.network.is_none() && is_legacy_executor_profile(&context.permission_profile())
        })
}

fn is_legacy_executor_profile(profile: &PermissionProfile) -> bool {
    if *profile == PermissionProfile::read_only() {
        return true;
    }
    // The concrete write path is a PathUri and can belong to a different OS.
    profile
        .file_system_sandbox_policy()
        .entries
        .into_iter()
        .find(|entry| entry.access == FileSystemAccessMode::Write)
        .is_some_and(|entry| match entry.path {
            FileSystemPath::Path { path } => {
                *profile == crate::environment::workspace_profile(std::slice::from_ref(&path))
            }
            _ => false,
        })
}

async fn validate_recorded_executor_policy(
    state: &ThreadState,
    history: &[RolloutItem],
    thread_id: &str,
) -> Result<()> {
    validate_sdk_receipt_identities(history, thread_id)?;
    let mut recorded_policies = history.iter().rev().filter_map(|record| {
        let RolloutItem::TurnContext(context) = record else {
            return None;
        };
        let receipt = context.sdk_receipt.as_ref()?;
        (receipt["thread_id"].as_str() == Some(thread_id) && has_executor_receipt_markers(receipt))
            .then_some((
                receipt.get("executors"),
                receipt.get("strict_executor_writes"),
            ))
    });
    let policy_error = "Local resume requires the recorded executor write and network policy";
    let current_strict_writes = state
        .thread
        .thread_extension_data()
        .get::<crate::environment::StrictExecutorWrites>()
        .is_some();
    let Some((previous, strict_writes)) = recorded_policies.next() else {
        if has_sampled_model(history) {
            // Released SDKs wrote receipt-free model-only and implicit WSS histories.
            // Only the current owner's default policy can reopen these profiles.
            ensure!(!current_strict_writes, "{policy_error}");
            let current = executor_policies(state).await?;
            let implicit = match current.as_slice() {
                [] => true,
                [policy] => can_drop_implicit_remote(policy) && policy["network_policy"].is_null(),
                _ => false,
            };
            ensure!(
                implicit && has_legacy_executor_profiles(history),
                "{policy_error}"
            );
        }
        return Ok(());
    };
    let previous = previous.and_then(Value::as_array).context(policy_error)?;
    let strict_writes = strict_writes
        .map(|strict| strict.as_bool().context(policy_error))
        .transpose()?;
    let current = executor_policies(state).await?;
    if previous.is_empty() {
        // Only a genuinely model-only history can attach a new implicit remote
        // executor. An empty turn cannot replace a recorded strict policy.
        ensure!(
            strict_writes != Some(true)
                && !current_strict_writes
                && current.iter().all(can_drop_implicit_remote)
                && !recorded_policies.any(|(older, strict)| {
                    strict.is_some_and(|value| value.as_bool() != Some(false))
                        || (!current.is_empty()
                            && older
                                .and_then(Value::as_array)
                                .is_none_or(|values| !values.is_empty()))
                }),
            "{policy_error}"
        );
        return Ok(());
    }
    let matches = if strict_writes == Some(true) || current_strict_writes {
        // A strict reopen requires durable evidence of the recorded strict mode.
        strict_writes == Some(current_strict_writes) && previous == &current
    } else if current.is_empty() {
        // Opening without an Environment leaves a model-only Session.
        previous.iter().all(can_drop_implicit_remote)
    } else {
        // Like portable restore, implicit remote workspaces can move.
        previous.len() == current.len()
            && previous.iter().zip(&current).all(|(old, new)| {
                old == new
                    || implicit_remote_authority(old)
                        .is_some_and(|old| Some(old) == implicit_remote_authority(new))
            })
    };
    ensure!(matches, "{policy_error}");
    Ok(())
}

pub(crate) async fn reconcile(state: &ThreadState, encoded: &str) -> Result<()> {
    let request: RecoveryRequest = serde_json::from_str(encoded)
        .map_err(|_| anyhow::anyhow!("Invalid local reconciliation receipt"))?;
    let tools = state
        .thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>()
        .context("Reconciliation requires an SDK-owned Session")?;
    let store = state
        .engine
        .local_checklist_store
        .as_ref()
        .context("Reconciliation requires a local storage writer")?;
    let _mutation = state.mutations.lock().await;
    tools.check_idle(&state.thread).await?;
    let history = store.load_owned_history(state.id).await?;
    let thread_id = state.id.to_string();
    validate_recorded_executor_policy(state, &history, &thread_id).await?;
    let guard = state
        .thread
        .thread_extension_data()
        .get::<crate::recovery_guard::Guard>()
        .context("Reconciliation requires the SDK replay guard")?;

    let mut receipts_by_thread = HashMap::<_, Vec<_>>::new();
    for receipt in &request.receipts {
        ensure!(
            receipt.thread_id == state.id || request.histories.contains_key(&receipt.thread_id),
            "Reconciliation receipt belongs to a different thread"
        );
        receipts_by_thread
            .entry(receipt.thread_id)
            .or_default()
            .push(receipt);
    }
    let root_receipts = receipts_by_thread.remove(&state.id).unwrap_or_default();
    let (root_append, protected) = plan(&history, &root_receipts)?;
    validate_interrupted_effects(
        &history,
        &request.root_recovery_turns,
        &root_append,
        &root_receipts,
    )?;

    // The root writer fences the original owner. Also reserve each child writer
    // so an independently reopened child cannot race recovery or change topology.
    verify_descendants(state, &request.histories).await?;
    let _child_writers = store.reserve_reconciliation_writers(request.histories.keys().copied())?;
    if !_child_writers.is_empty() {
        verify_descendants(state, &request.histories).await?;
    }
    let mut child_appends = Vec::new();
    let mut protected_children = HashMap::new();
    for (&id, expected) in &request.histories {
        let (path, history) = store.reconciliation_history(id).await?;
        ensure!(
            format!("{:x}", Sha256::digest(encode_history(&history)?)) == expected.digest,
            "Native descendant history changed during local recovery"
        );
        let receipts = receipts_by_thread.remove(&id).unwrap_or_default();
        let (outputs, protected) = plan(&history, &receipts)?;
        if !protected.is_empty() {
            protected_children.insert(id.to_string(), protected);
        }
        let writes_receipts = !outputs.is_empty();
        let prior_interruption = history.iter().any(|row| {
            matches!(row,
            RolloutItem::EventMsg(EventMsg::TurnAborted(event))
                if event.reason == TurnAbortReason::Interrupted && event.turn_id.is_some()
                    && event.model_attempt.is_none() && event.error.is_none()
                    && event.started_at.is_none() && event.completed_at.is_none() && event.duration_ms.is_none())
        });
        if writes_receipts || !expected.interrupted.is_empty() {
            validate_interrupted_effects(&history, &expected.interrupted, &outputs, &receipts)?;
            let mut items: Vec<_> = expected
                .interrupted
                .iter()
                .map(|turn| interruption(turn.clone()))
                .collect();
            items.extend(
                outputs
                    .into_iter()
                    .map(|item| RolloutItem::ResponseItem(ResponseItemEnvelope::new(item))),
            );
            child_appends.push((writes_receipts, id, path, items, receipts));
        } else if prior_interruption {
            // A previous replace can be visible before its directory sync
            // completed. Reconfirm it even when no new abort is necessary.
            child_appends.push((false, id, path, Vec::new(), receipts));
        }
    }
    // Validate every supplied result before recording any part of the batch.
    // Publish interrupted ancestors before receipts that could clear the last
    // unresolved call and allow the next open without confirmations.
    let mut active_root_turns = HashSet::new();
    for row in &history {
        match row {
            RolloutItem::EventMsg(EventMsg::TurnStarted(event)) => {
                active_root_turns.insert(event.turn_id.as_str());
            }
            RolloutItem::EventMsg(EventMsg::TurnComplete(event)) => {
                active_root_turns.remove(event.turn_id.as_str());
            }
            RolloutItem::EventMsg(EventMsg::TurnAborted(event)) => {
                if let Some(turn) = event.turn_id.as_deref() {
                    active_root_turns.remove(turn);
                }
            }
            _ => {}
        }
    }
    let root_interruptions: Vec<_> = request
        .root_recovery_turns
        .iter()
        .filter(|turn| active_root_turns.contains(turn.as_str()))
        .map(|turn| interruption(turn.clone()))
        .collect();
    if !root_interruptions.is_empty() {
        state
            .thread
            .append_rollout_items(&root_interruptions)
            .await?;
        state.thread.flush_rollout().await?;
    }
    child_appends.sort_by_key(|(writes_receipts, ..)| *writes_receipts);
    for (_, id, path, items, receipts) in child_appends {
        confirm_durable(
            &store.append_reconciliation(id, &path, items).await?,
            &receipts,
        )?;
    }
    if !root_append.is_empty() {
        state.thread.inject_response_items(root_append).await?;
        state.thread.flush_rollout().await?;
        confirm_durable(&store.load_owned_history(state.id).await?, &root_receipts)?;
    }
    guard.set_reconciled(protected);
    state
        .engine
        .recovery
        .set_reconciled_descendants(protected_children);
    Ok(())
}

fn interruption(turn: String) -> RolloutItem {
    RolloutItem::EventMsg(EventMsg::TurnAborted(TurnAbortedEvent {
        model_attempt: None,
        turn_id: Some(turn),
        reason: TurnAbortReason::Interrupted,
        error: None,
        started_at: None,
        completed_at: None,
        duration_ms: None,
    }))
}

// Aborting a cold turn changes its lifecycle, never the outcome of its tools.
// Check original retained invocations, including native tools omitted by the
// application reconciliation projection, before publishing any interruption.
fn validate_interrupted_effects(
    history: &[RolloutItem],
    turns: &[String],
    reconciled: &[ResponseItem],
    receipts: &[&ReconciledCall],
) -> Result<()> {
    if turns.is_empty() {
        return Ok(());
    }
    let mut context = None;
    let mut calls = HashMap::new();
    let mut outputs = HashMap::new();
    let mut terminal_commands = HashSet::new();
    for row in history {
        match row {
            RolloutItem::TurnContext(turn) => context = turn.turn_id.as_deref().or(context),
            RolloutItem::EventMsg(EventMsg::TurnStarted(turn)) => context = Some(&turn.turn_id),
            RolloutItem::EventMsg(EventMsg::ItemCompleted(event)) => {
                if let codex_protocol::items::TurnItem::CommandExecution(command) = &event.item
                    && command.status != codex_protocol::items::CommandExecutionStatus::InProgress
                {
                    terminal_commands.insert((event.turn_id.as_str(), command.id.as_str()));
                }
            }
            RolloutItem::ResponseItem(envelope) => {
                let item = &envelope.item;
                let Some(turn) = item.turn_id().or(context) else {
                    continue;
                };
                if !turns.iter().any(|id| id == turn) {
                    continue;
                }
                let call = match item {
                    ResponseItem::FunctionCall { call_id, .. }
                    | ResponseItem::CustomToolCall { call_id, .. }
                    | ResponseItem::ComputerCall { call_id, .. }
                    | ResponseItem::ApplyPatchCall { call_id, .. } => Some(call_id.as_str()),
                    ResponseItem::LocalShellCall { call_id, .. } => Some(
                        call_id
                            .as_deref()
                            .context("Native shell invocation lacks identity")?,
                    ),
                    ResponseItem::ShellCall {
                        provider_fields, ..
                    } => Some(
                        shell_call_id(provider_fields)
                            .context("Native shell invocation lacks identity")?
                            .as_str(),
                    ),
                    ResponseItem::McpApprovalRequest { id, .. } => {
                        Some(id.as_ref().context("MCP approval lacks identity")?.as_str())
                    }
                    _ => None,
                };
                if let Some(call) = call {
                    ensure!(
                        calls.insert((turn, call), item).is_none(),
                        "Ambiguous tool identity in interrupted history"
                    );
                } else if let Some((call, _)) = output_identity(item) {
                    outputs.insert((turn, call), item);
                }
            }
            _ => {}
        }
    }
    for item in reconciled {
        if let Some((call, Some(turn))) = output_identity(item) {
            outputs.insert((turn, call), item);
        }
    }
    for (key, call) in calls {
        ensure!(outputs.get(&key).is_some_and(|output| {
            output_matches_invocation(call, output)
                || matches!((call, *output), (ResponseItem::LocalShellCall { call_id: Some(expected), .. }, ResponseItem::FunctionCallOutput { call_id: Some(actual), .. }) if expected == actual)
        }), "Native local history contains unknown tool effects; interruption requires a retained result");
        if matches!(call, ResponseItem::FunctionCall { name, .. } if matches!(name.as_str(), "exec_command" | "write_stdin" | "shell" | "shell_command"))
        {
            // A tool result can acknowledge a still-running command process.
            // Cold recovery cannot establish that process's terminal state.
            ensure!(
                terminal_commands.contains(&key)
                    || receipts
                        .iter()
                        .any(|receipt| receipt.turn_id == key.0 && receipt.call_id == key.1),
                "Native local history has unproven command process lifetime; interruption is unsafe"
            );
        }
    }
    Ok(())
}

async fn verify_descendants(
    state: &ThreadState,
    expected: &HashMap<ThreadId, DescendantHistory>,
) -> Result<()> {
    state.engine.agent_graph.hydrate(state.id).await?;
    let current = crate::subagents::list(state).await?;
    ensure!(
        current.len() == expected.len()
            && current.iter().all(|child| {
                expected.get(&child.id).is_some_and(|snapshot| {
                    !child.runtime_loaded
                        && child.parent_agent_id == snapshot.parent
                        && child.status == snapshot.status
                })
            }),
        "Native descendant ownership changed during local recovery"
    );
    Ok(())
}

fn confirm_durable(history: &[RolloutItem], receipts: &[&ReconciledCall]) -> Result<()> {
    let (missing, _) = plan(history, receipts)?;
    ensure!(
        missing.is_empty(),
        "Reconciliation result was not confirmed in native storage"
    );
    Ok(())
}

fn plan(
    history: &[RolloutItem],
    receipts: &[&ReconciledCall],
) -> Result<(Vec<ResponseItem>, HashSet<String>)> {
    let mut seen = HashMap::new();
    let mut outputs = HashMap::new();
    let mut protected = HashSet::new();
    let mut context_turn = None;
    for record in history {
        match record {
            RolloutItem::TurnContext(context) => {
                context_turn = context.turn_id.as_deref().or(context_turn);
            }
            RolloutItem::ResponseItem(envelope) => {
                let item = &envelope.item;
                let Some(turn) = item.turn_id().or(context_turn) else {
                    continue;
                };
                match item {
                    ResponseItem::FunctionCall { call_id, .. }
                    | ResponseItem::CustomToolCall { call_id, .. }
                    | ResponseItem::ComputerCall { call_id, .. }
                    | ResponseItem::ApplyPatchCall { call_id, .. } => {
                        if matches!(item, ResponseItem::ApplyPatchCall { .. })
                            && !owns_application_patch(envelope)
                        {
                            continue;
                        }
                        if matches!(
                            item,
                            ResponseItem::ComputerCall { .. } | ResponseItem::ApplyPatchCall { .. }
                        ) && seen.contains_key(&(turn, call_id.as_str()))
                        {
                            seen.insert((turn, call_id.as_str()), None);
                            continue;
                        }
                        let entry = seen.entry((turn, call_id.as_str())).or_insert(Some(item));
                        if *entry != Some(item) {
                            *entry = None;
                        }
                    }
                    ResponseItem::ShellCall {
                        provider_fields, ..
                    } if envelope
                        .metadata
                        .as_ref()
                        .and_then(|metadata| metadata.sdk_application_tool.as_deref())
                        == Some("shell") =>
                    {
                        if let Some(call_id) = shell_call_id(provider_fields) {
                            seen.entry((turn, call_id.as_str()))
                                .and_modify(|invocation| *invocation = None)
                                .or_insert(Some(item));
                        }
                    }
                    _ => {
                        if let Some((call_id, _)) = output_identity(item) {
                            let entry = outputs.entry((turn, call_id)).or_insert(Some(item));
                            if *entry != Some(item) {
                                *entry = None;
                            }
                            if tag(item).is_some() {
                                protected.insert(call_id.to_owned());
                            }
                        }
                    }
                }
            }
            _ => {}
        }
    }
    let mut requested = HashSet::new();
    let mut append = Vec::new();
    for receipt in receipts {
        let key = (receipt.turn_id.as_str(), receipt.call_id.as_str());
        ensure!(requested.insert(key), "Reconciliation receipt is duplicate");
        let call = seen
            .get(&key)
            .copied()
            .flatten()
            .context("Reconciliation does not identify one retained invocation")?;
        ensure!(
            receipt.shell == matches!(call, ResponseItem::ShellCall { .. }),
            "Application Shell reconciliation requires a verified ShellOutput"
        );
        let mut typed = match call {
            ResponseItem::ShellCall { .. } => Some(shell_reconciliation(call, receipt)?),
            ResponseItem::ComputerCall { .. } => Some(computer_reconciliation(
                call,
                &receipt.result,
                &receipt.turn_id,
            )?),
            ResponseItem::ApplyPatchCall { .. } => {
                let result: codex_protocol::models::ApplyPatchResult =
                    serde_json::from_str(&receipt.result)
                        .context("Application patch reconciliation requires a typed result")?;
                Some(application_patch_receipt(call, codex_protocol::dynamic_tools::DynamicToolResponse {
                    content_items: vec![codex_protocol::dynamic_tools::DynamicToolCallOutputContentItem::InputText { text: receipt.result.clone() }],
                    success: result.status == codex_protocol::models::ApplyPatchStatus::Completed,
                }, &receipt.turn_id)?)
            }
            _ => None,
        };
        if let Some(item) = typed.as_mut() {
            let metadata = match item {
                ResponseItem::ComputerCallOutput {
                    internal_chat_message_metadata_passthrough,
                    ..
                }
                | ResponseItem::ApplyPatchCallOutput {
                    internal_chat_message_metadata_passthrough,
                    ..
                }
                | ResponseItem::ShellCallOutput {
                    internal_chat_message_metadata_passthrough,
                    ..
                } => internal_chat_message_metadata_passthrough,
                _ => unreachable!("typed reconciliation only creates native application output"),
            };
            *metadata = Some(InternalChatMessageMetadataPassthrough {
                turn_id: Some(receipt.turn_id.clone()),
                content_item_kinds: Some(vec![ContentItemKind(receipt.outcome.tag().to_owned())]),
                ..Default::default()
            });
        }
        if let Some(existing) = outputs.get(&key) {
            ensure!(
                existing.is_some_and(|existing| output_matches_invocation(call, existing)
                    && matches_receipt(existing, receipt, typed.as_ref())),
                "Reconciliation conflicts with a retained result"
            );
            continue;
        }
        if let Some(item) = typed {
            append.push(item);
            protected.insert(receipt.call_id.clone());
            continue;
        }
        let output = FunctionCallOutputPayload {
            body: FunctionCallOutputBody::Text(receipt.result.clone()),
            success: Some(matches!(receipt.outcome, Outcome::Applied)),
        };
        let metadata = Some(InternalChatMessageMetadataPassthrough {
            turn_id: Some(receipt.turn_id.clone()),
            content_item_kinds: Some(vec![ContentItemKind(receipt.outcome.tag().to_owned())]),
            ..Default::default()
        });
        append.push(match call {
            ResponseItem::FunctionCall {
                name, namespace, ..
            } => ResponseItem::FunctionCallOutput {
                id: None,
                call_id: Some(receipt.call_id.clone()),
                name: Some(name.clone()),
                namespace: namespace.clone(),
                output,
                internal_chat_message_metadata_passthrough: metadata,
            },
            ResponseItem::CustomToolCall { name, .. } => ResponseItem::CustomToolCallOutput {
                id: None,
                call_id: receipt.call_id.clone(),
                name: Some(name.clone()),
                output,
                internal_chat_message_metadata_passthrough: metadata,
            },
            _ => unreachable!("the call index retains only supported invocations"),
        });
        protected.insert(receipt.call_id.clone());
    }
    Ok((append, protected))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::environment::workspace_profile;

    #[test]
    fn cold_interruption_preserves_unknown_native_effects() {
        let start = || {
            RolloutItem::EventMsg(
                serde_json::from_value(serde_json::json!({
                    "type": "task_started", "turn_id": "original", "root_turn_id": "original"
                }))
                .unwrap(),
            )
        };
        let turns = vec!["original".to_owned()];
        assert!(validate_interrupted_effects(&[start()], &turns, &[], &[]).is_ok());
        for invocation in [
            serde_json::json!({"type":"function_call", "name":"exec_command", "call_id":"effect", "arguments":"{}"}),
            serde_json::json!({"type":"shell_call", "call_id":"effect", "action":{"commands":["synthetic"], "timeout_ms":1000, "max_output_length":1000}}),
            serde_json::json!({"type":"apply_patch_call", "call_id":"effect", "operation":{"type":"delete_file", "path":"synthetic.txt"}}),
        ] {
            let call: ResponseItem = serde_json::from_value(invocation).unwrap();
            let history = vec![
                start(),
                RolloutItem::ResponseItem(ResponseItemEnvelope::new(call)),
            ];
            assert!(validate_interrupted_effects(&history, &turns, &[], &[]).is_err());
        }
    }

    #[test]
    fn cold_interruption_requires_the_original_command_to_be_terminal() {
        let start = RolloutItem::EventMsg(
            serde_json::from_value(serde_json::json!({
                "type":"task_started", "turn_id":"original", "root_turn_id":"original"
            }))
            .unwrap(),
        );
        let row = |value| {
            RolloutItem::ResponseItem(ResponseItemEnvelope::new(
                serde_json::from_value(value).unwrap(),
            ))
        };
        let mut history = vec![
            start,
            row(
                serde_json::json!({"type":"function_call", "name":"exec_command", "call_id":"effect", "arguments":"{}"}),
            ),
            row(
                serde_json::json!({"type":"function_call_output", "call_id":"effect", "output":"Process running with session ID 123"}),
            ),
        ];
        let turns = vec!["original".to_owned()];
        assert!(validate_interrupted_effects(&history, &turns, &[], &[]).is_err());
        let terminal = |call, turn| {
            RolloutItem::EventMsg(serde_json::from_value(serde_json::json!({
            "type":"item_completed", "thread_id":ThreadId::new(), "turn_id":turn,
            "item":{"type":"CommandExecution", "id":call, "command":["synthetic"], "cwd":"file:///synthetic", "parsed_cmd":[], "source":"agent", "status":"completed", "exit_code":0}
        })).unwrap())
        };
        history.push(terminal("unrelated", "original"));
        history.push(terminal("effect", "unrelated"));
        assert!(validate_interrupted_effects(&history, &turns, &[], &[]).is_err());
        history.push(terminal("effect", "original"));
        assert!(validate_interrupted_effects(&history, &turns, &[], &[]).is_ok());
    }

    #[test]
    fn durable_resume_accepts_identified_fork_receipt() {
        use codex_protocol::protocol::{
            AskForApproval, SandboxPolicy, SessionMeta, SessionMetaLine,
        };
        use serde_json::json;

        let id = codex_protocol::ThreadId::new();
        let parent = codex_protocol::ThreadId::new();
        let fork = RolloutItem::SessionMeta(SessionMetaLine {
            meta: SessionMeta {
                id,
                forked_from_id: Some(parent),
                ..Default::default()
            },
            git: None,
        });
        let inherited = serde_json::from_value::<RolloutItem>(json!({
            "type": "turn_context",
            "payload": {
                "sdk_receipt": { "codex_source_tree": "fixture", "thread_id": parent },
                "cwd": std::env::current_dir().unwrap(),
                "approval_policy": AskForApproval::Never,
                "sandbox_policy": SandboxPolicy::new_read_only_policy(),
                "model": "synthetic-model",
                "summary": "auto",
            },
        }))
        .unwrap();
        assert!(validate_sdk_receipt_identities(&[fork, inherited], &id.to_string()).is_ok());
    }

    #[test]
    fn legacy_executor_profile_supports_a_windows_wss_workspace() {
        let cwd: PathUri = "file:///C:/workspace".parse().unwrap();
        let extra: PathUri = "file:///C:/extra".parse().unwrap();
        assert!(is_legacy_executor_profile(&workspace_profile(
            std::slice::from_ref(&cwd)
        )));
        assert!(!is_legacy_executor_profile(&workspace_profile(&[
            cwd, extra
        ])));
    }
}

#[cfg(test)]
mod computer_recovery_tests {
    use super::*;
    use serde_json::json;

    fn invocation() -> ResponseItem {
        serde_json::from_value(json!({
            "type": "computer_call", "call_id": "computer-1",
            "actions": [{"type": "click", "x": 1, "y": 2}],
            "pending_safety_checks": [{"id": "check-1", "code": "confirm", "message": "Confirm the action"}],
            "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"}
        })).unwrap()
    }

    fn screenshot() -> String {
        json!({
            "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64,AA=="},
            "acknowledged_safety_checks": [{"id": "check-1", "code": "confirm", "message": "Confirm the action"}]
        }).to_string()
    }

    #[test]
    fn computer_receipt_preserves_native_kind_and_exact_turn_call_identity() {
        let call = invocation();
        let result = computer_reconciliation(&call, &screenshot(), "turn-1").unwrap();
        assert_eq!(
            output_identity(&result),
            Some(("computer-1", Some("turn-1")))
        );
        assert!(output_matches_invocation(&call, &result));
        assert!(
            matches!(&result, ResponseItem::ComputerCallOutput { acknowledged_safety_checks, .. } if acknowledged_safety_checks.len() == 1)
        );
        let function: ResponseItem = serde_json::from_value(json!({
            "type": "function_call_output", "call_id": "computer-1", "output": screenshot()
        }))
        .unwrap();
        assert!(!output_matches_invocation(&call, &function));
        assert!(!computer_result_matches(&function, &screenshot()));
    }

    #[test]
    fn computer_reconciliation_requires_real_screenshot_and_complete_original_checks() {
        let call = invocation();
        for invalid in [
            "verified absent".to_owned(),
            json!({"output":{"type":"computer_screenshot","image_url":"data:image/png;base64,AA=="}}).to_string(),
            json!({"output":{"type":"computer_screenshot","image_url":"data:image/png;base64,AA=="},"acknowledged_safety_checks":[{"id":"check-1"}]}).to_string(),
        ] {
            assert!(computer_reconciliation(&call, &invalid, "turn-1").is_err());
        }
        assert!(computer_reconciliation(&call, &screenshot(), "turn-1").is_ok());
    }

    #[test]
    fn failed_or_invalid_computer_result_cannot_leak_payload_in_receipt_errors() {
        let call = invocation();
        let secret = "SYNTHETIC_CALLBACK_SECRET";
        let response = codex_protocol::dynamic_tools::DynamicToolResponse {
            content_items: vec![
                codex_protocol::dynamic_tools::DynamicToolCallOutputContentItem::InputText {
                    text: secret.to_owned(),
                },
            ],
            success: false,
        };
        let error = computer_receipt(&call, response, "turn-1").unwrap_err();
        assert!(!format!("{error:?}").contains(secret));
    }
    #[test]
    fn computer_reconciliation_receipts_preserve_outcome_and_structured_result() {
        let mut item = computer_reconciliation(&invocation(), &screenshot(), "turn-1").unwrap();
        if let ResponseItem::ComputerCallOutput {
            internal_chat_message_metadata_passthrough: Some(meta),
            ..
        } = &mut item
        {
            meta.content_item_kinds = Some(vec![ContentItemKind(NOT_APPLIED.into())]);
        }
        let receipt = ReconciledCall {
            thread_id: ThreadId::new(),
            turn_id: "turn-1".into(),
            call_id: "computer-1".into(),
            outcome: Outcome::NotApplied,
            result: screenshot(),
            shell: false,
        };
        assert!(matches_receipt(&item, &receipt, None));
        let changed = ReconciledCall {
            outcome: Outcome::Applied,
            ..receipt
        };
        assert!(!matches_receipt(&item, &changed, None));
    }
}
