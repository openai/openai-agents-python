//! Read the owning store's committed index without reconstructing a rollout.

use crate::engine::ThreadState;
use anyhow::{Context, Result, ensure};
use codex_protocol::ThreadId;
use codex_thread_store::{DeleteThreadParams, RevertThreadParams, ThreadStore, ThreadStoreError};
use codex_thread_store::{
    ItemSortKey, ListItemsParams, ListItemsPosition, ListThreadsParams, ListTurnsParams,
    SearchThreadOccurrencesParams, SortDirection, StoredThreadItem, StoredTurnItemsView,
    StoredTurnStatus, ThreadSortKey,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum CloseOperation {
    Revert { before_turn_id: String },
    Delete,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ForkRequest {
    before_turn_id: Option<String>,
    through_turn_id: Option<String>,
}

/// Decode only a boundary; the current native Session supplies the source ID.
pub(crate) fn fork_boundary(encoded: &str) -> Result<codex_thread_store::ForkBoundary> {
    let request: ForkRequest = serde_json::from_str(encoded)?;
    ensure!(
        request.before_turn_id.is_none() || request.through_turn_id.is_none(),
        "choose either before_turn_id or through_turn_id"
    );
    if let Some(id) = request.before_turn_id {
        ensure!(!id.is_empty(), "before_turn_id must not be empty");
        return Ok(codex_thread_store::ForkBoundary::BeforeTurn(id));
    }
    if let Some(id) = request.through_turn_id {
        ensure!(!id.is_empty(), "through_turn_id must not be empty");
        return Ok(codex_thread_store::ForkBoundary::ThroughTurn(id));
    }
    Ok(codex_thread_store::ForkBoundary::Latest)
}

/// One Session close action, after its writer joins and before database teardown.
pub struct CloseAction {
    store: Arc<dyn ThreadStore>,
    id: ThreadId,
    operation: Mutex<Option<CloseOperation>>,
}

impl CloseAction {
    pub fn capture(state: &ThreadState, encoded: &str) -> Result<Self> {
        let _operation = state.engine.operation(false)?;
        ensure!(
            state.engine.local_checklist_store.is_some(),
            "stored history revert/delete requires native LocalStorage; other stores are unsupported"
        );
        let operation: CloseOperation = serde_json::from_str(encoded)?;
        if let CloseOperation::Revert { before_turn_id } = &operation {
            ensure!(
                !before_turn_id.is_empty(),
                "before_turn_id must not be empty"
            );
        }
        Ok(Self {
            store: Arc::clone(&state.engine.store),
            id: state.id,
            operation: Mutex::new(Some(operation)),
        })
    }

    pub async fn apply(&self) -> Result<()> {
        let operation = self
            .operation
            .lock()
            .map_err(|_| anyhow::anyhow!("stored history close action lock poisoned"))?
            .take()
            .ok_or_else(|| anyhow::anyhow!("stored history close action already consumed"))?;
        match operation {
            CloseOperation::Revert { before_turn_id } => {
                self.store
                    .revert_thread(RevertThreadParams {
                        thread_id: self.id,
                        before_turn_id,
                        multi_agent_version: None,
                    })
                    .await?
            }
            CloseOperation::Delete => {
                self.store
                    .delete_thread(DeleteThreadParams { thread_id: self.id })
                    .await?
            }
        }
        Ok(())
    }
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Query {
    Sessions {
        limit: usize,
        cursor: Option<String>,
        archived: bool,
    },
    Turns {
        limit: usize,
        cursor: Option<String>,
        descending: bool,
        include_items: bool,
    },
    Items {
        limit: usize,
        cursor: Option<String>,
        descending: bool,
        turn_id: Option<String>,
    },
    Search {
        limit: usize,
        cursor: Option<String>,
        text: String,
    },
}

impl Query {
    fn check(&self) -> Result<()> {
        let limit = match self {
            Self::Sessions { limit, .. }
            | Self::Turns { limit, .. }
            | Self::Items { limit, .. }
            | Self::Search { limit, .. } => *limit,
        };
        ensure!(
            (1..=1000).contains(&limit),
            "stored history limit must be between 1 and 1000"
        );
        if let Self::Search { text, .. } = self {
            ensure!(
                !text.is_empty(),
                "stored history search requires nonempty text"
            );
        }
        Ok(())
    }
}

fn store_error(error: ThreadStoreError) -> anyhow::Error {
    match error {
        ThreadStoreError::InvalidRequest { message } if message.starts_with("invalid cursor:") => {
            anyhow::anyhow!("invalid stored history cursor")
        }
        other => other.into(),
    }
}

fn direction(descending: bool) -> SortDirection {
    if descending {
        SortDirection::Desc
    } else {
        SortDirection::Asc
    }
}

fn item(value: StoredThreadItem) -> Result<Value> {
    // Keep the indexed JSON bytes opaque across serde Value. The public reader
    // decodes this string once, avoiding reinterpretation of serde marker keys.
    let data_json = String::from_utf8(value.item_json)?;
    Ok(json!({
        "turn_id": value.turn_id,
        "id": value.item_id,
        "updated_at_ordinal": value.updated_at_ordinal.to_string(),
        "created_at_ms": value.created_at_ms,
        "started_at_ms": value.started_at_ms,
        "completed_at_ms": value.completed_at_ms,
        "data_json": data_json,
    }))
}

pub async fn read(state: &ThreadState, query: Query) -> Result<Value> {
    query.check()?;
    let _operation = state.engine.operation(false)?;
    ensure!(
        state.engine.store.supports_paginated_history_lists(),
        "native stored history index is unsupported by this store"
    );
    if matches!(&query, Query::Sessions { .. }) {
        ensure!(
            state.engine.local_checklist_store.is_some(),
            "stored application-session discovery requires native LocalStorage"
        );
    }
    // Match ordinary history reads: materialize/flush the already loaded owner,
    // including an active or approval-paused turn. No idle or mutation permit is
    // needed for the store's own committed-row transactions.
    state.thread.ensure_rollout_materialized().await;
    state.thread.flush_rollout().await?;
    let store = &state.engine.store;
    match query {
        Query::Sessions {
            limit,
            cursor,
            archived,
        } => {
            sessions(
                state
                    .engine
                    .local_checklist_store
                    .as_ref()
                    .context("stored application-session discovery requires native LocalStorage")?,
                limit,
                cursor,
                archived,
            )
            .await
        }
        Query::Turns {
            limit,
            cursor,
            descending,
            include_items,
        } => {
            let page = store
                .list_turns(ListTurnsParams {
                    thread_id: state.id,
                    include_archived: true,
                    cursor,
                    page_size: limit,
                    sort_direction: direction(descending),
                    items_view: if include_items {
                        StoredTurnItemsView::Summary
                    } else {
                        StoredTurnItemsView::NotLoaded
                    },
                })
                .await
                .map_err(store_error)?;
            let turns = page
                .turns
                .into_iter()
                .map(|turn| {
                    let items = turn
                        .items
                        .into_iter()
                        .map(item)
                        .collect::<Result<Vec<_>>>()?;
                    let status = match turn.status {
                        StoredTurnStatus::Completed => "completed",
                        StoredTurnStatus::Interrupted => "interrupted",
                        StoredTurnStatus::Failed => "failed",
                        StoredTurnStatus::InProgress => "in_progress",
                    };
                    Ok(
                        json!({"id": turn.turn_id, "status": status, "error": turn.error,
                    "started_at": turn.started_at, "completed_at": turn.completed_at,
                    "duration_ms": turn.duration_ms, "items": items}),
                    )
                })
                .collect::<Result<Vec<_>>>()?;
            Ok(json!({"turns": turns, "next_cursor": page.next_cursor,
                "backwards_cursor": page.backwards_cursor}))
        }
        Query::Items {
            limit,
            cursor,
            descending,
            turn_id,
        } => {
            let page = store
                .list_items(ListItemsParams {
                    thread_id: state.id,
                    turn_id,
                    include_archived: true,
                    position: cursor.map(ListItemsPosition::Cursor),
                    page_size: limit,
                    sort_direction: direction(descending),
                    sort_key: ItemSortKey::CreatedAtOrdinal,
                    after_updated_at_ordinal: None,
                })
                .await
                .map_err(store_error)?;
            let items = page
                .items
                .into_iter()
                .map(item)
                .collect::<Result<Vec<_>>>()?;
            Ok(json!({"items": items, "next_cursor": page.next_cursor,
                "backwards_cursor": page.backwards_cursor}))
        }
        Query::Search {
            limit,
            cursor,
            text,
        } => {
            let page = store
                .search_thread_occurrences(SearchThreadOccurrencesParams {
                    thread_id: state.id,
                    search_term: text,
                    cursor,
                    page_size: limit,
                })
                .await
                .map_err(store_error)?;
            Ok(serde_json::to_value(page)?)
        }
    }
}

/// One indexed page of application-session headers, without opening their writers.
async fn sessions(
    store: &crate::storage::LocalChecklistStore,
    limit: usize,
    cursor: Option<String>,
    archived: bool,
) -> Result<Value> {
    let page = store
        .list_threads_including_empty_preview(ListThreadsParams {
            page_size: limit,
            cursor,
            sort_key: ThreadSortKey::CreatedAt,
            sort_direction: SortDirection::Asc,
            // SDK root and stored-fork startup both use the manager's Exec source.
            allowed_sources: vec![codex_protocol::protocol::SessionSource::Exec],
            model_providers: Some(Vec::new()),
            cwd_filters: None,
            section: None,
            project_id: None,
            archived,
            search_term: None,
            relation_filter: None,
            use_state_db_only: true,
        })
        .await
        .map_err(store_error)?;
    let mut sessions = Vec::with_capacity(page.items.len());
    for candidate in page.items {
        // List metadata omits fork-parent links. Read the actual native header
        // instead of read_thread's supported missing-rollout SQLite fallback.
        let path = candidate
            .rollout_path
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("stored application-session metadata is unavailable"))?;
        let header = match codex_rollout::read_session_meta_line(path).await {
            Ok(header) => header,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => anyhow::bail!("invalid stored application-session metadata"),
        };
        ensure!(
            header.meta.id == candidate.thread_id,
            "stored application-session metadata identity mismatch"
        );
        // The index and a header can be observed at different moments.
        if !matches!(
            header.meta.source,
            codex_protocol::protocol::SessionSource::Exec
        ) {
            continue;
        }
        sessions.push(json!({
            "id": header.meta.id.to_string(),
            "forked_from_id": header.meta.forked_from_id.map(|id| id.to_string()),
            "parent_thread_id": header.meta.parent_thread_id.map(|id| id.to_string()),
            "created_at_ms": candidate.created_at.timestamp_millis(),
            "updated_at_ms": candidate.updated_at.timestamp_millis(),
        }));
    }
    // No refill: deleted entries can leave a short/empty nonterminal page.
    Ok(json!({"sessions": sessions, "next_cursor": page.next_cursor}))
}
