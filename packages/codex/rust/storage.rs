//! Native thread storage, accepted checklist data, and parent/child topology.

use std::collections::{HashMap, HashSet};
use std::io::{BufRead, BufReader, ErrorKind, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock, Weak};
use std::time::SystemTime;

use anyhow::Context;
use codex_agent_graph_store::{
    AgentGraphStore, AgentGraphStoreFuture, AgentGraphStoreResult, LocalAgentGraphStore,
    ThreadSpawnEdgeStatus,
};
use codex_app_server_protocol::{ThreadHistoryBuilder, TurnStatus, project_rollout_line};
use codex_core::context::{ContextualUserFragment, UserGoalUpdate};
use codex_history::{RolloutItem, RolloutLine};
use codex_protocol::{
    ThreadId,
    models::{InternalChatMessageMetadataPassthrough, ResponseItem},
    plan_tool::PlanItemArg,
    protocol::{EventMsg, ThreadHistoryMode, ThreadMemoryMode},
};
use codex_thread_store::*;
use serde::{Deserialize, Serialize};
use tokio::sync::{Mutex, mpsc};

use crate::manual_checkpoint::UnsupportedManualState;

/// Calls admitted to application Python code. Core cancellation text is not a
/// receipt for the external effect these callbacks may already have produced.
/// The engine and its extensions own this tracker for one Session lifetime;
/// entries grow with that Session's canonical calls and drop with the engine.
#[derive(Default)]
pub(crate) struct ManagedCalls {
    calls: std::sync::Mutex<HashSet<(String, String, String)>>,
    canonical_writer: std::sync::Weak<crate::canonical_storage::CanonicalStore>,
}

impl ManagedCalls {
    pub(crate) fn with_canonical_writer(
        canonical_writer: std::sync::Weak<crate::canonical_storage::CanonicalStore>,
    ) -> Self {
        Self {
            canonical_writer,
            ..Self::default()
        }
    }

    pub(crate) async fn record_shell_call(
        &self,
        thread: &str,
        turn: &str,
        call: &str,
    ) -> anyhow::Result<()> {
        if let Some(writer) = self.canonical_writer.upgrade() {
            writer
                .record_shell_call(ThreadId::from_string(thread)?, turn, call)
                .await?;
        }
        Ok(())
    }

    pub(crate) async fn discard_shell_call(
        &self,
        thread: &str,
        turn: &str,
        call: &str,
    ) -> anyhow::Result<()> {
        if let Some(writer) = self.canonical_writer.upgrade() {
            writer
                .discard_shell_call(ThreadId::from_string(thread)?, turn, call)
                .await?;
        }
        Ok(())
    }

    pub(crate) fn register(&self, thread: &str, turn: &str, call: &str) {
        self.calls.lock().expect("managed calls poisoned").insert((
            thread.into(),
            turn.into(),
            call.into(),
        ));
    }

    pub(crate) fn owns_output(&self, thread: ThreadId, item: &RolloutItem) -> bool {
        if let RolloutItem::ResponseItem(envelope) = item {
            return self.owns_output_item(thread, &envelope.item);
        }
        false
    }

    pub(crate) fn owns_output_item(&self, thread: ThreadId, item: &ResponseItem) -> bool {
        if let Some((call, Some(turn))) = crate::local_reconciliation::output_identity(item) {
            return self
                .calls
                .lock()
                .expect("managed calls poisoned")
                .contains(&(thread.to_string(), turn.to_owned(), call.to_owned()));
        }
        false
    }
}

pub(crate) const LOCAL_CHECKLIST_DIRECTORY: &str = "sdk-checklists";
pub(crate) const LOCAL_CHECKLIST_BUDGETS_DIRECTORY: &str = "budgets";

/// Last accepted whole-list update. Never a source of runtime configuration.
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChecklistState {
    pub thread_id: ThreadId,
    pub turn_id: String,
    pub explanation: Option<String>,
    pub plan: Vec<PlanItemArg>,
}

impl ChecklistState {
    pub fn validate(&self, id: ThreadId, history: &[RolloutItem]) -> anyhow::Result<()> {
        anyhow::ensure!(
            self.thread_id == id,
            "checkpoint checklist thread identity mismatch"
        );
        anyhow::ensure!(
            !self.turn_id.is_empty()
                && history.iter().any(|item| {
                    matches!(item, RolloutItem::EventMsg(EventMsg::TurnStarted(turn))
                    if turn.turn_id == self.turn_id)
                }),
            "checkpoint checklist turn identity missing from history"
        );
        Ok(())
    }
}

/// The native store retains complete replay records even for paginated threads.
/// Expose those records through the SDK's portable-checkpoint read boundary.
pub struct MemoryThreadStore {
    #[cfg(test)]
    pub(crate) fail_appends: std::sync::atomic::AtomicBool,
    #[cfg(test)]
    pub(crate) fail_response_observation_appends: std::sync::atomic::AtomicBool,
    #[cfg(test)]
    pub(crate) failed_response_observation_appends: std::sync::atomic::AtomicUsize,
    #[cfg(test)]
    pub(crate) completion_hold: std::sync::Mutex<Option<Arc<CompletionHold>>>,
    #[cfg(test)]
    pub(crate) next_flush_hold: std::sync::Mutex<Option<Arc<CompletionHold>>>,
    #[cfg(test)]
    pub(crate) hook_completion_flush_hold: std::sync::Mutex<Option<Arc<HookCompletionFlushHold>>>,
    inner: Arc<dyn ThreadStore>,
    archived: Mutex<HashMap<ThreadId, SystemTime>>,
    // The native in-memory store applies this policy but does not expose it.
    // Retain it with the store mutations for portable checkpoint export.
    memory_modes: Mutex<HashMap<ThreadId, ThreadMemoryMode>>,
    checklists: Mutex<HashMap<ThreadId, ChecklistState>>,
    writers: Mutex<HashMap<ThreadId, MemoryWriter>>,
    fork_boundary: OnceLock<Weak<crate::native_interception::Boundary>>,
}

/// Excludes native resume while a stored goal update settles.
pub(crate) struct StoredGoalWriter<'a> {
    store: &'a MemoryThreadStore,
    id: ThreadId,
    _writers: tokio::sync::MutexGuard<'a, HashMap<ThreadId, MemoryWriter>>,
}

impl StoredGoalWriter<'_> {
    pub(crate) async fn append(&self, update: UserGoalUpdate) -> ThreadStoreResult<()> {
        #[cfg(test)]
        if self
            .store
            .fail_appends
            .load(std::sync::atomic::Ordering::SeqCst)
        {
            return Err(checklist_error("synthetic native append failure"));
        }
        self.store
            .inner
            .append_items(AppendThreadItemsParams {
                thread_id: self.id,
                items: vec![RolloutItem::ResponseItem(
                    ContextualUserFragment::into(update).into(),
                )],
            })
            .await?;
        self.store.inner.flush_thread(self.id).await
    }
}

impl Default for MemoryThreadStore {
    fn default() -> Self {
        Self {
            #[cfg(test)]
            fail_appends: std::sync::atomic::AtomicBool::new(false),
            #[cfg(test)]
            fail_response_observation_appends: std::sync::atomic::AtomicBool::new(false),
            #[cfg(test)]
            failed_response_observation_appends: std::sync::atomic::AtomicUsize::new(0),
            #[cfg(test)]
            completion_hold: std::sync::Mutex::new(None),
            #[cfg(test)]
            next_flush_hold: std::sync::Mutex::new(None),
            #[cfg(test)]
            hook_completion_flush_hold: std::sync::Mutex::new(None),
            inner: Arc::new(InMemoryThreadStore::default()),
            archived: Mutex::new(HashMap::new()),
            memory_modes: Mutex::new(HashMap::new()),
            checklists: Mutex::new(HashMap::new()),
            writers: Mutex::new(HashMap::new()),
            fork_boundary: OnceLock::new(),
        }
    }
}

impl MemoryThreadStore {
    pub(crate) async fn stored_goal_writer(
        &self,
        id: ThreadId,
    ) -> ThreadStoreResult<StoredGoalWriter<'_>> {
        let writers = self.writers.lock().await;
        if writers.contains_key(&id) {
            return Err(checklist_error(
                "stored goal update conflicts with an open native writer",
            ));
        }
        self.inner
            .read_thread(ReadThreadParams {
                thread_id: id,
                include_archived: true,
                include_history: false,
            })
            .await?;
        Ok(StoredGoalWriter {
            store: self,
            id,
            _writers: writers,
        })
    }

    pub(crate) async fn create_checkpoint_thread(
        &self,
        params: CreateThreadParams,
        imported: &mut Vec<ThreadId>,
    ) -> ThreadStoreResult<()> {
        self.create_thread_inner(params, Some(imported)).await
    }

    async fn create_thread_inner(
        &self,
        params: CreateThreadParams,
        imported: Option<&mut Vec<ThreadId>>,
    ) -> ThreadStoreResult<()> {
        let id = params.thread_id;
        let memory_mode = params.metadata.memory_mode;
        let mut writers = self.writers.lock().await;
        let native_writer = imported.is_none();
        let mut memory_modes = self.memory_modes.lock().await;
        if imported.is_some() {
            // A running native session can create a child without the engine's
            // lifecycle lock. Check and create under the store's creation lock.
            match self
                .inner
                .read_thread(ReadThreadParams {
                    thread_id: id,
                    include_archived: true,
                    include_history: false,
                })
                .await
            {
                Err(ThreadStoreError::ThreadNotFound { .. }) => {}
                Err(error) => return Err(error),
                Ok(_) => {
                    return Err(ThreadStoreError::InvalidRequest {
                        message: "checkpoint thread already exists in this engine".to_owned(),
                    });
                }
            }
        }
        let now = SystemTime::now().into();
        let patch = ThreadMetadataPatch {
            created_at: Some(now),
            updated_at: Some(now),
            advance_recency_at: Some(now),
            model_provider: Some(params.metadata.model_provider.clone()),
            cwd: params.metadata.cwd.clone(),
            ..Default::default()
        };
        self.inner.create_thread(params).await?;
        if let Some(imported) = imported {
            imported.push(id);
        }
        self.inner
            .update_thread_metadata(UpdateThreadMetadataParams {
                thread_id: id,
                include_archived: true,
                patch,
            })
            .await?;
        memory_modes.insert(id, memory_mode);
        if native_writer {
            writers.entry(id).or_default();
        }
        Ok(())
    }

    pub(crate) fn bind_fork_boundary(&self, boundary: &Arc<crate::native_interception::Boundary>) {
        let _ = self.fork_boundary.set(Arc::downgrade(boundary));
    }

    fn numeric_fork_source(&self, thread_id: ThreadId, history: &mut Vec<RolloutItem>) -> bool {
        if !self
            .fork_boundary
            .get()
            .and_then(Weak::upgrade)
            .is_some_and(|boundary| boundary.take_numeric_fork_source(thread_id))
        {
            return false;
        }
        // A real replacement checkpoint is a model-history base, irrespective
        // of its producer or window-number schema. It is NOT an original user
        // turn; Core counts the genuine fork boundaries in its tail.
        // Supported application compaction refuses rollback-after-compaction
        // histories. If an imported/native history has one, let Core replay
        // it all; do not guess which older checkpoint survived a rollback.
        let mut checkpoint = None;
        for (index, item) in history.iter().enumerate().rev() {
            match item {
                RolloutItem::EventMsg(EventMsg::ThreadRolledBack(_)) => return false,
                RolloutItem::Compacted(compacted) => {
                    if compacted.replacement_history.is_some() {
                        checkpoint = Some(index);
                    }
                    break;
                }
                _ => {}
            }
        }
        let Some(checkpoint) = checkpoint else {
            return false;
        };
        // Preserve canonical source SessionMeta for Core's capability-root
        // selection. The copied numeric fork suffix alone is scoped, never
        // the underlying stored history, checkpoint, or a complete audit read.
        let meta = history[..checkpoint]
            .iter()
            .find_map(|item| matches!(item, RolloutItem::SessionMeta(_)).then(|| item.clone()));
        let mut tail = history.split_off(checkpoint);
        if let Some(meta) = meta {
            tail.insert(0, meta);
        }
        *history = tail;
        true
    }
    async fn append_committed(
        &self,
        params: AppendThreadItemsParams,
        writer: &mut MemoryWriter,
    ) -> ThreadStoreResult<()> {
        // Serialize the raw-event observation with the delegated append.
        // The native rollout filter removes PlanUpdate, but keeps turn boundaries.
        let mut checklists = self.checklists.lock().await;
        let id = params.thread_id;
        let last_plan =
            params
                .items
                .iter()
                .enumerate()
                .rev()
                .find_map(|(index, item)| match item {
                    RolloutItem::EventMsg(EventMsg::PlanUpdate(args)) => Some((index, args)),
                    _ => None,
                });
        let replacement = if let Some((index, args)) = last_plan {
            let history = self
                .inner
                .load_latest_model_context(LoadThreadHistoryParams {
                    thread_id: id,
                    include_archived: true,
                })
                .await?;
            let turn_id = history
                .items
                .iter()
                .chain(params.items[..index].iter())
                .rev()
                .find_map(|item| match item {
                    RolloutItem::EventMsg(EventMsg::TurnStarted(turn)) => {
                        Some(Some(turn.turn_id.clone()))
                    }
                    RolloutItem::EventMsg(EventMsg::TurnComplete(_) | EventMsg::TurnAborted(_)) => {
                        Some(None)
                    }
                    _ => None,
                })
                .flatten()
                .ok_or_else(|| ThreadStoreError::InvalidRequest {
                    message: "accepted checklist has no open native turn owner".to_owned(),
                })?;
            Some(ChecklistState {
                thread_id: id,
                turn_id,
                explanation: args.explanation.clone(),
                plan: args.plan.clone(),
            })
        } else {
            None
        };
        let mut sampling = writer.sampling.clone();
        for item in &params.items {
            sampling.observe(item);
        }
        self.inner.append_items(params).await?;
        writer.sampling = sampling;
        if let Some(replacement) = replacement {
            checklists.insert(id, replacement);
        }
        Ok(())
    }

    pub async fn memory_mode(&self, id: ThreadId) -> Option<ThreadMemoryMode> {
        self.memory_modes.lock().await.get(&id).copied()
    }

    pub async fn checklist(&self, id: ThreadId) -> ThreadStoreResult<Option<ChecklistState>> {
        let checklists = self.checklists.lock().await;
        // Missing native storage is an error, distinct from no accepted update.
        self.inner
            .read_thread(ReadThreadParams {
                thread_id: id,
                include_archived: true,
                include_history: false,
            })
            .await?;
        Ok(checklists.get(&id).cloned())
    }

    pub async fn restore_checklist(&self, checklist: ChecklistState) {
        self.checklists
            .lock()
            .await
            .insert(checklist.thread_id, checklist);
    }

    pub(crate) async fn native_mcp_suspension_ready(
        &self,
        id: ThreadId,
        captured: &codex_core::CapturedMcpApproval,
    ) -> anyhow::Result<bool> {
        let writers = self.writers.lock().await;
        let Some(writer) = writers.get(&id) else {
            return Ok(false);
        };
        Ok(self
            .native_mcp_suspension(id, writer, &captured.checkpoint_data())
            .await?
            .is_some())
    }

    pub(crate) async fn begin_native_mcp_suspension(
        &self,
        id: ThreadId,
        claim: &codex_core::ClaimedMcpApproval,
    ) -> anyhow::Result<bool> {
        let mut writers = self.writers.lock().await;
        let Some(writer) = writers.get_mut(&id) else {
            return Ok(false);
        };
        if !claim.is_live() {
            return Ok(false);
        }
        let Some(suspension) = self
            .native_mcp_suspension(id, writer, &claim.checkpoint_data())
            .await?
        else {
            return Ok(false);
        };
        if !claim.is_live() {
            return Ok(false);
        }
        writer.suspension = Some(suspension);
        Ok(true)
    }

    async fn native_mcp_suspension(
        &self,
        id: ThreadId,
        writer: &MemoryWriter,
        data: &codex_core::McpApprovalCheckpointData,
    ) -> anyhow::Result<Option<LossSuspension>> {
        if writer.suspension.is_some()
            || data.source_thread_id != id
            || writer.sampling.turn_id.as_deref() != Some(data.turn_id.as_str())
            || writer.sampling.tool_calls != 1
            || writer.sampling.completed_tool_calls != 1
        {
            return Ok(None);
        }
        self.inner.flush_thread(id).await?;
        let history = self
            .inner
            .load_latest_model_context(LoadThreadHistoryParams {
                thread_id: id,
                include_archived: true,
            })
            .await?;
        if !native_mcp_prefix_matches(&history.items, data) {
            return Ok(None);
        }
        Ok(LossSuspension::bind_calls(
            &writer.sampling,
            vec![SuspendedCall {
                turn: data.turn_id.clone(),
                call: data.call_id.clone(),
            }],
            history.items,
            false,
        ))
    }

    pub(crate) async fn begin_manual_suspension(
        &self,
        id: ThreadId,
        pending: Vec<crate::python_tools::PendingKey>,
        completed: &HashSet<String>,
    ) -> anyhow::Result<bool> {
        let mut writers = self.writers.lock().await;
        let Some(writer) = writers.get_mut(&id) else {
            return Ok(false);
        };
        let Some(suspension) = self
            .manual_suspension(id, writer, &pending, completed)
            .await?
        else {
            return Ok(false);
        };
        writer.suspension = Some(suspension);
        Ok(true)
    }

    pub(crate) async fn manual_suspension_ready(
        &self,
        id: ThreadId,
        pending: &[crate::python_tools::PendingKey],
        completed: &HashSet<String>,
    ) -> anyhow::Result<bool> {
        let writers = self.writers.lock().await;
        let Some(writer) = writers.get(&id) else {
            return Ok(false);
        };
        Ok(self
            .manual_suspension(id, writer, pending, completed)
            .await?
            .is_some())
    }

    async fn manual_suspension(
        &self,
        id: ThreadId,
        writer: &MemoryWriter,
        pending: &[crate::python_tools::PendingKey],
        completed: &HashSet<String>,
    ) -> anyhow::Result<Option<LossSuspension>> {
        // Only a complete response sampled by this live writer may authorize
        // parking. Imported history and partially sampled streams cannot.
        if writer.suspension.is_some()
            || pending.is_empty()
            || writer.sampling.tool_calls == 0
            || writer.sampling.tool_calls != writer.sampling.completed_tool_calls
            || pending.iter().any(|key| key.reply.thread != id.to_string())
        {
            return Ok(None);
        }
        self.inner.flush_thread(id).await?;
        let history = self
            .inner
            .load_latest_model_context(LoadThreadHistoryParams {
                thread_id: id,
                include_archived: true,
            })
            .await?;
        let Some(turn_id) = writer.sampling.turn_id.as_deref() else {
            return Ok(None);
        };
        if pending.iter().any(|key| key.turn != turn_id) {
            return Ok(None);
        }
        let Some(start) = history.items.iter().rposition(|item| {
            matches!(item, RolloutItem::EventMsg(EventMsg::TurnStarted(turn))
                if turn.turn_id == turn_id)
        }) else {
            return Ok(None);
        };
        let prefix = &history.items[start..];
        let pending_ids: HashSet<_> = pending.iter().map(|key| key.call.as_str()).collect();
        anyhow::ensure!(
            pending_ids.len() == pending.len(),
            UnsupportedManualState::NativeWork
        );
        // A result already in accepted Core history cannot be a parked manual
        // request. Never remove or replay that result as part of suspension.
        anyhow::ensure!(
            !prefix.iter().any(|item| matches!(item,
                RolloutItem::ResponseItem(envelope)
                if crate::local_reconciliation::output_identity(&envelope.item)
                    .is_some_and(|(call_id, _)| pending_ids.contains(call_id))
            )),
            UnsupportedManualState::NativeWork
        );
        // Sampling precedes dispatch. A not-yet-enqueued automatic sibling
        // must not be mistaken for completed work. This contains identities
        // only; Core remains the sole owner of actual result payloads.
        let mut unfinished = HashSet::new();
        // The ID space is shared by function, custom and computer calls. Remember the
        // kind until the matching receipt arrives; a receipt cannot settle
        // another kind of call, nor can a repeated ID inherit completion.
        #[derive(PartialEq)]
        enum CallKind {
            Function,
            Custom,
            Computer,
            ApplyPatch,
            Shell,
        }
        let mut calls = HashMap::new();
        let mut application_patches = HashSet::new();
        for item in prefix {
            match item {
                RolloutItem::EventMsg(EventMsg::TurnStarted(_)) => {
                    unfinished.clear();
                    calls.clear();
                    application_patches.clear();
                }
                RolloutItem::ResponseItem(item) => match &item.item {
                    ResponseItem::FunctionCall { call_id, .. }
                    | ResponseItem::CustomToolCall { call_id, .. }
                    | ResponseItem::ApplyPatchCall { call_id, .. }
                    | ResponseItem::ComputerCall { call_id, .. } => {
                        if crate::local_reconciliation::owns_application_patch(item) {
                            application_patches.insert(call_id.as_str());
                        }
                        let kind = match &item.item {
                            ResponseItem::FunctionCall { .. } => CallKind::Function,
                            ResponseItem::CustomToolCall { .. } => CallKind::Custom,
                            ResponseItem::ApplyPatchCall { .. } => CallKind::ApplyPatch,
                            _ => CallKind::Computer,
                        };
                        anyhow::ensure!(
                            calls.insert(call_id.as_str(), kind).is_none(),
                            UnsupportedManualState::NativeWork
                        );
                        unfinished.insert(call_id.as_str());
                    }
                    ResponseItem::ShellCall {
                        provider_fields, ..
                    } => {
                        let call_id = crate::local_reconciliation::shell_call_id(provider_fields)
                            .context("Shell call lacks identity")?;
                        anyhow::ensure!(
                            calls.insert(call_id.as_str(), CallKind::Shell).is_none(),
                            UnsupportedManualState::NativeWork
                        );
                        unfinished.insert(call_id.as_str());
                    }
                    ResponseItem::ShellCallOutput {
                        provider_fields, ..
                    } => {
                        let call_id = crate::local_reconciliation::shell_call_id(provider_fields)
                            .context("Shell output lacks identity")?;
                        anyhow::ensure!(
                            calls.get(call_id.as_str()) == Some(&CallKind::Shell)
                                && unfinished.remove(call_id.as_str()),
                            UnsupportedManualState::NativeWork
                        );
                    }
                    ResponseItem::FunctionCallOutput {
                        call_id: Some(call_id),
                        ..
                    } => {
                        anyhow::ensure!(
                            calls.get(call_id.as_str()) == Some(&CallKind::Function)
                                && unfinished.remove(call_id.as_str()),
                            UnsupportedManualState::NativeWork
                        );
                    }
                    ResponseItem::ApplyPatchCallOutput { call_id, .. } => {
                        anyhow::ensure!(
                            calls.get(call_id.as_str()) == Some(&CallKind::ApplyPatch)
                                && unfinished.remove(call_id.as_str()),
                            UnsupportedManualState::NativeWork
                        );
                    }
                    ResponseItem::ComputerCallOutput { call_id, .. }
                    | ResponseItem::ApplyPatchCallOutput { call_id, .. } => {
                        anyhow::ensure!(
                            calls.get(call_id.as_str()) == Some(&CallKind::Computer)
                                && unfinished.remove(call_id.as_str()),
                            UnsupportedManualState::NativeWork
                        );
                    }
                    ResponseItem::CustomToolCallOutput { call_id, .. } => {
                        anyhow::ensure!(
                            calls.get(call_id.as_str()) == Some(&CallKind::Custom)
                                && unfinished.remove(call_id.as_str()),
                            UnsupportedManualState::NativeWork
                        );
                    }
                    _ => {}
                },
                _ => {}
            }
        }
        anyhow::ensure!(
            pending
                .iter()
                .all(|key| match calls.get(key.call.as_str()) {
                    Some(CallKind::ApplyPatch) => application_patches.contains(key.call.as_str()),
                    Some(CallKind::Custom | CallKind::Computer) => false,
                    _ => true,
                }),
            UnsupportedManualState::NativeWork
        );
        if unfinished
            .iter()
            .any(|id| !completed.contains(*id) && !pending.iter().any(|key| key.call == *id))
        {
            return Ok(None);
        }
        let Some(suspension) =
            LossSuspension::bind_pending(&writer.sampling, pending.to_vec(), history.items, true)
        else {
            return Ok(None);
        };
        Ok(Some(suspension))
    }

    // Called only after Core's joined suspension outcome. On failure all Core
    // rows are committed unchanged and no portable manual state is authorized.
    pub(crate) async fn finish_manual_suspension(
        &self,
        id: ThreadId,
        suspended_turn: Option<&str>,
    ) -> ThreadStoreResult<bool> {
        let mut writers = self.writers.lock().await;
        let writer = writers
            .get_mut(&id)
            .ok_or(ThreadStoreError::ThreadNotFound { thread_id: id })?;
        let suspension = writer
            .suspension
            .take()
            .ok_or_else(|| checklist_error("no manual suspension writer lease"))?;
        let (preserve, excluded) = suspension.exclusions(suspended_turn);
        let mut ordinal = 0;
        for batch in suspension.suffix {
            let items = batch
                .into_iter()
                .filter_map(|item| {
                    let kept = !preserve || !excluded.contains(&ordinal);
                    ordinal += 1;
                    kept.then_some(item)
                })
                .collect();
            if let Err(error) = self
                .append_committed(
                    AppendThreadItemsParams {
                        thread_id: id,
                        items,
                    },
                    writer,
                )
                .await
            {
                if suspended_turn.is_some() {
                    let _ = self.inner.shutdown_thread(id).await;
                    writers.remove(&id);
                }
                return Err(error);
            }
        }
        if suspended_turn.is_some() {
            let result = self.inner.flush_thread(id).await;
            let closed = self.inner.shutdown_thread(id).await;
            writers.remove(&id);
            result?;
            closed?;
        }
        Ok(preserve)
    }
}

