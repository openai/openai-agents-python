//! Application-owned canonical snapshots. Memory is a rebuildable projection only.

use codex_protocol::dynamic_tools::{DynamicToolCallOutputContentItem, DynamicToolResponse};
use codex_protocol::models::ResponseItem;
use codex_protocol::protocol::EventMsg;
use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use codex_history::RolloutItem;
use codex_protocol::ThreadId;
use codex_thread_store::*;
use pyo3::prelude::*;
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;

fn failure() -> ThreadStoreError {
    ThreadStoreError::Internal {
        message:
            "Canonical storage failed; close and reconcile the saved execution before reopening"
                .into(),
    }
}

fn invocation_identity(item: &ResponseItem) -> Option<&str> {
    match item {
        ResponseItem::FunctionCall { call_id, .. } | ResponseItem::ComputerCall { call_id, .. } => {
            Some(call_id)
        }
        ResponseItem::McpApprovalRequest { id: Some(id), .. } => Some(id.as_str()),
        ResponseItem::ShellCall {
            provider_fields, ..
        } => crate::local_reconciliation::shell_call_id(provider_fields).map(String::as_str),
        _ => None,
    }
}

fn pending_invocation<'a>(
    items: &'a [RolloutItem],
    turn_id: &str,
    call_id: &str,
) -> ThreadStoreResult<&'a ResponseItem> {
    let mut turn = None;
    let mut invocation = None;
    let mut completed = false;
    for record in items {
        match record {
            RolloutItem::EventMsg(EventMsg::TurnStarted(event)) => {
                turn = Some(event.turn_id.as_str());
            }
            RolloutItem::ResponseItem(envelope)
                if envelope.item.turn_id().or(turn) == Some(turn_id) =>
            {
                if invocation_identity(&envelope.item) == Some(call_id) {
                    if invocation.replace(&envelope.item).is_some() {
                        return Err(ThreadStoreError::InvalidRequest {
                            message: "Reconciliation requires an unambiguous original call".into(),
                        });
                    }
                } else if crate::local_reconciliation::output_identity(&envelope.item)
                    .is_some_and(|(saved, _)| saved == call_id)
                {
                    completed = true;
                }
            }
            _ => {}
        }
    }
    invocation
        .filter(|_| !completed)
        .ok_or_else(|| ThreadStoreError::InvalidRequest {
            message: "Reconciliation requires an unresolved original turn/call identity".into(),
        })
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    schema_version: u32,
    sdk: String,
    runtime_revision: String,
    revision: u64,
    write_id: String,
    create: CreateThreadParams,
    items: Vec<RolloutItem>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pending_shell_calls: BTreeSet<(String, String)>,
    patch: ThreadMetadataPatch,
    summary: StoredThread,
}

struct Writer {
    token: String,
    snapshot: Option<Snapshot>,
    failed: bool,
}

pub(crate) struct CanonicalStore {
    pub(crate) managed_calls: Arc<crate::storage::ManagedCalls>,
    backend: Py<PyAny>,
    inner: InMemoryThreadStore,
    writers: Mutex<HashMap<ThreadId, Writer>>,
    failed: AtomicBool,
    cleanup_failed: AtomicBool,
}

impl CanonicalStore {
    pub(crate) fn new(backend: Py<PyAny>) -> Arc<Self> {
        Arc::new_cyclic(|writer| Self {
            backend,
            managed_calls: Arc::new(crate::storage::ManagedCalls::with_canonical_writer(
                writer.clone(),
            )),
            inner: InMemoryThreadStore::default(),
            writers: Mutex::new(HashMap::new()),
            failed: AtomicBool::new(false),
            cleanup_failed: AtomicBool::new(false),
        })
    }

    fn acquire(&self, id: ThreadId) -> ThreadStoreResult<String> {
        if self.failed.load(Ordering::Acquire) {
            return Err(failure());
        }
        Python::attach(|py| {
            self.backend
                .bind(py)
                .call_method1("acquire", (id.to_string(),))?
                .extract()
        })
        .map_err(|_| failure())
    }

