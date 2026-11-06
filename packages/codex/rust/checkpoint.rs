//! Portable replay state for a whole native session tree. Live work is never resumed.

use std::collections::HashSet;
use std::sync::Arc;

use anyhow::{Context, ensure};
use codex_agent_graph_store::ThreadSpawnEdgeStatus;
use codex_core_api::{AbsolutePathBuf, CodexThread, InitialHistory, ThreadId};
use codex_history::{ResumedHistory, RolloutItem};
use codex_protocol::dynamic_tools::DynamicToolSpec;
use codex_protocol::protocol::{
    AgentStatus, EventMsg, SessionMeta, ThreadMemoryMode, ThreadSettingsSnapshot, TurnAbortReason,
    TurnAbortedEvent,
};
use codex_thread_store::{
    AppendThreadItemsParams, ArchiveThreadParams, CreateThreadParams, PersistContext,
    ReadThreadParams, StoredThread, ThreadMetadataPatch, ThreadPersistenceMetadata,
    ThreadStoreError, UpdateThreadMetadataParams,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{
    NativeThread,
    engine::{EngineState, ThreadState},
    options,
};

#[derive(Serialize, Deserialize)]
pub struct Checkpoint {
    format_version: u32,
    codex_revision: String,
    source_session_id: ThreadId,
    history: Vec<RolloutItem>,
    root_metadata: StoredThread,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    root_memory_mode: Option<ThreadMemoryMode>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    root_checklist: Option<crate::storage::ChecklistState>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    root_goal: Option<crate::goals::GoalSnapshot>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    root_code_mode: Option<codex_code_mode::CodeModeSnapshot>,
    #[serde(default)]
    descendants: Vec<Descendant>,
    #[serde(default)]
    interrupted_thread_ids: Vec<ThreadId>,
    // Do not deserialize saved declarations into DynamicToolSpec: Core ignores
    // unknown fields there. Compare the complete saved value to trusted specs.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    managed_tool_specs: Option<Value>,
}

impl Checkpoint {
    fn check_revision(&self) -> anyhow::Result<()> {
        ensure!(
            matches!(self.format_version, 2 | 4),
            "unsupported checkpoint format"
        );
        ensure!(
            self.codex_revision == include_str!("../CODEX_REVISION").trim(),
            "checkpoint core revision differs"
        );
        Ok(())
    }

    // These checks depend only on immutable checkpoint data and the current
    // managed declarations; an executor must not be connected to perform them.
    fn validate(
        &self,
        python_specs: Option<&[DynamicToolSpec]>,
    ) -> anyhow::Result<HashSet<ThreadId>> {
        let trusted_specs = python_specs
            .map(crate::python_tools::checkpoint_specs)
            .transpose()?;
        ensure!(
            self.managed_tool_specs == trusted_specs,
            "checkpoint tool mode and current managed specs must match; startup checkpoints cannot convert to managed tools"
        );
        let has_code = self.root_code_mode.is_some()
            || self
                .descendants
                .iter()
                .any(|child| child.code_mode.is_some());
        ensure!(
            !has_code || matches!(self.format_version, 4 | 5),
            "code-mode state requires a new checkpoint version"
        );
        if let Some(snapshot) = &self.root_code_mode {
            snapshot.validate().map_err(anyhow::Error::msg)?;
        }
        for child in &self.descendants {
            if let Some(snapshot) = &child.code_mode {
                snapshot.validate().map_err(anyhow::Error::msg)?;
            }
        }
        let root = self.source_session_id;
        let mut ids = HashSet::from([root]);
        validate_history(root, &self.history)?;
        if let Some(checklist) = &self.root_checklist {
            checklist.validate(root, &self.history)?;
        }
        if let Some(goal) = &self.root_goal {
            goal.validate(root)?;
        }
        ensure!(
            self.root_metadata.thread_id == root,
            "checkpoint root metadata identity mismatch"
        );
        for child in &self.descendants {
            ensure!(ids.insert(child.thread_id), "duplicate checkpoint thread");
            let meta = validate_history(child.thread_id, &child.history)?;
            if let Some(checklist) = &child.checklist {
                checklist.validate(child.thread_id, &child.history)?;
            }
            if let Some(goal) = &child.goal {
                goal.validate(child.thread_id)?;
            }
            ensure!(
                meta.parent_thread_id == Some(child.parent_thread_id)
                    && child.metadata.thread_id == child.thread_id,
                "checkpoint child identity mismatch"
            );
            ensure!(
                matches!(child.status.as_str(), "active" | "closed"),
                "invalid checkpoint child status"
            );
            ensure!(
                child.status == "closed" || child.closed_at.is_none(),
                "active checkpoint child has a close timestamp"
            );
        }
        // Validate the entire graph before writing any native records.
        for child in &self.descendants {
            let mut path = HashSet::from([child.thread_id]);
            let mut parent = child.parent_thread_id;
            while parent != root {
                ensure!(path.insert(parent), "checkpoint graph contains a cycle");
                parent = self
                    .descendants
                    .iter()
                    .find(|entry| entry.thread_id == parent)
                    .context("checkpoint parent missing")?
                    .parent_thread_id;
            }
        }
        Ok(ids)
    }

    pub(crate) fn initial_managed_model(
        &self,
        python_specs: Option<&[DynamicToolSpec]>,
    ) -> anyhow::Result<Option<String>> {
        // This early read chooses the current native model services only.
        // restore_checkpoint validates a format-3 frame before lowering it
        // to the format-2 Core checkpoint accepted by check_revision.
        ensure!(
            matches!(self.format_version, 2 | 3 | 4 | 5),
            "unsupported checkpoint format"
        );
        ensure!(
            self.codex_revision == include_str!("../CODEX_REVISION").trim(),
            "checkpoint core revision differs"
        );
        self.validate(python_specs)?;
        Ok(self
            .managed_tool_specs
            .as_ref()
            .and_then(|_| managed_settings(&self.history, self.source_session_id))
            .map(|settings| settings.model.clone()))
    }
}

pub(crate) fn managed_settings(
    history: &[RolloutItem],
    root: ThreadId,
) -> Option<&ThreadSettingsSnapshot> {
    history.iter().rev().find_map(|item| match item {
        RolloutItem::EventMsg(EventMsg::ThreadSettingsApplied(event))
            if event.thread_id == Some(root) =>
        {
            Some(&event.thread_settings)
        }
        _ => None,
    })
}

#[derive(Serialize, Deserialize)]
struct Descendant {
    thread_id: ThreadId,
    parent_thread_id: ThreadId,
    status: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    closed_at: Option<i64>,
    history: Vec<RolloutItem>,
    metadata: StoredThread,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    memory_mode: Option<ThreadMemoryMode>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    checklist: Option<crate::storage::ChecklistState>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    goal: Option<crate::goals::GoalSnapshot>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    code_mode: Option<codex_code_mode::CodeModeSnapshot>,
}

pub async fn export(state: &ThreadState, pending: Option<&Value>) -> anyhow::Result<Value> {
    export_inner(state, pending, false, None).await
}

pub(crate) async fn export_suspended(
    state: &ThreadState,
    code_states: &CodeStates,
) -> anyhow::Result<Value> {
    ensure!(
        matches!(state.thread.agent_status().await, AgentStatus::Shutdown),
        "Manual checkpoint requires a closed Core writer"
    );
    export_inner(state, None, true, Some(code_states)).await
}

// The manual owner holds the native mutation gate and validates its live
// grants around this export. Core itself is idle on restored-frame reentry.
pub(crate) async fn export_restored_manual(state: &ThreadState) -> anyhow::Result<Value> {
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
        "Manual reentry requires an idle, open Core thread"
    );
    export_inner(state, None, true, None).await
}

