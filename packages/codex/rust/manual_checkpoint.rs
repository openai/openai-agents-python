//! Portable manual boundaries. Core remains the only model and history owner.

use anyhow::{Context, Result, ensure};
use codex_history::RolloutItem;
use codex_protocol::dynamic_tools::DynamicToolResponse;
use codex_protocol::{
    ThreadId,
    dynamic_tools::DynamicToolSpec,
    models::ResponseItem,
    protocol::{EventMsg, SessionSource, TokenUsage},
};
use codex_thread_store::StoredThread;
use serde_json::Value;
use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex, Weak};
use tokio::sync::oneshot;

const ORIGIN: &str = "agents-sdk";
#[derive(Debug)]
pub(crate) enum UnsupportedManualState {
    CodeState,
    LiveCodeCells,
    NativeWork,
    LiveTerminal,
    EphemeralChild,
}

impl std::fmt::Display for UnsupportedManualState {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::CodeState => "Code-mode manual checkpoints require unused code state",
            Self::LiveCodeCells => "code-mode session has active cells",
            Self::NativeWork => "Manual continuation contains nonportable native work",
            Self::LiveTerminal => "Manual checkpoints cannot include live background terminals",
            Self::EphemeralChild => "Manual checkpoints cannot include ephemeral children",
        })
    }
}

impl std::error::Error for UnsupportedManualState {}

pub(crate) struct Frame {
    pub turn: String,
    pub pending: Vec<ResponseItem>,
    pub order: VecDeque<String>,
    pub accepted: HashMap<String, ResponseItem>,
    pub seen: HashSet<String>,
    pub state: Value,
    pub receivers: Vec<oneshot::Receiver<DynamicToolResponse>>,
    pub sealed: bool,
}