impl ThreadStore for MemoryThreadStore {
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }

    fn create_thread(&self, params: CreateThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(self.create_thread_inner(params, None))
    }

    fn resume_thread(
        &self,
        params: ResumeThreadParams,
    ) -> ThreadStoreFuture<'_, Arc<Vec<RolloutItem>>> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            let id = params.thread_id;
            if writers
                .get(&id)
                .is_some_and(|writer| writer.suspension.is_some())
            {
                return Err(checklist_error("cannot resume a suspending manual writer"));
            }
            let history = self.inner.resume_thread(params).await?;
            let mut writer = MemoryWriter::default();
            for item in history.iter() {
                observe_turn(&mut writer.sampling.turn_id, item);
            }
            writers.insert(id, writer);
            Ok(history)
        })
    }

    fn append_items(&self, params: AppendThreadItemsParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            #[cfg(test)]
            {
                let hook_hold = self.hook_completion_flush_hold.lock().unwrap().clone();
                if let Some(hold) = hook_hold
                    && hold.thread == params.thread_id
                {
                    for item in &params.items {
                        if let RolloutItem::EventMsg(EventMsg::HookStarted(event)) = item
                            && event.run.event_name == hold.event_name
                        {
                            // The initial turn ID is returned by admission. Wait for
                            // that binding without consuming the one-shot hold.
                            drop(hold.bound.acquire().await.expect("hook turn binding"));
                            if event.turn_id.as_deref() == hold.turn_id.get().map(String::as_str) {
                                let claimed = {
                                    let pending = self.hook_completion_flush_hold.lock().unwrap();
                                    pending
                                        .as_ref()
                                        .is_some_and(|current| Arc::ptr_eq(current, &hold))
                                        && hold
                                            .claimed
                                            .compare_exchange(
                                                false,
                                                true,
                                                std::sync::atomic::Ordering::AcqRel,
                                                std::sync::atomic::Ordering::Acquire,
                                            )
                                            .is_ok()
                                };
                                if claimed {
                                    hold.run_id
                                        .set(event.run.id.clone())
                                        .expect("one hook claim");
                                }
                            }
                        }
                        if let RolloutItem::EventMsg(EventMsg::HookCompleted(event)) = item
                            && event.run.event_name == hold.event_name
                            && event.turn_id.as_deref() == hold.turn_id.get().map(String::as_str)
                            && hold.run_id.get().is_some_and(|run| run == &event.run.id)
                        {
                            let mut completed = hold.completed.lock().unwrap();
                            if completed.is_none() {
                                *completed = Some(event.run.status.clone());
                                if event.run.status
                                    == codex_protocol::protocol::HookRunStatus::Completed
                                {
                                    // These fixtures disable post-turn compaction and legacy
                                    // notify, and submit no late input until this flush enters.
                                    // The next same-thread flush follows RegularTask's final
                                    // pending-input check and precedes finalization's drain.
                                    let mut pending = self.next_flush_hold.lock().unwrap();
                                    assert!(pending.is_none(), "fixture flush hold already armed");
                                    *pending = Some(hold.flush.clone());
                                }
                            }
                        }
                    }
                }
                let hold = self.completion_hold.lock().unwrap().clone();
                if let Some(hold) = hold
                    && hold.parent == params.thread_id
                    && params.items.iter().any(|item| matches!(item,
                        RolloutItem::EventMsg(EventMsg::ItemStarted(event))
                        if matches!(&event.item, codex_protocol::items::TurnItem::SubAgentActivity(item)
                            if item.kind == codex_protocol::protocol::SubAgentActivityKind::Completed)))
                {
                    hold.entered.add_permits(1);
                    hold.release.acquire().await.unwrap().forget();
                }
            }
            #[cfg(test)]
            if self
                .fail_response_observation_appends
                .load(std::sync::atomic::Ordering::SeqCst)
                && params
                    .items
                    .iter()
                    .any(|item| matches!(item, RolloutItem::ResponseObservationRecord(_)))
            {
                self.failed_response_observation_appends
                    .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                return Err(ThreadStoreError::Internal {
                    message: "synthetic response observation append failure".to_owned(),
                });
            }
            #[cfg(test)]
            if self.fail_appends.load(std::sync::atomic::Ordering::SeqCst) {
                return Err(ThreadStoreError::Internal {
                    message: "synthetic native append failure".to_owned(),
                });
            }
            let mut writers = self.writers.lock().await;
            let writer = writers.entry(params.thread_id).or_default();
            if let Some(suspension) = &mut writer.suspension {
                suspension.suffix.push(params.items);
                return Ok(());
            }
            self.append_committed(params, writer).await
        })
    }

    fn persist_thread(&self, id: ThreadId, context: PersistContext) -> ThreadStoreFuture<'_, ()> {
        self.inner.persist_thread(id, context)
    }

    fn flush_thread(&self, id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        #[cfg(test)]
        {
            return Box::pin(async move {
                let hold = {
                    let mut pending = self.next_flush_hold.lock().unwrap();
                    if pending.as_ref().is_some_and(|hold| hold.parent == id) {
                        pending.take()
                    } else {
                        None
                    }
                };
                if let Some(hold) = hold {
                    hold.entered.add_permits(1);
                    hold.release.acquire().await.unwrap().forget();
                }
                self.inner.flush_thread(id).await
            });
        }
        #[cfg(not(test))]
        self.inner.flush_thread(id)
    }

    fn shutdown_thread(&self, id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            if writers
                .get(&id)
                .is_some_and(|writer| writer.suspension.is_some())
            {
                return self.inner.flush_thread(id).await;
            }
            self.inner.shutdown_thread(id).await?;
            writers.remove(&id);
            Ok(())
        })
    }

    fn discard_thread(&self, id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        // Discard releases the writer; the in-memory store retains durable data.
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            self.inner.discard_thread(id).await?;
            writers.remove(&id);
            Ok(())
        })
    }

    fn load_history(
        &self,
        params: LoadThreadHistoryParams,
    ) -> ThreadStoreFuture<'_, StoredThreadHistory> {
        Box::pin(async move {
            let context = self.inner.load_latest_model_context(params).await?;
            Ok(StoredThreadHistory {
                revision: context.revision,
                thread_id: context.thread_id,
                items: context.items,
            })
        })
    }

    fn read_original_history(
        &self,
        params: LoadThreadHistoryParams,
        reader: Box<dyn codex_thread_store::OriginalHistoryReader>,
    ) -> ThreadStoreFuture<'_, codex_thread_store::OriginalHistorySnapshot> {
        self.inner.read_original_history(params, reader)
    }

    fn load_latest_model_context(
        &self,
        params: LoadThreadHistoryParams,
    ) -> ThreadStoreFuture<'_, StoredModelContext> {
        Box::pin(async move {
            let mut context = self.inner.load_latest_model_context(params).await?;
            if self.numeric_fork_source(context.thread_id, &mut context.items) {
                context.revision = None;
            }
            Ok(context)
        })
    }

    fn prepare_fork(&self, params: PrepareForkParams) -> ThreadStoreFuture<'_, PreparedFork> {
        Box::pin(async move {
            let mut history = self
                .load_history(LoadThreadHistoryParams {
                    thread_id: params.thread_id,
                    include_archived: true,
                })
                .await?
                .items;
            let end = fork_end(&history, &params.boundary)?;
            history.truncate(end);
            Ok(PreparedFork::new(
                params.thread_id,
                None,
                Arc::new(history),
                (),
            ))
        })
    }

    fn read_thread(&self, mut params: ReadThreadParams) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(async move {
            let include_history = params.include_history;
            params.include_history = false;
            let history_params = LoadThreadHistoryParams {
                thread_id: params.thread_id,
                include_archived: params.include_archived,
            };
            let mut thread = self.inner.read_thread(params).await?;
            thread.archived_at = self
                .archived
                .lock()
                .await
                .get(&thread.thread_id)
                .copied()
                .map(Into::into);
            if thread.archived_at.is_some() && !history_params.include_archived {
                return Err(ThreadStoreError::ThreadNotFound {
                    thread_id: thread.thread_id,
                });
            }
            if include_history {
                let mut history = self.load_history(history_params).await?;
                if self.numeric_fork_source(history.thread_id, &mut history.items) {
                    history.revision = None;
                }
                thread.history = Some(history);
            }
            Ok(thread)
        })
    }

    fn read_thread_by_rollout_path(
        &self,
        _params: ReadThreadByRolloutPathParams,
    ) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(async {
            Err(ThreadStoreError::Unsupported {
                operation: "read_thread_by_rollout_path",
            })
        })
    }

    fn list_threads(&self, params: ListThreadsParams) -> ThreadStoreFuture<'_, ThreadPage> {
        Box::pin(async move {
            let include_archived = params.archived;
            let mut page = self.inner.list_threads(params).await?;
            let archived = self.archived.lock().await;
            for thread in &mut page.items {
                thread.archived_at = archived.get(&thread.thread_id).copied().map(Into::into);
            }
            page.items
                .retain(|thread| thread.archived_at.is_some() == include_archived);
            Ok(page)
        })
    }

    fn update_thread_metadata(
        &self,
        params: UpdateThreadMetadataParams,
    ) -> ThreadStoreFuture<'_, Option<StoredThread>> {
        Box::pin(async move {
            let id = params.thread_id;
            let mode = params.patch.memory_mode;
            let mut memory_modes = self.memory_modes.lock().await;
            let result = self.inner.update_thread_metadata(params).await?;
            if let Some(mode) = mode {
                memory_modes.insert(id, mode);
            }
            Ok(result)
        })
    }

    fn write_thread_metadata(
        &self,
        params: UpdateThreadMetadataParams,
    ) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let id = params.thread_id;
            let mode = params.patch.memory_mode;
            let mut memory_modes = self.memory_modes.lock().await;
            self.inner.write_thread_metadata(params).await?;
            if let Some(mode) = mode {
                memory_modes.insert(id, mode);
            }
            Ok(())
        })
    }

    fn archive_thread(&self, params: ArchiveThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            self.inner
                .read_thread(ReadThreadParams {
                    thread_id: params.thread_id,
                    include_archived: true,
                    include_history: false,
                })
                .await?;
            self.archived
                .lock()
                .await
                .insert(params.thread_id, SystemTime::now());
            Ok(())
        })
    }

    fn unarchive_thread(&self, params: ArchiveThreadParams) -> ThreadStoreFuture<'_, StoredThread> {
        Box::pin(async move {
            self.archived.lock().await.remove(&params.thread_id);
            self.inner.unarchive_thread(params).await
        })
    }

    fn delete_thread(&self, params: DeleteThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let id = params.thread_id;
            let mut memory_modes = self.memory_modes.lock().await;
            let mut checklists = self.checklists.lock().await;
            self.inner.delete_thread(params).await?;
            self.archived.lock().await.remove(&id);
            memory_modes.remove(&id);
            checklists.remove(&id);
            Ok(())
        })
    }
}

// A fixture-only marker that arms one flush hold after an exact successful
// Stop/SubagentStop hook. The hook itself runs unblocked. Fixtures must disable
// post-turn compaction/legacy notify and submit input only after flush entry.
#[cfg(test)]
pub(crate) struct HookCompletionFlushHold {
    thread: ThreadId,
    event_name: codex_protocol::protocol::HookEventName,
    turn_id: OnceLock<String>,
    run_id: OnceLock<String>,
    claimed: std::sync::atomic::AtomicBool,
    completed: std::sync::Mutex<Option<codex_protocol::protocol::HookRunStatus>>,
    bound: tokio::sync::Semaphore,
    pub flush: Arc<CompletionHold>,
}

#[cfg(test)]
impl HookCompletionFlushHold {
    pub fn new(thread: ThreadId, event_name: codex_protocol::protocol::HookEventName) -> Self {
        Self {
            thread,
            event_name,
            turn_id: OnceLock::new(),
            run_id: OnceLock::new(),
            claimed: std::sync::atomic::AtomicBool::new(false),
            completed: std::sync::Mutex::new(None),
            bound: tokio::sync::Semaphore::new(0),
            flush: Arc::new(CompletionHold {
                parent: thread,
                entered: tokio::sync::Semaphore::new(0),
                release: tokio::sync::Semaphore::new(0),
            }),
        }
    }

    pub fn bind(&self, turn_id: &str) {
        assert!(!turn_id.is_empty());
        assert!(
            self.turn_id.set(turn_id.to_owned()).is_ok(),
            "hook hold already bound"
        );
        self.bound.add_permits(1);
    }

    pub fn assert_completed(&self) {
        assert_eq!(
            *self.completed.lock().unwrap(),
            Some(codex_protocol::protocol::HookRunStatus::Completed),
            "the matching fixture hook must complete successfully",
        );
    }
}

#[cfg(test)]
pub(crate) struct CompletionHold {
    pub parent: ThreadId,
    pub entered: tokio::sync::Semaphore,
    pub release: tokio::sync::Semaphore,
}

// Scoped to one native checkpoint export. Closed graph members use the same
// OS exclusion as recovery; live members retain their current native writer.
pub(crate) struct CheckpointReader<'a> {
    store: &'a LocalChecklistStore,
    closed: HashSet<ThreadId>,
    _leases: Vec<codex_rollout::WriterLockGuard>,
}

impl CheckpointReader<'_> {
    fn check(
        &self,
        writers: &HashMap<ThreadId, ChecklistWriter>,
        id: ThreadId,
    ) -> ThreadStoreResult<()> {
        self.store.check_durability()?;
        if let Some(writer) = writers.get(&id) {
            writer.check()?;
            if writer.loss.is_some() {
                return Err(checklist_error("local writer is suspending its lost owner"));
            }
        } else if !self.closed.contains(&id) {
            return Err(checklist_error(
                "native writer changed during checkpoint capture",
            ));
        }
        Ok(())
    }

    pub(crate) async fn history(&self, id: ThreadId) -> ThreadStoreResult<Vec<RolloutItem>> {
        let writers = self.store.writers.lock().await;
        self.check(&writers, id)?;
        let path = if writers.contains_key(&id) {
            self.store.inner.flush_thread(id).await?;
            self.store.inner.live_rollout_path(id).await?
        } else {
            self.store
                .inner
                .read_thread(ReadThreadParams {
                    thread_id: id,
                    include_archived: true,
                    include_history: false,
                })
                .await?
                .rollout_path
                .ok_or_else(|| checklist_error("checkpoint thread has no rollout"))?
        };
        let mut header = codex_rollout::read_session_meta_line(&path)
            .await
            .map_err(checklist_error)?;
        if header.meta.id != id {
            return Err(checklist_error(
                "checkpoint rollout belongs to another thread",
            ));
        }
        let mut history = ThreadStore::load_original_history(
            &self.store.inner,
            LoadThreadHistoryParams {
                thread_id: id,
                include_archived: true,
            },
        )
        .await?
        .items;
        let Some(RolloutItem::SessionMeta(first)) = history.first_mut() else {
            return Err(checklist_error(
                "checkpoint history has no canonical header",
            ));
        };
        // Replace one header with one header. Later copied headers and absolute
        // lineage ordinals remain intact; memory restore never follows paths.
        header.meta.history_base = None;
        *first = header;
        self.check(&writers, id)?;
        Ok(history)
    }

    pub(crate) async fn checklist(
        &self,
        id: ThreadId,
        history: &[RolloutItem],
    ) -> ThreadStoreResult<Option<ChecklistState>> {
        let writers = self.store.writers.lock().await;
        self.check(&writers, id)?;
        let path = checklist_path(&self.store.directory, id);
        let saved =
            tokio::task::spawn_blocking(move || -> ThreadStoreResult<Option<ChecklistState>> {
                let file = match std::fs::File::open(path) {
                    Ok(file) => file,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
                    Err(error) => return Err(checklist_error(error)),
                };
                serde_json::from_reader(file)
                    .map(Some)
                    .map_err(checklist_error)
            })
            .await
            .map_err(checklist_error)??;
        if let Some(saved) = &saved {
            saved.validate(id, history).map_err(checklist_error)?;
        }
        self.check(&writers, id)?;
        Ok(saved)
    }
}

/// Local checklist records have the lifetime of a native thread, including across
/// new engines. Only a successful native live append can replace a record.
pub struct LocalChecklistStore {
    pub(crate) managed_calls: Arc<ManagedCalls>,
    inner: LocalThreadStore,
    graph: Arc<MemoryAgentGraph>,
    directory: PathBuf,
    recovery_locks: Arc<codex_rollout::WriterLockCoordinator>,
    // Serialize through native writer release. Native itself owns cross-process
    // exclusion; this gate only closes the append/shutdown race in this wrapper.
    writers: Mutex<HashMap<ThreadId, ChecklistWriter>>,
    resume_model: OnceLock<(ThreadId, Option<String>)>,
    agent_resume:
        Arc<std::sync::Mutex<HashMap<ThreadId, crate::agent_profiles::ExecutorResumeAdmission>>>,
    durability_failure: std::sync::atomic::AtomicBool,
}

pub(crate) struct AgentResumeLease {
    id: ThreadId,
    store: Arc<std::sync::Mutex<HashMap<ThreadId, crate::agent_profiles::ExecutorResumeAdmission>>>,
}

impl Drop for AgentResumeLease {
    fn drop(&mut self) {
        self.store.lock().expect("agent resume").remove(&self.id);
    }
}

#[derive(Default)]
struct ChecklistWriter {
    sampling: SamplingState,
    failure: Option<String>,
    // Core must rebuild in-memory history without giving up the native writer.
    rebuilding: Option<Rebuild>,
    // Core closes during suspension, before it returns the actual outcome.
    // Keep this native writer and its uncommitted, ordered suffix until then.
    loss: Option<LossSuspension>,
    // Receipts acknowledged to the live SDK owner before Core's ordered
    // drain. Only that owner's later drain can consume an entry here.
    early: HashMap<(String, String), ResponseItem>,
}

#[derive(Default)]
struct MemoryWriter {
    sampling: SamplingState,
    suspension: Option<LossSuspension>,
}

// Positions in this live attempt, observed before the native history filter.
// Imported history can provide a turn owner, never a sampling boundary.
#[derive(Clone, Default)]
struct SamplingState {
    turn_id: Option<String>,
    tool_calls: usize,
    completed_tool_calls: usize,
}

impl SamplingState {
    fn observe(&mut self, item: &RolloutItem) {
        observe_turn(&mut self.turn_id, item);
        match item {
            RolloutItem::EventMsg(
                EventMsg::TurnStarted(_) | EventMsg::TurnComplete(_) | EventMsg::TurnAborted(_),
            ) => {
                self.tool_calls = 0;
                self.completed_tool_calls = 0;
            }
            RolloutItem::ResponseItem(envelope)
                if matches!(
                    &envelope.item,
                    ResponseItem::FunctionCall { .. }
                        | ResponseItem::CustomToolCall { .. }
                        | ResponseItem::ComputerCall { .. }
                        | ResponseItem::ApplyPatchCall { .. }
                        | ResponseItem::ShellCall { .. }
                ) =>
            {
                self.tool_calls += 1;
            }
            RolloutItem::EventMsg(EventMsg::RawResponseCompleted(_)) => {
                self.completed_tool_calls = self.tool_calls;
            }
            _ => {}
        }
    }
}

fn native_mcp_prefix_matches(
    history: &[RolloutItem],
    data: &codex_core::McpApprovalCheckpointData,
) -> bool {
    let Some(start) = history.iter().rposition(|row| {
        matches!(row,
        RolloutItem::EventMsg(EventMsg::TurnStarted(turn)) if turn.turn_id == data.turn_id)
    }) else {
        return false;
    };
    let mut calls = 0;
    for row in &history[start..] {
        match row {
            RolloutItem::EventMsg(EventMsg::TurnComplete(_) | EventMsg::TurnAborted(_)) => {
                return false;
            }
            RolloutItem::ResponseItem(item) => match &item.item {
                ResponseItem::FunctionCall { .. } => {
                    if !data.matches_canonical_call(&item.item) {
                        return false;
                    }
                    calls += 1;
                }
                ResponseItem::FunctionCallOutput { .. }
                | ResponseItem::CustomToolCall { .. }
                | ResponseItem::CustomToolCallOutput { .. }
                | ResponseItem::ComputerCall { .. }
                | ResponseItem::ComputerCallOutput { .. }
                | ResponseItem::ApplyPatchCall { .. }
                | ResponseItem::ApplyPatchCallOutput { .. }
                | ResponseItem::ShellCall { .. }
                | ResponseItem::ShellCallOutput { .. } => return false,
                ResponseItem::Message { .. } | ResponseItem::Reasoning { .. } => {}
                _ => return false,
            },
            _ => {}
        }
    }
    calls == 1
}

