//! Application-owned canonical snapshots. Memory is a rebuildable projection only.

use codex_protocol::models::ResponseItem;
use codex_protocol::protocol::EventMsg;
use std::collections::HashMap;
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
}

impl CanonicalStore {
    pub(crate) fn new(backend: Py<PyAny>) -> Self {
        Self {
            backend,
            managed_calls: Arc::new(crate::storage::ManagedCalls::default()),
            inner: InMemoryThreadStore::default(),
            writers: Mutex::new(HashMap::new()),
            failed: AtomicBool::new(false),
        }
    }

    fn acquire(&self, id: ThreadId) -> ThreadStoreResult<String> {
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

    pub(crate) async fn commit_sdk_receipt(
        &self,
        id: ThreadId,
        key: &crate::python_tools::PendingKey,
        item: ResponseItem,
    ) -> ThreadStoreResult<()> {
        let mut writers = self.writers.lock().await;
        let writer = writers.get_mut(&id).ok_or_else(failure)?;
        let mut snapshot = writer.snapshot.clone().ok_or_else(failure)?;
        let mut turn = None;
        let mut found = false;
        for record in &snapshot.items {
            match record {
                RolloutItem::EventMsg(EventMsg::TurnStarted(event)) => {
                    turn = Some(event.turn_id.as_str())
                }
                RolloutItem::ResponseItem(envelope) => match &envelope.item {
                    ResponseItem::FunctionCall { call_id, .. }
                        if call_id == &key.call && turn == Some(key.turn.as_str()) =>
                    {
                        found = true
                    }
                    ResponseItem::FunctionCallOutput {
                        call_id: Some(call_id),
                        ..
                    } if call_id == &key.call => found = false,
                    _ => {}
                },
                _ => {}
            }
        }
        if !found || key.reply.thread != id.to_string() {
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
            let mut turn = None;
            let mut found = false;
            let mut completed = false;
            for record in &snapshot.items {
                match record {
                    RolloutItem::EventMsg(EventMsg::TurnStarted(event)) => {
                        turn = Some(event.turn_id.as_str())
                    }
                    RolloutItem::ResponseItem(envelope) => match &envelope.item {
                        ResponseItem::FunctionCall { call_id: saved, .. }
                            if saved == call_id && turn == Some(turn_id) =>
                        {
                            found = true
                        }
                        ResponseItem::FunctionCallOutput {
                            call_id: Some(saved),
                            internal_chat_message_metadata_passthrough,
                            ..
                        } if saved == call_id
                            && internal_chat_message_metadata_passthrough
                                .as_ref()
                                .and_then(|metadata| metadata.turn_id.as_deref())
                                .or(turn)
                                == Some(turn_id) =>
                        {
                            completed = true
                        }
                        _ => {}
                    },
                    _ => {}
                }
            }
            if !found || completed {
                return Err(ThreadStoreError::InvalidRequest {
                    message: "Reconciliation requires an unresolved original turn/call identity"
                        .into(),
                });
            }
            let response =
                serde_json::from_str::<codex_protocol::dynamic_tools::DynamicToolResponse>(output)
                    .map_err(|_| ThreadStoreError::InvalidRequest {
                        message: "Reconciliation requires valid tool output".into(),
                    })?;
            let item = crate::python_tools::PythonOutput(response).receipt_for(turn_id, call_id);
            snapshot.items.push(RolloutItem::ResponseItem(
                codex_history::ResponseItemEnvelope::new(item),
            ));
            self.commit(id, writer, snapshot)
        }
        .await;
        let released = self.discard_thread(id).await;
        result.and(released)
    }

    pub(crate) async fn close_all(&self) -> ThreadStoreResult<()> {
        let ids: Vec<_> = self.writers.lock().await.keys().copied().collect();
        let mut error = None;
        for id in ids {
            if let Err(err) = self.shutdown_thread(id).await {
                error = Some(err);
            }
        }
        if self.failed.load(Ordering::Acquire) {
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
            Ok(Arc::new(
                writers[&params.thread_id]
                    .snapshot
                    .as_ref()
                    .unwrap()
                    .items
                    .clone(),
            ))
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
                return if self.failed.load(Ordering::Acquire) {
                    Err(failure())
                } else {
                    Ok(())
                };
            };
            let flushed = Python::attach(|py| {
                self.backend
                    .bind(py)
                    .call_method1("flush", (id.to_string(), writer.token.as_str()))
                    .map(|_| ())
            });
            let released = self.release(id, &writer.token);
            let failed = writer.failed || flushed.is_err() || released.is_err();
            if released.is_ok() {
                writers.remove(&id);
            } else {
                writer.failed = true;
            }
            if failed {
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