struct ManualDescendants {
    graph: Vec<crate::subagents::Subagent>,
    loaded: Vec<(ThreadId, Arc<CodexThread>)>,
}

// Inspect every descendant before deciding to retire any. A settled writer
// has no active turn (including native waiters) or executor process to carry.
// Graph status is separate: an open edge can describe a completed child.
async fn inspect_manual_descendants(
    state: &ThreadState,
) -> anyhow::Result<Option<ManualDescendants>> {
    let before = crate::subagents::list(state).await?;
    let mut loaded = Vec::new();
    for child in &before {
        match state.engine.manager.get_thread(child.id).await {
            Ok(thread) => {
                if !manual_descendant_idle(&thread).await? {
                    return Ok(None);
                }
                // A completed event can precede the store's final flush.
                // As with a history read, only flush already admitted work.
                if !matches!(thread.agent_status().await, AgentStatus::Shutdown) {
                    thread.ensure_rollout_materialized().await;
                    thread.flush_rollout().await?;
                }
                loaded.push((child.id, thread));
            }
            Err(error)
                if matches!(
                    error.details(),
                    codex_protocol::error::CodexErrorDetails::ThreadNotFound(_)
                ) => {}
            Err(error) => return Err(error.into()),
        }
        if history_unfinished(&state.engine.load_checkpoint_history(child.id).await?) {
            return Ok(None);
        }
    }
    if graph_edges(&before) != graph_edges(&crate::subagents::list(state).await?) {
        return Ok(None);
    }
    // The graph outlives its native writers. Recheck both incarnation and
    // idleness so a followup that reused the same graph ID cannot pass.
    for (id, expected) in &loaded {
        let current = match state.engine.manager.get_thread(*id).await {
            Ok(current) => current,
            Err(error)
                if matches!(
                    error.details(),
                    codex_protocol::error::CodexErrorDetails::ThreadNotFound(_)
                ) =>
            {
                return Ok(None);
            }
            Err(error) => return Err(error.into()),
        };
        if !Arc::ptr_eq(&current, expected) || !manual_descendant_idle(&current).await? {
            return Ok(None);
        }
    }
    Ok(Some(ManualDescendants {
        graph: before,
        loaded,
    }))
}