struct SuspendedCall {
    turn: String,
    call: String,
}

struct LossSuspension {
    turn_id: String,
    pending: Vec<SuspendedCall>,
    // RawResponseCompleted is intentionally absent from the flushed prefix.
    // This is its observed live position, not a reconstruction from usage.
    completed_tool_calls: usize,
    // The unchanged, flushed current attempt. A further sample may complete
    // in the ordered suffix before the native tool drain.
    prefix: Vec<RolloutItem>,
    suffix: Vec<Vec<RolloutItem>>,
    // Supplied only by the admitted SDK suspension path, never by worker loss.
    sdk_approval: bool,
    continuations: HashMap<String, serde_json::Value>,
}

impl LossSuspension {
    fn bind(
        sampling: &SamplingState,
        pending: Vec<crate::python_tools::PendingKey>,
        items: Vec<RolloutItem>,
    ) -> Option<Self> {
        Self::bind_pending(sampling, pending, items, false)
    }

    fn bind_pending(
        sampling: &SamplingState,
        pending: Vec<crate::python_tools::PendingKey>,
        items: Vec<RolloutItem>,
        application_patches: bool,
    ) -> Option<Self> {
        Self::bind_calls(
            sampling,
            pending
                .into_iter()
                .map(|key| SuspendedCall {
                    turn: key.turn,
                    call: key.call,
                })
                .collect(),
            items,
            application_patches,
        )
    }

    fn bind_calls(
        sampling: &SamplingState,
        pending: Vec<SuspendedCall>,
        items: Vec<RolloutItem>,
        application_patches: bool,
    ) -> Option<Self> {
        let turn_id = sampling.turn_id.clone()?;
        let Some(start) = items.iter().rposition(|item| {
            matches!(item, RolloutItem::EventMsg(EventMsg::TurnStarted(turn))
                if turn.turn_id == turn_id)
        }) else {
            return None;
        };
        for key in &pending {
            if key.turn != turn_id
                || pending
                    .iter()
                    .filter(|other| other.call == key.call)
                    .count()
                    != 1
            {
                return None;
            }
            let mut calls = 0;
            for item in &items[start..] {
                if let RolloutItem::ResponseItem(envelope) = item {
                    if matches!(&envelope.item, ResponseItem::ApplyPatchCall { call_id, .. } if *call_id == key.call)
                    {
                        if !application_patches
                            || !crate::local_reconciliation::owns_application_patch(envelope)
                        {
                            return None;
                        }
                        calls += 1;
                    } else if matches!(&envelope.item, ResponseItem::CustomToolCall { call_id, .. } | ResponseItem::ComputerCall { call_id, .. } if *call_id == key.call)
                    {
                        return None;
                    } else if match &envelope.item {
                        ResponseItem::FunctionCall { call_id, .. } => call_id == &key.call,
                        ResponseItem::ShellCall {
                            provider_fields, ..
                        } => {
                            crate::local_reconciliation::shell_call_id(provider_fields)
                                == Some(&key.call)
                        }
                        _ => false,
                    } {
                        calls += 1;
                    }
                }
            }
            if calls != 1 {
                return None;
            }
        }
        Some(Self {
            turn_id,
            pending,
            completed_tool_calls: sampling.completed_tool_calls,
            prefix: items.into_iter().skip(start).collect(),
            suffix: Vec::new(),
            sdk_approval: false,
            continuations: HashMap::new(),
        })
    }

    // Preserve every suffix row unless the joined Core outcome and exact live
    // request binding prove which cancellation rows came from this suspension.
    fn exclusions(&self, suspended_turn: Option<&str>) -> (bool, HashSet<usize>) {
        let mut preserve = suspended_turn == Some(self.turn_id.as_str());
        let mut excluded = HashSet::new();
        if preserve {
            for key in &self.pending {
                let shell = self.prefix.iter().any(|row| matches!(row,
                    RolloutItem::ResponseItem(envelope)
                    if matches!(&envelope.item, ResponseItem::ShellCall { provider_fields, .. }
                        if crate::local_reconciliation::shell_call_id(provider_fields) == Some(&key.call))));
                let patch = self.prefix.iter().any(|row| matches!(row,
                    RolloutItem::ResponseItem(envelope) if crate::local_reconciliation::owns_application_patch(envelope)
                        && matches!(&envelope.item, ResponseItem::ApplyPatchCall { call_id, .. } if *call_id == key.call)));
                let mut matches = Vec::new();
                // None until this binding's call, and again after its attempt
                // ends. A completion from another turn cannot arm this key.
                let mut sampled = None;
                let mut tool_calls = 0;
                for (ordinal, item) in self
                    .prefix
                    .iter()
                    .chain(self.suffix.iter().flatten())
                    .enumerate()
                {
                    match item {
                        // Only the real pre-filter native append can produce
                        // this marker. During the lease it stays in order.
                        RolloutItem::EventMsg(EventMsg::RawResponseCompleted(_)) => {
                            sampled = sampled.map(|_| true);
                        }
                        RolloutItem::EventMsg(
                            EventMsg::TurnStarted(_)
                            | EventMsg::TurnComplete(_)
                            | EventMsg::TurnAborted(_),
                        ) => {
                            sampled = None;
                            tool_calls = 0;
                        }
                        RolloutItem::ResponseItem(envelope) => match &envelope.item {
                            ResponseItem::FunctionCall { call_id, .. }
                            | ResponseItem::ApplyPatchCall { call_id, .. }
                                if matches!(&envelope.item, ResponseItem::FunctionCall { .. })
                                    || crate::local_reconciliation::owns_application_patch(
                                        envelope,
                                    ) =>
                            {
                                tool_calls += 1;
                                if *call_id == key.call {
                                    if shell
                                        || matches!(
                                            &envelope.item,
                                            ResponseItem::ApplyPatchCall { .. }
                                        ) != patch
                                    {
                                        preserve = false;
                                    }
                                    sampled = Some(tool_calls <= self.completed_tool_calls);
                                    if ordinal >= self.prefix.len() {
                                        // Another attempt makes attribution ambiguous.
                                        preserve = false;
                                    }
                                }
                            }
                            ResponseItem::ShellCall {
                                provider_fields, ..
                            } => {
                                tool_calls += 1;
                                if crate::local_reconciliation::shell_call_id(provider_fields)
                                    == Some(&key.call)
                                {
                                    if !shell || ordinal >= self.prefix.len() {
                                        preserve = false;
                                    }
                                    sampled = Some(tool_calls <= self.completed_tool_calls);
                                }
                            }
                            ResponseItem::ShellCallOutput {
                                provider_fields, ..
                            } => {
                                if crate::local_reconciliation::shell_call_id(provider_fields)
                                    == Some(&key.call)
                                {
                                    if !shell {
                                        preserve = false;
                                    }
                                    if sampled == Some(true) && ordinal >= self.prefix.len() {
                                        matches.push(ordinal - self.prefix.len());
                                        if envelope.item.turn_id() != Some(key.turn.as_str()) {
                                            preserve = false;
                                        }
                                    }
                                }
                            }
                            ResponseItem::CustomToolCall { call_id, .. }
                            | ResponseItem::ComputerCall { call_id, .. }
                            | ResponseItem::ApplyPatchCall { call_id, .. } => {
                                tool_calls += 1;
                                if *call_id == key.call {
                                    // A later custom call must not turn a real
                                    // function result into a cancellation receipt.
                                    preserve = false;
                                }
                            }
                            ResponseItem::ApplyPatchCallOutput {
                                call_id,
                                status,
                                internal_chat_message_metadata_passthrough: meta,
                                ..
                            } if *call_id == key.call => {
                                if patch
                                    && sampled == Some(true)
                                    && ordinal >= self.prefix.len()
                                    && *status == codex_protocol::models::ApplyPatchStatus::Failed
                                    && meta.as_ref().and_then(|m| m.turn_id.as_deref())
                                        == Some(key.turn.as_str())
                                {
                                    matches.push(ordinal - self.prefix.len());
                                } else {
                                    preserve = false;
                                }
                            }
                            ResponseItem::ComputerCallOutput { call_id, .. }
                                if *call_id == key.call =>
                            {
                                preserve = false;
                            }
                            ResponseItem::FunctionCallOutput {
                                call_id: Some(call_id),
                                ..
                            } if *call_id == key.call && patch => {
                                preserve = false;
                            }
                            ResponseItem::FunctionCallOutput {
                                call_id: Some(call_id),
                                name,
                                namespace,
                                internal_chat_message_metadata_passthrough: meta,
                                ..
                            } if *call_id == key.call
                                && !patch
                                && sampled == Some(true)
                                && ordinal >= self.prefix.len() =>
                            {
                                if shell {
                                    preserve = false;
                                }
                                // Core stamps IDs on its own drain results too.
                                // Only after native sampling completed, for an
                                // executor still unreturned on this lost writer,
                                // can suspension's drain produce this result.
                                // Earlier provider rows are retained verbatim.
                                matches.push(ordinal - self.prefix.len());
                                if name.is_some()
                                    || namespace.is_some()
                                    || meta.as_ref().and_then(|m| m.turn_id.as_deref())
                                        != Some(key.turn.as_str())
                                {
                                    preserve = false;
                                }
                            }
                            _ => {}
                        },
                        _ => {}
                    }
                }
                if matches.len() > 1 {
                    preserve = false;
                }
                excluded.extend(matches);
            }
        }
        (preserve, excluded)
    }
}

fn sdk_calls_match(bound: &[ResponseItem], requested: &[ResponseItem]) -> bool {
    bound.len() == requested.len()
        && bound.iter().zip(requested).all(|(left, right)| {
            crate::python_tools::sdk_projection(left).is_some_and(|projection| {
                Some(projection) == crate::python_tools::sdk_projection(right)
            })
        })
}

fn sdk_import_allowed(history: &[RolloutItem], requested: &[ResponseItem]) -> bool {
    let mut existing = HashSet::new();
    let mut unresolved = HashSet::new();
    let mut computers = HashSet::new();
    for item in history {
        let RolloutItem::ResponseItem(envelope) = item else {
            continue;
        };
        if let Some((call_id, _)) = crate::python_tools::sdk_projection(&envelope.item) {
            if computers.contains(call_id) {
                return false;
            }
            existing.insert(call_id);
            unresolved.insert(call_id);
        }
        match &envelope.item {
            ResponseItem::ComputerCall { call_id, .. }
            | ResponseItem::ApplyPatchCall { call_id, .. } => {
                if !existing.insert(call_id.as_str()) {
                    return false;
                }
                computers.insert(call_id.as_str());
            }
            ResponseItem::ComputerCallOutput { call_id, .. }
            | ResponseItem::ApplyPatchCallOutput { call_id, .. } => {
                computers.remove(call_id.as_str());
            }
            ResponseItem::FunctionCallOutput {
                call_id: Some(call_id),
                ..
            } => {
                unresolved.remove(call_id.as_str());
            }
            _ => {}
        }
    }
    computers.is_empty()
        && unresolved.is_empty()
        && requested.iter().all(|item| {
            crate::python_tools::sdk_projection(item)
                .is_some_and(|(call_id, _)| !existing.contains(call_id))
        })
}

// The caller of admit_sdk_reentry is the SDK's interrupted RunState resolver,
// not native turn retry/recovery. An explicit resume may call the current
// application handler to reconcile its external receipt, but only for this
// writer's exact unresolved batch. This proves identity, not an effect outcome.
fn sdk_resume_matches(history: &[RolloutItem], turn_id: &str, requested: &[ResponseItem]) -> bool {
    let Some(start) = history
        .iter()
        .rposition(|item| matches!(item, RolloutItem::EventMsg(EventMsg::TurnStarted(_))))
    else {
        return false;
    };
    if !matches!(&history[start], RolloutItem::EventMsg(EventMsg::TurnStarted(turn))
        if turn.turn_id == turn_id)
    {
        return false;
    }
    let mut seen = HashSet::new();
    let mut unresolved = Vec::new();
    let mut computers = HashSet::new();
    for (index, item) in history.iter().enumerate() {
        match item {
            RolloutItem::Compacted(_)
            | RolloutItem::EventMsg(
                EventMsg::ThreadRolledBack(_)
                | EventMsg::TurnComplete(_)
                | EventMsg::TurnAborted(_),
            ) if index > start => return false,
            RolloutItem::ResponseItem(envelope) => {
                if let Some((call_id, _)) = crate::python_tools::sdk_projection(&envelope.item) {
                    // Refuse an ambiguous original ID even if one occurrence
                    // has an output. Never revive a completed native sibling.
                    if !seen.insert(call_id) {
                        return false;
                    }
                    unresolved.push((index, envelope.item.clone()));
                }
                match &envelope.item {
                    ResponseItem::ComputerCall { call_id, .. }
                    | ResponseItem::ApplyPatchCall { call_id, .. } => {
                        if !seen.insert(call_id.as_str()) {
                            return false;
                        }
                        computers.insert(call_id.as_str());
                    }
                    ResponseItem::ComputerCallOutput { call_id, .. }
                    | ResponseItem::ApplyPatchCallOutput { call_id, .. } => {
                        computers.remove(call_id.as_str());
                    }
                    _ => {}
                }
                if let ResponseItem::FunctionCallOutput {
                    call_id: Some(call_id),
                    ..
                } = &envelope.item
                {
                    unresolved.retain(|(_, call)| {
                        crate::python_tools::sdk_projection(call)
                            .is_none_or(|(id, _)| id != call_id.as_str())
                    });
                }
            }
            _ => {}
        }
    }
    // An unresolved call outside this turn still blocks an import/resume, as
    // does omitting or reordering any unresolved original sibling.
    computers.is_empty()
        && !requested.is_empty()
        && unresolved.iter().all(|(index, _)| *index > start)
        && sdk_calls_match(
            &unresolved
                .into_iter()
                .map(|(_, call)| call)
                .collect::<Vec<_>>(),
            requested,
        )
}

// The receipt is from cold recovery; compaction is from this thread's native
// context lease. Neither can be inferred from an imported rollout item.
pub(crate) enum Rebuild {
    Receipt(RebuildReceipt),
    Compaction,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RebuildReceipt {
    pub turn_id: String,
    pub call_id: String,
    pub success: Option<bool>,
}

impl From<RebuildReceipt> for Rebuild {
    fn from(receipt: RebuildReceipt) -> Self {
        Self::Receipt(receipt)
    }
}

impl ChecklistWriter {
    fn check(&self) -> ThreadStoreResult<()> {
        if let Some(message) = &self.failure {
            return Err(ThreadStoreError::Internal {
                message: message.clone(),
            });
        }
        Ok(())
    }
}

fn checklist_error(error: impl std::fmt::Display) -> ThreadStoreError {
    ThreadStoreError::Internal {
        message: format!("native checklist storage: {error}"),
    }
}

fn checklist_path(directory: &Path, id: ThreadId) -> PathBuf {
    directory.join(format!("{id}.json"))
}

// File data has already been synced and atomically persisted. Retain the
// directory-entry sync where it was supported. Windows has no equivalent
// std operation: File::open lacks directory flags, and a read directory handle
// is not a FlushFileBuffers contract. Do not report its absence as write failure
// or promise POSIX directory crash durability there.
fn sync_local_directory(directory: &Path) -> ThreadStoreResult<()> {
    #[cfg(not(windows))]
    std::fs::File::open(directory)
        .and_then(|dir| dir.sync_all())
        .map_err(checklist_error)?;
    #[cfg(windows)]
    let _ = directory;
    Ok(())
}

fn observe_turn(turn_id: &mut Option<String>, item: &RolloutItem) {
    match item {
        RolloutItem::EventMsg(EventMsg::TurnStarted(turn)) => *turn_id = Some(turn.turn_id.clone()),
        RolloutItem::EventMsg(EventMsg::TurnComplete(_) | EventMsg::TurnAborted(_)) => {
            *turn_id = None
        }
        _ => {}
    }
}

impl LocalChecklistStore {
    pub(crate) async fn checkpoint_reader(
        &self,
        mut ids: Vec<ThreadId>,
    ) -> ThreadStoreResult<CheckpointReader<'_>> {
        ids.sort_unstable_by_key(ToString::to_string);
        ids.dedup();
        let writers = self.writers.lock().await;
        let mut closed = HashSet::new();
        let mut leases = Vec::new();
        for id in ids {
            if let Some(writer) = writers.get(&id) {
                writer.check()?;
                if writer.loss.is_some() {
                    return Err(checklist_error("local writer is suspending its lost owner"));
                }
            } else {
                let lease = tokio::task::block_in_place(|| self.recovery_locks.acquire(id))
                    .map_err(checklist_error)?;
                closed.insert(id);
                leases.push(lease);
            }
        }
        Ok(CheckpointReader {
            store: self,
            closed,
            _leases: leases,
        })
    }

    pub(crate) async fn retire_thread_owner(&self, id: ThreadId) -> ThreadStoreResult<()> {
        let mut writers = self.writers.lock().await;
        self.check_durability()?;
        if let Some(writer) = writers.get(&id) {
            writer.check()?;
            if writer.loss.is_some() {
                return Err(checklist_error("local writer is suspending its lost owner"));
            }
        }
        self.inner.retire_thread_owner(id).await?;
        writers.remove(&id);
        Ok(())
    }

    pub(crate) async fn list_threads_including_empty_preview(
        &self,
        params: codex_thread_store::ListThreadsParams,
    ) -> codex_thread_store::ThreadStoreResult<codex_thread_store::ThreadPage> {
        self.inner
            .list_threads_including_empty_preview(params)
            .await
    }

    pub(crate) fn check_durability(&self) -> ThreadStoreResult<()> {
        if self
            .durability_failure
            .load(std::sync::atomic::Ordering::Acquire)
        {
            return Err(ThreadStoreError::Internal { message: "Canonical thread persistence failed; close and reconcile saved history before reopening".into() });
        }
        Ok(())
    }

    fn retain_durability_failure<T>(&self, result: ThreadStoreResult<T>) -> ThreadStoreResult<T> {
        if result.is_err() {
            self.durability_failure
                .store(true, std::sync::atomic::Ordering::Release);
        }
        result
    }

    pub(crate) async fn live_rollout_path(&self, id: ThreadId) -> ThreadStoreResult<PathBuf> {
        self.inner.live_rollout_path(id).await
    }

    pub fn new(
        config: LocalThreadStoreConfig,
        inner: LocalThreadStore,
        graph: Arc<MemoryAgentGraph>,
    ) -> Self {
        let directory = config.codex_home.join(LOCAL_CHECKLIST_DIRECTORY);
        let recovery_locks = Arc::new(codex_rollout::WriterLockCoordinator::new(
            &config.codex_home,
        ));
        let cleanup_directory = directory.clone();
        // Called by native delete only after its reference and writer checks, for
        // both single and bulk delete. A failure leaves native deletion retryable.
        let inner = inner.with_thread_data_cleanup(move |ids| {
            let directory = cleanup_directory.clone();
            Box::pin(async move {
                // Keep removal inside native's lifecycle locks even if the
                // deleting future is cancelled during filesystem I/O.
                tokio::task::block_in_place(move || {
                    for id in ids {
                        match std::fs::remove_file(checklist_path(&directory, id)) {
                            Ok(()) => {}
                            Err(e) if e.kind() == ErrorKind::NotFound => {}
                            Err(e) => return Err(checklist_error(e)),
                        }
                        match std::fs::remove_file(checklist_path(
                            &directory.join(LOCAL_CHECKLIST_BUDGETS_DIRECTORY),
                            id,
                        )) {
                            Ok(()) => {}
                            Err(e) if e.kind() == ErrorKind::NotFound => {}
                            Err(e) => return Err(checklist_error(e)),
                        }
                    }
                    Ok(())
                })
            })
        });
        Self {
            inner,
            graph,
            directory,
            recovery_locks,
            writers: Mutex::new(HashMap::new()),
            managed_calls: Arc::new(ManagedCalls::default()),
            resume_model: OnceLock::new(),
            agent_resume: Arc::default(),
            durability_failure: std::sync::atomic::AtomicBool::new(false),
        }
    }

    pub(crate) fn stage_agent_resume(
        &self,
        id: ThreadId,
        expected: crate::agent_profiles::ExecutorResumeAdmission,
    ) -> AgentResumeLease {
        self.agent_resume
            .lock()
            .expect("agent resume")
            .insert(id, expected);
        AgentResumeLease {
            id,
            store: Arc::clone(&self.agent_resume),
        }
    }

    pub(crate) fn expect_resume_model(
        &self,
        id: ThreadId,
        model: Option<String>,
    ) -> ThreadStoreResult<()> {
        self.resume_model
            .set((id, model))
            .map_err(|_| checklist_error("Local resume model was already captured"))
    }

    pub async fn checklist(&self, id: ThreadId) -> ThreadStoreResult<Option<ChecklistState>> {
        let writers = self.writers.lock().await;
        if let Some(writer) = writers.get(&id) {
            writer.check()?;
        }
        // A missing native thread is not the absence of a PlanUpdate.
        self.inner
            .read_thread(ReadThreadParams {
                thread_id: id,
                include_archived: true,
                include_history: false,
            })
            .await?;
        let path = checklist_path(&self.directory, id);
        tokio::task::spawn_blocking(move || {
            let file = match std::fs::File::open(path) {
                Ok(file) => file,
                Err(e) if e.kind() == ErrorKind::NotFound => return Ok(None),
                Err(e) => return Err(checklist_error(e)),
            };
            let saved: ChecklistState = serde_json::from_reader(file).map_err(checklist_error)?;
            if saved.thread_id != id || saved.turn_id.is_empty() {
                return Err(checklist_error("native checklist identity mismatch"));
            }
            Ok(Some(saved))
        })
        .await
        .map_err(checklist_error)?
    }

    pub(crate) async fn commit_sdk_receipt(
        &self,
        id: ThreadId,
        key: &crate::python_tools::PendingKey,
        item: ResponseItem,
        reentry: bool,
    ) -> ThreadStoreResult<()> {
        let mut writers = self.writers.lock().await;
        let writer = writers
            .get_mut(&id)
            .ok_or(ThreadStoreError::ThreadNotFound { thread_id: id })?;
        writer.check()?;
        if writer.loss.is_some()
            || writer.rebuilding.is_some()
            || (!reentry && writer.sampling.turn_id.as_deref() != Some(key.turn.as_str()))
        {
            return Err(checklist_error(
                "SDK receipt does not belong to the active writer",
            ));
        }
        let identity = (key.turn.clone(), key.call.clone());
        if matches!(
            &item,
            ResponseItem::ApplyPatchCallOutput { .. } | ResponseItem::ComputerCallOutput { .. }
        ) && (key.reply.thread != id.to_string()
            || crate::local_reconciliation::output_identity(&item)
                != Some((key.call.as_str(), Some(key.turn.as_str()))))
        {
            return Err(checklist_error("SDK computer receipt identity mismatch"));
        }
        if writer.early.contains_key(&identity) {
            return Err(checklist_error("SDK receipt was already committed"));
        }
        // Publish the actual result, not a promise of one, under the original
        // native OS writer. This survives loss behind an earlier parked call.
        let authoritative = item.clone();
        let result = async {
            self.inner
                .append_items(AppendThreadItemsParams {
                    thread_id: id,
                    items: vec![RolloutItem::ResponseItem(
                        codex_history::ResponseItemEnvelope::new(item),
                    )],
                })
                .await?;
            self.inner.flush_thread(id).await
        }
        .await;
        if let Err(error) = result {
            // Core ignores some append failures; this writer must refuse later
            // capture/close instead of accepting a synthetic cancelled result.
            writer.failure.get_or_insert_with(|| error.to_string());
            self.durability_failure
                .store(true, std::sync::atomic::Ordering::Release);
            return Err(error);
        }
        // Consume once at Core drain, or at the idle Core history injection
        // for imported calls. Both must update Core memory without a second
        // durable receipt.
        writer.early.insert(identity, authoritative);
        Ok(())
    }

