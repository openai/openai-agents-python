//! Portable manual boundaries. Core remains the only model and history owner.

use anyhow::{Context, Result, ensure};
use codex_history::{ResponseItemEnvelope, RolloutItem};
use codex_protocol::dynamic_tools::DynamicToolResponse;
use codex_protocol::{
    ThreadId,
    dynamic_tools::DynamicToolSpec,
    models::ResponseItem,
    protocol::{EventMsg, SessionSource, TokenUsage},
};
use codex_thread_store::StoredThread;
use serde::{Deserialize, Serialize};
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

/// Ordinary final outcome data; it never contains a live receipt or capability.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ToolOutcomes {
    pub response_observation_id: String,
    pub call_ids: Vec<String>,
    pub outcomes: Vec<(String, bool)>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub held_call_ids: Vec<String>,
}

impl ToolOutcomes {
    fn capture(history: &[RolloutItem], thread: ThreadId, turn: &str) -> Result<Option<Self>> {
        let Some((anchor, record)) =
            history
                .iter()
                .enumerate()
                .rev()
                .find_map(|(index, item)| match item {
                    RolloutItem::ResponseObservationRecord(record)
                        if record.thread_id == thread && record.turn_id == turn =>
                    {
                        Some((index, record))
                    }
                    _ => None,
                })
        else {
            return Ok(None);
        };
        if record.observation.status
            != codex_protocol::protocol::ResponseObservationStatus::Completed
        {
            return Ok(None);
        }
        let original_start = history[..anchor]
            .iter()
            .rposition(|item| matches!(item, RolloutItem::EventMsg(EventMsg::TurnStarted(_))))
            .context("Recovered tool batch lacks its original turn start")?;
        ensure!(
            matches!(&history[original_start], RolloutItem::EventMsg(EventMsg::TurnStarted(start)) if start.turn_id == turn),
            "Recovered tool batch has wrong original turn"
        );
        let start = history[original_start + 1..anchor]
            .iter()
            .rposition(|item| matches!(item, RolloutItem::ResponseObservationRecord(_)))
            .map_or(original_start + 1, |index| original_start + index + 2);
        let call_ids: Vec<_> = history[start..anchor]
            .iter()
            .filter_map(|item| match item {
                RolloutItem::ResponseItem(item) => match &item.item {
                    ResponseItem::FunctionCall { call_id, .. }
                    | ResponseItem::CustomToolCall { call_id, .. } => Some(call_id.clone()),
                    ResponseItem::ToolSearchCall { call_id, .. } => call_id.clone(),
                    _ => None,
                },
                _ => None,
            })
            .collect();
        if call_ids.is_empty() {
            return Ok(None);
        }
        let unique: HashSet<_> = call_ids.iter().collect();
        ensure!(
            unique.len() == call_ids.len() && !call_ids.iter().any(String::is_empty),
            "Invalid recovered tool batch identities"
        );
        let mut known = HashMap::new();
        for item in &history[anchor + 1..] {
            if let RolloutItem::ResponseItem(item) = item {
                let (id, success) = match &item.item {
                    ResponseItem::FunctionCallOutput {
                        call_id, output, ..
                    } => (call_id.as_deref(), output.success),
                    ResponseItem::CustomToolCallOutput {
                        call_id, output, ..
                    } => (Some(call_id.as_str()), output.success),
                    ResponseItem::ToolSearchOutput {
                        call_id,
                        status,
                        execution,
                        ..
                    } if execution == "client" && status == "completed" => {
                        (call_id.as_deref(), Some(true))
                    }
                    _ => continue,
                };
                if let (Some(id), Some(success)) = (id, success) {
                    if unique.contains(&id.to_owned()) {
                        ensure!(
                            known.insert(id.to_owned(), success).is_none(),
                            "Repeated recovered final outcome"
                        );
                    }
                }
            }
        }
        let outcomes = call_ids
            .iter()
            .filter_map(|id| known.get(id).map(|success| (id.clone(), *success)))
            .collect();
        Ok(Some(Self {
            held_call_ids: Vec::new(),
            response_observation_id: record.id.clone(),
            call_ids,
            outcomes,
        }))
    }
    fn validate(&self, history: &[RolloutItem], thread: ThreadId, turn: &str) -> Result<()> {
        let expected = Self::capture(history, thread, turn)?
            .context("Recovered tool outcomes lack a retained completed response")?;
        ensure!(
            self.response_observation_id == expected.response_observation_id
                && self.call_ids == expected.call_ids,
            "Recovered tool outcomes have wrong anchor or call order"
        );
        let mut seen = HashMap::new();
        for (id, success) in &self.outcomes {
            ensure!(
                self.call_ids.contains(id) && seen.insert(id, success).is_none(),
                "Duplicate or unrelated recovered outcome"
            );
        }
        for (id, success) in expected.outcomes {
            ensure!(
                seen.get(&id).is_none_or(|saved| **saved == success),
                "Recovered outcome conflicts with retained native success"
            );
        }
        Ok(())
    }
    fn record(&mut self, id: &str, success: bool) {
        if !self.call_ids.iter().any(|call| call == id) {
            return;
        }
        if let Some((_, value)) = self.outcomes.iter_mut().find(|(call, _)| call == id) {
            *value = success;
        } else {
            self.outcomes.push((id.to_owned(), success));
        }
    }
    pub(crate) fn candidates(
        &self,
        turn: &str,
        held_outputs: Vec<(ResponseItemEnvelope, bool)>,
    ) -> codex_extension_api::RecoveredToolCompletionCandidates {
        codex_extension_api::RecoveredToolCompletionCandidates {
            held_outputs,
            turn_id: turn.to_owned(),
            response_observation_id: self.response_observation_id.clone(),
            call_ids: self.call_ids.clone(),
            outcomes: self.outcomes.clone(),
        }
    }
}