    fn read(&self, id: ThreadId, token: &str) -> ThreadStoreResult<Option<String>> {
        Python::attach(|py| {
            self.backend
                .bind(py)
                .call_method1("read", (id.to_string(), token))?
                .extract()
        })
        .map_err(|_| failure())
    }

    fn release(&self, id: ThreadId, token: &str) -> ThreadStoreResult<()> {
        Python::attach(|py| {
            self.backend
                .bind(py)
                .call_method1("release", (id.to_string(), token))
                .map(|_| ())
        })
        .map_err(|_| failure())
    }

    async fn load(
        &self,
        id: ThreadId,
        writers: &mut HashMap<ThreadId, Writer>,
    ) -> ThreadStoreResult<()> {
        if let Some(writer) = writers.get(&id) {
            return if writer.failed {
                Err(failure())
            } else {
                Ok(())
            };
        }
        let token = self.acquire(id)?;
        let result = async {
            let encoded = self
                .read(id, &token)?
                .ok_or(ThreadStoreError::ThreadNotFound { thread_id: id })?;
            let snapshot: Snapshot = serde_json::from_str(&encoded).map_err(|_| failure())?;
            if snapshot.schema_version != 1
                || snapshot.sdk != "agents-python"
                || snapshot.runtime_revision != include_str!("../CODEX_REVISION").trim()
                || snapshot.create.thread_id != id
                || snapshot.create.parent_thread_id.is_some()
            {
                return Err(failure());
            }
            crate::checkpoint::validate_imported_history(&snapshot.items).map_err(|_| failure())?;
            self.inner.create_thread(snapshot.create.clone()).await?;
            // Keep the original canonical header in snapshots. The projection's
            // generated header is used only to rebuild native summary metadata.
            self.inner
                .append_items(AppendThreadItemsParams {
                    thread_id: id,
                    items: snapshot.items.iter().skip(1).cloned().collect(),
                })
                .await?;
            self.inner
                .update_thread_metadata(UpdateThreadMetadataParams {
                    thread_id: id,
                    patch: snapshot.patch.clone(),
                    include_archived: true,
                })
                .await?;
            Ok(snapshot)
        }
        .await;
        match result {
            Ok(snapshot) => {
                writers.insert(
                    id,
                    Writer {
                        token,
                        snapshot: Some(snapshot),
                        failed: false,
                    },
                );
                Ok(())
            }
            Err(error) => {
                if self.release(id, &token).is_err() {
                    self.failed.store(true, Ordering::Release);
                    writers.insert(
                        id,
                        Writer {
                            token,
                            snapshot: None,
                            failed: true,
                        },
                    );
                }
                Err(error)
            }
        }
    }

    fn commit(
        &self,
        id: ThreadId,
        writer: &mut Writer,
        mut snapshot: Snapshot,
    ) -> ThreadStoreResult<()> {
        if writer.failed {
            return Err(failure());
        }
        let revision = writer.snapshot.as_ref().map_or(0, |s| s.revision);
        snapshot.revision = revision + 1;
        // The writer token is unique across acquisitions, and revisions increase
        // within one writer. This identity is reused while reconciling the ACK.
        snapshot.write_id = format!("{}:{}", writer.token, snapshot.revision);
        let encoded = serde_json::to_string(&snapshot).map_err(|_| failure())?;
        let accepted = Python::attach(|py| {
            self.backend
                .bind(py)
                .call_method1(
                    "commit",
                    (
                        id.to_string(),
                        writer.token.as_str(),
                        revision,
                        snapshot.write_id.as_str(),
                        encoded.as_str(),
                    ),
                )
                .map(|_| ())
        });
        if accepted.is_err() {
            let matches =
                self.read(id, &writer.token).ok().flatten().as_deref() == Some(encoded.as_str());
            // A readable replacement may precede its final durability fence.
            // Require a successful idempotent commit acknowledgment as well.
            let confirmed = matches
                && Python::attach(|py| {
                    self.backend
                        .bind(py)
                        .call_method1(
                            "commit",
                            (
                                id.to_string(),
                                writer.token.as_str(),
                                revision,
                                snapshot.write_id.as_str(),
                                encoded.as_str(),
                            ),
                        )
                        .map(|_| ())
                })
                .is_ok();
            if !confirmed {
                writer.failed = true;
                self.failed.store(true, Ordering::Release);
                return Err(failure());
            }
        }
        writer.snapshot = Some(snapshot);
        Ok(())
    }

