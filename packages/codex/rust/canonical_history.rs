//! Core-owned canonical history transactions; no host-language replay state.
use crate::engine::ThreadState;
use anyhow::{Context, ensure};
use codex_history::RolloutItem;
use codex_protocol::models::ResponseItem;
use codex_protocol::protocol::AgentStatus;
use serde_json::{Value, json};
use std::collections::HashSet;

async fn idle(state: &ThreadState) -> anyhow::Result<()> {
    ensure!(
        state
            .thread
            .active_turn_environment_selections()
            .await
            .is_none()
            && !matches!(
                state.thread.agent_status().await,
                AgentStatus::Running | AgentStatus::Shutdown | AgentStatus::NotFound
            ),
        "canonical history requires an idle, open thread"
    );
    if let Some(tools) = state
        .thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>()
    {
        tools.check_idle(&state.thread).await?;
    }
    Ok(())
}

pub async fn snapshot(
    state: &ThreadState,
    limit: Option<usize>,
    for_replay: bool,
) -> anyhow::Result<Value> {
    let _operation = state.engine.operation(false)?;
    let _mutation = state
        .mutations
        .try_lock()
        .context("native mutation is in progress")?;
    idle(&state).await?;
    let (revision, total, items) = if for_replay {
        state.thread.sdk_replay_history_snapshot().await?
    } else {
        state.thread.sdk_history_snapshot(limit).await?
    };
    Ok(json!({"revision": revision, "total": total, "items": items}))
}

pub async fn replace(
    state: &ThreadState,
    expected: String,
    keep: usize,
    items: Vec<String>,
) -> anyhow::Result<Value> {
    let _operation = state
        .engine
        .context_gate
        .clone()
        .try_write_owned()
        .context("native operations are in progress")?;
    ensure!(
        !state
            .engine
            .closed
            .load(std::sync::atomic::Ordering::Acquire)
            && !state
                .engine
                .context_retired
                .load(std::sync::atomic::Ordering::Acquire),
        "Codex engine is closed"
    );
    let _mutation = state
        .mutations
        .try_lock()
        .context("native mutation is in progress")?;
    idle(&state).await?;
    ensure!(
        state.engine.manager.list_thread_ids().await == vec![state.id],
        "history transactions require an exclusively owned root without native children"
    );
    ensure!(
        crate::subagents::list(&state).await?.is_empty(),
        "history transactions do not rewrite child graphs"
    );
    ensure!(
        state.thread.list_background_terminals().await.is_empty(),
        "history transactions require completed background execution"
    );
    ensure!(
        state.engine.goals.is_none(),
        "history transactions do not rewrite goal-backed sessions"
    );
    // The Core revision is rechecked under its state lock when replacing below.
    // Validate before durability: neither retained nor appended history may split
    // a client tool call/result pair. Provider-hosted records keep their existing
    // context-validator treatment, including server-side tool-search output.
    let (current_revision, total, _) = state.thread.sdk_history_snapshot(Some(0)).await?;
    ensure!(
        current_revision == expected,
        "canonical history revision conflict"
    );
    ensure!(keep <= total, "history keep exceeds snapshot length");
    // Eligibility is fixed at admission. Never reinterpret old audit-only JSON.
    let projected = state.thread.sdk_history_projection(None).await?;
    let retained = state.thread.sdk_history_projection(Some(keep)).await?;
    crate::context::check_pairs(&projected, &(0..retained.len()).collect::<Vec<_>>())
        .context("canonical history requires complete tool call/result pairs")?;
    let appended: Vec<ResponseItem> = items
        .iter()
        .map(|item| codex_history::sdk_originals::input_projection(item))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| anyhow::anyhow!("Invalid canonical history item"))?
        .into_iter()
        .flatten()
        .collect();
    crate::context::check_pairs(&appended, &(0..appended.len()).collect::<Vec<_>>())
        .context("canonical history requires complete tool call/result pairs")?;
    match state
        .thread
        .sdk_replace_history(&expected, keep, items)
        .await
    {
        Ok(revision) => Ok(json!({"revision": revision})),
        Err(error)
            if matches!(
                error.details(),
                codex_protocol::error::CodexErrorDetails::Fatal(_)
            ) =>
        {
            // Core entered persistence but cannot prove the final durable/live outcome.
            // Fence admission before releasing the exclusive mutation permit. Cleanup
            // remains retryable through the existing context retirement mechanism.
            state
                .engine
                .context_retired
                .store(true, std::sync::atomic::Ordering::Release);
            Ok(json!({"outcome": "uncertain"}))
        }
        Err(error) => Err(error.into()),
    }
}

