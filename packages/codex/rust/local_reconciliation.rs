//! Import application-confirmed outcomes for original local calls without replay.

use std::collections::{HashMap, HashSet};

use anyhow::{Context, Result, ensure};
use codex_history::RolloutItem;
use codex_protocol::models::{
    ContentItemKind, FunctionCallOutputBody, FunctionCallOutputPayload,
    InternalChatMessageMetadataPassthrough, ResponseItem,
};
use codex_protocol::protocol::sdk_executor_policy_receipt;
use serde::Deserialize;
use serde_json::Value;

use crate::engine::ThreadState;

const APPLIED: &str = "sdk_reconciliation.applied";
const NOT_APPLIED: &str = "sdk_reconciliation.not_applied";

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
    thread_id: String,
    turn_id: String,
    call_id: String,
    outcome: Outcome,
    result: String,
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
        } => internal_chat_message_metadata_passthrough.as_ref()?,
        _ => return None,
    };
    let tags = metadata.content_item_kinds.as_deref()?;
    match tags {
        [ContentItemKind(tag)] if tag == APPLIED || tag == NOT_APPLIED => Some(tag),
        _ => None,
    }
}

fn matches_receipt(item: &ResponseItem, receipt: &ReconciledCall) -> bool {
    item.turn_id() == Some(receipt.turn_id.as_str())
        && tag(item) == Some(receipt.outcome.tag())
        && output(item).is_some_and(|(id, payload)| {
            id == receipt.call_id
                && matches!(&payload.body, FunctionCallOutputBody::Text(text) if text == &receipt.result)
        })
}

pub(crate) async fn executor_policies(state: &ThreadState) -> Result<Vec<Value>> {
    let manager = state.engine.manager.environment_manager();
    state
        .thread
        .environment_selections()
        .await
        .iter()
        .map(|selection| {
            let environment = manager
                .get_environment(&selection.environment_id)
                .context("Selected executor is unavailable")?;
            sdk_executor_policy_receipt(selection, environment.is_remote())
                .context("Selected executor policy is unavailable")
        })
        .collect()
}

pub(crate) async fn reconcile(state: &ThreadState, encoded: &str) -> Result<()> {
    let receipts: Vec<ReconciledCall> = serde_json::from_str(encoded)
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
    if let Some(previous) = history.iter().rev().find_map(|record| {
        let RolloutItem::TurnContext(context) = record else {
            return None;
        };
        let receipt = context.sdk_receipt.as_ref()?;
        (receipt["thread_id"].as_str() == Some(thread_id.as_str()))
            .then(|| receipt["executors"].as_array())
            .flatten()
    }) {
        ensure!(
            previous == &executor_policies(state).await?,
            "Local resume requires the recorded executor write and network policy"
        );
    }
    let guard = state
        .thread
        .thread_extension_data()
        .get::<crate::recovery_guard::Guard>()
        .context("Reconciliation requires the SDK replay guard")?;

    let mut seen = HashMap::new();
    let mut outputs = HashMap::new();
    let mut protected = HashSet::new();
    let mut context_turn = None;
    for record in &history {
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
                    | ResponseItem::CustomToolCall { call_id, .. } => {
                        let entry = seen.entry((turn, call_id.as_str())).or_insert(Some(item));
                        if *entry != Some(item) {
                            *entry = None;
                        }
                    }
                    _ => {
                        if let Some((call_id, _)) = output(item) {
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
    for receipt in &receipts {
        let key = (receipt.turn_id.as_str(), receipt.call_id.as_str());
        ensure!(
            receipt.thread_id == thread_id && requested.insert(key),
            "Reconciliation receipt is duplicate or belongs to a different thread"
        );
        let call = seen
            .get(&key)
            .copied()
            .flatten()
            .context("Reconciliation does not identify one retained invocation")?;
        if let Some(existing) = outputs.get(&key) {
            ensure!(
                existing.is_some_and(|existing| matches_receipt(existing, receipt)),
                "Reconciliation conflicts with a retained result"
            );
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
    if !append.is_empty() {
        state.thread.inject_response_items(append).await?;
        state.thread.flush_rollout().await?;
        let durable = store.load_owned_history(state.id).await?;
        for receipt in &receipts {
            ensure!(
                durable.iter().any(|record| matches!(
                    record,
                    RolloutItem::ResponseItem(envelope) if matches_receipt(&envelope.item, receipt)
                )),
                "Reconciliation result was not confirmed in native storage"
            );
        }
    }
    guard.set_reconciled(protected);
    Ok(())
}