    pub(crate) async fn record_shell_call(
        &self,
        id: ThreadId,
        turn: &str,
        call: &str,
    ) -> ThreadStoreResult<()> {
        let mut writers = self.writers.lock().await;
        let writer = writers.get_mut(&id).ok_or_else(failure)?;
        let mut snapshot = writer.snapshot.clone().ok_or_else(failure)?;
        if !matches!(
            pending_invocation(&snapshot.items, turn, call),
            Ok(ResponseItem::ShellCall { .. })
        ) || !snapshot
            .pending_shell_calls
            .insert((turn.to_owned(), call.to_owned()))
        {
            return Err(failure());
        }
        self.commit(id, writer, snapshot)
    }

    pub(crate) async fn discard_shell_call(
        &self,
        id: ThreadId,
        turn: &str,
        call: &str,
    ) -> ThreadStoreResult<()> {
        let mut writers = self.writers.lock().await;
        let writer = writers.get_mut(&id).ok_or_else(failure)?;
        let mut snapshot = writer.snapshot.clone().ok_or_else(failure)?;
        if !snapshot
            .pending_shell_calls
            .remove(&(turn.to_owned(), call.to_owned()))
        {
            return Err(failure());
        }
        self.commit(id, writer, snapshot)
    }

    pub(crate) async fn pending_shell_calls(
        &self,
        id: ThreadId,
    ) -> ThreadStoreResult<Vec<(String, String, ResponseItem)>> {
        let writers = self.writers.lock().await;
        let snapshot = writers
            .get(&id)
            .and_then(|writer| writer.snapshot.as_ref())
            .ok_or_else(failure)?;
        snapshot
            .pending_shell_calls
            .iter()
            .map(|(turn, call)| {
                let item =
                    pending_invocation(&snapshot.items, turn, call).map_err(|_| failure())?;
                if !matches!(item, ResponseItem::ShellCall { .. }) {
                    return Err(failure());
                }
                Ok((turn.clone(), call.clone(), item.clone()))
            })
            .collect()
    }

    pub(crate) async fn commit_sdk_receipt(
        &self,
        id: ThreadId,
        key: &crate::python_tools::PendingKey,
        item: ResponseItem,
    ) -> ThreadStoreResult<()> {
        let mut writers = self.writers.lock().await;
        let writer = writers.get_mut(&id).ok_or_else(failure)?;
        let mut snapshot = writer.snapshot.clone().ok_or_else(failure)?;
        let invocation =
            pending_invocation(&snapshot.items, &key.turn, &key.call).map_err(|_| failure())?;
        if key.reply.thread != id.to_string()
            || !crate::local_reconciliation::output_matches_invocation(invocation, &item)
            || crate::local_reconciliation::output_identity(&item)
                != Some((key.call.as_str(), Some(key.turn.as_str())))
            || (matches!(invocation, ResponseItem::ShellCall { .. })
                && !snapshot
                    .pending_shell_calls
                    .remove(&(key.turn.clone(), key.call.clone())))
        {
            return Err(failure());
        }
        let record = RolloutItem::ResponseItem(codex_history::ResponseItemEnvelope::new(item));
        snapshot.items.push(record.clone());
        self.commit(id, writer, snapshot)?;
        self.inner
            .append_items(AppendThreadItemsParams {
                thread_id: id,
                items: vec![record],
            })
            .await?;
        Ok(())
    }