    // Seal only a joined approval suspension. Keep the historical local path
    // for reopen compatibility; this authority is never exported in checkpoints.
    fn seal_sdk_approval(
        &self,
        id: ThreadId,
        mut saved: serde_json::Value,
        calls: &[ResponseItem],
        continuations: &HashMap<String, serde_json::Value>,
    ) -> ThreadStoreResult<()> {
        let directory = self.directory.join(LOCAL_CHECKLIST_BUDGETS_DIRECTORY);
        tokio::task::block_in_place(|| {
            std::fs::create_dir_all(&directory).map_err(checklist_error)?;
            saved["sdk_parked"] = serde_json::json!({
                "calls": calls, "continuations": continuations,
            });
            let mut temp = tempfile::NamedTempFile::new_in(&directory).map_err(checklist_error)?;
            serde_json::to_writer(&mut temp, &saved).map_err(checklist_error)?;
            temp.flush().map_err(checklist_error)?;
            temp.as_file().sync_all().map_err(checklist_error)?;
            temp.persist(checklist_path(&directory, id))
                .map_err(checklist_error)?;
            sync_local_directory(&directory)
        })
    }

    // Only for explicit SDK RunState resumption. Returns the existing turn ID,
    // or None for a fresh/legacy admission; never use this for native retries.
    pub(crate) async fn admit_sdk_reentry(
        &self,
        id: ThreadId,
        calls: &[ResponseItem],
        continuations: &HashMap<String, serde_json::Value>,
    ) -> ThreadStoreResult<Option<String>> {
        let history = self.load_owned_history(id).await?;
        let writers = self.writers.lock().await;
        let writer = writers
            .get(&id)
            .ok_or(ThreadStoreError::ThreadNotFound { thread_id: id })?;
        writer.check()?;
        let directory = self.directory.join(LOCAL_CHECKLIST_BUDGETS_DIRECTORY);
        tokio::task::block_in_place(|| {
            let path = checklist_path(&directory, id);
            let mut saved: Option<serde_json::Value> = match std::fs::File::open(&path) {
                Ok(file) => Some(serde_json::from_reader(file).map_err(checklist_error)?),
                Err(error) if error.kind() == ErrorKind::NotFound => None,
                Err(error) => return Err(checklist_error(error)),
            };
            if let Some(record) = saved.as_mut() {
                if let Some(sealed) = record.get("sdk_parked") {
                    let turn = record
                        .get("turn_id")
                        .and_then(serde_json::Value::as_str)
                        .ok_or_else(|| checklist_error("SDK seal has no turn owner"))?
                        .to_owned();
                    let bound: Vec<ResponseItem> = serde_json::from_value(
                        sealed
                            .get("calls")
                            .cloned()
                            .ok_or_else(|| checklist_error("SDK seal has no calls"))?,
                    )
                    .map_err(checklist_error)?;
                    let bound_continuations: HashMap<String, serde_json::Value> =
                        serde_json::from_value(sealed.get("continuations").cloned().ok_or_else(
                            || checklist_error("SDK seal has no continuation owner"),
                        )?)
                        .map_err(checklist_error)?;
                    if (writer.sampling.turn_id.as_deref() != Some(turn.as_str())
                        && turn != format!("sdk-import-{id}"))
                        || !sdk_calls_match(&bound, calls)
                        || bound_continuations != *continuations
                    {
                        return Err(checklist_error(
                            "SDK approval does not match the native writer seal",
                        ));
                    }
                    record
                        .as_object_mut()
                        .expect("approval record")
                        .remove("sdk_parked");
                    let mut temp =
                        tempfile::NamedTempFile::new_in(&directory).map_err(checklist_error)?;
                    serde_json::to_writer(&mut temp, record).map_err(checklist_error)?;
                    temp.flush().map_err(checklist_error)?;
                    temp.as_file().sync_all().map_err(checklist_error)?;
                    temp.persist(&path).map_err(checklist_error)?;
                    sync_local_directory(&directory)?;
                    return Ok(Some(turn));
                }
                // A consumed native approval cannot establish "not started".
                // The documented SDK resume instead invokes current trusted
                // application code, which owns checking its external receipt.
                // Unknown nested continuations still require their exact seal.
                if let Some(turn) = writer.sampling.turn_id.as_deref()
                    && record.get("turn_id").and_then(serde_json::Value::as_str) == Some(turn)
                    && continuations.is_empty()
                    && sdk_resume_matches(&history, turn, calls)
                {
                    return Ok(Some(turn.to_owned()));
                }
            }
            // Imported state cannot escape an unknown effect by changing its
            // call ID, or ask for replay of any completed native sibling.
            if !sdk_import_allowed(&history, calls) {
                return Err(checklist_error(
                    "native SDK call has no reusable approval seal",
                ));
            }
            Ok(None)
        })
    }

    // Re-entry has no running Core turn to cancel. Before expiring these
    // receivers, seal only its current, known parked calls under the same
    // native writer. Early replies have already flushed their real receipts.
    pub(crate) async fn seal_sdk_reentry(
        &self,
        id: ThreadId,
        keys: &[crate::python_tools::PendingKey],
        continuations: &HashMap<String, serde_json::Value>,
    ) -> ThreadStoreResult<()> {
        let history = self.load_owned_history(id).await?;
        let writers = self.writers.lock().await;
        let writer = writers
            .get(&id)
            .ok_or(ThreadStoreError::ThreadNotFound { thread_id: id })?;
        writer.check()?;
        let turn = &keys[0].turn;
        if keys.iter().any(|key| key.turn != *turn)
            || (writer.sampling.turn_id.as_deref() != Some(turn.as_str())
                && *turn != format!("sdk-import-{id}"))
        {
            return Err(checklist_error(
                "SDK re-entry belongs to another writer turn",
            ));
        }
        let calls: Vec<_> = history
            .iter()
            .filter_map(|item| {
                let RolloutItem::ResponseItem(envelope) = item else {
                    return None;
                };
                let (call_id, _) = crate::python_tools::sdk_projection(&envelope.item)?;
                keys.iter()
                    .any(|key| key.call == call_id)
                    .then(|| envelope.item.clone())
            })
            .collect();
        if calls.len() != keys.len() {
            return Err(checklist_error(
                "SDK re-entry calls are not unique in native history",
            ));
        }
        let path = checklist_path(&self.directory.join(LOCAL_CHECKLIST_BUDGETS_DIRECTORY), id);
        let saved = tokio::task::block_in_place(|| -> ThreadStoreResult<serde_json::Value> {
            if *turn == format!("sdk-import-{id}") {
                return Ok(serde_json::json!({"turn_id": turn}));
            }
            let file = std::fs::File::open(path).map_err(checklist_error)?;
            let saved: serde_json::Value =
                serde_json::from_reader(file).map_err(checklist_error)?;
            if saved.get("turn_id").and_then(serde_json::Value::as_str) != Some(turn.as_str()) {
                return Err(checklist_error("missing original SDK turn record"));
            }
            Ok(saved)
        })?;
        self.seal_sdk_approval(id, saved, &calls, continuations)
    }

    pub async fn load_owned_history(&self, id: ThreadId) -> ThreadStoreResult<Vec<RolloutItem>> {
        let writers = self.writers.lock().await;
        let writer = writers
            .get(&id)
            .ok_or(ThreadStoreError::ThreadNotFound { thread_id: id })?;
        writer.check()?;
        if writer.loss.is_some() {
            return Err(checklist_error("local writer is suspending its lost owner"));
        }
        // Checkpoint reads run after the caller flushes. Keep shutdown excluded
        // while reading the exact rollout selected by this native writer.
        let path = self.inner.live_rollout_path(id).await?;
        Self::read_recovery_rollout(id, &path).await
    }

    async fn read_recovery_rollout(
        id: ThreadId,
        path: &Path,
    ) -> ThreadStoreResult<Vec<RolloutItem>> {
        let (items, owner, parse_errors) = codex_rollout::RolloutRecorder::load_rollout_items(path)
            .await
            .map_err(checklist_error)?;
        if owner != Some(id) || parse_errors != 0 {
            return Err(checklist_error(
                "cannot capture incomplete rollout or another thread's history",
            ));
        }
        Ok(items)
    }

    // Use the same exclusive lease as a native runtime, without a recorder task.
    pub(crate) fn reserve_reconciliation_writers(
        &self,
        ids: impl IntoIterator<Item = ThreadId>,
    ) -> ThreadStoreResult<Vec<codex_rollout::WriterLockGuard>> {
        tokio::task::block_in_place(|| {
            ids.into_iter()
                .map(|id| self.recovery_locks.acquire(id).map_err(checklist_error))
                .collect()
        })
    }

    pub(crate) async fn reconciliation_history(
        &self,
        id: ThreadId,
    ) -> ThreadStoreResult<(PathBuf, Vec<RolloutItem>)> {
        let stored = self
            .inner
            .read_thread(ReadThreadParams {
                thread_id: id,
                include_archived: true,
                include_history: false,
            })
            .await?;
        let path = stored
            .rollout_path
            .ok_or_else(|| checklist_error("native thread store returned no rollout path"))?;
        let items = Self::read_recovery_rollout(id, &path).await?;
        Ok((path, items))
    }

    pub(crate) async fn append_reconciliation(
        &self,
        id: ThreadId,
        path: &Path,
        items: Vec<RolloutItem>,
    ) -> ThreadStoreResult<Vec<RolloutItem>> {
        if items.is_empty() {
            // Reconfirm an earlier recovery replacement after an interrupted
            // directory sync, without replacing the unchanged rollout again.
            tokio::task::block_in_place(|| {
                std::fs::OpenOptions::new()
                    .write(true)
                    .open(path)
                    .map_err(checklist_error)?
                    .sync_all()
                    .map_err(checklist_error)?;
                sync_local_directory(
                    path.parent()
                        .ok_or_else(|| checklist_error("native rollout has no parent directory"))?,
                )
            })?;
            return Self::read_recovery_rollout(id, path).await;
        }
        // Keep the original bytes and ordinal offsets for native fork readers.
        // A process can die during any stage write without tearing the live rollout.
        let path = codex_rollout::materialize_rollout_for_reference(path)
            .await
            .map_err(checklist_error)?;
        let directory = path
            .parent()
            .ok_or_else(|| checklist_error("native rollout has no parent directory"))?;
        let name = format!(".sdk-recovery-{id}");
        let staged = tokio::task::block_in_place(|| -> ThreadStoreResult<_> {
            match std::fs::remove_file(directory.join(&name)) {
                Ok(()) => {}
                Err(error) if error.kind() == ErrorKind::NotFound => {}
                Err(error) => return Err(checklist_error(error)),
            }
            let file = tempfile::Builder::new()
                .prefix(&name)
                .rand_bytes(0)
                .tempfile_in(directory)
                .map_err(checklist_error)?;
            std::fs::copy(&path, file.path()).map_err(checklist_error)?;
            Ok(file)
        })?;
        for item in items {
            codex_rollout::append_rollout_item_to_path(staged.path(), &item)
                .await
                .map_err(checklist_error)?;
        }
        tokio::task::block_in_place(|| {
            staged.as_file().sync_all().map_err(checklist_error)?;
            staged.persist(&path).map_err(checklist_error)?;
            sync_local_directory(directory)
        })?;
        Self::read_recovery_rollout(id, &path).await
    }

    pub async fn begin_rebuild(
        &self,
        id: ThreadId,
        expected: &[RolloutItem],
        prepared: &[RolloutItem],
        receipt: impl Into<Rebuild>,
    ) -> ThreadStoreResult<()> {
        let rebuild = receipt.into();
        let mut writers = self.writers.lock().await;
        let writer = writers
            .get_mut(&id)
            .ok_or(ThreadStoreError::ThreadNotFound { thread_id: id })?;
        writer.check()?;
        if writer.rebuilding.is_some() || writer.loss.is_some() {
            return Err(checklist_error(
                "local thread writer is already in transfer",
            ));
        }
        // This exclusion also covers every append/flush/shutdown in this store.
        // Do not accept an earlier export if native has written since then.
        self.inner.flush_thread(id).await?;
        let path = self.inner.live_rollout_path(id).await?;
        let (current, owner, parse_errors) =
            codex_rollout::RolloutRecorder::load_rollout_items(&path)
                .await
                .map_err(checklist_error)?;
        let values = |items: &[RolloutItem]| serde_json::to_value(items).map_err(checklist_error);
        if owner != Some(id) || parse_errors != 0 || values(&current)? != values(expected)? {
            return Err(checklist_error("local recovery history changed"));
        }
        let before = values(expected)?;
        let after = values(prepared)?;
        let before = before.as_array().expect("serialized rollout items");
        let after = after.as_array().expect("serialized rollout items");
        let receipt = match &rebuild {
            Rebuild::Compaction => {
                // Only NativeThread's live ContextChange can supply this
                // candidate. Verify its captured prefix at the writer barrier.
                if after.len() != before.len() + 1 || after[..before.len()] != before[..] {
                    return Err(checklist_error("local compaction history changed"));
                }
                let Some(RolloutItem::Compacted(item)) = prepared.last() else {
                    return Err(checklist_error(
                        "local compaction requires its prepared item",
                    ));
                };
                self.inner
                    .append_items(AppendThreadItemsParams {
                        thread_id: id,
                        items: vec![RolloutItem::Compacted(item.clone())],
                    })
                    .await?;
                self.inner.flush_thread(id).await?;
                writer.rebuilding = Some(rebuild);
                return Ok(());
            }
            Rebuild::Receipt(receipt) => receipt,
        };
        if after.len() == before.len() + 1 && after[..before.len()] == before[..] {
            // Missing results stay on the ordinary append path. The only new
            // record possible here is a caller-confirmed function result.
            let Some(RolloutItem::ResponseItem(envelope)) = prepared.last() else {
                return Err(checklist_error(
                    "local recovery requires one confirmed result",
                ));
            };
            let ResponseItem::FunctionCallOutput {
                id: None,
                call_id: Some(call_id),
                name: None,
                namespace: None,
                internal_chat_message_metadata_passthrough: Some(meta),
                ..
            } = &envelope.item
            else {
                return Err(checklist_error(
                    "local recovery requires one confirmed result",
                ));
            };
            if call_id.is_empty()
                || meta.turn_id.as_deref().is_none_or(str::is_empty)
                || *call_id != receipt.call_id
                || meta.turn_id.as_deref() != Some(receipt.turn_id.as_str())
                || envelope.metadata.is_some()
                || *meta
                    != (InternalChatMessageMetadataPassthrough {
                        turn_id: meta.turn_id.clone(),
                        ..Default::default()
                    })
            {
                return Err(checklist_error("local recovery requires a bound result"));
            }
            self.inner
                .append_items(AppendThreadItemsParams {
                    thread_id: id,
                    items: vec![RolloutItem::ResponseItem(envelope.clone())],
                })
                .await?;
            self.inner.flush_thread(id).await?;
        } else if after.len() == before.len() {
            let mut differences = before
                .iter()
                .zip(after)
                .enumerate()
                .filter_map(|(index, (old, new))| (old != new).then_some(index));
            let Some(index) = differences.next() else {
                return Err(checklist_error(
                    "local recovery requires one confirmed result",
                ));
            };
            if differences.next().is_some() {
                return Err(checklist_error("local recovery cannot replace history"));
            }
            // Receipt preparation supplies only a payload, never native history
            // budgets or attribution. Keep the envelope from the checked live
            // history; accept exactly the canonical payload change within it.
            let RolloutItem::ResponseItem(provided) = &prepared[index] else {
                return Err(checklist_error("local recovery cannot replace history"));
            };
            if provided.metadata.is_some() {
                return Err(checklist_error(
                    "local recovery cannot supply native history metadata",
                ));
            }
            let RolloutItem::ResponseItem(mut allowed) = current[index].clone() else {
                return Err(checklist_error("local recovery cannot replace history"));
            };
            let ResponseItem::FunctionCallOutput {
                id: item_id,
                call_id: Some(call_id),
                name,
                namespace,
                output,
                internal_chat_message_metadata_passthrough: Some(meta),
            } = &mut allowed.item
            else {
                return Err(checklist_error("local recovery requires a recorded result"));
            };
            let Some(turn_id) = meta.turn_id.clone() else {
                return Err(checklist_error("local recovery requires a bound result"));
            };
            if item_id.is_none()
                || call_id.is_empty()
                || turn_id.is_empty()
                || *call_id != receipt.call_id
                || turn_id != receipt.turn_id
                || !matches!(
                    &output.body,
                    codex_protocol::models::FunctionCallOutputBody::Text(_)
                )
            {
                return Err(checklist_error(
                    "local recovery requires an unconfirmed text result",
                ));
            }
            *item_id = None;
            *name = None;
            *namespace = None;
            *meta = InternalChatMessageMetadataPassthrough {
                turn_id: Some(turn_id),
                ..Default::default()
            };
            if serde_json::to_value(&allowed.item).map_err(checklist_error)?
                != serde_json::to_value(&provided.item).map_err(checklist_error)?
            {
                return Err(checklist_error(
                    "local recovery result differs from recorded text",
                ));
            }
            let allowed = RolloutItem::ResponseItem(allowed);
            // Paginated projection ignores ResponseItems; it does retain byte
            // offsets. Keep the exact file length, timestamp, ordinal and inode.
            // Core's live recorder owns that inode and continues appending to it.
            // No detached write may survive cancellation of the current owner.
            tokio::task::block_in_place(|| {
                let mut file = std::fs::OpenOptions::new()
                    .read(true)
                    .write(true)
                    .open(&path)
                    .map_err(checklist_error)?;
                let mut reader = BufReader::new(&mut file);
                let mut bytes = Vec::new();
                let mut ordinal = 0;
                let mut offset = 0;
                loop {
                    bytes.clear();
                    let count = reader
                        .read_until(b'\n', &mut bytes)
                        .map_err(checklist_error)?;
                    if count == 0 {
                        return Err(checklist_error("local recovery ordinal disappeared"));
                    }
                    if bytes.iter().all(u8::is_ascii_whitespace) {
                        offset += count as u64;
                        continue;
                    }
                    let mut line =
                        codex_rollout::parse_rollout_line_bytes(&bytes).map_err(checklist_error)?;
                    if ordinal == index {
                        if serde_json::to_value(&line.item).map_err(checklist_error)?
                            != before[index]
                        {
                            return Err(checklist_error("local recovery ordinal changed"));
                        }
                        line.item = allowed;
                        let mut encoded = serde_json::to_vec(&line).map_err(checklist_error)?;
                        if encoded.len() >= count || bytes.last() != Some(&b'\n') {
                            return Err(checklist_error(
                                "confirmed result cannot retain its native byte extent",
                            ));
                        }
                        encoded.resize(count - 1, b' ');
                        encoded.push(b'\n');
                        drop(reader);
                        // Only an actual write/flush failure poisons this writer.
                        // Guards above this point have made no durable change.
                        let written = (|| -> std::io::Result<()> {
                            file.seek(SeekFrom::Start(offset))?;
                            file.write_all(&encoded)?;
                            file.flush()?;
                            file.sync_all()
                        })();
                        if let Err(error) = written {
                            writer.failure.get_or_insert_with(|| error.to_string());
                            return Err(checklist_error(error));
                        }
                        return Ok(());
                    }
                    ordinal += 1;
                    offset += count as u64;
                }
            })?;
        } else {
            return Err(checklist_error("local recovery cannot replace history"));
        }
        writer.rebuilding = Some(rebuild);
        Ok(())
    }

    pub async fn abort_rebuild(&self, id: ThreadId, owner_closed: bool) -> ThreadStoreResult<()> {
        let mut writers = self.writers.lock().await;
        if let Some(writer) = writers.get_mut(&id)
            && writer.rebuilding.is_some()
        {
            if owner_closed {
                self.inner.shutdown_thread(id).await?;
                writers.remove(&id);
            } else {
                // Return release to this live owner's ordinary shutdown path.
                // The receipt stays committed; Python fences failed adoption.
                writer.rebuilding = None;
            }
        }
        Ok(())
    }

    pub async fn begin_loss_suspension(
        &self,
        id: ThreadId,
        pending: Vec<crate::python_tools::PendingKey>,
    ) -> ThreadStoreResult<bool> {
        let mut writers = self.writers.lock().await;
        let Some(writer) = writers.get_mut(&id) else {
            return Ok(false);
        };
        writer.check()?;
        if writer.rebuilding.is_some() || writer.loss.is_some() || pending.is_empty() {
            return Ok(false);
        }
        if writer.sampling.turn_id.is_none() {
            return Ok(false);
        }
        // The live binding proves provenance, the owned prefix binds it to the
        // exact attempt. Do not infer slot/generation from a nameless output.
        self.inner.flush_thread(id).await?;
        let path = self.inner.live_rollout_path(id).await?;
        let (items, owner, parse_errors) =
            codex_rollout::RolloutRecorder::load_rollout_items(&path)
                .await
                .map_err(checklist_error)?;
        if owner != Some(id) || parse_errors != 0 {
            return Err(checklist_error("cannot suspend incomplete local history"));
        }
        writer.loss = LossSuspension::bind(&writer.sampling, pending, items);
        Ok(writer.loss.is_some())
    }

    pub(crate) async fn begin_sdk_suspension(
        &self,
        id: ThreadId,
        pending: Vec<crate::python_tools::PendingKey>,
        continuations: HashMap<String, serde_json::Value>,
    ) -> ThreadStoreResult<bool> {
        if !self.begin_loss_suspension(id, pending).await? {
            return Ok(false);
        }
        let mut writers = self.writers.lock().await;
        let writer = writers
            .get_mut(&id)
            .ok_or(ThreadStoreError::ThreadNotFound { thread_id: id })?;
        let loss = writer.loss.as_mut().expect("retained suspension writer");
        loss.sdk_approval = true;
        loss.continuations = continuations;
        Ok(true)
    }