pub(crate) fn validate(
    state: &Value,
    instructions: &str,
    specs: &[DynamicToolSpec],
    manual_slots: &[usize],
) -> Result<Frame> {
    ensure!(
        (state["format_version"] == 3 || state["format_version"] == 5)
            && state["codex_revision"] == include_str!("../CODEX_REVISION").trim(),
        "Unsupported native manual continuation version or revision"
    );
    ensure!(
        state
            .get("pending_actions")
            .is_none_or(|value| value.as_array().is_some_and(Vec::is_empty)),
        "Manual continuation cannot contain pending_actions"
    );
    crate::checkpoint::validate_manual_descendants(state)?;
    let frame = state["native_continuation"]
        .as_object()
        .context("Missing native manual continuation")?;
    ensure!(
        (frame.len() == 6 || frame.len() == 7)
            && frame.keys().all(|key| matches!(
                key.as_str(),
                "turn_id"
                    | "seen_call_ids"
                    | "turn_usage"
                    | "reply_order"
                    | "accepted_replies"
                    | "pending_manual"
                    | "output_schema"
            ))
            && frame.get("output_schema").is_none_or(Value::is_object),
        "Invalid native continuation fields"
    );
    let turn = frame["turn_id"]
        .as_str()
        .filter(|id| !id.is_empty())
        .context("Manual continuation has no turn identity")?
        .to_owned();
    let seen: Vec<String> = serde_json::from_value(frame["seen_call_ids"].clone())?;
    let seen_calls: HashSet<String> = seen.iter().cloned().collect();
    ensure!(
        seen_calls.len() == seen.len() && !seen.iter().any(String::is_empty),
        "Repeated or empty continuation call identity"
    );
    let order: VecDeque<String> = serde_json::from_value(frame["reply_order"].clone())?;
    let ordered: HashSet<String> = order.iter().cloned().collect();
    ensure!(
        ordered.len() == order.len() && ordered.is_subset(&seen_calls),
        "Continuation receipt order does not match admitted calls"
    );
    let accepted: HashMap<String, ResponseItem> =
        serde_json::from_value(frame["accepted_replies"].clone())?;
    let pending = frame["pending_manual"]
        .as_array()
        .context("Invalid pending manual list")?;
    ensure!(
        !pending.is_empty(),
        "Manual continuation has no pending action"
    );
    let history: Vec<RolloutItem> = serde_json::from_value(state["history"].clone())?;
    ensure!(
        portable_manual_actions(&history),
        "Manual continuation contains nonportable native work"
    );
    crate::application_instructions::validate(&history, instructions)?;
    let root: StoredThread = serde_json::from_value(state["root_metadata"].clone())?;
    let source_id: ThreadId = serde_json::from_value(state["source_session_id"].clone())?;
    let header = crate::checkpoint::validate_history(source_id, &history)?;
    ensure!(
        root.thread_id == source_id
            && root.parent_thread_id.is_none()
            && header.parent_thread_id.is_none()
            && (header.source == SessionSource::Exec
                || (header.originator == ORIGIN
                    && header.source == SessionSource::Custom(ORIGIN.into()))),
        "Manual checkpoint does not belong to an ordinary native root"
    );
    let _: TokenUsage = serde_json::from_value(frame["turn_usage"].clone())?;
    // Validate against the canonical last active turn before changing the
    // owner. Never infer accepted effects from caller-provided names alone.
    let mut calls: HashMap<String, Value> = HashMap::new();
    let mut admitted_order = Vec::new();
    let mut results = HashSet::new();
    let mut last_started = None;
    for line in &history {
        let raw = serde_json::to_value(line)?;
        if raw["type"] == "event_msg" && raw["payload"]["type"] == "task_started" {
            last_started = raw["payload"]["turn_id"].as_str().map(str::to_owned);
            calls.clear();
            admitted_order.clear();
            results.clear();
        }
        if raw["type"] == "event_msg"
            && matches!(
                raw["payload"]["type"].as_str(),
                Some("task_complete" | "turn_aborted")
            )
        {
            last_started = None;
        }
        if let RolloutItem::ResponseItem(value) = line {
            let value = value.item.clone();
            let raw = serde_json::to_value(&value)?;
            if last_started.as_deref() == Some(&turn) {
                if matches!(
                    raw["type"].as_str(),
                    Some("function_call" | "custom_tool_call")
                ) {
                    let call = raw["call_id"]
                        .as_str()
                        .context("History call lacks identity")?
                        .to_owned();
                    admitted_order.push(call.clone());
                    ensure!(
                        calls.insert(call, raw.clone()).is_none(),
                        "Duplicate admitted history call"
                    );
                }
                if matches!(
                    raw["type"].as_str(),
                    Some("function_call_output" | "custom_tool_call_output")
                ) {
                    let call = raw["call_id"]
                        .as_str()
                        .context("History reply lacks identity")?
                        .to_owned();
                    ensure!(
                        admitted_order
                            .get(results.len())
                            .is_some_and(|expected| expected == &call)
                            && calls
                                .get(&call)
                                .is_some_and(|original| receipt_matches(original, &raw)),
                        "History replies are not the ordered prefix of admitted calls"
                    );
                    ensure!(results.insert(call), "Duplicate history reply");
                }
            }
        }
    }
    ensure!(
        last_started.as_deref() == Some(&turn),
        "Continuation is not the unfinished last turn"
    );
    ensure!(
        calls.keys().cloned().collect::<HashSet<_>>() == seen_calls,
        "Continuation seen calls do not match canonical history"
    );
    ensure!(
        results.is_subset(&seen_calls)
            && results.is_disjoint(&ordered)
            && results.len() + order.len() == seen_calls.len(),
        "Continuation history/remaining receipt partition is incomplete"
    );
    ensure!(
        admitted_order
            .iter()
            .filter(|call| !results.contains(*call))
            .eq(order.iter()),
        "Continuation result ordering differs from canonical calls"
    );
    let saved_specs: Vec<DynamicToolSpec> =
        serde_json::from_value(state["managed_tool_specs"].clone())?;
    let saved_names: HashSet<_> = saved_specs
        .iter()
        .filter_map(|spec| {
            if let DynamicToolSpec::Function(spec) = spec {
                Some(spec.name.as_str())
            } else {
                None
            }
        })
        .collect();
    ensure!(
        saved_names.len() == saved_specs.len(),
        "Unsupported saved tool declarations"
    );
    let mut admitted = Vec::new();
    let mut pending_ids = HashSet::new();
    for value in pending {
        let fields = value
            .as_object()
            .context("Invalid manual continuation request")?;
        ensure!(
            fields.len() == 3
                && fields.contains_key("call_id")
                && fields.contains_key("tool")
                && fields.contains_key("arguments"),
            "Unknown manual continuation authority fields"
        );
        let call = value["call_id"]
            .as_str()
            .context("Pending manual has no call identity")?;
        let name = value["tool"]
            .as_str()
            .context("Pending manual has no tool")?;
        let args = value["arguments"]
            .as_str()
            .context("Pending manual arguments must be JSON text")?;
        ensure!(
            pending_ids.insert(call.to_owned())
                && ordered.contains(call)
                && !accepted.contains_key(call)
                && saved_names.contains(name),
            "Invalid pending manual call ownership"
        );
        let saved = calls
            .get(call)
            .context("Pending manual has no canonical call")?;
        ensure!(
            saved["type"] == "function_call"
                && saved["name"] == name
                && matches!(saved["namespace"].as_str(), None | Some("" | "functions"))
                && saved["arguments"] == args,
            "Pending manual arguments or name differ from accepted history"
        );
        let slot = specs
            .iter()
            .position(|spec| {
                matches!(spec,
                    DynamicToolSpec::Function(spec) if spec.name == name
                )
            })
            .context("Current catalog does not allow a pending manual tool")?;
        ensure!(
            manual_slots.contains(&slot),
            "Current declaration is not a manual tool"
        );
        admitted.push(serde_json::from_value::<ResponseItem>(saved.clone())?);
    }
    ensure!(
        pending_ids.len() + accepted.len() == order.len(),
        "Continuation receipt partition is incomplete"
    );
    ensure!(
        order
            .iter()
            .filter(|call| pending_ids.contains(*call))
            .eq(admitted.iter().map(|item| match item {
                ResponseItem::FunctionCall { call_id, .. } => call_id,
                _ => unreachable!(),
            })),
        "Pending manual requests differ from canonical receipt order"
    );
    for (call, receipt) in &accepted {
        let raw = serde_json::to_value(receipt)?;
        ensure!(
            ordered.contains(call)
                && !pending_ids.contains(call)
                && calls
                    .get(call)
                    .is_some_and(|original| receipt_matches(original, &raw)),
            "Accepted continuation result has wrong call identity or kind"
        );
    }
    Ok(Frame {
        turn,
        pending: admitted,
        order,
        accepted,
        seen: seen_calls,
        state: state.clone(),
        receivers: Vec::new(),
        sealed: false,
    })
}