async fn manual_descendant_idle(thread: &CodexThread) -> anyhow::Result<bool> {
    if !matches!(
        thread.agent_status().await,
        AgentStatus::Completed(_)
            | AgentStatus::Errored(_)
            | AgentStatus::Interrupted
            | AgentStatus::Shutdown
    ) || thread.active_turn_environment_selections().await.is_some()
    {
        return Ok(false);
    }
    ensure!(
        !thread.config_snapshot().await.ephemeral,
        crate::manual_checkpoint::UnsupportedManualState::EphemeralChild
    );
    ensure!(
        thread.list_background_terminals().await.is_empty(),
        crate::manual_checkpoint::UnsupportedManualState::LiveTerminal
    );
    Ok(true)
}

// Readiness can inspect idle loaded writers, but must never shut them down.
pub(crate) async fn manual_descendants_settled(state: &ThreadState) -> anyhow::Result<bool> {
    Ok(inspect_manual_descendants(state).await?.is_some())
}

// Call only after the manual owner has proved root sampling, grants and
// callbacks complete, holds the native mutation gate, and has begun store
// suspension. Core requires all descendants unloaded for root suspension.
// This method does not call close_agent: ordinary thread shutdown releases the
// writer without changing the graph's historical open/closed status.
//
// Once invoked, every failure is a capture error, never a fresh refusal.
// Some writers may already have stopped. The owner must finalize its store
// suspension and close the session on error; it must not expose a partial
// tree checkpoint or claim the original live state remains intact.
pub(crate) async fn retire_manual_descendants(state: &ThreadState) -> anyhow::Result<()> {
    let inspected = inspect_manual_descendants(state)
        .await?
        .context("Manual descendants changed before suspension")?;
    tokio::time::timeout(crate::engine::CLOSE_TIMEOUT, async {
        for (id, expected) in &inspected.loaded {
            let current = state.engine.manager.get_thread(*id).await?;
            ensure!(
                Arc::ptr_eq(&current, expected) && manual_descendant_idle(&current).await?,
                "Manual descendant {id} changed before writer shutdown"
            );
            // Export will see an unloaded thread. Record its latest committed
            // settings while its writer is still available, as ordinary
            // checkpoint capture does. This belongs after the capture gate,
            // not in the non-destructive readiness check.
            if !matches!(current.agent_status().await, AgentStatus::Shutdown) {
                current.checkpoint_thread_settings().await?;
            }
            current.shutdown_and_wait().await?;
            ensure!(
                state
                    .engine
                    .manager
                    .remove_thread_if_matches(id, expected)
                    .await
                    .is_some(),
                "Manual descendant {id} changed during writer shutdown"
            );
        }
        let after = inspect_manual_descendants(state)
            .await?
            .context("Manual descendant history changed during writer shutdown")?;
        ensure!(
            after.loaded.is_empty() && graph_edges(&inspected.graph) == graph_edges(&after.graph),
            "Manual descendant graph changed during writer shutdown"
        );
        anyhow::Ok(())
    })
    .await
    .context("Manual descendant shutdown timed out")?
}

// Check portable graph records before import, and after each whole-tree
// export. Ordinary restore validates graph, history, planning and budgets.
pub(crate) fn validate_manual_descendants(state: &Value) -> anyhow::Result<()> {
    let checkpoint: Checkpoint = serde_json::from_value(state.clone())?;
    ensure!(
        checkpoint.interrupted_thread_ids.is_empty(),
        "Manual continuation cannot contain interrupted threads"
    );
    for child in &checkpoint.descendants {
        ensure!(
            !history_unfinished(&child.history),
            "Manual continuation requires terminal stored descendants"
        );
    }
    Ok(())
}

pub(crate) type CodeStates = std::collections::HashMap<ThreadId, codex_code_mode::CodeModeSnapshot>;