    // Only Engine's joined native outcome can finalize this lease. No tentative
    // suffix is exported and no ordinal is allocated until native appends here.
    pub async fn finish_loss_suspension(
        &self,
        id: ThreadId,
        suspended_turn: Option<&str>,
    ) -> ThreadStoreResult<bool> {
        let mut writers = self.writers.lock().await;
        let writer = writers
            .get_mut(&id)
            .ok_or(ThreadStoreError::ThreadNotFound { thread_id: id })?;
        let loss = writer
            .loss
            .take()
            .ok_or_else(|| checklist_error("no loss writer lease"))?;
        let (preserve, excluded) = loss.exclusions(suspended_turn);
        let mut ordinal = 0;
        // Keep native response order, independent of pending-map iteration order.
        let sealed_calls: Vec<_> = loss
            .prefix
            .iter()
            .filter_map(|item| match item {
                RolloutItem::ResponseItem(envelope)
                    if matches!(&envelope.item, ResponseItem::FunctionCall { call_id, .. }
                    if loss.pending.iter().any(|key| key.call == *call_id)) =>
                {
                    Some(envelope.item.clone())
                }
                _ => None,
            })
            .collect();
        for batch in loss.suffix {
            let items = batch
                .into_iter()
                .filter_map(|item| {
                    let kept = !preserve || !excluded.contains(&ordinal);
                    ordinal += 1;
                    kept.then_some(item)
                })
                .collect();
            let result = self
                .append_committed(
                    AppendThreadItemsParams {
                        thread_id: id,
                        items,
                    },
                    &mut writers,
                )
                .await;
            if let Err(error) = result {
                // Core may log and ignore append errors. Keep it observable
                // through ordinary close, even for non-plan buffered items.
                if let Some(writer) = writers.get_mut(&id) {
                    writer.failure.get_or_insert_with(|| error.to_string());
                }
                if suspended_turn.is_some() {
                    // Core already joined and closed. No live task remains to
                    // release this retained OS writer in ordinary cleanup.
                    // Keep the original sticky failure and still report it.
                    let _ = self.inner.shutdown_thread(id).await;
                }
                return Err(error);
            }
        }
        if preserve && loss.sdk_approval {
            // Still holding the only native writer: seal publication cannot
            // race reopen or be replaced by imported checkpoint data.
            let saved = serde_json::json!({"turn_id": loss.turn_id});
            if let Err(error) =
                self.seal_sdk_approval(id, saved, &sealed_calls, &loss.continuations)
            {
                if let Some(writer) = writers.get_mut(&id) {
                    writer.failure.get_or_insert_with(|| error.to_string());
                }
                let _ = self.inner.shutdown_thread(id).await;
                return Err(error);
            }
        }
        if suspended_turn.is_some() {
            if let Err(error) = self.inner.flush_thread(id).await {
                if let Some(writer) = writers.get_mut(&id) {
                    writer.failure.get_or_insert_with(|| error.to_string());
                }
                let _ = self.inner.shutdown_thread(id).await;
                return Err(error);
            }
            self.inner.shutdown_thread(id).await?;
            if let Some(writer) = writers.remove(&id) {
                writer.check()?;
            }
        }
        Ok(preserve)
    }

    async fn append_committed(
        &self,
        mut params: AppendThreadItemsParams,
        writers: &mut HashMap<ThreadId, ChecklistWriter>,
    ) -> ThreadStoreResult<()> {
        let id = params.thread_id;
        let mut sampling = writers
            .get(&id)
            .map(|writer| writer.sampling.clone())
            .unwrap_or_default();
        let mut receipt = None;
        if let Some(writer) = writers.get_mut(&id) {
            params.items.retain(|item| {
                // Actual results enter through commit_sdk_receipt. Never let a
                // synthesized cancellation claim the external effect settled.
                let managed = self.managed_calls.owns_output(id, item);
                if let RolloutItem::ResponseItem(envelope) = item
                    && let Some((call, Some(turn))) =
                        crate::local_reconciliation::output_identity(&envelope.item)
                {
                    if let Some(accepted) = writer.early.remove(&(turn.to_owned(), call.to_owned()))
                    {
                        if !crate::local_reconciliation::same_output_payload(
                            &accepted,
                            &envelope.item,
                        ) {
                            writer.failure =
                                Some("Core drain changed an acknowledged SDK receipt".into());
                        }
                        return false;
                    }
                    return !managed;
                }
                !managed
            });
            writer.check()?;
        }
        for item in &params.items {
            sampling.observe(item);
            if let RolloutItem::EventMsg(EventMsg::PlanUpdate(args)) = item {
                receipt = Some(
                    sampling
                        .turn_id
                        .clone()
                        .map(|turn_id| ChecklistState {
                            thread_id: id,
                            turn_id,
                            explanation: args.explanation.clone(),
                            plan: args.plan.clone(),
                        })
                        .ok_or_else(|| ThreadStoreError::InvalidRequest {
                            message: "accepted checklist has no open native turn owner".to_owned(),
                        }),
                );
            }
        }
        // Native checks its live recorder before filtering even a plan-only
        // append. Never persist data after the recorder rejects this writer.
        let has_receipt = receipt.is_some();
        let result: ThreadStoreResult<()> = async {
            self.inner.append_items(params).await?;
            let writer = writers
                .get_mut(&id)
                .ok_or(ThreadStoreError::ThreadNotFound { thread_id: id })?;
            writer.sampling = sampling;
            if let Some(receipt) = receipt {
                writer.check()?;
                self.inner.flush_thread(id).await?;
                self.write_checklist(receipt?)?;
            }
            Ok(())
        }
        .await;
        // Codex logs (and otherwise ignores) append errors. An accepted
        // receipt failure must remain visible at persist/flush/read.
        if has_receipt
            && let Err(error) = &result
            && let Some(writer) = writers.get_mut(&id)
        {
            writer.failure.get_or_insert_with(|| error.to_string());
        }
        result
    }

    fn write_checklist(&self, state: ChecklistState) -> ThreadStoreResult<()> {
        // Do not detach a blocking write at an await point: cancellation could
        // otherwise release the native writer before that old write commits.
        tokio::task::block_in_place(|| {
            std::fs::create_dir_all(&self.directory).map_err(checklist_error)?;
            let mut temp =
                tempfile::NamedTempFile::new_in(&self.directory).map_err(checklist_error)?;
            serde_json::to_writer(&mut temp, &state).map_err(checklist_error)?;
            temp.flush().map_err(checklist_error)?;
            temp.as_file().sync_all().map_err(checklist_error)?;
            temp.persist(checklist_path(&self.directory, state.thread_id))
                .map_err(checklist_error)?;
            sync_local_directory(&self.directory)
        })
    }
}

impl ThreadStore for LocalChecklistStore {
    // Pinned Codex relies on this escape hatch for the native state DB, paths,
    // and resume. Keep the exact native identity while intercepting trait appends.
    fn as_any(&self) -> &dyn std::any::Any {
        self.inner.as_any()
    }

    fn create_thread(&self, params: CreateThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let id = params.thread_id;
            let parent = params.source.parent_thread_id();
            let mut writers = self.writers.lock().await;
            self.inner.create_thread(params).await?;
            // Core waits for its graph write before sending child input, but it
            // only warns on failure. Acquire the native writer first, then gate
            // startup here, where a persistence error stops the child.
            if let Some(parent) = parent {
                let admission = async {
                    // Local reads cannot resolve an unmaterialized recorder.
                    // Commit the actual header before its edge becomes durable.
                    self.inner
                        .persist_thread(id, PersistContext::Standard)
                        .await?;
                    self.retain_durability_failure(self.inner.flush_thread(id).await)?;
                    self.check_durability()?;
                    self.graph.admit(parent, id).await.map_err(checklist_error)
                }
                .await;
                if let Err(error) = admission {
                    self.inner.discard_thread(id).await?;
                    return Err(error);
                }
            }
            writers.insert(id, ChecklistWriter::default());
            Ok(())
        })
    }

    fn resume_thread(
        &self,
        params: ResumeThreadParams,
    ) -> ThreadStoreFuture<'_, Arc<Vec<RolloutItem>>> {
        Box::pin(async move {
            let id = params.thread_id;
            let mut writers = self.writers.lock().await;
            if writers.get(&id).is_some_and(|w| w.loss.is_some()) {
                return Err(checklist_error("local writer is suspending its lost owner"));
            }
            if let Some(writer) = writers.get_mut(&id)
                && let Some(rebuild) = &writer.rebuilding
            {
                writer.check()?;
                let mut history = self
                    .inner
                    .load_latest_model_context(LoadThreadHistoryParams {
                        thread_id: id,
                        include_archived: true,
                    })
                    .await?;
                if let Rebuild::Receipt(receipt) = rebuild
                    && let Some(success) = receipt.success
                {
                    // Preparation admitted only the latest matching attempt
                    // of this root turn. Paginated context can omit an older
                    // prefix, so do not use absolute rollout offsets here.
                    // Never take status or result identity from params.history.
                    let output = history.items.iter_mut().rev().find_map(|item| {
                        let RolloutItem::ResponseItem(envelope) = item else {
                            return None;
                        };
                        match &mut envelope.item {
                            ResponseItem::FunctionCallOutput {
                                id: None,
                                call_id: Some(call_id),
                                name: None,
                                namespace: None,
                                output,
                                internal_chat_message_metadata_passthrough: Some(meta),
                            } if *call_id == receipt.call_id
                                && *meta
                                    == (InternalChatMessageMetadataPassthrough {
                                        turn_id: Some(receipt.turn_id.clone()),
                                        ..Default::default()
                                    }) =>
                            {
                                Some(output)
                            }
                            _ => None,
                        }
                    });
                    output
                        .ok_or_else(|| {
                            checklist_error("admitted local receipt missing from native replay")
                        })?
                        .success = Some(success);
                }
                writer.rebuilding = None;
                let mut replay = Arc::new(history.items);
                crate::checkpoint::remove_replayed_mcp_approvals(&mut replay);
                return Ok(replay);
            }
            let mut history = self.inner.resume_thread(params).await?;
            let agent_resume = self.agent_resume.lock().expect("agent resume").remove(&id);
            if let Some(expected) = agent_resume {
                // Read the complete rollout under the native writer. Model
                // context can omit a pre-compaction profile publication.
                let validated = async {
                    let path = self.inner.live_rollout_path(id).await?;
                    let complete = Self::read_recovery_rollout(id, &path).await?;
                    expected.validate(&complete).map_err(checklist_error)?;
                    Ok::<_, ThreadStoreError>(())
                }
                .await;
                if let Err(error) = validated {
                    self.inner.shutdown_thread(id).await?;
                    return Err(error);
                }
            }
            if let Some((expected_id, expected_model)) = self.resume_model.get()
                && *expected_id == id
            {
                // The early hosted admission read cannot authorize a later
                // writer snapshot. Refuse before Core publishes stale settings.
                let current = self
                    .inner
                    .read_thread(ReadThreadParams {
                        thread_id: id,
                        include_archived: true,
                        include_history: false,
                    })
                    .await;
                let error = match current {
                    Ok(current) if current.model == *expected_model => None,
                    Ok(_) => Some(checklist_error(
                        "LocalStorage model changed during hosted resume",
                    )),
                    Err(error) => Some(error),
                };
                if let Some(error) = error {
                    self.inner.shutdown_thread(id).await?;
                    return Err(error);
                }
            }
            // Refresh under native writer ownership before Core restores child
            // metadata. Never reopen a child from caller-supplied session options.
            let restore = async {
                self.graph.hydrate(id).await?;
                if let Some(RolloutItem::SessionMeta(header)) = history.first()
                    && let Some(parent) = header.meta.source.parent_thread_id()
                {
                    self.graph.admit(parent, id).await?;
                }
                AgentGraphStoreResult::Ok(())
            }
            .await;
            if let Err(error) = restore {
                self.inner.shutdown_thread(id).await?;
                return Err(checklist_error(error));
            }
            let mut writer = ChecklistWriter::default();
            for item in history.iter() {
                observe_turn(&mut writer.sampling.turn_id, item);
            }
            writers.insert(id, writer);
            crate::checkpoint::remove_replayed_mcp_approvals(&mut history);
            Ok(history)
        })
    }

    fn append_items(&self, params: AppendThreadItemsParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let id = params.thread_id;
            let mut writers = self.writers.lock().await;
            if let Some(writer) = writers.get_mut(&id)
                && let Some(loss) = &mut writer.loss
            {
                loss.suffix.push(params.items);
                return Ok(());
            }
            self.check_durability()?;
            let result = self.append_committed(params, &mut writers).await;
            self.retain_durability_failure(result)
        })
    }

    fn persist_thread(&self, id: ThreadId, context: PersistContext) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let writers = self.writers.lock().await;
            self.retain_durability_failure(self.inner.persist_thread(id, context).await)?;
            self.check_durability()?;
            if let Some(writer) = writers.get(&id) {
                writer.check()?;
            }
            Ok(())
        })
    }

    fn flush_thread(&self, id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let writers = self.writers.lock().await;
            self.retain_durability_failure(self.inner.flush_thread(id).await)?;
            self.check_durability()?;
            if let Some(writer) = writers.get(&id) {
                writer.check()?;
            }
            Ok(())
        })
    }

    fn shutdown_thread(&self, id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            if let Some(writer) = writers.get(&id)
                && (writer.rebuilding.is_some() || writer.loss.is_some())
            {
                writer.check()?;
                self.retain_durability_failure(self.inner.flush_thread(id).await)?;
                return self.check_durability();
            }
            // Release the native OS writer even if an earlier append failed.
            self.retain_durability_failure(self.inner.shutdown_thread(id).await)?;
            if let Some(writer) = writers.remove(&id) {
                writer.check()?;
            }
            self.check_durability()
        })
    }

    fn discard_thread(&self, id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            self.inner.discard_thread(id).await?;
            if let Some(writer) = writers.remove(&id) {
                writer.check()?;
            }
            Ok(())
        })
    }
    fn default_history_mode(&self) -> ThreadHistoryMode {
        ThreadStore::default_history_mode(&self.inner)
    }

    fn stage_pending_thread_metadata(
        &self,
        thread_id: ThreadId,
        patch: ThreadMetadataPatch,
    ) -> ThreadStoreFuture<'_, ()> {
        ThreadStore::stage_pending_thread_metadata(&self.inner, thread_id, patch)
    }

    fn read_pending_thread_metadata(
        &self,
        thread_id: ThreadId,
    ) -> ThreadStoreFuture<'_, Option<ThreadMetadataPatch>> {
        ThreadStore::read_pending_thread_metadata(&self.inner, thread_id)
    }

    fn remove_pending_thread_metadata(&self, thread_id: ThreadId) -> ThreadStoreFuture<'_, ()> {
        ThreadStore::remove_pending_thread_metadata(&self.inner, thread_id)
    }

    fn load_history(
        &self,
        params: LoadThreadHistoryParams,
    ) -> ThreadStoreFuture<'_, StoredThreadHistory> {
        Box::pin(async move {
            let id = params.thread_id;
            let writers = self.writers.lock().await;
            if let Some(writer) = writers.get(&id) {
                writer.check()?;
                if writer.loss.is_some() {
                    return Err(checklist_error("local writer is suspending its lost owner"));
                }
            }
            let stored = self
                .inner
                .read_thread(ReadThreadParams {
                    thread_id: id,
                    include_archived: params.include_archived,
                    include_history: false,
                })
                .await?;
            if stored.history_mode != ThreadHistoryMode::Paginated {
                return ThreadStore::load_history(&self.inner, params).await;
            }
            // Native rejects its legacy history API for paginated threads.
            // Public SDK item/turn views need the complete saved audit, not
            // only the current model window returned by load_latest_model_context.
            // Resolve the path exclusively through this authorized native store.
            let path = stored.rollout_path.ok_or_else(|| {
                checklist_error("native thread store returned no path for stored history")
            })?;
            let (items, owner, parse_errors) =
                codex_rollout::RolloutRecorder::load_rollout_items(&path)
                    .await
                    .map_err(checklist_error)?;
            if owner != Some(id) || parse_errors != 0 {
                return Err(checklist_error(
                    "cannot read incomplete rollout or another thread's history",
                ));
            }
            Ok(StoredThreadHistory {
                thread_id: id,
                // Native does not issue a revision for this whole-file audit.
                // Only its model-context API grants a revision usable for writes.
                revision: None,
                items,
            })
        })
    }

    fn canonical_tool_output<'a>(
        &'a self,
        thread_id: ThreadId,
        output: &'a ResponseItem,
    ) -> ThreadStoreFuture<'a, Option<ResponseItem>> {
        Box::pin(async move {
            let mut writers = self.writers.lock().await;
            let writer = writers
                .get_mut(&thread_id)
                .ok_or(ThreadStoreError::ThreadNotFound { thread_id })?;
            writer.check()?;
            if writer.loss.is_some() {
                return Err(checklist_error("local writer is suspending its lost owner"));
            }
            let Some((call, Some(turn))) = crate::local_reconciliation::output_identity(output)
            else {
                return Ok(None);
            };
            let Some(accepted) = writer.early.get(&(turn.to_owned(), call.to_owned())) else {
                return Ok(None);
            };
            if crate::local_reconciliation::output_identity(accepted) != Some((call, Some(turn)))
                || !crate::local_reconciliation::same_acknowledged_output(accepted, output)
            {
                writer.failure = Some("Core drain changed an acknowledged SDK receipt".into());
                writer.check()?;
            }
            Ok(Some(accepted.clone()))
        })
    }

    fn load_original_history(
        &self,
        params: LoadThreadHistoryParams,
    ) -> ThreadStoreFuture<'_, StoredThreadHistory> {
        Box::pin(async move {
            let writers = self.writers.lock().await;
            if let Some(writer) = writers.get(&params.thread_id) {
                writer.check()?;
                if writer.loss.is_some() {
                    return Err(checklist_error("local writer is suspending its lost owner"));
                }
            }
            ThreadStore::load_original_history(&self.inner, params).await
        })
    }

    fn read_original_history(
        &self,
        params: LoadThreadHistoryParams,
        reader: Box<dyn codex_thread_store::OriginalHistoryReader>,
    ) -> ThreadStoreFuture<'_, codex_thread_store::OriginalHistorySnapshot> {
        Box::pin(async move {
            let writers = self.writers.lock().await;
            if let Some(writer) = writers.get(&params.thread_id) {
                writer.check()?;
                if writer.loss.is_some() {
                    return Err(checklist_error("local writer is suspending its lost owner"));
                }
            }
            ThreadStore::read_original_history(&self.inner, params, reader).await
        })
    }

    fn load_latest_model_context(
        &self,
        params: LoadThreadHistoryParams,
    ) -> ThreadStoreFuture<'_, StoredModelContext> {
        ThreadStore::load_latest_model_context(&self.inner, params)
    }

    fn prepare_fork(&self, params: PrepareForkParams) -> ThreadStoreFuture<'_, PreparedFork> {
        ThreadStore::prepare_fork(&self.inner, params)
    }

    fn revert_thread(&self, params: RevertThreadParams) -> ThreadStoreFuture<'_, ()> {
        ThreadStore::revert_thread(&self.inner, params)
    }

    fn read_thread(&self, params: ReadThreadParams) -> ThreadStoreFuture<'_, StoredThread> {
        ThreadStore::read_thread(&self.inner, params)
    }

    fn read_thread_by_rollout_path(
        &self,
        params: ReadThreadByRolloutPathParams,
    ) -> ThreadStoreFuture<'_, StoredThread> {
        ThreadStore::read_thread_by_rollout_path(&self.inner, params)
    }

    fn list_threads(&self, params: ListThreadsParams) -> ThreadStoreFuture<'_, ThreadPage> {
        ThreadStore::list_threads(&self.inner, params)
    }

    fn supports_thread_sections(&self) -> bool {
        ThreadStore::supports_thread_sections(&self.inner)
    }

    fn list_thread_sections(
        &self,
        params: ListThreadSectionsParams,
    ) -> ThreadStoreFuture<'_, StoredThreadSectionsPage> {
        ThreadStore::list_thread_sections(&self.inner, params)
    }

    fn create_thread_section(
        &self,
        params: CreateThreadSectionParams,
    ) -> ThreadStoreFuture<'_, StoredThreadSection> {
        ThreadStore::create_thread_section(&self.inner, params)
    }

    fn rename_thread_section(
        &self,
        params: RenameThreadSectionParams,
    ) -> ThreadStoreFuture<'_, Option<StoredThreadSection>> {
        ThreadStore::rename_thread_section(&self.inner, params)
    }

    fn delete_thread_section(
        &self,
        params: DeleteThreadSectionParams,
    ) -> ThreadStoreFuture<'_, bool> {
        ThreadStore::delete_thread_section(&self.inner, params)
    }

    fn supports_thread_attachments(&self) -> bool {
        ThreadStore::supports_thread_attachments(&self.inner)
    }

    fn copy_thread_attachments(
        &self,
        source_thread_id: ThreadId,
        destination_thread_id: ThreadId,
    ) -> ThreadStoreFuture<'_, ()> {
        ThreadStore::copy_thread_attachments(&self.inner, source_thread_id, destination_thread_id)
    }

    fn add_thread_attachment(
        &self,
        params: AddThreadAttachmentParams,
    ) -> ThreadStoreFuture<'_, AddThreadAttachmentOutcome> {
        ThreadStore::add_thread_attachment(&self.inner, params)
    }

    fn list_thread_attachments(
        &self,
        params: ListThreadAttachmentsParams,
    ) -> ThreadStoreFuture<'_, ThreadAttachmentPage> {
        ThreadStore::list_thread_attachments(&self.inner, params)
    }

    fn list_thread_attachment_threads(
        &self,
        params: ListThreadAttachmentThreadsParams,
    ) -> ThreadStoreFuture<'_, ThreadAttachmentOwnerPage> {
        ThreadStore::list_thread_attachment_threads(&self.inner, params)
    }

    fn remove_thread_attachment(
        &self,
        params: RemoveThreadAttachmentParams,
    ) -> ThreadStoreFuture<'_, RemoveThreadAttachmentOutcome> {
        ThreadStore::remove_thread_attachment(&self.inner, params)
    }

    fn supports_projects(&self) -> bool {
        ThreadStore::supports_projects(&self.inner)
    }

    fn list_projects(
        &self,
        params: ListProjectsParams,
    ) -> ThreadStoreFuture<'_, StoredProjectsPage> {
        ThreadStore::list_projects(&self.inner, params)
    }

    fn read_project(&self, project_id: String) -> ThreadStoreFuture<'_, Option<StoredProject>> {
        ThreadStore::read_project(&self.inner, project_id)
    }

    fn create_project(&self, params: CreateProjectParams) -> ThreadStoreFuture<'_, CreatedProject> {
        ThreadStore::create_project(&self.inner, params)
    }

    fn update_project(
        &self,
        params: UpdateProjectParams,
    ) -> ThreadStoreFuture<'_, Option<UpdatedProject>> {
        ThreadStore::update_project(&self.inner, params)
    }

    fn move_project(
        &self,
        params: MoveProjectParams,
    ) -> ThreadStoreFuture<'_, Option<ProjectMoveOutcome>> {
        ThreadStore::move_project(&self.inner, params)
    }

    fn delete_project(&self, project_id: String) -> ThreadStoreFuture<'_, Option<DeletedProject>> {
        ThreadStore::delete_project(&self.inner, project_id)
    }

    fn supports_paginated_history_lists(&self) -> bool {
        ThreadStore::supports_paginated_history_lists(&self.inner)
    }

    fn list_turns(&self, params: ListTurnsParams) -> ThreadStoreFuture<'_, TurnPage> {
        ThreadStore::list_turns(&self.inner, params)
    }

    fn list_items(&self, params: ListItemsParams) -> ThreadStoreFuture<'_, ItemPage> {
        ThreadStore::list_items(&self.inner, params)
    }

    fn list_timeline(&self, params: ListTimelineParams) -> ThreadStoreFuture<'_, TimelinePage> {
        ThreadStore::list_timeline(&self.inner, params)
    }

    fn search_threads(
        &self,
        params: SearchThreadsParams,
    ) -> ThreadStoreFuture<'_, ThreadSearchPage> {
        ThreadStore::search_threads(&self.inner, params)
    }

    fn search_thread_occurrences(
        &self,
        params: SearchThreadOccurrencesParams,
    ) -> ThreadStoreFuture<'_, ThreadOccurrenceSearchPage> {
        ThreadStore::search_thread_occurrences(&self.inner, params)
    }

    fn update_thread_metadata(
        &self,
        params: UpdateThreadMetadataParams,
    ) -> ThreadStoreFuture<'_, Option<StoredThread>> {
        Box::pin(async move {
            self.check_durability()?;
            self.retain_durability_failure(self.inner.update_thread_metadata(params).await)
        })
    }

    fn move_thread_to_section(
        &self,
        params: MoveThreadToSectionParams,
    ) -> ThreadStoreFuture<'_, ()> {
        ThreadStore::move_thread_to_section(&self.inner, params)
    }

    fn archive_thread(&self, params: ArchiveThreadParams) -> ThreadStoreFuture<'_, ()> {
        ThreadStore::archive_thread(&self.inner, params)
    }

    fn archive_threads(
        &self,
        params: ArchiveThreadsParams,
    ) -> ThreadStoreFuture<'_, Vec<ThreadId>> {
        ThreadStore::archive_threads(&self.inner, params)
    }

    fn unarchive_thread(&self, params: ArchiveThreadParams) -> ThreadStoreFuture<'_, StoredThread> {
        ThreadStore::unarchive_thread(&self.inner, params)
    }

    fn delete_thread(&self, params: DeleteThreadParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let id = params.thread_id;
            let mut writers = self.writers.lock().await;
            ThreadStore::delete_thread(&self.inner, params).await?;
            writers.remove(&id);
            Ok(())
        })
    }

    fn delete_threads(&self, params: DeleteThreadsParams) -> ThreadStoreFuture<'_, ()> {
        Box::pin(async move {
            let ids = params.thread_ids.clone();
            let mut writers = self.writers.lock().await;
            ThreadStore::delete_threads(&self.inner, params).await?;
            for id in ids {
                writers.remove(&id);
            }
            Ok(())
        })
    }
}