fn receipt_matches(call: &Value, receipt: &Value) -> bool {
    call["call_id"] == receipt["call_id"]
        && matches!(
            (call["type"].as_str(), receipt["type"].as_str()),
            (Some("function_call"), Some("function_call_output"))
                | (Some("custom_tool_call"), Some("custom_tool_call_output"))
        )
}

pub(crate) type Owner = Arc<Mutex<Option<Frame>>>;
pub(crate) type WeakOwner = Weak<Mutex<Option<Frame>>>;

// v3 carries completed typed function and apply_patch receipts. The native
// completed-ID set establishes settlement before capture. Pending grants stay
// manual FunctionCalls; no native call acquires a new grant from saved data.
fn portable_manual_actions(history: &[RolloutItem]) -> bool {
    // Search only reads the admitted catalog. Carry a completed client search
    // as history, never as a pending effect or a destination grant.
    let mut search_receipts = HashSet::new();
    for item in history.iter().rev() {
        if matches!(item, RolloutItem::EventMsg(EventMsg::TurnStarted(_))) {
            break;
        }
        let RolloutItem::ResponseItem(envelope) = item else {
            continue;
        };
        match &envelope.item {
            ResponseItem::Message { .. }
            | ResponseItem::Reasoning { .. }
            | ResponseItem::AdditionalTools { .. }
            | ResponseItem::AgentMessage { .. }
            | ResponseItem::Compaction { .. }
            | ResponseItem::ContextCompaction { .. }
            | ResponseItem::ConfigurationUpdate { .. }
            | ResponseItem::FunctionCall { .. }
            | ResponseItem::FunctionCallOutput {
                call_id: Some(_), ..
            } => {}
            ResponseItem::CustomToolCall { name, .. } if name == "apply_patch" => {}
            ResponseItem::CustomToolCallOutput { .. } => {}
            ResponseItem::ToolSearchOutput {
                call_id: Some(call_id),
                execution,
                status,
                ..
            } if execution == "client" && status == "completed" => {
                if call_id.is_empty() || !search_receipts.insert(call_id) {
                    return false;
                }
            }
            ResponseItem::ToolSearchCall {
                call_id: Some(call_id),
                execution,
                ..
            } if execution == "client" => {
                if !search_receipts.remove(call_id) {
                    return false;
                }
            }
            _ => return false,
        }
    }
    search_receipts.is_empty()
}

