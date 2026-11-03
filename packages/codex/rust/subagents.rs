//! Read the native spawn graph without loading or resuming child runtimes.

use anyhow::Context;
use codex_agent_graph_store::ThreadSpawnEdgeStatus;
use codex_protocol::ThreadId;
use codex_thread_store::{LoadThreadHistoryParams, ThreadStoreError};
use serde::Serialize;

use crate::engine::ThreadState;

#[derive(Serialize)]
pub struct Subagent {
    pub id: ThreadId,
    object: &'static str,
    session_id: ThreadId,
    name: Option<String>,
    pub parent_agent_id: ThreadId,
    pub status: &'static str,
    runtime_loaded: bool,
    opened_at: i64,
    pub closed_at: Option<i64>,
}

pub async fn list(state: &ThreadState) -> anyhow::Result<Vec<Subagent>> {
    let engine = &state.engine;
    let ids = engine
        .manager
        .list_agent_subtree_thread_ids(state.id)
        .await?;
    let mut subagents = Vec::new();
    for id in ids.into_iter().filter(|id| *id != state.id) {
        let edge = engine.agent_graph.edge(id).await;
        let live = match engine.manager.get_thread(id).await {
            Ok(thread) => Some(thread.config_snapshot().await),
            Err(error)
                if matches!(
                    error.details(),
                    codex_protocol::error::CodexErrorDetails::ThreadNotFound(_)
                ) =>
            {
                None
            }
            Err(error) => return Err(error.into()),
        };
        let stored = if live.as_ref().is_some_and(|config| config.ephemeral) {
            None
        } else {
            match engine.read_stored_thread(id).await {
                Ok(stored) => Some(stored),
                // Graph persistence can precede rollout materialization or outlive a cancelled spawn.
                Err(ThreadStoreError::ThreadNotFound { .. })
                    if live.is_some() || edge.is_some() =>
                {
                    None
                }
                Err(error) => return Err(error).context(format!("Cannot read subagent {id}")),
            }
        };
        let parent_agent_id = edge
            .map(|edge| edge.parent)
            .or_else(|| {
                live.as_ref()
                    .and_then(|config| {
                        config
                            .parent_thread_id
                            .or(config.session_source.parent_thread_id())
                    })
                    .or_else(|| stored.as_ref().and_then(|thread| thread.parent_thread_id))
            })
            .with_context(|| format!("Subagent {id} has no parent metadata"))?;
        let name = live
            .as_ref()
            .and_then(|config| config.session_source.get_nickname())
            .or_else(|| {
                stored
                    .as_ref()
                    .and_then(|thread| thread.agent_nickname.clone())
            });
        let opened_at = match stored {
            Some(thread) => thread.created_at.timestamp(),
            None => creation_timestamp(id)?,
        };
        subagents.push(Subagent {
            id,
            object: "agent.session.subagent",
            session_id: state.id,
            name,
            parent_agent_id,
            status: if edge.is_some_and(|edge| edge.status == ThreadSpawnEdgeStatus::Closed) {
                "closed"
            } else {
                "active"
            },
            runtime_loaded: live.is_some(),
            opened_at,
            closed_at: edge.and_then(|edge| edge.closed_at),
        });
    }
    Ok(subagents)
}

pub async fn read_history(
    state: &ThreadState,
    subagent_id: Option<ThreadId>,
) -> anyhow::Result<Vec<codex_history::RolloutItem>> {
    let id = subagent_id.unwrap_or(state.id);
    anyhow::ensure!(
        state
            .engine
            .manager
            .list_agent_subtree_thread_ids(state.id)
            .await?
            .contains(&id),
        "Thread is not part of this session"
    );
    // Flush an already loaded runtime only; history reads never load or resume children.
    match state.engine.manager.get_thread(id).await {
        Ok(thread) => {
            if !matches!(
                thread.agent_status().await,
                codex_protocol::protocol::AgentStatus::Shutdown
                    | codex_protocol::protocol::AgentStatus::NotFound
            ) {
                thread.ensure_rollout_materialized().await;
                thread.flush_rollout().await?;
            }
        }
        Err(error)
            if matches!(
                error.details(),
                codex_protocol::error::CodexErrorDetails::ThreadNotFound(_)
            ) => {}
        Err(error) => return Err(error.into()),
    }
    Ok(state
        .engine
        .store
        .load_history(LoadThreadHistoryParams {
            thread_id: id,
            include_archived: true,
        })
        .await?
        .items)
}

fn creation_timestamp(id: ThreadId) -> anyhow::Result<i64> {
    // Codex-generated thread IDs are UUIDv7; their first 48 bits are Unix milliseconds.
    let id = id.to_string();
    anyhow::ensure!(
        id.as_bytes()[14] == b'7',
        "Subagent creation time unavailable for {id}"
    );
    let timestamp = id[..8].to_owned() + &id[9..13];
    Ok(i64::from_str_radix(&timestamp, 16)? / 1000)
}