fn fork_end(history: &[RolloutItem], boundary: &ForkBoundary) -> Result<usize, ThreadStoreError> {
    let turn_id = match boundary {
        ForkBoundary::Latest => return Ok(history.len()),
        ForkBoundary::BeforeTurn(id) | ForkBoundary::ThroughTurn(id) => id,
    };
    let start = match history.first() {
        Some(RolloutItem::SessionMeta(header)) => {
            header.meta.subagent_history_start_ordinal.unwrap_or(0)
        }
        _ => 0,
    };
    let mut builder = ThreadHistoryBuilder::new();
    let mut position: Option<(usize, Option<usize>)> = None;
    for (index, item) in history
        .iter()
        .enumerate()
        .filter(|(index, _)| *index as u64 >= start)
    {
        let changes = builder.handle_rollout_item_with_changes(item);
        if changes.removed_turn_ids.contains(turn_id) {
            position = None;
        }
        let projected = project_rollout_line(&RolloutLine {
            timestamp: String::new(),
            ordinal: Some(index as u64),
            item: item.clone(),
        });
        for turn in projected.changed_turns {
            if turn.turn_id != *turn_id {
                continue;
            }
            let (_, end) = position.get_or_insert((index, None));
            // Match the native materializer: preserve the first start and terminal
            // boundaries instead of extending a finished turn with later snapshots.
            if end.is_none() && turn.status != TurnStatus::InProgress {
                *end = Some(index + 1);
            }
        }
    }
    let result = match (boundary, position) {
        (ForkBoundary::BeforeTurn(_), Some((start, end))) if end != Some(start + 1) => Some(start),
        (ForkBoundary::ThroughTurn(_), Some((_, end))) => end,
        _ => None,
    };
    result.ok_or_else(|| ThreadStoreError::InvalidRequest {
        message: "fork boundary must identify a visible turn with a persisted start or completion"
            .to_string(),
    })
}

#[derive(Clone, Copy)]
pub struct SpawnEdge {
    pub parent: ThreadId,
    pub status: ThreadSpawnEdgeStatus,
    pub closed_at: Option<i64>,
}

#[derive(Clone, serde::Serialize)]
pub struct GraphEvent {
    pub kind: &'static str,
    pub subagent_id: ThreadId,
    pub parent_agent_id: ThreadId,
    pub status: &'static str,
    pub closed_at: Option<i64>,
}

impl GraphEvent {
    fn new(kind: &'static str, child: ThreadId, edge: SpawnEdge) -> Self {
        Self {
            kind,
            subagent_id: child,
            parent_agent_id: edge.parent,
            status: if edge.status == ThreadSpawnEdgeStatus::Closed {
                "closed"
            } else {
                "active"
            },
            closed_at: edge.closed_at,
        }
    }
}

#[derive(Default)]
struct GraphState {
    edges: HashMap<ThreadId, GraphEntry>,
    subscribers: Vec<(ThreadId, mpsc::UnboundedSender<GraphEvent>)>,
}

#[derive(Clone, Copy)]
struct GraphEntry {
    edge: SpawnEdge,
    // Admission commits a readable native record before the thread is ready.
    // Core's upsert or cancelled-spawn cleanup publishes the first transition.
    published: bool,
}

#[derive(Default)]
pub struct MemoryAgentGraph {
    state: Mutex<GraphState>,
    local: Option<LocalAgentGraphStore>,
}

impl MemoryAgentGraph {
    pub fn local(state_db: Arc<codex_state::StateRuntime>) -> Self {
        Self {
            state: Mutex::new(GraphState::default()),
            local: Some(LocalAgentGraphStore::new(state_db)),
        }
    }

    async fn admit(&self, parent: ThreadId, child: ThreadId) -> AgentGraphStoreResult<()> {
        let mut state = self.state.lock().await;
        if let Some(local) = &self.local {
            local
                .upsert_thread_spawn_edge(parent, child, ThreadSpawnEdgeStatus::Open)
                .await?;
            state.edges.entry(child).or_insert(GraphEntry {
                edge: SpawnEdge {
                    parent,
                    status: ThreadSpawnEdgeStatus::Open,
                    closed_at: None,
                },
                published: false,
            });
        }
        // Core's later upsert publishes the live event once it has finished
        // constructing the thread. Admission only owns durable records.
        Ok(())
    }

    /// Load persisted topology while holding the same lock used for subscription
    /// snapshots and writes. The pinned store exposes status but no close time.
    pub async fn hydrate(&self, root: ThreadId) -> AgentGraphStoreResult<()> {
        let Some(local) = &self.local else {
            return Ok(());
        };
        let mut state = self.state.lock().await;
        let mut queue = std::collections::VecDeque::from([root]);
        let mut visited = HashSet::from([root]);
        // Stage the complete read before changing the cache. A failed read must
        // not publish a partial tree or delete the last known good snapshot.
        let mut loaded = HashMap::new();
        while let Some(parent) = queue.pop_front() {
            for status in [ThreadSpawnEdgeStatus::Open, ThreadSpawnEdgeStatus::Closed] {
                for child in local
                    .list_thread_spawn_children(parent, Some(status))
                    .await?
                {
                    if visited.insert(child) {
                        let closed_at = state
                            .edges
                            .get(&child)
                            .filter(|entry| {
                                entry.edge.parent == parent && entry.edge.status == status
                            })
                            .and_then(|entry| entry.edge.closed_at);
                        loaded.insert(
                            child,
                            GraphEntry {
                                edge: SpawnEdge {
                                    parent,
                                    status,
                                    closed_at,
                                },
                                published: true,
                            },
                        );
                        queue.push_back(child);
                    }
                }
            }
        }
        for child in descendants(&state.edges, root) {
            state.edges.remove(&child);
        }
        state.edges.extend(loaded);
        Ok(())
    }

    pub async fn edge(&self, child: ThreadId) -> Option<SpawnEdge> {
        self.state
            .lock()
            .await
            .edges
            .get(&child)
            .map(|entry| entry.edge)
    }

    pub async fn restore_edge(&self, child: ThreadId, edge: SpawnEdge) {
        // Import preserves unknown historical close times and emits no transitions.
        self.state.lock().await.edges.insert(
            child,
            GraphEntry {
                edge,
                published: true,
            },
        );
    }

    pub(crate) async fn remove_checkpoint_edges(&self, imported: &[ThreadId]) {
        let mut state = self.state.lock().await;
        for id in imported {
            state.edges.remove(id);
        }
    }

    pub async fn subscribe(&self, root: ThreadId) -> mpsc::UnboundedReceiver<GraphEvent> {
        let (sender, receiver) = mpsc::unbounded_channel();
        let mut state = self.state.lock().await;
        for child in descendants(&state.edges, root) {
            let entry = state.edges[&child];
            if entry.published {
                let _ = sender.send(GraphEvent::new("snapshot", child, entry.edge));
            }
        }
        // Snapshot and subscription share the mutation lock, so neither gaps nor duplicates occur.
        state.subscribers.push((root, sender));
        receiver
    }

    async fn update(
        &self,
        parent: Option<ThreadId>,
        child: ThreadId,
        status: ThreadSpawnEdgeStatus,
    ) -> AgentGraphStoreResult<()> {
        let mut state = self.state.lock().await;
        let previous = state.edges.get(&child).copied();
        if let Some(local) = &self.local {
            match parent {
                Some(parent) => {
                    local
                        .upsert_thread_spawn_edge(parent, child, status)
                        .await?
                }
                None => local.set_thread_spawn_edge_status(child, status).await?,
            }
        }
        let Some(parent) = parent.or(previous.map(|entry| entry.edge.parent)) else {
            return Ok(());
        };
        let changed = previous.is_none_or(|entry| !entry.published || entry.edge.status != status);
        let edge = SpawnEdge {
            parent,
            status,
            closed_at: if !changed {
                previous.and_then(|entry| entry.edge.closed_at)
            } else if status == ThreadSpawnEdgeStatus::Closed {
                Some(
                    SystemTime::now()
                        .duration_since(SystemTime::UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs() as i64,
                )
            } else {
                None
            },
        };
        state.edges.insert(
            child,
            GraphEntry {
                edge,
                published: true,
            },
        );
        if !changed {
            return Ok(());
        }
        let kind = if previous.is_none()
            || (status == ThreadSpawnEdgeStatus::Open
                && previous.is_some_and(|entry| !entry.published))
        {
            "created"
        } else if status == ThreadSpawnEdgeStatus::Closed {
            "closed"
        } else {
            "active"
        };
        let event = GraphEvent::new(kind, child, edge);
        let GraphState { edges, subscribers } = &mut *state;
        subscribers.retain(|(root, sender)| {
            !sender.is_closed()
                && (!is_descendant(edges, *root, child) || sender.send(event.clone()).is_ok())
        });
        Ok(())
    }
}

fn is_descendant(edges: &HashMap<ThreadId, GraphEntry>, root: ThreadId, child: ThreadId) -> bool {
    let mut id = child;
    let mut visited = HashSet::new();
    while visited.insert(id) {
        let Some(entry) = edges.get(&id) else {
            return false;
        };
        if entry.edge.parent == root {
            return child != root;
        }
        id = entry.edge.parent;
    }
    false
}

fn children(
    edges: &HashMap<ThreadId, GraphEntry>,
    parent: ThreadId,
    status: Option<ThreadSpawnEdgeStatus>,
) -> Vec<ThreadId> {
    let mut children: Vec<_> = edges
        .iter()
        .filter_map(|(child, entry)| {
            (entry.edge.parent == parent && status.is_none_or(|status| status == entry.edge.status))
                .then_some(*child)
        })
        .collect();
    children.sort_by_key(ToString::to_string);
    children
}

fn descendants(edges: &HashMap<ThreadId, GraphEntry>, root: ThreadId) -> Vec<ThreadId> {
    let mut visited = HashSet::from([root]);
    let mut layer = vec![root];
    let mut descendants = Vec::new();
    while !layer.is_empty() {
        let mut next: Vec<_> = layer
            .into_iter()
            .flat_map(|parent| children(edges, parent, None))
            .filter(|child| visited.insert(*child))
            .collect();
        next.sort_by_key(ToString::to_string);
        descendants.extend(&next);
        layer = next;
    }
    descendants
}

impl AgentGraphStore for MemoryAgentGraph {
    fn upsert_thread_spawn_edge(
        &self,
        parent: ThreadId,
        child: ThreadId,
        status: ThreadSpawnEdgeStatus,
    ) -> AgentGraphStoreFuture<'_, ()> {
        Box::pin(self.update(Some(parent), child, status))
    }

    fn set_thread_spawn_edge_status(
        &self,
        child: ThreadId,
        status: ThreadSpawnEdgeStatus,
    ) -> AgentGraphStoreFuture<'_, ()> {
        Box::pin(self.update(None, child, status))
    }

    fn list_thread_spawn_children(
        &self,
        parent: ThreadId,
        status: Option<ThreadSpawnEdgeStatus>,
    ) -> AgentGraphStoreFuture<'_, Vec<ThreadId>> {
        Box::pin(async move {
            let state = self.state.lock().await;
            if let Some(local) = &self.local {
                local.list_thread_spawn_children(parent, status).await
            } else {
                Ok(children(&state.edges, parent, status))
            }
        })
    }

    fn list_thread_spawn_descendants(
        &self,
        root: ThreadId,
        status: Option<ThreadSpawnEdgeStatus>,
    ) -> AgentGraphStoreFuture<'_, Vec<ThreadId>> {
        Box::pin(async move {
            let state = self.state.lock().await;
            if let Some(local) = &self.local {
                return local.list_thread_spawn_descendants(root, status).await;
            }
            Ok(descendants(&state.edges, root)
                .into_iter()
                .filter(|id| status.is_none_or(|status| state.edges[id].edge.status == status))
                .collect())
        })
    }
}

#[cfg(test)]
#[path = "stored_goal_tests.rs"]
mod stored_goal_tests;

#[cfg(test)]
mod managed_receipt_tests {
    use super::*;
    use codex_core_api::{AbsolutePathBuf, SqliteConfig};
    use codex_protocol::models::{BaseInstructions, InternalChatMessageMetadataPassthrough};
    use codex_protocol::protocol::SessionSource;
    use serde_json::json;

    fn call(id: &str) -> ResponseItem {
        serde_json::from_value(json!({
            "type":"function_call", "name":"dispatch", "namespace":"_agents_sdk",
            "call_id":id, "arguments": json!({"original":{
                "type":"function_call","name":"shell_command", "call_id":id,"arguments":"{invalid"
            }}).to_string()
        }))
        .unwrap()
    }

    fn rollout(item: ResponseItem) -> RolloutItem {
        RolloutItem::ResponseItem(codex_history::ResponseItemEnvelope::new(item))
    }

    fn key(owner: ThreadId, id: &str) -> crate::python_tools::PendingKey {
        crate::python_tools::PendingKey {
            reply: crate::python_tools::ReplyKey {
                thread: owner.to_string(),
                generation: 1,
                slot: 0,
                instance: None,
            },
            turn: format!("sdk-import-{owner}"),
            call: id.into(),
        }
    }

    fn receipt(key: &crate::python_tools::PendingKey) -> ResponseItem {
        let response = serde_json::from_value(json!({
            "contentItems":[{"type":"inputText","text":"actual result"}], "success":true
        }))
        .unwrap();
        crate::python_tools::PythonOutput(response).receipt(key)
    }

    fn custom_call(id: &str) -> ResponseItem {
        serde_json::from_value(json!({
            "type": "custom_tool_call", "call_id": id,
            "name": "apply_patch", "input": "*** Begin Patch\n*** End Patch"
        }))
        .unwrap()
    }

    fn custom_receipt(id: &str) -> ResponseItem {
        serde_json::from_value(json!({
            "type": "custom_tool_call_output", "call_id": id,
            "output": "Patch applied"
        }))
        .unwrap()
    }

    fn turn(id: &str) -> RolloutItem {
        RolloutItem::EventMsg(
            serde_json::from_value(json!({
                "type":"task_started", "turn_id":id, "root_turn_id":id
            }))
            .unwrap(),
        )
    }

    #[tokio::test]
    async fn memory_manual_suspension_keeps_ordered_native_receipts_and_requires_live_sampling() {
        let store = MemoryThreadStore::default();
        let id = ThreadId::new();
        let pending = key(id, "manual");
        let sibling = key(id, "automatic");
        let completed = HashSet::from(["automatic".to_owned(), "patch".to_owned()]);
        store
            .create_thread(CreateThreadParams {
                sdk_mcp_source_required: false,
                creator_user_id: None,
                creator_account_id: None,
                session_id: id.into(),
                thread_id: id,
                extra_config: None,
                forked_from_id: None,
                parent_thread_id: None,
                source: SessionSource::Exec,
                thread_source: None,
                originator: "memory-manual-suspension-test".into(),
                base_instructions: BaseInstructions::default(),
                dynamic_tools: vec![],
                selected_capability_roots: vec![],
                multi_agent_version: None,
                history_mode: ThreadHistoryMode::Legacy,
                history_base: None,
                subagent_history_start_ordinal: None,
                initial_window_id: id.to_string(),
                runtime_workspace_roots: None,
                metadata: ThreadPersistenceMetadata {
                    cwd: None,
                    model_provider: "fixture-provider".into(),
                    memory_mode: ThreadMemoryMode::Disabled,
                },
            })
            .await
            .unwrap();
        store
            .append_items(AppendThreadItemsParams {
                thread_id: id,
                items: vec![
                    turn(&pending.turn),
                    rollout(call("manual")),
                    rollout(call("automatic")),
                    rollout(custom_call("patch")),
                ],
            })
            .await
            .unwrap();

        // The call may have arrived while the provider is still streaming.
        assert!(
            !store
                .manual_suspension_ready(id, &[pending.clone()], &completed)
                .await
                .unwrap()
        );
        assert!(
            !store
                .begin_manual_suspension(id, vec![pending.clone()], &completed)
                .await
                .unwrap()
        );
        store
            .append_items(AppendThreadItemsParams {
                thread_id: id,
                items: vec![RolloutItem::EventMsg(EventMsg::RawResponseCompleted(
                    codex_protocol::protocol::RawResponseCompletedEvent {
                        response_id: "sampled".into(),
                        token_usage: None,
                        usage_metadata: None,
                        model_attempt_id: None,
                    },
                ))],
            })
            .await
            .unwrap();
        assert!(
            !store
                .manual_suspension_ready(id, &[pending.clone()], &HashSet::new())
                .await
                .unwrap()
        );
        // A sampled custom call still needs its own native completion. A
        // completed function sibling cannot lend it admission.
        assert!(
            !store
                .manual_suspension_ready(
                    id,
                    &[pending.clone()],
                    &HashSet::from(["automatic".to_owned()]),
                )
                .await
                .unwrap()
        );
        assert!(
            store
                .manual_suspension_ready(id, &[pending.clone()], &completed)
                .await
                .unwrap()
        );
        assert!(
            store
                .manual_suspension_ready(id, &[pending.clone(), pending.clone()], &completed)
                .await
                .unwrap_err()
                .is::<UnsupportedManualState>()
        );
        let foreign = key(ThreadId::new(), "manual");
        assert!(
            !store
                .manual_suspension_ready(id, &[foreign], &completed)
                .await
                .unwrap()
        );
        assert!(
            store
                .begin_manual_suspension(id, vec![pending.clone()], &completed)
                .await
                .unwrap()
        );
        assert!(
            !store
                .manual_suspension_ready(id, &[pending.clone()], &completed)
                .await
                .unwrap()
        );

        assert!(
            !store
                .begin_manual_suspension(id, vec![pending.clone()], &completed)
                .await
                .unwrap()
        );

        let automatic_result = receipt(&sibling);
        store
            .append_items(AppendThreadItemsParams {
                thread_id: id,
                // Core drains in response order, after automatic siblings have
                // already returned. Only the still-bound manual row is cancelled.
                items: vec![
                    rollout(receipt(&pending)),
                    rollout(automatic_result.clone()),
                    rollout(custom_receipt("patch")),
                ],
            })
            .await
            .unwrap();
        store.shutdown_thread(id).await.unwrap();
        let request = LoadThreadHistoryParams {
            thread_id: id,
            include_archived: true,
        };
        let prefix = store.load_history(request.clone()).await.unwrap();
        assert!(!prefix.items.iter().any(|item| matches!(item,
            RolloutItem::ResponseItem(envelope)
                if matches!(&envelope.item,
                    ResponseItem::FunctionCallOutput { .. }
                    | ResponseItem::CustomToolCallOutput { .. })
        )));
        assert!(
            store
                .finish_manual_suspension(id, Some(&pending.turn))
                .await
                .unwrap()
        );
        let finished = store.load_history(request).await.unwrap();
        let receipts: Vec<_> = finished
            .items
            .iter()
            .filter_map(|item| match item {
                RolloutItem::ResponseItem(envelope)
                    if matches!(
                        &envelope.item,
                        ResponseItem::FunctionCallOutput { .. }
                            | ResponseItem::CustomToolCallOutput { .. }
                    ) =>
                {
                    Some(serde_json::to_value(&envelope.item).unwrap())
                }
                _ => None,
            })
            .collect();
        assert_eq!(
            receipts,
            vec![
                serde_json::to_value(automatic_result).unwrap(),
                serde_json::to_value(custom_receipt("patch")).unwrap(),
            ]
        );
    }

    #[test]
    fn custom_call_before_manual_uses_the_same_live_sampling_position() {
        let pending = key(ThreadId::new(), "manual");
        let mut sampling = SamplingState::default();
        let mut prefix = vec![turn(&pending.turn), rollout(custom_call("patch"))];
        for item in &prefix {
            sampling.observe(item);
        }
        sampling.observe(&RolloutItem::EventMsg(EventMsg::RawResponseCompleted(
            codex_protocol::protocol::RawResponseCompletedEvent {
                response_id: "first".into(),
                token_usage: None,
                usage_metadata: None,
                model_attempt_id: None,
            },
        )));
        // The manual call belongs to a later, still-unsampled response. The
        // older custom call must not make its drain look like a parked reply.
        prefix.push(rollout(call("manual")));
        sampling.observe(prefix.last().unwrap());
        let mut suspension =
            LossSuspension::bind(&sampling, vec![pending.clone()], prefix).unwrap();
        suspension.suffix.push(vec![
            rollout(receipt(&pending)),
            rollout(custom_receipt("patch")),
        ]);
        let (preserve, excluded) = suspension.exclusions(Some(&pending.turn));
        assert!(preserve);
        assert!(excluded.is_empty());
    }