// Legacy or unopened runtimes have no snapshot. Admit them only when full
// canonical history proves the namespace has never executed a cell.
fn check_unused_code_state(
    thread: &crate::engine::ThreadState,
    id: ThreadId,
    history: &[RolloutItem],
) -> Result<()> {
    if !thread
        .engine
        .service_config
        .features
        .enabled(codex_core_api::Feature::CodeMode)
    {
        return Ok(());
    }
    check_unused_code_history(id, history)
}

pub(crate) fn check_unused_code_history(id: ThreadId, history: &[RolloutItem]) -> Result<()> {
    ensure!(
        matches!(
            history.first(),
            Some(RolloutItem::SessionMeta(header))
                if header.meta.id == id
                    && header.meta.history_base.is_none()
        ),
        UnsupportedManualState::CodeState
    );
    for item in &history[1..] {
        match item {
            // Replacement and rollback can hide earlier executions. A later
            // header is not proof of a new, empty VM in this native thread.
            RolloutItem::SessionMeta(_)
            | RolloutItem::Compacted(_)
            | RolloutItem::EventMsg(EventMsg::ThreadRolledBack(_)) => {
                anyhow::bail!(UnsupportedManualState::CodeState);
            }
            RolloutItem::ResponseItem(envelope) => match &envelope.item {
                ResponseItem::Compaction { .. } | ResponseItem::ContextCompaction { .. } => {
                    anyhow::bail!(UnsupportedManualState::CodeState);
                }
                ResponseItem::FunctionCall {
                    name, namespace, ..
                }
                | ResponseItem::CustomToolCall {
                    name, namespace, ..
                } => {
                    let native_namespace =
                        matches!(namespace.as_deref(), None | Some("" | "functions"));
                    ensure!(
                        !(native_namespace
                            && matches!(
                                name.as_str(),
                                "exec" | "wait" | "functions.exec" | "functions.wait"
                            )),
                        UnsupportedManualState::CodeState
                    );
                }
                _ => {}
            },
            _ => {}
        }
    }
    Ok(())
}

fn policy_failed(thread: &crate::engine::ThreadState, turn: &str) -> bool {
    thread
        .thread
        .thread_extension_data()
        .get::<crate::mcp_result_control::McpResultControl>()
        .is_some_and(|control| control.has_settled_failure(turn))
}

