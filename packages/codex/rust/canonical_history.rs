//! Core-owned canonical history transactions; no host-language replay state.
use crate::engine::ThreadState;
use anyhow::{Context, ensure};
use codex_protocol::models::ResponseItem;
use codex_protocol::protocol::AgentStatus;
use serde_json::{Value, json};

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

pub async fn snapshot(state: &ThreadState, limit: Option<usize>) -> anyhow::Result<Value> {
    let _operation = state.engine.operation(false)?;
    let _mutation = state
        .mutations
        .try_lock()
        .context("native mutation is in progress")?;
    idle(&state).await?;
    let (revision, total, items) = state.thread.sdk_history_snapshot(limit).await;
    Ok(json!({"revision": revision, "total": total, "items": items}))
}

pub async fn replace(
    state: &ThreadState,
    expected: String,
    keep: usize,
    items: Vec<ResponseItem>,
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
    let (current_revision, _, originals) = state.thread.sdk_history_snapshot(None).await;
    ensure!(
        current_revision == expected,
        "canonical history revision conflict"
    );
    ensure!(
        keep <= originals.len(),
        "history keep exceeds snapshot length"
    );
    crate::context::check_pairs(&originals, &(0..keep).collect::<Vec<_>>())
        .context("canonical history requires complete tool call/result pairs")?;
    crate::context::check_pairs(&items, &(0..items.len()).collect::<Vec<_>>())
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