pub(crate) struct Frame {
    tool_outcomes: Option<ToolOutcomes>,
    fresh: Option<crate::python_tools::FreshTerminalReceipts>,
    terminal_calls: HashSet<String>,
    pub turn: String,
    pub pending: Vec<ResponseItem>,
    pub order: VecDeque<String>,
    pub accepted: HashMap<String, ResponseItemEnvelope>,
    pub seen: HashSet<String>,
    pub state: Value,
    pub receivers: Vec<oneshot::Receiver<crate::python_tools::ToolReply>>,
    pub sealed: bool,
    pub profile_source: Option<codex_extension_api::AgentProfileReceipt>,
    handoffs: Vec<(
        codex_tools::ToolName,
        codex_protocol::agent_profile::AgentProfileHandoff,
    )>,
}

/// Recover source identity from accepted history, never a runtime allocation ID.
pub(crate) fn validate_for_opening(
    state: &Value,
    instructions: &str,
    specs: &[DynamicToolSpec],
    manual_slots: &[usize],
    opening: Option<&crate::agent_profiles::OpeningProfiles>,
) -> Result<Frame> {
    let Some(opening) = opening else {
        let frame = validate(state, instructions, specs, manual_slots)?;
        ensure!(
            frame.handoffs.is_empty(),
            "Restored handoff requires declared Agent graph"
        );
        return Ok(frame);
    };
    let history: Vec<RolloutItem> = serde_json::from_value(state["history"].clone())?;
    let pending = state["native_continuation"]["pending_manual"]
        .as_array()
        .context("Missing pending manual calls")?;
    let ids = pending
        .iter()
        .map(|item| {
            item["call_id"]
                .as_str()
                .context("Missing pending call identity")
        })
        .collect::<Result<HashSet<_>>>()?;
    ensure!(
        !ids.is_empty() && ids.len() == pending.len(),
        "Invalid pending call identities"
    );
    let mut current = None;
    let mut source = None;
    let mut found = HashSet::new();
    let turn = state["native_continuation"]["turn_id"]
        .as_str()
        .context("Missing manual turn")?;
    let mut in_turn = false;
    let mut call_sources = HashMap::new();
    let mut result_indices = HashMap::new();
    let mut transitions = Vec::new();
    for (index, item) in history.iter().enumerate() {
        if let RolloutItem::EventMsg(EventMsg::TurnStarted(start)) = item {
            in_turn = start.turn_id == turn;
        }
        let RolloutItem::ResponseItem(envelope) = item else {
            continue;
        };
        if let Some(metadata) = &envelope.metadata
            && let Some(profile) = &metadata.sdk_agent_profile
        {
            let receipt = crate::agent_profiles::profile_receipt(profile)?;
            let graph = metadata
                .sdk_agent_graph
                .as_deref()
                .context("Captured Agent history has no declaration graph")?;
            transitions.push((index, receipt.clone(), graph));
            current = Some((receipt, graph));
        }
        if !in_turn {
            continue;
        }
        if let Some(handoff) = envelope
            .metadata
            .as_ref()
            .and_then(|metadata| metadata.sdk_agent_handoff.as_ref())
        {
            ensure!(
                result_indices
                    .insert(handoff.call_id.clone(), index)
                    .is_none(),
                "Repeated canonical handoff result"
            );
        }
        if let ResponseItem::FunctionCall { call_id, .. } = &envelope.item {
            if let Some((receipt, graph)) = &current {
                ensure!(
                    call_sources
                        .insert(call_id.clone(), (receipt.clone(), (*graph).to_owned()))
                        .is_none(),
                    "Repeated call source in manual history"
                );
            }
        }
        if let ResponseItem::FunctionCall { call_id, .. } = &envelope.item
            && ids.contains(call_id.as_str())
        {
            ensure!(
                found.insert(call_id.as_str()),
                "Repeated pending call in history"
            );
            let (receipt, graph) = current
                .as_ref()
                .context("Pending call has no captured Agent source")?;
            ensure!(
                *graph == opening.fingerprint,
                "Pending call belongs to a different Agent graph"
            );
            if let Some(previous) = &source {
                ensure!(
                    previous == receipt,
                    "Pending calls have different Agent sources"
                );
            } else {
                source = Some(receipt.clone());
            }
        }
    }
    ensure!(
        found.len() == ids.len(),
        "Pending source does not cover every call"
    );
    let source = source.context("Missing pending Agent source")?;
    ensure!(
        current
            .as_ref()
            .is_some_and(|(receipt, _)| receipt == &source),
        "Pending calls differ from the active Agent source"
    );
    opening.validate_pending_source(&source)?;
    let accepted = crate::agent_profiles::validate_restored(opening, &history)?;
    let (specs, manual_slots) = opening.restore_catalog(&history, &state["managed_tool_specs"])?;
    let mut frame = validate_frame(state, &accepted.text, &specs, &manual_slots)?;
    let mut target = None;
    let mut pending_handoffs = Vec::new();
    for (name, handoff) in frame.handoffs.drain(..) {
        let (original, graph) = call_sources
            .get(&handoff.call_id)
            .context("Handoff has no original call source")?;
        ensure!(
            original == &handoff.source && graph == &opening.fingerprint,
            "Handoff differs from its original call source or admitted graph"
        );
        // A canonical target receipt proves that this handoff already applied.
        // Buffered sibling results have no later transition and stay pending.
        let next_transition = result_indices.get(&handoff.call_id).and_then(|index| {
            transitions
                .iter()
                .find(|(next_index, _, _)| next_index > index)
        });
        opening.validate_restored_handoff(&name, &handoff, next_transition.is_none())?;
        if let Some((_, next, next_graph)) = next_transition {
            ensure!(
                next.profile_id == handoff.target_profile_id
                    && handoff.source.epoch.checked_add(1) == Some(next.epoch)
                    && *next_graph == opening.fingerprint,
                "Handoff does not match its next accepted Agent transition"
            );
            continue;
        }
        ensure!(
            handoff.source == source,
            "Deferred handoff differs from pending source"
        );
        ensure!(
            target
                .as_ref()
                .is_none_or(|previous| previous == &handoff.target_profile_id),
            "Conflicting deferred Agent handoffs"
        );
        target = Some(handoff.target_profile_id.clone());
        pending_handoffs.push((name, handoff));
    }
    frame.handoffs = pending_handoffs;
    frame.profile_source = Some(source);
    Ok(frame)
}