    pub(crate) async fn reconcile(
        &self,
        id: ThreadId,
        turn_id: &str,
        call_id: &str,
        output: &str,
    ) -> ThreadStoreResult<()> {
        let result = async {
            let mut writers = self.writers.lock().await;
            self.load(id, &mut writers).await?;
            let writer = writers.get_mut(&id).ok_or_else(failure)?;
            let mut snapshot = writer.snapshot.clone().ok_or_else(failure)?;
            let original = pending_invocation(&snapshot.items, turn_id, call_id)?;
            let item = if matches!(original, ResponseItem::ShellCall { .. }) {
                if !snapshot.pending_shell_calls.remove(&(turn_id.to_owned(), call_id.to_owned())) {
                    return Err(ThreadStoreError::InvalidRequest {
                        message: "Reconciliation requires an unresolved original application Shell call".into(),
                    });
                }
                let arguments = serde_json::to_string(original).map_err(|_| failure())?;
                let response = DynamicToolResponse {
                    content_items: vec![DynamicToolCallOutputContentItem::InputText {
                        text: output.to_owned(),
                    }],
                    success: true,
                };
                crate::local_reconciliation::shell_receipt(&arguments, response, turn_id, call_id)
                    .map_err(|_| ThreadStoreError::InvalidRequest {
                        message: "Shell reconciliation requires a valid ShellOutput".into(),
                    })?
            } else {
                let response =
                    serde_json::from_str::<DynamicToolResponse>(output).map_err(|_| {
                        ThreadStoreError::InvalidRequest {
                            message: "Reconciliation requires valid tool output".into(),
                        }
                    })?;
                if matches!(original, ResponseItem::ComputerCall { .. }) {
                    crate::local_reconciliation::computer_receipt(original, response, turn_id)
                        .map_err(|_| ThreadStoreError::InvalidRequest {
                            message: "Computer reconciliation requires a valid screenshot and safety acknowledgements".into(),
                        })?
                } else {
                    crate::python_tools::PythonOutput(response).receipt_for(turn_id, call_id)
                }
            };
            snapshot.items.push(RolloutItem::ResponseItem(
                codex_history::ResponseItemEnvelope::new(item),
            ));
            self.commit(id, writer, snapshot)
        }
        .await;
        let released = self.discard_thread(id).await;
        match (result, released) {
            (Ok(()), Ok(())) => Ok(()),
            (Err(primary), Ok(())) => Err(primary),
            (Ok(()), Err(cleanup)) => Err(cleanup),
            (Err(primary), Err(cleanup)) => Err(ThreadStoreError::Internal {
                message: format!(
                    "Reconciliation failed: {primary}; writer cleanup failed: {cleanup}"
                ),
            }),
        }
    }

    pub(crate) async fn close_all(&self) -> ThreadStoreResult<()> {
        let ids: Vec<_> = self.writers.lock().await.keys().copied().collect();
        let mut error = None;
        for id in ids {
            if let Err(err) = self.shutdown_thread(id).await {
                error = Some(err);
            }
        }
        // Core shutdown may already have observed and consumed a store error.
        // Report that cleanup failure once without making later close sticky.
        if self.cleanup_failed.swap(false, Ordering::AcqRel) {
            return Err(failure());
        }
        error.map_or(Ok(()), Err)
    }
}

impl Drop for CanonicalStore {
    fn drop(&mut self) {
        for (id, writer) in self.writers.get_mut().drain() {
            let _ = Python::attach(|py| {
                self.backend
                    .bind(py)
                    .call_method1("release", (id.to_string(), writer.token))
                    .map(|_| ())
            });
        }
    }
}