impl Frame {
    pub(crate) fn record(
        &mut self,
        key: &crate::python_tools::PendingKey,
        value: &DynamicToolResponse,
    ) {
        if !self.sealed && key.turn == self.turn && self.order.contains(&key.call) {
            self.accepted.insert(
                key.call.clone(),
                crate::python_tools::PythonOutput(value.clone()).receipt(key),
            );
        }
    }

    pub(crate) fn snapshot(&self) -> Result<Value> {
        let mut value = self.state.clone();
        value["native_continuation"]["accepted_replies"] = serde_json::to_value(&self.accepted)?;
        value["native_continuation"]["pending_manual"]
            .as_array_mut()
            .context("Missing pending manual calls")?
            .retain(|call| {
                call["call_id"]
                    .as_str()
                    .is_some_and(|id| !self.accepted.contains_key(id))
            });
        ensure!(
            value["native_continuation"]["pending_manual"]
                .as_array()
                .is_some_and(|pending| !pending.is_empty()),
            "No pending manual replies remain"
        );
        Ok(value)
    }
}

pub(crate) fn prepare(thread: &crate::engine::ThreadState, owner: &Owner) -> Result<()> {
    let mut current = owner
        .lock()
        .map_err(|_| anyhow::anyhow!("Manual continuation poisoned"))?;
    let frame = current.as_mut().context("No manual continuation")?;
    let history: Vec<RolloutItem> = serde_json::from_value(frame.state["history"].clone())?;
    // Validate before granting any restored request or binding its sender.
    if frame.state["root_code_mode"].is_null() {
        check_unused_code_state(thread, thread.id, &history)?;
    }
    if thread
        .engine
        .service_config
        .features
        .enabled(codex_core_api::Feature::CodeMode)
        && let Some(children) = frame.state.get("descendants")
    {
        for child in children
            .as_array()
            .context("Invalid manual continuation descendants")?
        {
            let id = serde_json::from_value(child["thread_id"].clone())?;
            let history: Vec<RolloutItem> = serde_json::from_value(child["history"].clone())?;
            if child["code_mode"].is_null() {
                check_unused_code_state(thread, id, &history)?;
            }
        }
    }
    ensure!(
        frame.receivers.is_empty() && !frame.sealed,
        "Manual continuation already opened"
    );
    let tools = thread
        .thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>()
        .context("Manual continuation requires current tools")?;
    let source_id = serde_json::from_value(frame.state["source_session_id"].clone())?;
    let model = crate::checkpoint::managed_settings(&history, source_id)
        .map(|settings| settings.model.as_str());
    for item in &frame.pending {
        frame.receivers.push(tools.enqueue_manual_reentry(
            &thread.id.to_string(),
            &frame.turn,
            item,
            model,
        )?);
    }
    tools.bind_manual_restore(Arc::downgrade(owner));
    Ok(())
}

pub(crate) async fn continue_if_ready(
    thread: &crate::engine::ThreadState,
    owner: &Owner,
    trace: Option<codex_protocol::protocol::W3cTraceContext>,
) -> Result<Value> {
    let _operation = thread.engine.operation(false)?;
    let _mutation = thread.mutations.lock().await;
    let frame = {
        let mut current = owner
            .lock()
            .map_err(|_| anyhow::anyhow!("Manual continuation poisoned"))?;
        let Some(frame) = current.as_ref() else {
            return Ok(serde_json::json!({"status":"not_pending"}));
        };
        ensure!(!frame.sealed, "Manual continuation is sealed");
        if frame
            .order
            .iter()
            .any(|call| !frame.accepted.contains_key(call))
        {
            return Ok(serde_json::json!({"status":"pending"}));
        }
        current.take().expect("Checked continuation")
    };
    let outputs = frame
        .order
        .iter()
        .map(|call| frame.accepted[call].clone())
        .collect();
    thread.thread.inject_response_items(outputs).await?;
    let result = crate::ops::recover_prepared(
        &thread.thread,
        frame.turn,
        frame.seen.into_iter().collect(),
        match frame.state["native_continuation"].get("output_schema") {
            Some(schema) => serde_json::json!({"output_schema":schema,"trace":trace}),
            None => serde_json::json!({"trace":trace}),
        },
    )
    .await?;
    ensure!(
        result["status"] == "started",
        "Core did not resume the manual turn"
    );
    Ok(result)
}