    #[test]
    fn pending_function_key_cannot_bind_a_custom_collision_or_filter_its_output() {
        let pending = key(ThreadId::new(), "manual");
        let sampling = SamplingState {
            turn_id: Some(pending.turn.clone()),
            tool_calls: 2,
            completed_tool_calls: 2,
        };
        assert!(
            LossSuspension::bind(
                &sampling,
                vec![pending.clone()],
                vec![
                    turn(&pending.turn),
                    rollout(call("manual")),
                    rollout(custom_call("manual")),
                ],
            )
            .is_none()
        );
        assert!(
            LossSuspension::bind(
                &sampling,
                vec![pending.clone()],
                vec![turn(&pending.turn), rollout(custom_call("manual"))],
            )
            .is_none()
        );

        let mut suspension = LossSuspension::bind(
            &sampling,
            vec![pending.clone()],
            vec![turn(&pending.turn), rollout(call("manual"))],
        )
        .unwrap();
        // A collision arriving after the live bind must leave the entire
        // suffix intact for the caller, including the true custom result.
        suspension.suffix.push(vec![
            rollout(custom_call("manual")),
            rollout(custom_receipt("manual")),
            rollout(receipt(&pending)),
        ]);
        assert!(!suspension.exclusions(Some(&pending.turn)).0);
    }

    #[test]
    fn ambiguous_manual_cancellation_never_filters_a_real_core_suffix() {
        let id = ThreadId::new();
        let pending = key(id, "manual");
        let sampling = SamplingState {
            turn_id: Some(pending.turn.clone()),
            tool_calls: 1,
            completed_tool_calls: 1,
        };
        let mut suspension = LossSuspension::bind(
            &sampling,
            vec![pending.clone()],
            vec![turn(&pending.turn), rollout(call("manual"))],
        )
        .unwrap();
        suspension
            .suffix
            .push(vec![rollout(receipt(&pending)), rollout(receipt(&pending))]);
        assert!(!suspension.exclusions(Some(&pending.turn)).0);
        assert!(!suspension.exclusions(None).0);
        assert!(!suspension.exclusions(Some("other-turn")).0);
    }

    #[test]
    fn matching_discards_only_native_metadata_not_original_call_data() {
        let unstamped = call("first");
        let mut stamped = unstamped.clone();
        if let ResponseItem::FunctionCall {
            internal_chat_message_metadata_passthrough,
            ..
        } = &mut stamped
        {
            *internal_chat_message_metadata_passthrough =
                Some(InternalChatMessageMetadataPassthrough {
                    turn_id: Some("owned turn".into()),
                    ..Default::default()
                });
        }
        assert!(sdk_calls_match(&[stamped.clone()], &[unstamped.clone()]));
        if let ResponseItem::FunctionCall { arguments, .. } = &mut stamped {
            *arguments = "{}".into();
        }
        assert!(!sdk_calls_match(&[stamped], &[unstamped]));
        assert!(!sdk_calls_match(
            &[call("first"), call("second")],
            &[call("second"), call("first")]
        ));
    }

    #[test]
    fn cannot_escape_unknown_or_replay_completed_sibling_by_changing_import_ids() {
        let owner = ThreadId::new();
        let a = call("parked");
        let b = call("done");
        let mut history = vec![
            rollout(a),
            rollout(b),
            rollout(receipt(&key(owner, "done"))),
        ];
        assert!(!sdk_import_allowed(&history, &[call("new-id")]));
        assert!(!sdk_import_allowed(&history, &[call("done")]));
        history.push(rollout(receipt(&key(owner, "parked"))));
        assert!(!sdk_import_allowed(&history, &[call("done")]));
        assert!(sdk_import_allowed(&history, &[call("new-id")]));
    }

    #[test]
    fn explicit_resume_matches_only_all_unresolved_calls_in_the_original_turn() {
        let id = ThreadId::new();
        let mut history = vec![
            turn("original"),
            rollout(call("first")),
            rollout(call("done")),
            rollout(call("last")),
            rollout(receipt(&key(id, "done"))),
        ];
        let requested = [call("first"), call("last")];
        assert!(sdk_resume_matches(&history, "original", &requested));
        // Automatic recovery/import is not entitled to the same permission.
        assert!(!sdk_import_allowed(&history, &requested));
        assert!(!sdk_resume_matches(&history, "another turn", &requested));
        assert!(!sdk_resume_matches(&history, "original", &[call("first")]));
        assert!(!sdk_resume_matches(
            &history,
            "original",
            &[call("last"), call("first")]
        ));
        assert!(!sdk_resume_matches(
            &history,
            "original",
            &[call("first"), call("done"), call("last")]
        ));
        assert!(!sdk_resume_matches(
            &history,
            "original",
            &[call("first"), call("changed-id")]
        ));
        let mut changed = call("last");
        if let ResponseItem::FunctionCall { arguments, .. } = &mut changed {
            *arguments = json!({"original":{
                "type":"function_call","name":"another-handler",
                "call_id":"last","arguments":"{}"
            }})
            .to_string();
        }
        assert!(!sdk_resume_matches(
            &history,
            "original",
            &[call("first"), changed]
        ));
        // Receipt committed before a post-effect failure fences old approvals,
        // even if their full serialized graph still describes them as pending.
        history.push(rollout(receipt(&key(id, "first"))));
        assert!(!sdk_resume_matches(&history, "original", &requested));
        assert!(sdk_resume_matches(&history, "original", &[call("last")]));
        history.push(rollout(receipt(&key(id, "last"))));
        assert!(!sdk_resume_matches(&history, "original", &[call("last")]));
    }

    #[test]
    fn interrupted_turn_identity_cannot_be_borrowed_from_later_history() {
        let id = ThreadId::new();
        let mut history = vec![
            turn("older"),
            rollout(call("unknown")),
            turn("current"),
            rollout(call("current")),
        ];
        assert!(!sdk_resume_matches(&history, "older", &[call("unknown")]));
        assert!(!sdk_resume_matches(&history, "current", &[call("current")]));
        history.insert(2, rollout(receipt(&key(id, "unknown"))));
        assert!(sdk_resume_matches(&history, "current", &[call("current")]));
        // Two identical original IDs are ambiguous; their mere presence must
        // never be used as an ownership proof for one resumed invocation.
        history.push(rollout(call("current")));
        assert!(!sdk_resume_matches(&history, "current", &[call("current")]));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn existing_writer_owns_early_receipt_and_repeated_reentry_seal() {
        let home = tempfile::TempDir::new().unwrap();
        let config = LocalThreadStoreConfig {
            codex_home: home.path().to_path_buf(),
            sqlite: SqliteConfig::from_sqlite_home(
                AbsolutePathBuf::try_from(home.path().to_path_buf()).unwrap(),
            ),
            default_model_provider_id: "fixture-provider".into(),
        };
        let owner = LocalChecklistStore::new(
            config.clone(),
            LocalThreadStore::new(config, None),
            Arc::new(MemoryAgentGraph::default()),
        );
        let id = ThreadId::new();
        owner
            .create_thread(CreateThreadParams {
                sdk_mcp_source_required: false,
                creator_user_id: None,
                creator_account_id: None,
                session_id: id.into(),
                thread_id: id,
                extra_config: None,
                forked_from_id: None,
                parent_thread_id: None,
                source: SessionSource::Exec,
                thread_source: None,
                originator: "sdk-managed-test".into(),
                base_instructions: BaseInstructions::default(),
                dynamic_tools: vec![],
                selected_capability_roots: vec![],
                multi_agent_version: None,
                history_mode: ThreadHistoryMode::Legacy,
                history_base: None,
                subagent_history_start_ordinal: None,
                initial_window_id: id.to_string(),
                runtime_workspace_roots: None,
                metadata: ThreadPersistenceMetadata {
                    cwd: Some(home.path().into()),
                    model_provider: "fixture-provider".into(),
                    memory_mode: ThreadMemoryMode::Disabled,
                },
            })
            .await
            .unwrap();
        owner
            .persist_thread(id, PersistContext::Standard)
            .await
            .unwrap();
        let first = key(id, "first");
        let done = key(id, "second");
        owner
            .append_items(AppendThreadItemsParams {
                thread_id: id,
                items: vec![rollout(call("first")), rollout(call("second"))],
            })
            .await
            .unwrap();
        owner.flush_thread(id).await.unwrap();
        let actual = receipt(&done);
        owner
            .commit_sdk_receipt(id, &done, actual.clone(), true)
            .await
            .unwrap();
        // A blocked earlier approval has no result. The actual sibling receipt
        // is already durable even before idle injection/ordered native drain.
        let history = owner.load_owned_history(id).await.unwrap();
        let outputs = |history: &[RolloutItem]| {
            history.iter().filter(|item| matches!(item,
            RolloutItem::ResponseItem(envelope) if matches!(envelope.item, ResponseItem::FunctionCallOutput { .. })
        )).count()
        };
        assert_eq!(outputs(&history), 1);
        assert!(
            owner
                .commit_sdk_receipt(id, &done, actual.clone(), true)
                .await
                .is_err()
        );
        let proof = HashMap::from([(
            "first".into(),
            json!({"child_calls":["c1"],"child_receipts":[]}),
        )]);
        owner
            .seal_sdk_reentry(id, &[first.clone()], &proof)
            .await
            .unwrap();
        // Emulate only the subsequent idle Core history append; it must not
        // write a second durable copy of its already acknowledged receipt.
        owner
            .append_items(AppendThreadItemsParams {
                thread_id: id,
                items: vec![rollout(actual)],
            })
            .await
            .unwrap();
        owner.flush_thread(id).await.unwrap();
        assert_eq!(outputs(&owner.load_owned_history(id).await.unwrap()), 1);
        assert!(
            owner
                .admit_sdk_reentry(id, &[call("first")], &HashMap::new())
                .await
                .is_err()
        );
        assert!(
            owner
                .admit_sdk_reentry(id, &[call("second")], &proof)
                .await
                .is_err()
        );
        assert_eq!(
            owner
                .admit_sdk_reentry(id, &[call("first")], &proof)
                .await
                .unwrap(),
            Some(first.turn.clone())
        );
        assert!(
            owner
                .admit_sdk_reentry(id, &[call("first")], &proof)
                .await
                .is_err(),
            "seal is single-use"
        );
        // A trusted newly parked binding can re-seal on the existing writer.
        owner
            .seal_sdk_reentry(id, &[first.clone()], &proof)
            .await
            .unwrap();
        assert_eq!(
            owner
                .admit_sdk_reentry(id, &[call("first")], &proof)
                .await
                .unwrap(),
            Some(first.turn.clone())
        );
        owner
            .commit_sdk_receipt(id, &first, receipt(&first), true)
            .await
            .unwrap();
        // Now a genuine native turn with a consumed SDK approval and one
        // already-receipted sibling. Explicit application RunState recovery
        // must keep this original turn, not inject the remaining call again.
        owner
            .append_items(AppendThreadItemsParams {
                thread_id: id,
                items: vec![
                    turn("application-turn"),
                    rollout(call("inventory")),
                    rollout(call("delivery")),
                ],
            })
            .await
            .unwrap();
        let mut inventory = key(id, "inventory");
        inventory.turn = "application-turn".into();
        owner
            .commit_sdk_receipt(id, &inventory, receipt(&inventory), true)
            .await
            .unwrap();
        owner.flush_thread(id).await.unwrap();
        // The old sdk-import record cannot authorize another native turn.
        assert!(
            owner
                .admit_sdk_reentry(id, &[call("delivery")], &HashMap::new())
                .await
                .is_err()
        );
        owner
            .seal_sdk_approval(
                id,
                json!({"turn_id": "application-turn"}),
                &[call("delivery")],
                &HashMap::new(),
            )
            .unwrap();
        assert_eq!(
            owner
                .admit_sdk_reentry(id, &[call("delivery")], &HashMap::new())
                .await
                .unwrap(),
            Some("application-turn".into())
        );
        assert!(
            owner
                .admit_sdk_reentry(id, &[call("inventory"), call("delivery")], &HashMap::new())
                .await
                .is_err()
        );
        assert!(
            owner
                .admit_sdk_reentry(id, &[call("delivery")], &proof)
                .await
                .is_err(),
            "an unsealed nested continuation is never ordinary receipt reconciliation"
        );
        let before = serde_json::to_value(owner.load_owned_history(id).await.unwrap()).unwrap();
        assert_eq!(
            owner
                .admit_sdk_reentry(id, &[call("delivery")], &HashMap::new())
                .await
                .unwrap(),
            Some("application-turn".into())
        );
        assert_eq!(
            serde_json::to_value(owner.load_owned_history(id).await.unwrap()).unwrap(),
            before,
            "admission neither imports the call nor records an outcome"
        );
        assert!(
            owner
                .admit_sdk_reentry(ThreadId::new(), &[call("delivery")], &HashMap::new())
                .await
                .is_err(),
            "only the live original writer can admit this receipt handler"
        );
        let mut delivery = key(id, "delivery");
        delivery.turn = "application-turn".into();
        owner
            .commit_sdk_receipt(id, &delivery, receipt(&delivery), true)
            .await
            .unwrap();
        assert!(
            owner
                .admit_sdk_reentry(id, &[call("delivery")], &HashMap::new())
                .await
                .is_err(),
            "a completed receipt must fence the old approval on the same writer"
        );
        owner.shutdown_thread(id).await.unwrap();
    }
}

#[cfg(test)]
mod local_rebuild_tests {
    use super::*;
    use codex_core_api::{AbsolutePathBuf, SqliteConfig};
    use codex_protocol::models::{BaseInstructions, FunctionCallOutputPayload};
    use codex_protocol::protocol::SessionSource;

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn only_admitted_status_reaches_the_retained_writer_replay() {
        let home = tempfile::TempDir::new().expect("local store home");
        let config = LocalThreadStoreConfig {
            codex_home: home.path().to_path_buf(),
            sqlite: SqliteConfig::from_sqlite_home(
                AbsolutePathBuf::try_from(home.path().to_path_buf()).expect("absolute home"),
            ),
            default_model_provider_id: "fixture-provider".into(),
        };
        let owner = LocalChecklistStore::new(
            config.clone(),
            LocalThreadStore::new(config, None),
            Arc::new(MemoryAgentGraph::default()),
        );
        let id = ThreadId::new();
        let metadata = ThreadPersistenceMetadata {
            cwd: Some(home.path().to_path_buf()),
            model_provider: "fixture-provider".into(),
            memory_mode: ThreadMemoryMode::Disabled,
        };
        owner
            .create_thread(CreateThreadParams {
                sdk_mcp_source_required: false,
                creator_user_id: None,
                creator_account_id: None,
                session_id: id.into(),
                thread_id: id,
                extra_config: None,
                forked_from_id: None,
                parent_thread_id: None,
                source: SessionSource::Exec,
                thread_source: None,
                originator: "sdk-recovery".into(),
                base_instructions: BaseInstructions::default(),
                dynamic_tools: Vec::new(),
                selected_capability_roots: Vec::new(),
                multi_agent_version: None,
                history_mode: ThreadHistoryMode::Legacy,
                history_base: None,
                subagent_history_start_ordinal: None,
                initial_window_id: id.to_string(),
                runtime_workspace_roots: None,
                metadata: metadata.clone(),
            })
            .await
            .expect("create original native writer");
        owner
            .persist_thread(id, PersistContext::Standard)
            .await
            .unwrap();
        owner.flush_thread(id).await.unwrap();
        let mut resume = ResumeThreadParams {
            thread_id: id,
            rollout_path: Some(owner.inner.live_rollout_path(id).await.unwrap()),
            history: None,
            history_revision: None,
            include_archived: false,
            metadata,
        };
        // Distinct root receipts: first the legacy omitted scalar, then an
        // application-confirmed failure. These are status values, not new IDs
        // granted authority by the incoming ResumeThreadParams history.
        for (call_id, status) in [("known-legacy", None), ("known-failure", Some(false))] {
            let expected = owner.load_owned_history(id).await.unwrap();
            let mut prepared = expected.clone();
            let receipt = RolloutItem::ResponseItem(
                ResponseItem::FunctionCallOutput {
                    id: None,
                    call_id: Some(call_id.into()),
                    name: None,
                    namespace: None,
                    output: FunctionCallOutputPayload::from_text("literal known receipt".into()),
                    internal_chat_message_metadata_passthrough: Some(
                        InternalChatMessageMetadataPassthrough {
                            turn_id: Some("original-turn".into()),
                            ..Default::default()
                        },
                    ),
                }
                .into(),
            );
            prepared.push(receipt);
            let denied = owner
                .begin_rebuild(
                    id,
                    &expected,
                    &prepared,
                    RebuildReceipt {
                        turn_id: "original-turn".into(),
                        call_id: "not-the-confirmed-call".into(),
                        success: Some(false),
                    },
                )
                .await;
            assert!(
                denied.is_err(),
                "status for an unconfirmed binding must not publish"
            );
            let unchanged = owner.load_owned_history(id).await.unwrap();
            assert_eq!(serde_json::json!(unchanged), serde_json::json!(expected));
            owner
                .begin_rebuild(
                    id,
                    &expected,
                    &prepared,
                    RebuildReceipt {
                        turn_id: "original-turn".into(),
                        call_id: call_id.into(),
                        success: status,
                    },
                )
                .await
                .expect("accept exactly the verified native result");
            // Core gives our store initial history but MUST use the writer
            // reload. Even a typed, contradictory status here is not evidence.
            let RolloutItem::ResponseItem(untrusted) = prepared.last_mut().unwrap() else {
                panic!("function result");
            };
            let ResponseItem::FunctionCallOutput { output, .. } = &mut untrusted.item else {
                panic!("function result");
            };
            output.success = Some(true);
            resume.history = Some(Arc::new(prepared));
            owner
                .shutdown_thread(id)
                .await
                .expect("retain through original close");
            let replay = owner
                .resume_thread(resume.clone())
                .await
                .expect("native adoption");
            let found = replay
                .iter()
                .filter_map(|item| match item {
                    RolloutItem::ResponseItem(envelope) => match &envelope.item {
                        ResponseItem::FunctionCallOutput {
                            call_id: Some(actual),
                            output,
                            ..
                        } if actual == call_id => Some(output),
                        _ => None,
                    },
                    _ => None,
                })
                .collect::<Vec<_>>();
            assert_eq!(found.len(), 1, "one result only, no effect replay");
            assert_eq!(found[0].text_content(), Some("literal known receipt"));
            assert_eq!(found[0].success, status);
            // It is scoped to this one typed reconstruction. Ordinary persisted
            // history and all older/unspecified receipt statuses stay unknown.
            assert!(
                owner
                    .load_owned_history(id)
                    .await
                    .unwrap()
                    .iter()
                    .all(|item| {
                        !matches!(item, RolloutItem::ResponseItem(envelope)
                    if matches!(&envelope.item, ResponseItem::FunctionCallOutput { output, .. }
                        if output.success.is_some()))
                    })
            );
        }
        owner
            .shutdown_thread(id)
            .await
            .expect("ordinary final shutdown");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn pre_close_abort_retains_writer_until_native_shutdown() {
        let home = tempfile::TempDir::new().expect("local store home");
        let config = LocalThreadStoreConfig {
            codex_home: home.path().to_path_buf(),
            sqlite: SqliteConfig::from_sqlite_home(
                AbsolutePathBuf::try_from(home.path().to_path_buf()).expect("absolute home"),
            ),
            default_model_provider_id: "fixture-provider".into(),
        };
        let owner = LocalChecklistStore::new(
            config.clone(),
            LocalThreadStore::new(config.clone(), None),
            Arc::new(MemoryAgentGraph::default()),
        );
        let other = LocalThreadStore::new(config, None);
        let id = ThreadId::new();
        let metadata = ThreadPersistenceMetadata {
            cwd: Some(home.path().to_path_buf()),
            model_provider: "fixture-provider".into(),
            memory_mode: ThreadMemoryMode::Disabled,
        };
        owner
            .create_thread(CreateThreadParams {
                sdk_mcp_source_required: false,
                creator_user_id: None,
                creator_account_id: None,
                session_id: id.into(),
                thread_id: id,
                extra_config: None,
                forked_from_id: None,
                parent_thread_id: None,
                source: SessionSource::Exec,
                thread_source: None,
                originator: "sdk-recovery".into(),
                base_instructions: BaseInstructions::default(),
                dynamic_tools: Vec::new(),
                selected_capability_roots: Vec::new(),
                multi_agent_version: None,
                history_mode: ThreadHistoryMode::Legacy,
                history_base: None,
                subagent_history_start_ordinal: None,
                initial_window_id: id.to_string(),
                runtime_workspace_roots: None,
                metadata: metadata.clone(),
            })
            .await
            .expect("create original native writer");
        owner
            .persist_thread(id, PersistContext::Standard)
            .await
            .expect("persist original thread");
        owner.flush_thread(id).await.expect("flush original thread");
        let expected = owner.load_owned_history(id).await.expect("owned history");
        let resume = ResumeThreadParams {
            thread_id: id,
            rollout_path: Some(owner.inner.live_rollout_path(id).await.expect("live path")),
            history: None,
            history_revision: None,
            include_archived: false,
            metadata,
        };
        let mut prepared = expected.clone();
        prepared.push(RolloutItem::ResponseItem(
            ResponseItem::FunctionCallOutput {
                id: None,
                call_id: Some("receipt-call".into()),
                name: None,
                namespace: None,
                output: FunctionCallOutputPayload::from_text("known receipt".into()),
                internal_chat_message_metadata_passthrough: Some(
                    InternalChatMessageMetadataPassthrough {
                        turn_id: Some("receipt-turn".into()),
                        ..Default::default()
                    },
                ),
            }
            .into(),
        ));
        owner
            .begin_rebuild(
                id,
                &expected,
                &prepared,
                RebuildReceipt {
                    turn_id: "receipt-turn".into(),
                    call_id: "receipt-call".into(),
                    success: None,
                },
            )
            .await
            .expect("durably publish the admitted result");
        // The engine's carry_config/refresh may fail before close. The live
        // owner must keep the writer until its ordinary native shutdown.
        owner.abort_rebuild(id, false).await.expect("abort rebuild");
        let error = other
            .resume_thread(resume.clone())
            .await
            .expect_err("a second writer cannot own the still-live native thread");
        assert!(matches!(error, ThreadStoreError::Conflict { .. }));
        owner
            .shutdown_thread(id)
            .await
            .expect("ordinary native shutdown must now release the writer");
        let received = other
            .resume_thread(resume)
            .await
            .expect("closed native writer can be acquired");
        assert_eq!(serde_json::json!(*received), serde_json::json!(prepared));
        other.shutdown_thread(id).await.expect("close new owner");
    }
}

#[cfg(test)]
mod graph_tests {
    use super::*;
    use codex_core_api::{AbsolutePathBuf, SqliteConfig};
    use codex_history::CompactedItem;
    use codex_protocol::models::{BaseInstructions, ContentItem};
    use codex_protocol::protocol::{SessionSource, SubAgentSource};

    fn local_config(home: &Path) -> LocalThreadStoreConfig {
        LocalThreadStoreConfig {
            codex_home: home.to_path_buf(),
            sqlite: SqliteConfig::from_sqlite_home(
                AbsolutePathBuf::try_from(home.to_path_buf()).expect("absolute test home"),
            ),
            default_model_provider_id: "test-provider".into(),
        }
    }