impl ThreadStore for CanonicalStore {
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }

    fn create_thread(&self, params: CreateThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            if params.parent_thread_id.is_some() {
                return Err(ThreadStoreError::Unsupported {
                    operation: "custom-store children",
                });
            }
            let id = params.thread_id;
            let mut writers = self.writers.lock().await;
            if writers.contains_key(&id) {
                return Err(failure());
            }
            let token = self.acquire(id)?;
            let result = async {
                if self.read(id, &token)?.is_some() {
                    return Err(failure());
                }
                self.inner.create_thread(params.clone()).await?;
                let history = self
                    .inner
                    .load_history(LoadThreadHistoryParams {
                        thread_id: id,
                        include_archived: true,
                    })
                    .await?;
                let mut writer = Writer {
                    token: token.clone(),
                    snapshot: None,
                    failed: false,
                };
                self.commit(
                    id,
                    &mut writer,
                    Snapshot {
                        schema_version: 1,
                        sdk: "agents-python".into(),
                        runtime_revision: include_str!("../CODEX_REVISION").trim().into(),
                        revision: 0,
                        write_id: String::new(),
                        create: params,
                        items: history.items,
                        pending_shell_calls: BTreeSet::new(),
                        patch: ThreadMetadataPatch::default(),
                        summary: self
                            .inner
                            .read_thread(ReadThreadParams {
                                thread_id: id,
                                include_archived: true,
                                include_history: false,
                            })
                            .await?,
                    },
                )?;
                Ok(writer)
            }
            .await;
            match result {
                Ok(writer) => {
                    writers.insert(id, writer);
                    Ok(())
                }
                Err(error) => {
                    if self.release(id, &token).is_err() {
                        self.failed.store(true, Ordering::Release);
                        writers.insert(
                            id,
                            Writer {
                                token,
                                snapshot: None,
                                failed: true,
                            },
                        );
                    }
                    Err(error)
                }
            }
        })
    }

    fn resume_thread(
        &self,
        params: ResumeThreadParams,
    ) -> ThreadStoreFuture<'_, Arc<Vec<RolloutItem>>> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            self.load(params.thread_id, &mut writers).await?;
            let mut history = Arc::new(
                writers[&params.thread_id]
                    .snapshot
                    .as_ref()
                    .unwrap()
                    .items
                    .clone(),
            );
            crate::checkpoint::remove_replayed_mcp_approvals(&mut history);
            Ok(history)
        })
    }

    fn append_items(&self, params: AppendThreadItemsParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            let writer = writers.get_mut(&params.thread_id).ok_or_else(failure)?;
            if writer.failed {
                return Err(failure());
            }
            let mut snapshot = writer.snapshot.clone().ok_or_else(failure)?;
            let pending = params
                .items
                .into_iter()
                .filter(|item| !self.managed_calls.owns_output(params.thread_id, item))
                .collect();
            let items =
                codex_rollout::into_persisted_rollout_items(pending, snapshot.create.history_mode);
            if items.is_empty() {
                return Ok(());
            }
            snapshot.items.extend(items.clone());
            self.commit(params.thread_id, writer, snapshot)?;
            self.inner
                .append_items(AppendThreadItemsParams {
                    thread_id: params.thread_id,
                    items,
                })
                .await
        })
    }

    fn persist_thread(&self, id: ThreadId, _context: PersistContext) -> ThreadStoreFuture<'_, ()> {
        self.flush_thread(id)
    }

    fn flush_thread(&self, id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            let writer = writers.get_mut(&id).ok_or_else(failure)?;
            if writer.failed {
                return Err(failure());
            }
            let result = Python::attach(|py| {
                self.backend
                    .bind(py)
                    .call_method1("flush", (id.to_string(), writer.token.as_str()))
                    .map(|_| ())
            });
            if result.is_err() {
                writer.failed = true;
                self.failed.store(true, Ordering::Release);
                return Err(failure());
            }
            Ok(())
        })
    }

    fn shutdown_thread(&self, id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            let Some(writer) = writers.get_mut(&id) else {
                return Ok(());
            };
            // A failed writer is fenced. Retry only its unfinished release,
            // without replaying flush or any operation that already failed.
            let flushed = if writer.failed {
                Ok(())
            } else {
                Python::attach(|py| {
                    self.backend
                        .bind(py)
                        .call_method1("flush", (id.to_string(), writer.token.as_str()))
                        .map(|_| ())
                })
            };
            let released = self.release(id, &writer.token);
            let failed = flushed.is_err() || released.is_err();
            if released.is_ok() {
                writers.remove(&id);
            } else {
                writer.failed = true;
            }
            if failed {
                self.cleanup_failed.store(true, Ordering::Release);
                self.failed.store(true, Ordering::Release);
                return Err(failure());
            }
            Ok(())
        })
    }

    fn discard_thread(&self, id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            if let Some(writer) = writers.get_mut(&id) {
                if self.release(id, &writer.token).is_err() {
                    writer.failed = true;
                    self.cleanup_failed.store(true, Ordering::Release);
                    self.failed.store(true, Ordering::Release);
                    return Err(failure());
                }
                writers.remove(&id);
            }
            Ok(())
        })
    }

    fn load_history(
        &self,
        params: LoadThreadHistoryParams,
    ) -> ThreadStoreFuture<'_, StoredThreadHistory> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            self.load(params.thread_id, &mut writers).await?;
            Ok(StoredThreadHistory {
                thread_id: params.thread_id,
                revision: None,
                items: writers[&params.thread_id]
                    .snapshot
                    .as_ref()
                    .unwrap()
                    .items
                    .clone(),
            })
        })
    }

    fn read_original_history(
        &self,
        params: LoadThreadHistoryParams,
        mut reader: Box<dyn codex_thread_store::OriginalHistoryReader>,
    ) -> ThreadStoreFuture<'_, codex_thread_store::OriginalHistorySnapshot> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            self.load(params.thread_id, &mut writers).await?;
            let snapshot = writers[&params.thread_id].snapshot.as_ref().unwrap();
            reader.read_items(&snapshot.items)
        })
    }

    fn load_latest_model_context(
        &self,
        params: LoadThreadHistoryParams,
    ) -> ThreadStoreFuture<'_, StoredModelContext> {
        Box::pin(async move {
            let history = self.load_history(params).await?;
            Ok(StoredModelContext {
                thread_id: history.thread_id,
                items: history.items,
                revision: None,
            })
        })
    }

    fn read_thread(&self, params: ReadThreadParams) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            self.load(params.thread_id, &mut writers).await?;
            let include_history = params.include_history;
            let id = params.thread_id;
            let mut thread = writers[&id].snapshot.as_ref().unwrap().summary.clone();
            if include_history {
                thread.history = Some(StoredThreadHistory {
                    thread_id: id,
                    revision: None,
                    items: writers[&id].snapshot.as_ref().unwrap().items.clone(),
                });
            }
            Ok(thread)
        })
    }

    fn update_thread_metadata(
        &self,
        params: UpdateThreadMetadataParams,
    ) -> ThreadStoreFuture<'_, Option<StoredThread>> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            let writer = writers.get_mut(&params.thread_id).ok_or_else(failure)?;
            let mut snapshot = writer.snapshot.clone().ok_or_else(failure)?;
            snapshot.patch.merge(params.patch.clone());
            let mut updated = self.inner.update_thread_metadata(params.clone()).await?;
            if let Some(summary) = &mut updated {
                summary.created_at = snapshot.summary.created_at;
                snapshot.summary = summary.clone();
            }
            self.commit(params.thread_id, writer, snapshot)?;
            Ok(updated)
        })
    }

    fn read_thread_by_rollout_path(
        &self,
        _: ReadThreadByRolloutPathParams,
    ) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(async {
            Err(ThreadStoreError::Unsupported {
                operation: "read_thread_by_rollout_path",
            })
        })
    }
    fn list_threads(&self, _: ListThreadsParams) -> ThreadStoreFuture<'_, ThreadPage> {
        Box::pin(async {
            Err(ThreadStoreError::Unsupported {
                operation: "list_threads",
            })
        })
    }
    fn archive_thread(&self, _: ArchiveThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async {
            Err(ThreadStoreError::Unsupported {
                operation: "archive_thread",
            })
        })
    }
    fn unarchive_thread(&self, _: ArchiveThreadParams) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(async {
            Err(ThreadStoreError::Unsupported {
                operation: "unarchive_thread",
            })
        })
    }
    fn delete_thread(&self, _: DeleteThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async {
            Err(ThreadStoreError::Unsupported {
                operation: "delete_thread",
            })
        })
    }
}