pub(crate) fn validate(
    state: &Value,
    instructions: &str,
    specs: &[DynamicToolSpec],
    manual_slots: &[usize],
) -> Result<Frame> {
    let history: Vec<RolloutItem> = serde_json::from_value(state["history"].clone())?;
    crate::application_instructions::validate(&history, instructions)?;
    validate_frame(state, instructions, specs, manual_slots)
}

// A canonical wrapper preserves accepted handoff metadata. Older snapshots stored
// bare model items and remain readable without manufacturing that provenance.
fn decode_accepted_replies(value: &Value) -> Result<HashMap<String, ResponseItemEnvelope>> {
    value
        .as_object()
        .context("Invalid accepted continuation replies")?
        .iter()
        .map(|(call, value)| {
            let envelope = if value["type"] == "response_item" {
                let RolloutItem::ResponseItem(envelope) = serde_json::from_value(value.clone())?
                else {
                    anyhow::bail!("Accepted continuation reply is not a response item")
                };
                envelope
            } else {
                let item: ResponseItem = serde_json::from_value(value.clone())?;
                item.into()
            };
            Ok((call.clone(), envelope))
        })
        .collect()
}

fn encode_accepted_replies(replies: &HashMap<String, ResponseItemEnvelope>) -> Result<Value> {
    Ok(serde_json::to_value(
        replies
            .iter()
            .map(|(call, envelope)| (call, RolloutItem::ResponseItem(envelope.clone())))
            .collect::<HashMap<_, _>>(),
    )?)
}