pub(crate) async fn ready(thread: &crate::engine::ThreadState, owner: &Owner) -> Result<bool> {
    let restored = owner
        .lock()
        .map_err(|_| anyhow::anyhow!("Manual continuation poisoned"))?
        .as_ref()
        .is_some_and(|frame| !frame.sealed);
    let tools = thread
        .thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>()
        .context("Manual continuation requires current tools")?;
    let Ok(pending) = tools.pending_manual_keys(&thread.id.to_string()) else {
        return Ok(false);
    };
    if policy_failed(thread, &pending[0].turn) {
        return Ok(false);
    }
    if thread
        .engine
        .service_config
        .features
        .enabled(codex_core_api::Feature::CodeMode)
    {
        // Nested code-mode grants cannot become direct-call checkpoints.
        // Reject that permanent boundary before waiting for store readiness.
        let history = thread.engine.load_checkpoint_history(thread.id).await?;
        if crate::checkpoint::code_state(thread, thread.id)
            .await?
            .is_none()
        {
            check_unused_code_state(thread, thread.id, &history)?;
        }
    }
    if !restored {
        let store = thread
            .engine
            .store
            .as_any()
            .downcast_ref::<crate::storage::MemoryThreadStore>()
            .context("Manual continuation requires the in-memory store")?;
        if !store
            .manual_suspension_ready(
                thread.id,
                &pending,
                &tools.completed_calls(&pending[0].turn),
            )
            .await?
        {
            return Ok(false);
        }
    }
    // Read after the complete-sampling gate: the response may have recorded
    // another code call while readiness waited for the store.
    let history = thread.engine.load_checkpoint_history(thread.id).await?;
    if crate::checkpoint::code_state(thread, thread.id)
        .await?
        .is_none()
    {
        check_unused_code_state(thread, thread.id, &history)?;
    }
    ensure!(
        restored || portable_manual_actions(&history),
        UnsupportedManualState::NativeWork
    );
    // Finished calls may have registered a child or background terminal.
    // Observe those resources after the call-completion snapshot.
    ensure!(
        thread.thread.list_background_terminals().await.is_empty(),
        UnsupportedManualState::LiveTerminal
    );
    if policy_failed(thread, &pending[0].turn)
        || !crate::checkpoint::manual_descendants_settled(thread).await?
    {
        return Ok(false);
    }
    // A finished child may still own code-mode stored values or live cells.
    // Its terminal turn and empty native process list do not prove those
    // values can be dropped. Child configs inherit the root's code mode.
    if thread
        .engine
        .service_config
        .features
        .enabled(codex_core_api::Feature::CodeMode)
    {
        for child in crate::subagents::list(thread).await? {
            let history = thread.engine.load_checkpoint_history(child.id).await?;
            if crate::checkpoint::code_state(thread, child.id)
                .await?
                .is_none()
            {
                check_unused_code_state(thread, child.id, &history)?;
            }
        }
    }
    Ok(tools
        .pending_manual_keys(&thread.id.to_string())
        .is_ok_and(|current| current == pending))
}

