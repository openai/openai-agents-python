//! Read and validate applied executor policies for restore.

use anyhow::{Context, Result, ensure};
use codex_exec_server::REMOTE_ENVIRONMENT_ID;
use codex_history::RolloutItem;
use codex_protocol::models::{PermissionProfile, ResponseItem};
use codex_protocol::permissions::{FileSystemAccessMode, FileSystemPath};
use codex_protocol::protocol::{EventMsg, sdk_executor_policy_receipt};
use codex_utils_path_uri::PathUri;
use serde_json::{Map, Value};

use crate::engine::ThreadState;

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
            turn.last_agent_message.is_some() || turn.time_to_first_token_ms.is_some()
        }
        RolloutItem::ResponseItem(response) => match &response.item {
            ResponseItem::Message { role, .. } => role == "assistant",
            ResponseItem::Reasoning { .. }
            | ResponseItem::LocalShellCall { .. }
            | ResponseItem::FunctionCall { .. }
            | ResponseItem::CustomToolCall { .. }
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

pub(crate) async fn validate_executor_policy(state: &ThreadState) -> Result<()> {
    let tools = state
        .thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>()
        .context("Local resume requires an SDK-owned Session")?;
    let store = state
        .engine
        .local_checklist_store
        .as_ref()
        .context("Local resume requires a local storage writer")?;
    let _mutation = state.mutations.lock().await;
    tools.check_idle(&state.thread).await?;
    let history = store.load_owned_history(state.id).await?;
    let thread_id = state.id.to_string();
    validate_sdk_receipt_identities(&history, &thread_id)?;
    let mut recorded_policies = history.iter().rev().filter_map(|record| {
        let RolloutItem::TurnContext(context) = record else {
            return None;
        };
        let receipt = context.sdk_receipt.as_ref()?;
        (receipt["thread_id"].as_str() == Some(thread_id.as_str())
            && has_executor_receipt_markers(receipt))
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
        if has_sampled_model(&history) {
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
                implicit && has_legacy_executor_profiles(&history),
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
        // Legacy receipts lacked the mode and still require an exact profile.
        strict_writes.is_none_or(|strict| strict == current_strict_writes) && previous == &current
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
#[cfg(test)]
mod tests {
    use super::*;
    use crate::environment::workspace_profile;

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