fn validate_frame(
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
        (6..=8).contains(&frame.len())
            && frame.keys().all(|key| matches!(
                key.as_str(),
                "turn_id"
                    | "seen_call_ids"
                    | "turn_usage"
                    | "reply_order"
                    | "accepted_replies"
                    | "pending_manual"
                    | "output_schema"
                    | "tool_outcomes"
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
    let accepted = decode_accepted_replies(&frame["accepted_replies"])?;
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
    ensure!(
        crate::application_instructions::latest(&history)?.text == instructions,
        "Manual restore requires matching accepted instructions"
    );
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
    let mut handoffs = Vec::new();
    for line in &history {
        let raw = serde_json::to_value(line)?;
        if raw["type"] == "event_msg" && raw["payload"]["type"] == "task_started" {
            last_started = raw["payload"]["turn_id"].as_str().map(str::to_owned);
            calls.clear();
            admitted_order.clear();
            results.clear();
            handoffs.clear();
        }
        if raw["type"] == "event_msg"
            && matches!(
                raw["payload"]["type"].as_str(),
                Some("task_complete" | "turn_aborted")
            )
        {
            last_started = None;
        }
        if let RolloutItem::ResponseItem(envelope) = line {
            let value = envelope.item.clone();
            let raw = serde_json::to_value(&value)?;
            if last_started.as_deref() == Some(&turn) {
                if let Some(handoff) = envelope
                    .metadata
                    .as_ref()
                    .and_then(|metadata| metadata.sdk_agent_handoff.as_ref())
                {
                    ensure!(
                        raw["type"] == "function_call_output" && raw["call_id"] == handoff.call_id,
                        "Restored handoff is not attached to its canonical result"
                    );
                    let call = calls
                        .get(&handoff.call_id)
                        .context("Restored handoff has no original call")?;
                    ensure!(
                        call["type"] == "function_call",
                        "Restored handoff requires a function call"
                    );
                    let name = call["name"]
                        .as_str()
                        .context("Restored handoff has no tool name")?;
                    let namespace = call["namespace"].as_str().map(str::to_owned);
                    handoffs.push((codex_tools::ToolName::new(namespace, name), handoff.clone()));
                }
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
    crate::python_tools::validate_declarations(&saved_specs)?;
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
    let mut admitted = Vec::new();
    let mut pending_ids = HashSet::new();
    for value in pending {
        let fields = value
            .as_object()
            .context("Invalid manual continuation request")?;
        ensure!(
            fields.keys().all(|key| matches!(
                key.as_str(),
                "call_id" | "tool" | "arguments" | "approval_required" | "effective_arguments"
            )) && fields
                .get("approval_required")
                .is_none_or(Value::is_boolean)
                && fields
                    .get("effective_arguments")
                    .is_none_or(|value| value.is_null()
                        || value
                            .as_str()
                            .is_some_and(|text| serde_json::from_str::<Value>(text).is_ok()))
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
        let raw = serde_json::to_value(&receipt.item)?;
        ensure!(
            ordered.contains(call)
                && !pending_ids.contains(call)
                && calls
                    .get(call)
                    .is_some_and(|original| receipt_matches(original, &raw)),
            "Accepted continuation result has wrong call identity or kind"
        );
        if let Some(handoff) = receipt
            .metadata
            .as_ref()
            .and_then(|metadata| metadata.sdk_agent_handoff.as_ref())
        {
            ensure!(
                raw["type"] == "function_call_output" && raw["call_id"] == handoff.call_id,
                "Restored handoff is not attached to its canonical result"
            );
            let original = calls
                .get(call)
                .context("Restored handoff has no original call")?;
            ensure!(
                original["type"] == "function_call",
                "Restored handoff requires a function call"
            );
            let name = original["name"]
                .as_str()
                .context("Restored handoff has no tool name")?;
            let namespace = original["namespace"].as_str().map(str::to_owned);
            handoffs.push((codex_tools::ToolName::new(namespace, name), handoff.clone()));
        }
    }
    let tool_outcomes = match frame.get("tool_outcomes") {
        Some(value) => {
            let outcomes: ToolOutcomes = serde_json::from_value(value.clone())?;
            outcomes.validate(&history, source_id, &turn)?;
            Some(outcomes)
        }
        None => ToolOutcomes::capture(&history, source_id, &turn)?,
    };
    Ok(Frame {
        profile_source: None,
        handoffs,
        tool_outcomes,
        fresh: None,
        terminal_calls: HashSet::new(),
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
        invocation: u64,
        generation: Arc<crate::python_tools::Generation>,
        value: &DynamicToolResponse,
    ) {
        if !self.sealed && key.turn == self.turn && self.order.contains(&key.call) {
            if let Some(outcomes) = &mut self.tool_outcomes {
                outcomes.record(&key.call, value.success);
            }
            if value.success {
                crate::python_tools::FreshTerminalReceipts::record(
                    &mut self.fresh,
                    generation,
                    key,
                    invocation,
                );
            }
            self.accepted.insert(
                key.call.clone(),
                crate::python_tools::PythonOutput(value.clone())
                    .receipt(key)
                    .into(),
            );
        }
    }

    pub(crate) fn snapshot(&self) -> Result<Value> {
        let mut value = self.state.clone();
        value["native_continuation"]["accepted_replies"] = encode_accepted_replies(&self.accepted)?;
        if let Some(outcomes) = &self.tool_outcomes {
            value["native_continuation"]["tool_outcomes"] = serde_json::to_value(outcomes)?;
        }
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

pub(crate) async fn prepare(thread: &crate::engine::ThreadState, owner: &Owner) -> Result<()> {
    let expected_source = owner
        .lock()
        .map_err(|_| anyhow::anyhow!("Manual continuation poisoned"))?
        .as_ref()
        .context("No manual continuation")?
        .profile_source
        .clone();
    let captured = if expected_source.is_some() {
        Some(
            thread
                .thread
                .captured_agent_profile()
                .await?
                .context("Manual continuation has no committed Agent source")?,
        )
    } else {
        None
    };
    let mut current = owner
        .lock()
        .map_err(|_| anyhow::anyhow!("Manual continuation poisoned"))?;
    let frame = current.as_mut().context("No manual continuation")?;
    ensure!(
        frame.profile_source == expected_source,
        "Manual continuation source changed"
    );
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
    frame.terminal_calls = tools.private_terminal_calls(
        captured.as_deref(),
        &history,
        &frame.order.iter().cloned().collect(),
    )?;
    let source_id = serde_json::from_value(frame.state["source_session_id"].clone())?;
    let model = crate::checkpoint::managed_settings(&history, source_id)
        .map(|settings| settings.model.as_str());
    for (name, handoff) in &frame.handoffs {
        crate::agent_profiles::restore_handoff(
            thread,
            captured
                .as_deref()
                .context("Restored handoff has no captured Agent source")?,
            &frame.turn,
            name,
            handoff,
        )?;
    }
    for item in &frame.pending {
        frame
            .receivers
            .push(if let Some(source) = &frame.profile_source {
                crate::agent_profiles::enqueue_manual_reentry(
                    thread,
                    source,
                    captured
                        .as_deref()
                        .context("Missing captured manual source")?,
                    &frame.turn,
                    item,
                    model,
                )?
            } else {
                tools.enqueue_manual_reentry(&thread.id.to_string(), &frame.turn, item, model)?
            });
    }
    for pending in frame.state["native_continuation"]["pending_manual"]
        .as_array()
        .context("Missing captured manual requirements")?
    {
        tools.restore_approval_requirement(
            pending["call_id"]
                .as_str()
                .context("Missing pending call identity")?,
            pending["approval_required"].as_bool().unwrap_or(false),
            pending["effective_arguments"]
                .as_str()
                .map(serde_json::from_str::<Value>)
                .transpose()?,
        );
    }
    tools.bind_manual_restore(Arc::downgrade(owner));
    Ok(())
}

pub(crate) async fn continue_if_ready(
    thread: &crate::engine::ThreadState,
    owner: &Owner,
    trace: Option<codex_protocol::protocol::W3cTraceContext>,
    observation_submission_id: Option<String>,
    operation_declaration_source: Option<codex_extension_api::AgentProfileReceipt>,
) -> Result<Value> {
    let _operation = thread.engine.operation(false)?;
    let _mutation = thread.mutations.lock().await;
    let mut frame = {
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
        if let Some(fresh) = &frame.fresh {
            let tools = thread
                .thread
                .thread_extension_data()
                .get::<crate::python_tools::PythonTools>()
                .context("Manual continuation requires current tools")?;
            fresh.check(&tools)?;
        }
        current.take().expect("Checked continuation")
    };
    let saved_held_ids: HashSet<_> = frame
        .tool_outcomes
        .as_ref()
        .map(|outcomes| outcomes.held_call_ids.iter().cloned().collect())
        .unwrap_or_default();
    ensure!(
        saved_held_ids.len()
            == frame
                .tool_outcomes
                .as_ref()
                .map_or(0, |outcomes| outcomes.held_call_ids.len())
            && saved_held_ids
                .iter()
                .all(|id| frame.accepted.contains_key(id) && frame.terminal_calls.contains(id)),
        "Invalid held terminal reply identities"
    );
    // Erasing a checkpoint hint cannot publish an actual current terminal reply.
    let held_ids = &frame.terminal_calls;
    ensure!(
        held_ids.is_empty() || frame.tool_outcomes.is_some(),
        "Terminal continuation lacks completed-response facts"
    );
    if let Some(outcomes) = &mut frame.tool_outcomes {
        outcomes.held_call_ids = frame
            .order
            .iter()
            .filter(|id| held_ids.contains(*id))
            .cloned()
            .collect();
    }
    let held_outputs = frame
        .order
        .iter()
        .filter(|id| held_ids.contains(*id))
        .map(|id| {
            let output = frame.accepted[id].clone();
            let success = frame
                .tool_outcomes
                .as_ref()
                .and_then(|outcomes| {
                    outcomes
                        .outcomes
                        .iter()
                        .find(|(call, _)| call == id)
                        .map(|(_, success)| *success)
                })
                // Missing historical success cannot authorize terminal completion.
                .unwrap_or(false);
            Ok((output, success))
        })
        .collect::<Result<Vec<_>>>()?;
    let outputs: Vec<_> = frame
        .order
        .iter()
        .filter(|call| !held_ids.contains(*call))
        .map(|call| frame.accepted[call].clone())
        .collect();
    if !outputs.is_empty() {
        thread
            .thread
            .inject_sdk_response_item_envelopes(outputs, Some(&frame.turn))
            .await?;
    }
    let mut recovery_options = match frame.state["native_continuation"].get("output_schema") {
        Some(schema) => serde_json::json!({"output_schema":schema}),
        None => serde_json::json!({}),
    };
    recovery_options["trace"] = serde_json::json!(trace);
    recovery_options["observation_submission_id"] = serde_json::json!(observation_submission_id);
    if let Some(source) = operation_declaration_source {
        recovery_options["operation_declaration_source"] = serde_json::to_value(source)?;
    }
    let reset_tool_choice = thread
        .thread
        .thread_settings_snapshot()
        .await
        .request_controls
        .reset_tool_choice
        .flatten()
        == Some(true);
    let terminal = match frame.tool_outcomes.take() {
        Some(outcomes)
            if frame.fresh.is_some() || !held_outputs.is_empty() || reset_tool_choice =>
        {
            Some(crate::python_tools::RecoveredToolCompletionSeed::new(
                frame.turn.clone(),
                outcomes,
                frame.fresh.take(),
                held_outputs,
            ))
        }
        _ => None,
    };
    let result = crate::ops::recover_prepared(
        thread,
        frame.turn,
        frame.seen.into_iter().collect(),
        recovery_options,
        terminal,
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
    // Shutdown revokes live waiter metadata. Preserve its captured requirement
    // with the exact pending keys before beginning that existing transaction.
    let approvals: HashMap<_, _> = pending
        .iter()
        .map(|key| Ok((key.call.clone(), tools.manual_approval_state(key)?)))
        .collect::<Result<_>>()?;
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
    // Capture the accepted catalog before suspension revokes all live getters.
    let managed_specs = match crate::agent_profiles::active_specs(&thread.thread).await? {
        Some(specs) => specs,
        None => tools.current_specs()?,
    };
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
    let mut tool_outcomes = None;
    let snapshot = match preserved {
        Ok(true) if suspended_turn == Some(turn.as_str()) => {
            async {
                let final_history = thread.engine.load_checkpoint_history(thread.id).await?;
                tool_outcomes = ToolOutcomes::capture(&final_history, thread.id, &turn)?;
                crate::checkpoint::export_suspended(thread, &code_states, &managed_specs).await
            }
            .await
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
                        outputs.insert(call_id.to_owned(), item.clone()).is_none(),
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
    let held_outputs = tools.held_tool_outputs(&turn);
    let mut held_ids = HashSet::new();
    for (output, succeeded) in held_outputs {
        let ResponseItem::FunctionCallOutput {
            call_id: Some(id), ..
        } = &output.item
        else {
            anyhow::bail!("Invalid held terminal output");
        };
        ensure!(
            held_ids.insert(id.clone()) && !outputs.contains_key(id),
            "Duplicate held terminal output"
        );
        if let Some(outcomes) = tool_outcomes.as_mut() {
            ensure!(
                outcomes.call_ids.contains(id),
                "Held terminal output has no sampled call"
            );
            outcomes.record(id, succeeded);
            outcomes.held_call_ids.push(id.clone());
        } else {
            anyhow::bail!("Held terminal output has no sampled batch");
        }
        outputs.insert(id.clone(), output);
    }
    let prefix = calls
        .iter()
        .take_while(|(id, _)| outputs.contains_key(id) && !held_ids.contains(id))
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
        let (required, effective_arguments) = approvals
            .get(id)
            .context("Missing captured manual approval state")?;
        let mut captured = serde_json::json!({"call_id":id,"tool":name,"arguments":arguments});
        if *required {
            captured["approval_required"] = Value::Bool(true);
        }
        if let Some(arguments) = effective_arguments {
            captured["effective_arguments"] = Value::String(serde_json::to_string(&arguments)?);
        }
        pending_manual.push(captured);
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
        "turn_usage":usage,"reply_order":order,"accepted_replies":encode_accepted_replies(&accepted)?,"pending_manual":pending_manual
    });
    if let Some(outcomes) = tool_outcomes {
        snapshot["native_continuation"]["tool_outcomes"] = serde_json::to_value(outcomes)?;
    }
    if let Some(schema) = output_schema {
        snapshot["native_continuation"]["output_schema"] = schema;
    }
    Ok(Some(snapshot))
}