pub(crate) async fn code_state(
    state: &ThreadState,
    id: ThreadId,
) -> anyhow::Result<Option<codex_code_mode::CodeModeSnapshot>> {
    code_state_for_thread(&state.engine, id).await
}

pub(crate) async fn code_state_for_thread(
    engine: &EngineState,
    id: ThreadId,
) -> anyhow::Result<Option<codex_code_mode::CodeModeSnapshot>> {
    if let Some(snapshot) = engine.interception.pending_code_state(id) {
        return Ok(Some(snapshot.as_ref().clone()));
    }
    let code_enabled = engine
        .service_config
        .features
        .enabled(codex_core_api::Feature::CodeMode);
    let snapshot = match engine.manager.get_thread(id).await {
        Ok(thread) => {
            let snapshot = thread.export_code_mode_state().await.map_err(|error| {
                if error == "code-mode session has active cells" {
                    anyhow::Error::from(
                        crate::manual_checkpoint::UnsupportedManualState::LiveCodeCells,
                    )
                } else {
                    anyhow::Error::msg(error)
                }
            })?;
            if snapshot.is_none() && code_enabled {
                thread.ensure_rollout_materialized().await;
                thread.flush_rollout().await?;
            }
            snapshot
        }
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
    if snapshot.is_some() || !code_enabled {
        return Ok(snapshot);
    }
    // Establish the empty namespace before fork or compaction replaces history.
    // A missing runtime alone cannot prove that a resumed namespace is empty.
    let history = engine.load_checkpoint_history(id).await?;
    crate::manual_checkpoint::check_unused_code_history(id, &history)?;
    Ok(Some(codex_code_mode::CodeModeSnapshot {
        format_version: 1,
        next_cell_id: 1,
        stored_values: Default::default(),
        completed_cells: Default::default(),
    }))
}

pub(crate) async fn capture_code_states(state: &ThreadState) -> anyhow::Result<CodeStates> {
    let mut snapshots = CodeStates::new();
    for id in std::iter::once(state.id).chain(
        crate::subagents::list(state)
            .await?
            .into_iter()
            .map(|child| child.id),
    ) {
        if let Some(snapshot) = code_state(state, id).await? {
            snapshots.insert(id, snapshot);
        }
    }
    Ok(snapshots)
}

async fn export_inner(
    state: &ThreadState,
    pending: Option<&Value>,
    suspended: bool,
    captured_code: Option<&CodeStates>,
) -> anyhow::Result<Value> {
    let tools = state
        .thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>();
    let managed_tool_specs = if let Some(tools) = &tools {
        if let Some(key) = pending {
            check_capture(state, key)?;
        } else if !suspended {
            tools.check_idle(&state.thread).await?;
        }
        Some(tools.current_specs()?)
    } else {
        if let Some(key) = pending {
            check_capture(state, key)?;
        }
        None
    };
    let code_states = match captured_code {
        Some(snapshot) => snapshot.clone(),
        None => capture_code_states(state).await?,
    };
    let before = crate::subagents::list(state).await?;
    let mut interrupted_thread_ids = Vec::new();
    let (history, root_metadata) =
        capture(&state.engine, state.id, &mut interrupted_thread_ids).await?;
    let root_memory_mode = memory_mode(state, state.id).await?;
    let root_checklist = checklist(state, state.id).await?;
    let root_goal = goal(state, state.id).await?;
    let mut descendants = Vec::new();
    for child in &before {
        let (history, metadata) =
            capture(&state.engine, child.id, &mut interrupted_thread_ids).await?;
        descendants.push(Descendant {
            thread_id: child.id,
            parent_thread_id: child.parent_agent_id,
            status: child.status.to_owned(),
            closed_at: child.closed_at,
            history,
            metadata,
            memory_mode: memory_mode(state, child.id).await?,
            checklist: checklist(state, child.id).await?,
            goal: goal(state, child.id).await?,
            code_mode: code_states.get(&child.id).cloned(),
        });
    }
    ensure!(
        graph_edges(&before) == graph_edges(&crate::subagents::list(state).await?),
        "subagent graph changed during checkpoint; save again at a stable boundary"
    );
    // Native cancellation can retire a pending executor while history flushes.
    if let Some(key) = pending {
        check_capture(state, key)?;
    }
    Ok(serde_json::to_value(Checkpoint {
        format_version: if code_states.is_empty() { 2 } else { 4 },
        codex_revision: include_str!("../CODEX_REVISION").trim().to_owned(),
        source_session_id: state.id,
        history,
        root_metadata,
        root_memory_mode,
        root_checklist,
        root_goal,
        root_code_mode: code_states.get(&state.id).cloned(),
        descendants,
        interrupted_thread_ids,
        managed_tool_specs,
    })?)
}

// Only the managed tool ingress may capture before an effect, under its
// Python ownership gate. Check the exact pending call at each boundary.
pub(crate) fn check_capture(state: &ThreadState, request: &Value) -> anyhow::Result<()> {
    let tools = state
        .thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>()
        .context("tool capture requires managed tools")?;
    let key = serde_json::from_value(request.clone())?;
    tools.check_pending(&state.id.to_string(), &key)?;
    Ok(())
}

async fn memory_mode(
    state: &ThreadState,
    id: ThreadId,
) -> anyhow::Result<Option<ThreadMemoryMode>> {
    if state.engine.local_checklist_store.is_some() {
        // Local resume owns memory mode. Portable imports use the latest header.
        return Ok(None);
    }
    let store = state
        .engine
        .store
        .as_any()
        .downcast_ref::<crate::storage::MemoryThreadStore>()
        .context("checkpoint requires the SDK-owned thread store")?;
    Ok(store.memory_mode(id).await)
}

async fn checklist(
    state: &ThreadState,
    id: ThreadId,
) -> anyhow::Result<Option<crate::storage::ChecklistState>> {
    if let Some(store) = &state.engine.local_checklist_store {
        Ok(store.checklist(id).await?)
    } else {
        Ok(state
            .engine
            .store
            .as_any()
            .downcast_ref::<crate::storage::MemoryThreadStore>()
            .context("checkpoint requires the SDK-owned thread store")?
            .checklist(id)
            .await?)
    }
}

async fn goal(
    state: &ThreadState,
    id: ThreadId,
) -> anyhow::Result<Option<crate::goals::GoalSnapshot>> {
    match &state.engine.goals {
        Some(backend) => crate::goals::GoalSnapshot::capture(backend, id).await,
        None => Ok(None),
    }
}

fn graph_edges(
    children: &[crate::subagents::Subagent],
) -> HashSet<(ThreadId, ThreadId, &str, Option<i64>)> {
    children
        .iter()
        .map(|child| {
            (
                child.id,
                child.parent_agent_id,
                child.status,
                child.closed_at,
            )
        })
        .collect()
}

async fn capture(
    engine: &EngineState,
    id: ThreadId,
    interrupted: &mut Vec<ThreadId>,
) -> anyhow::Result<(Vec<RolloutItem>, StoredThread)> {
    if let Ok(thread) = engine.manager.get_thread(id).await {
        ensure!(
            !thread.config_snapshot().await.ephemeral,
            "checkpoint cannot capture ephemeral thread {id}"
        );
        if !matches!(
            thread.agent_status().await,
            AgentStatus::Shutdown | AgentStatus::NotFound
        ) {
            thread.checkpoint_thread_settings().await?;
            thread.ensure_rollout_materialized().await;
            thread.flush_rollout().await?;
        }
    }
    let mut metadata = engine.read_stored_thread(id).await?;
    let history = engine.load_checkpoint_history(id).await?;
    if history_unfinished(&history) {
        interrupted.push(id);
    }
    validate_history(id, &history)?;
    metadata.rollout_path = None;
    metadata.history = None;
    Ok((history, metadata))
}

fn history_unfinished(history: &[RolloutItem]) -> bool {
    history
        .iter()
        .rev()
        .find_map(|item| match item {
            RolloutItem::EventMsg(EventMsg::TurnStarted(_)) => Some(true),
            RolloutItem::EventMsg(EventMsg::TurnComplete(_) | EventMsg::TurnAborted(_)) => {
                Some(false)
            }
            _ => None,
        })
        .unwrap_or(false)
}

pub(crate) fn validate_history(
    id: ThreadId,
    history: &[RolloutItem],
) -> anyhow::Result<&SessionMeta> {
    let Some(RolloutItem::SessionMeta(header)) = history.first() else {
        anyhow::bail!("checkpoint thread {id} has no session metadata");
    };
    ensure!(header.meta.id == id, "checkpoint history identity mismatch");
    ensure!(
        header.meta.history_base.is_none(),
        "checkpoint history contains unresolved rollout references"
    );
    ensure!(
        header.meta.base_instructions.is_some(),
        "checkpoint lacks base instructions"
    );
    validate_imported_history(history)?;
    ensure!(
        header
            .meta
            .memory_mode
            .as_deref()
            .is_none_or(|mode| matches!(mode, "enabled" | "disabled")),
        "checkpoint contains an unknown memory mode"
    );
    if let Some(roots) = &header.meta.runtime_workspace_roots {
        for root in roots {
            AbsolutePathBuf::try_from(root.clone())?;
        }
    }
    Ok(&header.meta)
}

// Imported, cached, or caller-appended history must not pose as an accepted
// update. Only native tool appends (including same-batch starts) bypass this
// check; the pinned rollout never stores PlanUpdate in exported history.
pub(crate) fn validate_imported_history(history: &[RolloutItem]) -> anyhow::Result<()> {
    ensure!(
        !history
            .iter()
            .any(|item| matches!(item, RolloutItem::EventMsg(EventMsg::PlanUpdate(_)))),
        "caller-supplied history cannot contain a transient plan update"
    );
    Ok(())
}

pub async fn restore(
    engine: &Arc<EngineState>,
    mut checkpoint: Checkpoint,
    mut options: options::ThreadOptions,
    python_specs: Option<Vec<DynamicToolSpec>>,
) -> anyhow::Result<NativeThread> {
    let _lifecycle = engine.lifecycle.lock().await;
    checkpoint.check_revision()?;
    let ids = checkpoint.validate(python_specs.as_deref())?;
    let root = checkpoint.source_session_id;
    if engine
        .service_config
        .features
        .enabled(codex_core_api::Feature::CodeMode)
    {
        if checkpoint.root_code_mode.is_none() {
            crate::manual_checkpoint::check_unused_code_history(root, &checkpoint.history)?;
        }
        for child in &checkpoint.descendants {
            if child.code_mode.is_none() {
                crate::manual_checkpoint::check_unused_code_history(
                    child.thread_id,
                    &child.history,
                )?;
            }
        }
    }
    // Validated values may remain dormant when the destination disables CodeMode.
    // Current configuration alone decides whether a runtime can be prepared.
    let has_goals = checkpoint.root_goal.is_some()
        || checkpoint
            .descendants
            .iter()
            .any(|child| child.goal.is_some());
    ensure!(
        !has_goals || engine.goals.is_some(),
        "checkpoint goals require current native goal storage"
    );
    for id in &ids {
        engine.ensure_no_live_owner(*id).await?;
        match engine
            .store
            .read_thread(ReadThreadParams {
                thread_id: *id,
                include_archived: true,
                include_history: false,
            })
            .await
        {
            Err(ThreadStoreError::ThreadNotFound { .. }) => {}
            Err(error) => return Err(error.into()),
            Ok(_) => anyhow::bail!("checkpoint thread already exists in this engine"),
        }
    }
    let application_instructions = options
        .application_instructions
        .as_deref()
        .map(|expected| crate::application_instructions::validate(&checkpoint.history, expected))
        .transpose()?;
    let mut current_files = options.sdk_instruction_files.clone();
    let manual_slots = std::mem::take(&mut options.manual_tool_slots);
    let transform_context = options.transform_context;
    let before_compact = options.before_compact;
    let (mut startup, instructions) = engine.build_startup(options, None).await?;
    // Snapshot data is not authority to enable model-facing goal tools. The
    // validated record still requires goal storage above, but never the feature.
    if let Some(specs) = python_specs {
        // Core seeds these settings from Config on reopen. Recover only the
        // managed model fields; current credentials, policy and tools stay current.
        if let Some(settings) = managed_settings(&checkpoint.history, root) {
            ensure!(
                settings.reasoning_effort
                    != Some(codex_protocol::openai_models::ReasoningEffort::Persistent),
                "persistent reasoning is unsupported for replaceable sessions"
            );
            startup.config.model = Some(settings.model.clone());
            startup.config.model_reasoning_effort = settings.reasoning_effort.clone();
            startup.config.model_reasoning_summary = settings.reasoning_summary;
            startup.config.service_tier = settings.service_tier.clone();
            startup.config.sdk_tool_choice = settings.tool_choice;
        }
        // Validate the actual fixed history and current policy before import:
        // a bad catalog must not leave records that prevent a corrected restore.
        startup.initial_history = InitialHistory::Resumed(ResumedHistory {
            conversation_id: root,
            history: Arc::new(checkpoint.history.clone()),
            history_revision: None,
            rollout_path: None,
        });
        crate::python_tools::PythonTools::install_manual(
            &mut startup,
            specs,
            &manual_slots,
            transform_context,
            before_compact,
        )?;
    }
    if startup
        .thread_extension_init
        .get::<crate::native_agents::Policy>()
        .is_some()
    {
        // The catalog was admitted and frozen before executor connection.
        // Do not let a later checkpoint replace it, even with a slug prefix
        // the pinned model manager would treat as non-fallback metadata.
        ensure!(
            startup.config.model == engine.service_config.model,
            "NativeAgents restored model differs from its initially admitted model"
        );
    }
    // A zero-turn SDK receipt has no native reference context. If restored
    // already active, Core's first full context render cannot consult history
    // and projects the accepted instructions a second time. After validating
    // the complete checkpoint, transfer its one exact initial receipt through
    // Core's public injection API. Core creates its real context before it
    // records and acknowledges the same receipt. Native and nonempty histories
    // keep their original reconstruction path.
    let initial_receipt = application_instructions.as_ref().and_then(|accepted| {
        crate::application_instructions::take_initial_receipt(
            &mut checkpoint.history,
            accepted,
            root,
        )
    });
    if initial_receipt.is_none() {
        if let Some(accepted) = application_instructions {
            startup.thread_extension_init.insert(
                crate::application_instructions::Instructions::restored(
                    accepted.with_current_files(current_files.take()),
                ),
            );
        }
    }
    let store = engine
        .store
        .as_any()
        .downcast_ref::<crate::storage::MemoryThreadStore>()
        .context("portable checkpoint restore requires isolated memory storage")?;
    let mut imported = ImportState {
        engine,
        store,
        model_provider: startup.config.model_provider_id.clone(),
        ids: Vec::with_capacity(ids.len()),
        interrupted: &checkpoint.interrupted_thread_ids,
    };
    let mut started = None;
    if let Some(snapshot) = &checkpoint.root_code_mode {
        engine.interception.stage_code_state(root, snapshot.clone());
    }
    for child in &checkpoint.descendants {
        if let Some(snapshot) = &child.code_mode {
            engine
                .interception
                .stage_code_state(child.thread_id, snapshot.clone());
        }
    }
    let result = async {
        import(
            &mut imported,
            root,
            checkpoint.history,
            &checkpoint.root_metadata,
            checkpoint.root_memory_mode,
        )
        .await?;
        if let Some(checklist) = checkpoint.root_checklist {
            store.restore_checklist(checklist).await;
        }
        if let Some(goal) = checkpoint.root_goal {
            goal.restore(engine, &startup.config).await?;
        }
        for child in checkpoint.descendants {
            import(
                &mut imported,
                child.thread_id,
                child.history,
                &child.metadata,
                child.memory_mode,
            )
            .await?;
            if let Some(checklist) = child.checklist {
                store.restore_checklist(checklist).await;
            }
            if let Some(goal) = child.goal {
                goal.restore(engine, &startup.config).await?;
            }
            engine
                .agent_graph
                .restore_edge(
                    child.thread_id,
                    crate::storage::SpawnEdge {
                        parent: child.parent_thread_id,
                        status: if child.status == "closed" {
                            ThreadSpawnEdgeStatus::Closed
                        } else {
                            ThreadSpawnEdgeStatus::Open
                        },
                        closed_at: child.closed_at,
                    },
                )
                .await;
        }
        startup.initial_history = engine.history(&root.to_string()).await?;
        let native = engine.start_prepared(startup, instructions).await?;
        let state = native.current()?;
        started = Some(Arc::clone(&state));
        if engine
            .service_config
            .features
            .enabled(codex_core_api::Feature::CodeMode)
            && engine.interception.pending_code_state(root).is_some()
        {
            state
                .thread
                .prepare_code_mode_state()
                .await
                .map_err(anyhow::Error::msg)?;
        }
        if let Some(item) = initial_receipt {
            crate::application_instructions::publish_item(&state.thread, item, current_files)
                .await?;
        }
        anyhow::Ok(native)
    }
    .await;
    if result.is_err() {
        for id in &ids {
            engine.interception.clear_code_state(*id);
        }
    }
    match result {
        Ok(mut native) => {
            native.checkpoint_import = Some(imported.ids);
            Ok(native)
        }
        Err(error) => match engine
            .rollback_checkpoint_import(&imported.ids, started.as_deref())
            .await
        {
            Ok(()) => Err(error),
            Err(cleanup) => {
                let message = format!("{error}; checkpoint import rollback failed: {cleanup}");
                Err(error.context(message))
            }
        },
    }
}

struct ImportState<'a> {
    engine: &'a EngineState,
    store: &'a crate::storage::MemoryThreadStore,
    model_provider: String,
    ids: Vec<ThreadId>,
    interrupted: &'a [ThreadId],
}