// None is a refusal before suspension; the current writer and grants remain live.
pub(crate) async fn capture(
    thread: &crate::engine::ThreadState,
    owner: &Owner,
    output_schema: Option<Value>,
) -> Result<Option<Value>> {
    let _operation = thread.engine.operation(false)?;
    let _mutation = thread.mutations.lock().await;
    let restored = {
        let current = owner
            .lock()
            .map_err(|_| anyhow::anyhow!("Manual continuation poisoned"))?;
        current.is_some()
    };
    if restored {
        ensure!(
            ready(thread, owner).await?,
            "Manual continuation is no longer settled"
        );
        let mut snapshot = crate::checkpoint::export_restored_manual(thread).await?;
        // Export sees only the unchanged root manual turn as interrupted.
        // Child interruptions must remain visible and are rejected below.
        let root = serde_json::to_value(thread.id)?;
        snapshot["interrupted_thread_ids"]
            .as_array_mut()
            .context("Checkpoint lacks interrupted threads")?
            .retain(|id| *id != root);
        crate::checkpoint::validate_manual_descendants(&snapshot)?;
        ensure!(
            ready(thread, owner).await?,
            "Manual continuation changed during export"
        );
        let mut current = owner
            .lock()
            .map_err(|_| anyhow::anyhow!("Manual continuation poisoned"))?;
        let frame = current
            .as_mut()
            .context("Manual continuation is no longer pending")?;
        snapshot["format_version"] = serde_json::json!(if snapshot["format_version"] == 4 {
            5
        } else {
            3
        });
        snapshot["native_continuation"] = frame.snapshot()?["native_continuation"].clone();
        frame.sealed = true;
        frame.receivers.clear();
        return Ok(Some(snapshot));
    }
    match ready(thread, owner).await {
        Ok(true) => {}
        Ok(false) => return Ok(None),
        // No writer suspension has begun. Keep the live turn and grants
        // usable when the earlier Python readiness probe became stale.
        Err(error) if error.is::<UnsupportedManualState>() => return Ok(None),
        Err(error) => return Err(error),
    }
    let tools = thread
        .thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>()
        .context("Manual continuation requires current tools")?;
    let pending = tools.pending_manual_keys(&thread.id.to_string())?;
    let turn = pending[0].turn.clone();
    ensure!(
        pending.iter().all(|key| key.turn == turn),
        "Manual calls span different turns"
    );
    let store = thread
        .engine
        .store
        .as_any()
        .downcast_ref::<crate::storage::MemoryThreadStore>()
        .context("Manual continuation requires the in-memory store")?;
    let code_states = match crate::checkpoint::capture_code_states(thread).await {
        Ok(states) => states,
        Err(error) if error.is::<UnsupportedManualState>() => return Ok(None),
        Err(error) => return Err(error),
    };
    match store
        .begin_manual_suspension(thread.id, pending.clone(), &tools.completed_calls(&turn))
        .await
    {
        Ok(true) => {}
        Ok(false) => return Ok(None),
        Err(error) if error.is::<UnsupportedManualState>() => return Ok(None),
        Err(error) => return Err(error),
    }
    // Finalize the begun store transaction even if retiring an idle child
    // fails. The Python owner treats this as terminal and closes the session;
    // it must not inherit a store still intercepting cancellation receipts.
    let outcome: Result<_> = async {
        crate::checkpoint::retire_manual_descendants(thread).await?;
        Ok(thread.thread.suspend_turn_and_shutdown().await?)
    }
    .await;
    let suspended_turn = match &outcome {
        Ok(codex_protocol::turn_input::SuspendTurnOutcome::Suspended { turn_id }) => {
            Some(turn_id.as_str())
        }
        _ => None,
    };
    let preserved = store
        .finish_manual_suspension(thread.id, suspended_turn)
        .await;
    let suspended = suspended_turn.is_some();
    let snapshot = match preserved {
        Ok(true) if suspended_turn == Some(turn.as_str()) => {
            crate::checkpoint::export_suspended(thread, &code_states).await
        }
        Ok(_) => Err(anyhow::anyhow!(
            "Core did not preserve the pending manual turn"
        )),
        Err(error) => Err(error.into()),
    };
    if suspended {
        thread.retire_suspended().await;
    }
    outcome?;
    let mut snapshot = snapshot?;
    snapshot["format_version"] = serde_json::json!(if snapshot["format_version"] == 4 {
        5
    } else {
        3
    });
    let root = serde_json::to_value(thread.id)?;
    snapshot["interrupted_thread_ids"]
        .as_array_mut()
        .context("Checkpoint lacks interrupted threads")?
        .retain(|id| *id != root);
    crate::checkpoint::validate_manual_descendants(&snapshot)?;
    let history: Vec<RolloutItem> = serde_json::from_value(snapshot["history"].clone())?;
    let mut calls = Vec::new();
    let mut outputs = HashMap::new();
    let mut active_start = None;
    let mut usage = TokenUsage::default();
    for (index, item) in history.iter().enumerate() {
        let raw = serde_json::to_value(item)?;
        if raw["type"] == "event_msg" && raw["payload"]["type"] == "task_started" {
            active_start = (raw["payload"]["turn_id"] == turn).then_some(index);
            calls.clear();
            outputs.clear();
        }
        if active_start.is_none() {
            continue;
        }
        if let RolloutItem::ResponseItem(item) = item {
            match &item.item {
                ResponseItem::FunctionCall { call_id, .. }
                | ResponseItem::CustomToolCall { call_id, .. } => {
                    calls.push((call_id.clone(), item.item.clone()))
                }
                ResponseItem::FunctionCallOutput { .. }
                | ResponseItem::CustomToolCallOutput { .. } => {
                    let output = serde_json::to_value(&item.item)?;
                    let call_id = output["call_id"]
                        .as_str()
                        .context("Tool output lacks call identity")?;
                    ensure!(
                        outputs
                            .insert(call_id.to_owned(), item.item.clone())
                            .is_none(),
                        "Repeated tool result"
                    );
                }
                _ => {}
            }
        }
        if let RolloutItem::TokenUsageRecord(record) = item {
            if record.turn_id == turn && record.thread_id == thread.id {
                usage = record.turn_token_usage.clone();
            }
        }
    }
    let start = active_start.context("Core lost the pending turn history")?;
    let prefix = calls
        .iter()
        .take_while(|(id, _)| outputs.contains_key(id))
        .count();
    let order: Vec<String> = calls
        .iter()
        .skip(prefix)
        .map(|(id, _)| id.clone())
        .collect();
    let pending_ids: HashSet<&str> = pending.iter().map(|key| key.call.as_str()).collect();
    let mut pending_manual = Vec::new();
    for (id, item) in &calls {
        if !pending_ids.contains(id.as_str()) {
            continue;
        }
        let ResponseItem::FunctionCall {
            name, arguments, ..
        } = item
        else {
            anyhow::bail!("A custom call cannot own a manual reply")
        };
        pending_manual.push(serde_json::json!({"call_id":id,"tool":name,"arguments":arguments}));
    }
    ensure!(
        pending_manual.len() == pending.len(),
        "Core history does not contain every manual call"
    );
    let accepted: HashMap<_, _> = outputs
        .into_iter()
        .filter(|(id, _)| order.contains(id))
        .collect();
    let history: Vec<_> = history
        .into_iter()
        .enumerate()
        .filter_map(|(index, item)| {
            if index >= start {
                if let RolloutItem::ResponseItem(response) = &item {
                    let call_id = match &response.item {
                        ResponseItem::FunctionCallOutput { call_id, .. } => call_id.as_ref(),
                        ResponseItem::CustomToolCallOutput { call_id, .. } => Some(call_id),
                        _ => None,
                    };
                    if call_id.is_some_and(|id| order.contains(id)) {
                        return None;
                    }
                }
            }
            Some(item)
        })
        .collect();
    snapshot["history"] = serde_json::to_value(history)?;
    snapshot["native_continuation"] = serde_json::json!({
        "turn_id":turn,"seen_call_ids":calls.iter().map(|(id,_)|id).collect::<Vec<_>>(),
        "turn_usage":usage,"reply_order":order,"accepted_replies":accepted,"pending_manual":pending_manual
    });
    if let Some(schema) = output_schema {
        snapshot["native_continuation"]["output_schema"] = schema;
    }
    Ok(Some(snapshot))
}