    async fn state(config: &LocalThreadStoreConfig) -> Arc<codex_state::StateRuntime> {
        codex_state::StateRuntime::init(
            config.sqlite.clone(),
            config.default_model_provider_id.clone(),
        )
        .await
        .expect("initialize selected local state")
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn canonical_metadata_failure_remains_visible_after_successful_native_flush() {
        let home = tempfile::TempDir::new().unwrap();
        let config = local_config(home.path());
        let db = state(&config).await;
        let owner = LocalChecklistStore::new(
            config.clone(),
            LocalThreadStore::new(config.clone(), Some(db.clone())),
            Arc::new(MemoryAgentGraph::local(db.clone())),
        );
        let id = ThreadId::new();
        let params = CreateThreadParams {
            sdk_mcp_source_required: false,
            creator_user_id: None,
            creator_account_id: None,
            session_id: id.into(),
            thread_id: id,
            extra_config: None,
            forked_from_id: None,
            parent_thread_id: None,
            source: SessionSource::Exec,
            thread_source: None,
            originator: "sdk-metadata-durability".into(),
            base_instructions: BaseInstructions::default(),
            dynamic_tools: Vec::new(),
            selected_capability_roots: Vec::new(),
            multi_agent_version: None,
            history_mode: ThreadHistoryMode::Paginated,
            history_base: None,
            subagent_history_start_ordinal: None,
            initial_window_id: id.to_string(),
            runtime_workspace_roots: None,
            metadata: ThreadPersistenceMetadata {
                cwd: Some(home.path().to_path_buf()),
                model_provider: "test-provider".into(),
                memory_mode: ThreadMemoryMode::Disabled,
            },
        };
        owner.create_thread(params.clone()).await.unwrap();
        owner
            .persist_thread(id, PersistContext::Standard)
            .await
            .unwrap();
        owner.flush_thread(id).await.unwrap();

        // Paginated metadata is canonical SQLite state. Its failure must remain
        // visible even though the separately persisted native history can flush.
        db.close().await;
        owner
            .update_thread_metadata(UpdateThreadMetadataParams {
                thread_id: id,
                patch: ThreadMetadataPatch {
                    memory_mode: Some(ThreadMemoryMode::Enabled),
                    ..Default::default()
                },
                include_archived: false,
            })
            .await
            .expect_err("canonical metadata update must fail");
        owner.inner.flush_thread(id).await.unwrap();
        owner
            .flush_thread(id)
            .await
            .expect_err("flush must retain metadata failure");
        owner
            .append_items(AppendThreadItemsParams {
                thread_id: id,
                items: vec![RolloutItem::EventMsg(
                    codex_protocol::protocol::EventMsg::UserMessage(
                        codex_protocol::protocol::UserMessageEvent {
                            message: "must wait for recovery".into(),
                            ..Default::default()
                        },
                    ),
                )],
            })
            .await
            .expect_err("later appends require recovery");
        owner
            .shutdown_thread(id)
            .await
            .expect_err("close must retain metadata failure");
        assert!(owner.inner.live_rollout_path(id).await.is_err());

        // Failure belongs to this session's store, rather than a process-wide
        // flag. A separate owner can still persist and close its own thread.
        let healthy_home = tempfile::TempDir::new().unwrap();
        let healthy_config = local_config(healthy_home.path());
        let healthy_db = state(&healthy_config).await;
        let healthy = LocalChecklistStore::new(
            healthy_config.clone(),
            LocalThreadStore::new(healthy_config, Some(healthy_db.clone())),
            Arc::new(MemoryAgentGraph::local(healthy_db)),
        );
        let mut healthy_params = params;
        let healthy_id = ThreadId::new();
        healthy_params.thread_id = healthy_id;
        healthy_params.session_id = healthy_id.into();
        healthy_params.initial_window_id = healthy_id.to_string();
        healthy.create_thread(healthy_params).await.unwrap();
        healthy
            .persist_thread(healthy_id, PersistContext::Standard)
            .await
            .unwrap();
        healthy.flush_thread(healthy_id).await.unwrap();
        healthy.shutdown_thread(healthy_id).await.unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn local_graph_reopen_restores_identity_status_and_unknown_close_times() {
        let home = tempfile::TempDir::new().unwrap();
        let config = local_config(home.path());
        let root = ThreadId::new();
        let child = ThreadId::new();
        let grandchild = ThreadId::new();
        let old_db = state(&config).await;
        let original = MemoryAgentGraph::local(old_db.clone());
        original
            .upsert_thread_spawn_edge(root, child, ThreadSpawnEdgeStatus::Open)
            .await
            .unwrap();
        original
            .upsert_thread_spawn_edge(child, grandchild, ThreadSpawnEdgeStatus::Open)
            .await
            .unwrap();
        original
            .set_thread_spawn_edge_status(child, ThreadSpawnEdgeStatus::Closed)
            .await
            .unwrap();
        assert!(original.edge(child).await.unwrap().closed_at.is_some());
        old_db.close().await;
        drop(original);

        let current_db = state(&config).await;
        let reopened = MemoryAgentGraph::local(current_db.clone());
        reopened.hydrate(root).await.unwrap();
        let mut events = reopened.subscribe(root).await;
        let first = events.try_recv().unwrap();
        assert_eq!(
            (
                first.kind,
                first.subagent_id,
                first.parent_agent_id,
                first.status,
                first.closed_at
            ),
            ("snapshot", child, root, "closed", None)
        );
        let second = events.try_recv().unwrap();
        assert_eq!(
            (
                second.kind,
                second.subagent_id,
                second.parent_agent_id,
                second.status
            ),
            ("snapshot", grandchild, child, "active")
        );
        assert!(events.try_recv().is_err());
        reopened
            .set_thread_spawn_edge_status(child, ThreadSpawnEdgeStatus::Closed)
            .await
            .unwrap();
        assert!(events.try_recv().is_err());
        assert!(reopened.edge(child).await.unwrap().closed_at.is_none());
        assert_eq!(
            reopened
                .list_thread_spawn_descendants(root, None)
                .await
                .unwrap(),
            vec![child, grandchild]
        );

        // Failure must not become a locally visible close, partial hydration,
        // or a false empty list. A caller can decide whether to retry.
        current_db.close().await;
        assert!(
            reopened
                .set_thread_spawn_edge_status(grandchild, ThreadSpawnEdgeStatus::Closed)
                .await
                .is_err()
        );
        assert!(reopened.hydrate(root).await.is_err());
        assert!(
            reopened
                .list_thread_spawn_descendants(root, None)
                .await
                .is_err()
        );
        assert!(events.try_recv().is_err());
        assert!(reopened.edge(grandchild).await.unwrap().status == ThreadSpawnEdgeStatus::Open);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn cancelled_admitted_child_is_closed_in_live_graph_reads_and_snapshots() {
        let home = tempfile::TempDir::new().unwrap();
        let config = local_config(home.path());
        let db = state(&config).await;
        let graph = Arc::new(MemoryAgentGraph::local(db));
        let owner = LocalChecklistStore::new(
            config.clone(),
            LocalThreadStore::new(config, None),
            graph.clone(),
        );
        let root = ThreadId::new();
        let child = ThreadId::new();
        let mut existing = graph.subscribe(root).await;
        let params = CreateThreadParams {
            sdk_mcp_source_required: false,
            creator_user_id: None,
            creator_account_id: None,
            session_id: root.into(),
            thread_id: child,
            extra_config: None,
            forked_from_id: None,
            parent_thread_id: Some(root),
            source: SessionSource::SubAgent(SubAgentSource::ThreadSpawn {
                parent_thread_id: root,
                depth: 1,
                agent_path: None,
                agent_nickname: None,
                agent_role: None,
            }),
            thread_source: None,
            originator: "sdk-cancelled-spawn".into(),
            base_instructions: BaseInstructions::default(),
            dynamic_tools: Vec::new(),
            selected_capability_roots: Vec::new(),
            multi_agent_version: None,
            history_mode: ThreadHistoryMode::Paginated,
            history_base: None,
            subagent_history_start_ordinal: None,
            initial_window_id: child.to_string(),
            runtime_workspace_roots: None,
            metadata: ThreadPersistenceMetadata {
                cwd: Some(home.path().to_path_buf()),
                model_provider: "test-provider".into(),
                memory_mode: ThreadMemoryMode::Disabled,
            },
        };
        owner.create_thread(params.clone()).await.unwrap();
        let path = owner.inner.live_rollout_path(child).await.unwrap();
        // The SDK commits the header before admitting the edge. Core has not
        // yet materialized its rollout, issued input, or updated a checklist.
        assert!(path.is_file());
        // Core has not published thread readiness or admitted child input.
        assert!(existing.try_recv().is_err());
        let mut joining = graph.subscribe(root).await;
        assert!(joining.try_recv().is_err());
        // Follow PendingSpawn::drop's observable store sequence: native writer
        // shutdown and closure of the pending edge without the later Open upsert.
        owner.shutdown_thread(child).await.unwrap();
        graph
            .set_thread_spawn_edge_status(child, ThreadSpawnEdgeStatus::Closed)
            .await
            .unwrap();
        assert_eq!(
            graph
                .list_thread_spawn_children(root, Some(ThreadSpawnEdgeStatus::Closed))
                .await
                .unwrap(),
            vec![child]
        );
        let edge = graph.edge(child).await.expect("committed edge is visible");
        assert_eq!(edge.parent, root);
        assert!(edge.status == ThreadSpawnEdgeStatus::Closed);
        let notification = existing.try_recv().expect("closed transition");
        assert_eq!(
            (
                notification.kind,
                notification.subagent_id,
                notification.status
            ),
            ("closed", child, "closed")
        );
        let joined_notification = joining.try_recv().expect("closed transition");
        assert_eq!(joined_notification.closed_at, notification.closed_at);
        let mut snapshots = graph.subscribe(root).await;
        let closed = snapshots
            .try_recv()
            .expect("snapshot includes cancelled child");
        assert_eq!(
            (
                closed.kind,
                closed.subagent_id,
                closed.parent_agent_id,
                closed.status
            ),
            ("snapshot", child, root, "closed")
        );
        assert_eq!(closed.closed_at, edge.closed_at);
        assert!(closed.closed_at.is_some());
        let stored = owner
            .read_thread(ReadThreadParams {
                thread_id: child,
                include_archived: true,
                include_history: false,
            })
            .await
            .unwrap();
        assert_eq!(stored.parent_thread_id, Some(root));
        let history = owner
            .load_history(LoadThreadHistoryParams {
                thread_id: child,
                include_archived: true,
            })
            .await
            .expect("cancelled child's admitted history must remain readable");
        assert!(history.items.iter().any(|item| matches!(
            item, RolloutItem::SessionMeta(meta) if meta.meta.id == child
        )));
        assert!(owner.checklist(child).await.unwrap().is_none());

        // The same absence of a checklist is not permission to hide damaged
        // history. Listing and Closed status alone do not establish freshness.
        let original = std::fs::read(&path).unwrap();
        std::fs::write(&path, b"{invalid synthetic rollout\n").unwrap();
        assert!(
            owner
                .load_history(LoadThreadHistoryParams {
                    thread_id: child,
                    include_archived: true,
                })
                .await
                .is_err()
        );
        std::fs::write(&path, original).unwrap();
        std::fs::remove_file(&path).unwrap();
        assert_eq!(graph.edge(child).await.unwrap().parent, root);
        assert!(
            owner
                .load_history(LoadThreadHistoryParams {
                    thread_id: child,
                    include_archived: true,
                })
                .await
                .is_err()
        );

        // Memory storage creates its header synchronously. Core publishes and
        // closes the edge after creation; discard does not remove its history.
        let memory = MemoryThreadStore::default();
        let memory_graph = MemoryAgentGraph::default();
        memory.create_thread(params).await.unwrap();
        memory_graph
            .upsert_thread_spawn_edge(root, child, ThreadSpawnEdgeStatus::Open)
            .await
            .unwrap();
        memory.shutdown_thread(child).await.unwrap();
        memory.discard_thread(child).await.unwrap();
        memory_graph
            .set_thread_spawn_edge_status(child, ThreadSpawnEdgeStatus::Closed)
            .await
            .unwrap();
        assert_eq!(memory_graph.edge(child).await.unwrap().parent, root);
        let history = memory
            .load_history(LoadThreadHistoryParams {
                thread_id: child,
                include_archived: true,
            })
            .await
            .unwrap();
        assert!(history.items.iter().any(|item| matches!(
            item, RolloutItem::SessionMeta(meta) if meta.meta.id == child
        )));
        assert!(memory.checklist(child).await.unwrap().is_none());
        // Simulate lost storage independently of the SDK's cached data.
        memory
            .inner
            .delete_thread(DeleteThreadParams { thread_id: child })
            .await
            .unwrap();
        assert!(memory.checklist(child).await.is_err());
        assert!(
            memory
                .load_history(LoadThreadHistoryParams {
                    thread_id: child,
                    include_archived: true,
                })
                .await
                .is_err()
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn failed_graph_admission_releases_writer_and_keeps_original_rollout() {
        let home = tempfile::TempDir::new().unwrap();
        let config = local_config(home.path());
        let db = state(&config).await;
        let graph = Arc::new(MemoryAgentGraph::local(db.clone()));
        // Keep a working thread backend while failing only the real graph
        // backend. This isolates the admission boundary from rollout failures.
        let owner = LocalChecklistStore::new(
            config.clone(),
            LocalThreadStore::new(config.clone(), None),
            graph.clone(),
        );
        let other = LocalThreadStore::new(config.clone(), None);
        let root = ThreadId::new();
        let child = ThreadId::new();
        let metadata = ThreadPersistenceMetadata {
            cwd: Some(home.path().to_path_buf()),
            model_provider: "test-provider".into(),
            memory_mode: ThreadMemoryMode::Disabled,
        };
        let mut params = CreateThreadParams {
            sdk_mcp_source_required: false,
            creator_user_id: None,
            creator_account_id: None,
            session_id: root.into(),
            thread_id: root,
            extra_config: None,
            forked_from_id: None,
            parent_thread_id: None,
            source: SessionSource::Exec,
            thread_source: None,
            originator: "sdk-graph-admission".into(),
            base_instructions: BaseInstructions::default(),
            dynamic_tools: Vec::new(),
            selected_capability_roots: Vec::new(),
            multi_agent_version: None,
            history_mode: ThreadHistoryMode::Legacy,
            history_base: None,
            subagent_history_start_ordinal: None,
            initial_window_id: root.to_string(),
            runtime_workspace_roots: None,
            metadata: metadata.clone(),
        };
        owner.create_thread(params.clone()).await.unwrap();
        owner
            .persist_thread(root, PersistContext::Standard)
            .await
            .unwrap();
        owner.flush_thread(root).await.unwrap();
        let path = owner.inner.live_rollout_path(root).await.unwrap();
        owner.shutdown_thread(root).await.unwrap();
        let saved = std::fs::read(&path).unwrap();
        let resume = ResumeThreadParams {
            thread_id: root,
            rollout_path: Some(path.clone()),
            history: None,
            history_revision: None,
            include_archived: false,
            metadata,
        };
        let mut events = graph.subscribe(root).await;
        params.thread_id = child;
        params.parent_thread_id = Some(root);
        params.source = SessionSource::SubAgent(SubAgentSource::ThreadSpawn {
            parent_thread_id: root,
            depth: 1,
            agent_path: None,
            agent_nickname: None,
            agent_role: None,
        });
        params.initial_window_id = child.to_string();
        params.history_mode = ThreadHistoryMode::Paginated;
        owner.create_thread(params.clone()).await.unwrap();
        // A committed edge is readable through native storage before Core
        // publishes child creation. This covers nonforked delayed persistence.
        assert_eq!(
            graph
                .list_thread_spawn_descendants(root, None)
                .await
                .unwrap(),
            vec![child]
        );
        let recorded = owner
            .read_thread(ReadThreadParams {
                thread_id: child,
                include_archived: false,
                include_history: false,
            })
            .await
            .unwrap();
        assert_eq!(recorded.thread_id, child);
        assert_eq!(recorded.parent_thread_id, Some(root));
        assert!(events.try_recv().is_err());
        graph
            .upsert_thread_spawn_edge(root, child, ThreadSpawnEdgeStatus::Open)
            .await
            .unwrap();
        let notification = events.try_recv().unwrap();
        assert_eq!(
            (notification.kind, notification.subagent_id),
            ("created", child)
        );
        let message = |text: &str| ResponseItem::Message {
            id: None,
            role: "user".into(),
            content: vec![ContentItem::InputText { text: text.into() }],
            phase: None,
            internal_chat_message_metadata_passthrough: None,
        };
        owner
            .append_items(AppendThreadItemsParams {
                thread_id: child,
                items: vec![
                    RolloutItem::ResponseItem(message("ORIGINAL_CHILD_RECORD").into()),
                    RolloutItem::Compacted(CompactedItem {
                        message: "CHILD_CURRENT_WINDOW".into(),
                        replacement_history: Some(vec![message("CHILD_CURRENT_WINDOW").into()]),
                        sdk_originals: None,
                        guardian_history: None,
                        retained_context: None,
                        mcp_resource_origins: None,
                        window_number: None,
                        first_window_id: None,
                        previous_window_id: None,
                        window_id: None,
                        compaction_response_id: None,
                        latest_token_usage_record: None,
                        resume_metadata: None,
                    }),
                ],
            })
            .await
            .unwrap();
        owner.flush_thread(child).await.unwrap();
        owner.shutdown_thread(child).await.unwrap();
        owner
            .archive_thread(ArchiveThreadParams { thread_id: child })
            .await
            .unwrap();
        let load = LoadThreadHistoryParams {
            thread_id: child,
            include_archived: true,
        };
        let audit = owner.load_history(load).await.unwrap();
        assert!(audit.revision.is_none());
        let saved_history = serde_json::to_string(&audit.items).unwrap();
        assert!(saved_history.contains("ORIGINAL_CHILD_RECORD"));
        assert!(saved_history.contains("CHILD_CURRENT_WINDOW"));
        assert!(
            owner
                .load_history(LoadThreadHistoryParams {
                    thread_id: child,
                    include_archived: false,
                })
                .await
                .is_err()
        );

        db.close().await;
        let child = ThreadId::new();
        params.thread_id = child;
        params.initial_window_id = child.to_string();
        assert!(owner.create_thread(params.clone()).await.is_err());
        assert!(events.try_recv().is_err());
        assert!(graph.edge(child).await.is_none());
        // A failed new child did not strand its acquired native writer.
        other.create_thread(params).await.unwrap();
        other.discard_thread(child).await.unwrap();

        assert!(owner.resume_thread(resume.clone()).await.is_err());
        assert_eq!(std::fs::read(&path).unwrap(), saved);
        // A failed root graph restore did not delete original data or hold the
        // native writer hostage. Another live writer can claim it normally.
        other.resume_thread(resume).await.unwrap();
        other.shutdown_thread(root).await.unwrap();
        assert_eq!(std::fs::read(path).unwrap(), saved);
    }

    #[tokio::test]
    async fn transitions_remain_ordered_when_consumers_lag() {
        let graph = MemoryAgentGraph::default();
        let root = ThreadId::new();
        let child = ThreadId::new();
        let mut events = graph.subscribe(root).await;
        let mut unrelated = graph.subscribe(ThreadId::new()).await;
        graph
            .upsert_thread_spawn_edge(root, child, ThreadSpawnEdgeStatus::Open)
            .await
            .unwrap();
        let before = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        // Exceed a typical broadcast capacity before draining any events.
        for _ in 0..300 {
            graph
                .set_thread_spawn_edge_status(child, ThreadSpawnEdgeStatus::Closed)
                .await
                .unwrap();
            graph
                .set_thread_spawn_edge_status(child, ThreadSpawnEdgeStatus::Closed)
                .await
                .unwrap();
            graph
                .upsert_thread_spawn_edge(root, child, ThreadSpawnEdgeStatus::Open)
                .await
                .unwrap();
        }
        let after = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        let created = events.try_recv().unwrap();
        assert_eq!(
            (created.kind, created.subagent_id, created.parent_agent_id),
            ("created", child, root)
        );
        assert!(created.closed_at.is_none());
        for _ in 0..300 {
            let closed = events.try_recv().unwrap();
            assert_eq!((closed.kind, closed.status), ("closed", "closed"));
            assert!((before..=after).contains(&closed.closed_at.unwrap()));
            let reopened = events.try_recv().unwrap();
            assert_eq!((reopened.kind, reopened.status), ("active", "active"));
            assert!(reopened.closed_at.is_none());
        }
        assert!(events.try_recv().is_err());
        assert!(unrelated.try_recv().is_err());
        assert!(graph.edge(child).await.unwrap().closed_at.is_none());
    }

    #[tokio::test]
    async fn restore_preserves_close_times_and_seeds_the_whole_subtree() {
        let graph = MemoryAgentGraph::default();
        let root = ThreadId::new();
        let child = ThreadId::new();
        let grandchild = ThreadId::new();
        let mut existing = graph.subscribe(root).await;
        graph
            .restore_edge(
                child,
                SpawnEdge {
                    parent: root,
                    status: ThreadSpawnEdgeStatus::Closed,
                    closed_at: Some(123),
                },
            )
            .await;
        graph
            .restore_edge(
                grandchild,
                SpawnEdge {
                    parent: child,
                    status: ThreadSpawnEdgeStatus::Closed,
                    closed_at: None,
                },
            )
            .await;
        assert!(existing.try_recv().is_err());
        let mut snapshots = graph.subscribe(root).await;
        let parent = snapshots.try_recv().unwrap();
        let nested = snapshots.try_recv().unwrap();
        assert_eq!(
            (parent.kind, parent.subagent_id, parent.closed_at),
            ("snapshot", child, Some(123))
        );
        assert_eq!(
            (
                nested.kind,
                nested.subagent_id,
                nested.parent_agent_id,
                nested.closed_at
            ),
            ("snapshot", grandchild, child, None)
        );
        graph
            .set_thread_spawn_edge_status(child, ThreadSpawnEdgeStatus::Closed)
            .await
            .unwrap();
        assert!(snapshots.try_recv().is_err());
        assert_eq!(graph.edge(child).await.unwrap().closed_at, Some(123));
        graph
            .set_thread_spawn_edge_status(grandchild, ThreadSpawnEdgeStatus::Open)
            .await
            .unwrap();
        assert_eq!(snapshots.try_recv().unwrap().kind, "active");
        assert_eq!(
            graph
                .list_thread_spawn_descendants(root, Some(ThreadSpawnEdgeStatus::Open))
                .await
                .unwrap(),
            vec![grandchild]
        );
    }
}

#[cfg(test)]
mod computer_storage_reentry_tests {
    use super::*;
    use serde_json::json;
    fn row(value: serde_json::Value) -> RolloutItem {
        RolloutItem::ResponseItem(codex_history::ResponseItemEnvelope::new(
            serde_json::from_value(value).unwrap(),
        ))
    }
    #[test]
    fn unknown_computer_effect_cannot_escape_through_function_only_sdk_import() {
        let call = row(
            json!({"type":"computer_call","call_id":"computer-1","actions":[{"type":"click","x":1,"y":2}]}),
        );
        let mut history = vec![call.clone()];
        assert!(!sdk_import_allowed(&history, &[]));
        history.push(row(json!({"type":"function_call_output","call_id":"computer-1","output":"counterfeit completion"})));
        assert!(!sdk_import_allowed(&history, &[]));
        history.pop();
        history.push(row(json!({"type":"computer_call_output","call_id":"computer-1","output":{"type":"computer_screenshot","image_url":"data:image/png;base64,AA=="},"acknowledged_safety_checks":[]})));
        assert!(sdk_import_allowed(&history, &[]));
        history.push(call);
        assert!(!sdk_import_allowed(&history, &[]));
    }
}