async fn import(
    imported: &mut ImportState<'_>,
    id: ThreadId,
    mut history: Vec<RolloutItem>,
    stored: &StoredThread,
    captured_memory_mode: Option<ThreadMemoryMode>,
) -> anyhow::Result<()> {
    let engine = imported.engine;
    let meta = validate_history(id, &history)?.clone();
    let latest_memory_mode = history.iter().rev().find_map(|item| match item {
        RolloutItem::SessionMeta(header) if header.meta.id == id => {
            header.meta.memory_mode.as_deref()
        }
        _ => None,
    });
    let memory_mode = match (captured_memory_mode, latest_memory_mode) {
        (Some(mode), _) => mode,
        (None, Some("disabled")) => ThreadMemoryMode::Disabled,
        (None, Some("enabled") | None) => ThreadMemoryMode::Enabled,
        (None, Some(_)) => anyhow::bail!("checkpoint contains an unknown memory mode"),
    };
    imported
        .store
        .create_checkpoint_thread(
            CreateThreadParams {
                creator_user_id: meta.creator_user_id,
                creator_account_id: meta.creator_account_id,
                session_id: meta.session_id,
                thread_id: id,
                extra_config: stored.extra_config.clone(),
                forked_from_id: meta.forked_from_id,
                parent_thread_id: meta.parent_thread_id,
                source: meta.source,
                thread_source: meta.thread_source,
                originator: meta.originator,
                base_instructions: meta
                    .base_instructions
                    .context("checkpoint lacks base instructions")?,
                dynamic_tools: meta.dynamic_tools.unwrap_or_default(),
                // Capability access belongs to the destination Environment, not saved history.
                selected_capability_roots: engine.capability_roots()?,
                multi_agent_version: meta.multi_agent_version,
                history_mode: meta.history_mode,
                history_base: None,
                subagent_history_start_ordinal: meta.subagent_history_start_ordinal,
                initial_window_id: meta
                    .context_window
                    .map(|window| window.window_id)
                    .unwrap_or_else(|| ThreadId::new().to_string()),
                runtime_workspace_roots: meta
                    .runtime_workspace_roots
                    .map(|roots| roots.into_iter().map(AbsolutePathBuf::try_from).collect())
                    .transpose()?,
                metadata: ThreadPersistenceMetadata {
                    cwd: Some(meta.cwd),
                    model_provider: imported.model_provider.clone(),
                    memory_mode,
                },
            },
            &mut imported.ids,
        )
        .await?;
    // create_thread owns the canonical header; retaining a second copy shifts paginated ordinals.
    history.remove(0);
    if imported.interrupted.contains(&id) {
        let turn_id = history.iter().rev().find_map(|item| match item {
            RolloutItem::EventMsg(EventMsg::TurnStarted(event)) => Some(event.turn_id.clone()),
            _ => None,
        });
        history.push(RolloutItem::EventMsg(EventMsg::TurnAborted(
            TurnAbortedEvent {
                turn_id,
                reason: TurnAbortReason::Interrupted,
                error: None,
                started_at: None,
                completed_at: None,
                duration_ms: None,
            },
        )));
    }
    let result = async {
        engine
            .store
            .append_items(AppendThreadItemsParams {
                thread_id: id,
                items: history,
            })
            .await?;
        engine
            .store
            .persist_thread(id, PersistContext::Standard)
            .await?;
        engine
            .store
            .update_thread_metadata(UpdateThreadMetadataParams {
                thread_id: id,
                include_archived: true,
                patch: ThreadMetadataPatch {
                    name: Some(stored.name.clone()),
                    created_at: Some(stored.created_at),
                    updated_at: Some(stored.updated_at),
                    advance_recency_at: Some(stored.recency_at),
                    preview: Some(stored.preview.clone()),
                    first_user_message: stored.first_user_message.clone(),
                    model_provider: Some(imported.model_provider.clone()),
                    model: stored.model.clone(),
                    reasoning_effort: Some(stored.reasoning_effort.clone()),
                    approval_mode: Some(stored.approval_mode),
                    permission_profile: Some(stored.permission_profile.clone()),
                    cwd: Some(stored.cwd.clone()),
                    memory_mode: Some(memory_mode),
                    agent_nickname: Some(stored.agent_nickname.clone()),
                    agent_role: Some(stored.agent_role.clone()),
                    agent_path: Some(stored.agent_path.clone()),
                    token_usage: stored.token_usage.clone(),
                    ..Default::default()
                },
            })
            .await?;
        anyhow::Ok(())
    }
    .await;
    let shutdown = engine.store.shutdown_thread(id).await;
    result?;
    shutdown?;
    if stored.archived_at.is_some() {
        engine
            .store
            .archive_thread(ArchiveThreadParams { thread_id: id })
            .await?;
    }
    engine
        .restored_creation_times
        .lock()
        .await
        .insert(id, stored.created_at.into());
    Ok(())
}