const MEMORY_INPUT_ADMITTED: &str = "sandbox_memory_input_admitted";

fn sampling_receipt(context: &codex_protocol::protocol::TurnContextItem) -> bool {
    context.sdk_receipt.as_ref().is_some_and(|receipt| {
        receipt.get("version").and_then(Value::as_u64) == Some(1)
            && receipt
                .get("thread_id")
                .and_then(Value::as_str)
                .is_some_and(|id| !id.is_empty())
            && receipt.get("model").is_some_and(Value::is_string)
            && receipt.get("approval_policy").is_some()
    })
}

/// Only memory eligibility changes; the exact sampling context remains intact.
pub async fn record_memory_input_admission(
    state: &ThreadState,
    turn_id: &str,
) -> anyhow::Result<()> {
    let _operation = state.engine.operation(false)?;
    let (_, total, items) = state.thread.sdk_replay_history_snapshot().await?;
    ensure!(
        total <= 256,
        "Native sandbox memory supports at most 256 original records"
    );
    drop(items);
    let history = crate::subagents::read_history(state, None).await?;
    let mut context = history
        .iter()
        .rev()
        .find_map(|item| match item {
            RolloutItem::TurnContext(context)
                if context.turn_id.as_deref() == Some(turn_id) && sampling_receipt(context) =>
            {
                Some(context.clone())
            }
            _ => None,
        })
        .context("Memory input admission requires an exact captured sampling receipt")?;
    ensure!(
        context
            .sdk_receipt
            .as_ref()
            .and_then(|receipt| receipt.get("thread_id"))
            .and_then(Value::as_str)
            == Some(state.id.to_string().as_str()),
        "Memory admission must belong to this native thread"
    );
    let receipt = context
        .sdk_receipt
        .as_mut()
        .and_then(Value::as_object_mut)
        .context("Memory input admission requires an object sampling receipt")?;
    receipt.insert(MEMORY_INPUT_ADMITTED.to_owned(), Value::Bool(true));
    state
        .thread
        .append_rollout_items(&[RolloutItem::TurnContext(context)])
        .await?;
    state.thread.flush_rollout().await?;
    Ok(())
}

/// Unproven input remains outside memory, including after ordinary resume/fork.
pub async fn memory_snapshot(state: &ThreadState, turn_id: &str) -> anyhow::Result<Value> {
    let _operation = state.engine.operation(false)?;
    let _mutation = state
        .mutations
        .try_lock()
        .context("native mutation is in progress")?;
    idle(state).await?;
    let (revision, total, items) = state.thread.sdk_replay_history_snapshot().await?;
    ensure!(
        total <= 256,
        "Native sandbox memory supports at most 256 original records"
    );
    let history = crate::subagents::read_history(state, None).await?;
    let mut admitted = HashSet::new();
    for item in &history {
        match item {
            RolloutItem::TurnContext(context)
                if sampling_receipt(context)
                    && context
                        .sdk_receipt
                        .as_ref()
                        .and_then(|receipt| receipt.get(MEMORY_INPUT_ADMITTED))
                        .and_then(Value::as_bool)
                        == Some(true) =>
            {
                if let Some(turn) = &context.turn_id {
                    admitted.insert(turn.clone());
                }
            }
            _ => {}
        }
    }
    ensure!(
        admitted.contains(turn_id),
        "Native sandbox memory requires proven input admission"
    );
    let items = items
        .into_iter()
        .filter_map(|raw| {
            let item: Value = match serde_json::from_str(&raw) {
                Ok(item) => item,
                Err(error) => return Some(Err(error)),
            };
            let turn = item
                .get("internal_chat_message_metadata_passthrough")
                .and_then(|metadata| metadata.get("turn_id"))
                .and_then(Value::as_str);
            turn.is_some_and(|turn| admitted.contains(turn))
                .then_some(Ok(raw))
        })
        .collect::<Result<Vec<_>, serde_json::Error>>()?;
    Ok(json!({"revision":revision,"total":total,"items":items}))
}
