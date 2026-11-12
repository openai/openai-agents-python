use anyhow::ensure;
use codex_protocol::protocol::AgentStatus;
use serde_json::{Value, json};

use crate::engine::ThreadState;

pub async fn read(state: &ThreadState) -> anyhow::Result<Value> {
    let _mutation = state.mutations.lock().await;
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
        "model context requires an idle, open native thread"
    );
    if let Some(tools) = state
        .thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>()
    {
        tools.check_idle(&state.thread).await?;
    }
    let snapshot = state.thread.conversation_history_snapshot().await;
    Ok(json!({
        "thread_id": state.id,
        "history_version": snapshot.history_version(),
        "user_message_revision": snapshot.user_message_revision(),
        "guardian_review_context_revision": snapshot.guardian_review_context_revision(),
        "retained_context": snapshot.retained_context(),
        "items": snapshot.items().collect::<Vec<_>>(),
    }))
}

// A short-lived SDK transaction owns this lease. Ordinary public native ingress
// shares the engine gate; event readers remain independent of it.
pub struct Change {
    pub state: std::sync::Arc<ThreadState>,
    _permit: tokio::sync::OwnedRwLockWriteGuard<()>,
    snapshot: std::sync::Arc<dyn codex_extension_api::ConversationHistorySnapshot>,
    pub checkpoint: Value,
    settings: codex_protocol::protocol::ThreadSettingsSnapshot,
    instructions: Option<String>,
    executor_policy: Option<Value>,
}

pub async fn begin(state: std::sync::Arc<ThreadState>) -> anyhow::Result<Change> {
    use anyhow::Context;
    let permit = state
        .engine
        .context_gate
        .clone()
        .try_write_owned()
        .context("native operations are still in progress")?;
    ensure!(
        !state
            .engine
            .closed
            .load(std::sync::atomic::Ordering::Acquire),
        "Codex engine is closed"
    );
    ensure!(
        state.engine.manager.list_thread_ids().await == vec![state.id],
        "application compaction requires an exclusively owned root without native children"
    );
    ensure!(
        crate::subagents::list(&state).await?.is_empty(),
        "application compaction does not migrate native child graphs"
    );
    ensure!(
        state.thread.list_background_terminals().await.is_empty(),
        "application compaction requires completed background execution"
    );
    // read() takes the mutation gate and verifies native idle/pending ownership.
    read(&state).await?;
    let snapshot = state.thread.conversation_history_snapshot().await;
    let settings = state.thread.thread_settings_snapshot().await;
    let executor_policy = if state
        .thread
        .thread_extension_data()
        .get::<crate::environment::StrictExecutorWrites>()
        .is_some()
    {
        let [policy] = crate::local_reconciliation::executor_policies(&state)
            .await?
            .try_into()
            .map_err(|_| anyhow::anyhow!("compaction requires one applied executor policy"))?;
        Some(policy)
    } else {
        None
    };
    let instructions = state
        .instructions
        .0
        .read()
        .map_err(|error| anyhow::anyhow!("could not read current thread instructions: {error}"))?
        .clone();
    let mut checkpoint = crate::checkpoint::export(&state, None)
        .await
        .context("supplied compaction requires settled native code and portable history")?;
    if let Some(guard) = state
        .thread
        .thread_extension_data()
        .get::<crate::recovery_guard::Guard>()
    {
        checkpoint["root_tool_origins"] = serde_json::to_value(guard.origins())?;
    }
    checkpoint["agent_options"] = json!({
        "config": {"mcp_servers": crate::mcp_roster::checkpoint_servers(&state.thread).await}
    });
    ensure!(
        checkpoint["interrupted_thread_ids"]
            .as_array()
            .is_none_or(Vec::is_empty),
        "application compaction requires settled turns"
    );
    Ok(Change {
        state,
        _permit: permit,
        snapshot,
        checkpoint,
        settings,
        instructions,
        executor_policy,
    })
}

impl Change {
    pub fn options(&self) -> Value {
        let instruction_files = self
            .state
            .thread
            .thread_extension_data()
            .get::<crate::application_instructions::Instructions>()
            .and_then(|instructions| instructions.files());
        json!({
            "instructions": self.instructions,
            "instruction_files": instruction_files,
            "settings": self.settings,
            "executor_writable_roots": self.executor_policy.as_ref().map(|policy| &policy["writable_roots"]),
        })
    }

    pub async fn validate_candidate(&self, candidate: &ThreadState) -> anyhow::Result<()> {
        use anyhow::Context;
        use codex_protocol::protocol::ThreadSettingsOverrides;
        // Only a different, private engine can be prepared under the source lease.
        ensure!(
            !std::sync::Arc::ptr_eq(&self.state.engine, &candidate.engine)
                && self.state.id == candidate.id,
            "compaction candidate must own a private engine for the same conversation"
        );
        let _operation = candidate.engine.operation(false)?;
        let _mutation = candidate.mutations.lock().await;
        if let Some(policy) = self.executor_policy.as_ref() {
            ensure!(
                crate::local_reconciliation::executor_policies(candidate)
                    .await?
                    .as_slice()
                    == std::slice::from_ref(policy),
                "compaction cannot change the applied executor policy"
            );
            let writes = self
                .state
                .thread
                .thread_extension_data()
                .get::<crate::environment::StrictExecutorWrites>()
                .context("compaction source lost its strict write policy")?;
            let candidate_store = candidate.thread.thread_extension_data();
            ensure!(
                candidate_store
                    .get::<crate::environment::StrictExecutorWrites>()
                    .is_some(),
                "compaction candidate lost its strict write policy"
            );
            candidate_store.insert((*writes).clone());
        }
        // Startup options already restore these preferences in the common case.
        // Avoid rewriting an absent tier: native clear normalizes it to "default".
        let before = candidate.thread.thread_settings_snapshot().await;
        let effort = (before.reasoning_effort != self.settings.reasoning_effort)
            .then(|| self.settings.reasoning_effort.clone());
        let service_tier = (before.service_tier != self.settings.service_tier)
            .then(|| self.settings.service_tier.clone());
        if effort.is_some() || service_tier.is_some() {
            candidate
                .thread
                .update_thread_settings(ThreadSettingsOverrides {
                    effort,
                    service_tier,
                    ..Default::default()
                })
                .await?;
        }
        let actual = candidate.thread.thread_settings_snapshot().await;
        ensure!(
            actual == self.settings,
            "compaction cannot carry current thread settings into the restored candidate"
        );
        ensure!(
            *candidate
                .instructions
                .0
                .read()
                .map_err(|error| anyhow::anyhow!(
                    "could not read candidate instructions: {error}"
                ))?
                == self.instructions,
            "compaction cannot carry current thread instructions"
        );
        let source_tools = self
            .state
            .thread
            .thread_extension_data()
            .get::<crate::python_tools::PythonTools>();
        let candidate_tools = candidate
            .thread
            .thread_extension_data()
            .get::<crate::python_tools::PythonTools>();
        match (source_tools, candidate_tools) {
            (Some(source), Some(target)) => {
                target.check_idle(&candidate.thread).await?;
                target.retain_context_revision(&source)?;
            }
            (None, None) => {}
            _ => anyhow::bail!("compaction candidate changed managed tool ownership"),
        }
        Ok(())
    }

    pub fn context(&self) -> Value {
        json!({
            "thread_id": self.state.id,
            "history_version": self.snapshot.history_version(),
            "user_message_revision": self.snapshot.user_message_revision(),
            "guardian_review_context_revision": self.snapshot.guardian_review_context_revision(),
            "retained_context": self.snapshot.retained_context(),
            "items": self.snapshot.items().collect::<Vec<_>>(),
        })
    }

    pub fn prepare(&self, summary: &str, retained_indices: &[usize]) -> anyhow::Result<Value> {
        use anyhow::Context;
        use codex_history::{
            CompactedItem, GuardianHistoryCheckpoint, ResponseItemEnvelope, RolloutItem,
        };
        use codex_protocol::models::ResponseItem;
        let live = self.snapshot.items().cloned().collect::<Vec<_>>();
        ensure!(
            retained_indices.windows(2).all(|pair| pair[0] < pair[1])
                && retained_indices.iter().all(|index| *index < live.len()),
            "retained indices must be unique, increasing and within the current model context"
        );
        check_pairs(&live, retained_indices)?;
        let history: Vec<RolloutItem> = serde_json::from_value(self.checkpoint["history"].clone())?;
        let mut current = Vec::new();
        let mut review = Vec::new();
        let mut saw_compaction = false;
        for row in &history {
            match row {
                RolloutItem::ResponseItem(item) => {
                    current.push(item.clone());
                    review.push(item.clone());
                }
                RolloutItem::Compacted(item) => {
                    saw_compaction = true;
                    current = item
                        .replacement_history
                        .clone()
                        .context("legacy compaction without original envelopes is unsupported")?;
                    if let Some(items) = &item.guardian_history {
                        review = items.0.clone();
                    }
                }
                RolloutItem::EventMsg(codex_protocol::protocol::EventMsg::ThreadRolledBack(_))
                    if saw_compaction =>
                {
                    anyhow::bail!(
                        "application compaction cannot establish envelope provenance after a rolled-back compaction"
                    )
                }
                _ => {}
            }
        }
        let originals = original_envelopes(&live, &current)?;
        let mut replacement = retained_indices
            .iter()
            .map(|index| originals[*index].clone())
            .collect::<Vec<_>>();
        // Use the same explicit context kind as native compaction. A summary is
        // model context, never a new original user authorization.
        let summary_item: ResponseItem = serde_json::from_value(json!({
            "type": "message", "role": "user",
            "id": format!("msg_{}", codex_protocol::ThreadId::new()),
            "content": [{"type": "input_text", "text": summary}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["compaction.summary"]
            }
        }))?;
        replacement.push(ResponseItemEnvelope::new(summary_item));
        let guardian_history = if self.snapshot.uses_independent_review_history() {
            Some(GuardianHistoryCheckpoint(original_envelopes(
                &self.snapshot.review_items().cloned().collect::<Vec<_>>(),
                &review,
            )?))
        } else {
            None
        };
        let compacted = CompactedItem {
            message: summary.to_owned(),
            replacement_history: Some(replacement),
            sdk_originals: Some(
                codex_history::sdk_originals::SdkOriginalHistoryCheckpoint::preserve(),
            ),
            guardian_history,
            retained_context: self.snapshot.retained_context().cloned(),
            // Full canonical history is preserved. Native restore replays its
            // existing origin checkpoints and events, including rolled-back turns.
            mcp_resource_origins: None,
            window_number: None,
            first_window_id: None,
            previous_window_id: None,
            window_id: None,
            compaction_response_id: None,
            latest_token_usage_record: None,
            resume_metadata: None,
        };
        let mut candidate = self.checkpoint.clone();
        candidate["history"]
            .as_array_mut()
            .context("checkpoint lacks history")?
            .push(serde_json::to_value(RolloutItem::Compacted(compacted))?);
        Ok(candidate)
    }
}

// Join the complete native sequence to canonical originals before selecting
// indices. Earliest/latest feasible positions preserve occurrence identity and
// expose ambiguity instead of inventing metadata for duplicate payloads.
fn original_envelopes(
    items: &[codex_protocol::models::ResponseItem],
    source: &[codex_history::ResponseItemEnvelope],
) -> anyhow::Result<Vec<codex_history::ResponseItemEnvelope>> {
    use anyhow::Context;
    let live = items
        .iter()
        .map(serde_json::to_value)
        .collect::<Result<Vec<_>, _>>()?;
    let original = source
        .iter()
        .map(|item| serde_json::to_value(&item.item))
        .collect::<Result<Vec<_>, _>>()?;
    let matches = |i: usize, j: usize| -> bool {
        if live[i] == original[j] {
            return true;
        }
        if !matches!(
            live[i]["type"].as_str(),
            Some("function_call_output" | "custom_tool_call_output")
        ) {
            return false;
        }
        // Native shortens these payloads without changing their envelope
        // metadata. Everything outside output, including IDs, must still match.
        let mut left = live[i].clone();
        let mut right = original[j].clone();
        left.as_object_mut()
            .expect("response item is an object")
            .remove("output");
        right
            .as_object_mut()
            .expect("response item is an object")
            .remove("output");
        left == right
    };
    let mut earliest = Vec::with_capacity(items.len());
    let mut cursor = 0;
    for i in 0..items.len() {
        let j = (cursor..source.len())
            .find(|j| matches(i, *j))
            .context("current context has no ordered canonical original")?;
        earliest.push(j);
        cursor = j + 1;
    }
    let mut latest = vec![0; items.len()];
    cursor = source.len();
    for i in (0..items.len()).rev() {
        let j = (0..cursor)
            .rev()
            .find(|j| matches(i, *j))
            .context("current context has no ordered canonical original")?;
        latest[i] = j;
        cursor = j;
    }
    items
        .iter()
        .enumerate()
        .map(|(i, item)| {
            let start = if i == 0 { 0 } else { earliest[i - 1] + 1 };
            let end = if i + 1 == items.len() {
                source.len()
            } else {
                latest[i + 1]
            };
            let metadata = &source[earliest[i]].metadata;
            ensure!(
                (start..end)
                    .filter(|j| matches(i, *j))
                    .all(|j| source[j].metadata == *metadata),
                "original context metadata is ambiguous between ordered occurrences"
            );
            Ok(codex_history::ResponseItemEnvelope {
                item: item.clone(),
                metadata: metadata.clone(),
            })
        })
        .collect()
}

pub(crate) fn check_pairs(
    items: &[codex_protocol::models::ResponseItem],
    indices: &[usize],
) -> anyhow::Result<()> {
    use codex_protocol::models::ResponseItem;
    use std::collections::HashMap;
    let mut calls: HashMap<(&str, &str), usize> = HashMap::new();
    for (index, item) in items.iter().enumerate() {
        let (family, id, is_call) = match item {
            ResponseItem::FunctionCall { call_id, .. } => ("function", Some(call_id), true),
            ResponseItem::LocalShellCall { call_id, .. } => ("function", call_id.as_ref(), true),
            ResponseItem::FunctionCallOutput { call_id, .. } => {
                ("function", call_id.as_ref(), false)
            }
            ResponseItem::ApplyPatchCall { call_id, .. } => ("apply_patch", Some(call_id), true),
            ResponseItem::ApplyPatchCallOutput { call_id, .. } => {
                ("apply_patch", Some(call_id), false)
            }
            ResponseItem::ShellCall {
                provider_fields, ..
            }
            | ResponseItem::ShellCallOutput {
                provider_fields, ..
            } => (
                "shell",
                provider_fields
                    .get("call_id")
                    .and_then(|value| match value {
                        Value::String(id) => Some(id),
                        _ => None,
                    }),
                matches!(item, ResponseItem::ShellCall { .. }),
            ),
            ResponseItem::ComputerCall { call_id, .. } => ("computer", Some(call_id), true),
            ResponseItem::ComputerCallOutput { call_id, .. } => ("computer", Some(call_id), false),
            ResponseItem::CustomToolCall { call_id, .. } => ("custom", Some(call_id), true),
            ResponseItem::CustomToolCallOutput { call_id, .. } => ("custom", Some(call_id), false),
            ResponseItem::ToolSearchCall {
                call_id, execution, ..
            } if execution != "server" => ("search", call_id.as_ref(), true),
            ResponseItem::ToolSearchOutput {
                call_id, execution, ..
            } if execution != "server" => ("search", call_id.as_ref(), false),
            _ => continue,
        };
        let Some(id) = id else {
            anyhow::bail!("application compaction cannot preserve an unidentified tool pair");
        };
        let key = (family, id.as_str());
        if is_call {
            ensure!(
                calls.insert(key, index).is_none(),
                "application compaction requires nonoverlapping resolved tool calls"
            );
        } else {
            let Some(call) = calls.remove(&key) else {
                anyhow::bail!(
                    "application compaction requires resolved tool calls in their original family"
                );
            };
            ensure!(
                indices.binary_search(&call).is_ok() == indices.binary_search(&index).is_ok(),
                "retain both original tool call and result, or neither"
            );
        }
    }
    ensure!(
        calls.is_empty(),
        "application compaction requires resolved tool calls in their original family"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_history::{CodexHarnessMetadata, ResponseItemEnvelope};
    use codex_protocol::models::ResponseItem;

    fn item(value: Value, marker: usize) -> ResponseItemEnvelope {
        ResponseItemEnvelope {
            item: serde_json::from_value(value).unwrap(),
            metadata: Some(CodexHarnessMetadata {
                history_truncation_token_limit: Some(marker),
                ..Default::default()
            }),
        }
    }

    #[test]
    fn duplicate_originals_follow_occurrence_order() {
        let value =
            json!({"type":"message","role":"user","content":[{"type":"input_text","text":"same"}]});
        let first = item(value.clone(), 11);
        let second = item(value, 22);
        let live = vec![first.item.clone(), second.item.clone()];
        let actual = original_envelopes(&live, &[first.clone(), second.clone()]).unwrap();
        assert_eq!(actual, vec![first.clone(), second.clone()]);
        assert!(original_envelopes(&live[..1], &[first, second]).is_err());
    }

    #[test]
    fn truncated_outputs_keep_ordered_original_metadata() {
        let first = item(
            json!({"type":"custom_tool_call_output","call_id":"repeat","output":"long original A"}),
            31,
        );
        let second = item(
            json!({"type":"custom_tool_call_output","call_id":"repeat","output":"long original B"}),
            42,
        );
        let short: ResponseItem = serde_json::from_value(
            json!({"type":"custom_tool_call_output","call_id":"repeat","output":"short"}),
        )
        .unwrap();
        let actual =
            original_envelopes(&[short.clone(), short], &[first.clone(), second.clone()]).unwrap();
        assert_eq!(actual[0].metadata, first.metadata);
        assert_eq!(actual[1].metadata, second.metadata);
    }

    #[test]
    fn shell_compaction_preserves_both_original_items_or_neither() {
        let call: ResponseItem = serde_json::from_value(json!({"type":"shell_call", "call_id":"shell", "action":{"commands":["printf sample"]}})).unwrap();
        let output: ResponseItem = serde_json::from_value(json!({"type":"shell_call_output", "call_id":"shell", "output":[{"stdout":"accepted", "stderr":"", "outcome":{"type":"exit", "exit_code":0}}]})).unwrap();
        let items = [call.clone(), output.clone()];
        assert!(check_pairs(&items, &[]).is_ok());
        assert!(check_pairs(&items, &[0, 1]).is_ok());
        assert!(check_pairs(&items, &[0]).is_err());
        assert!(check_pairs(&items, &[1]).is_err());
        let function_output: ResponseItem = serde_json::from_value(
            json!({"type":"function_call_output", "call_id":"shell", "output":"accepted"}),
        )
        .unwrap();
        assert!(check_pairs(&[call, function_output], &[0, 1]).is_err());
        let unidentified: ResponseItem = serde_json::from_value(
            json!({"type":"shell_call", "action":{"commands":["printf sample"]}}),
        )
        .unwrap();
        assert!(check_pairs(&[unidentified, output], &[]).is_err());
    }

    #[test]
    fn pairs_preserve_native_families_and_client_search_outputs() {
        let custom: ResponseItem = serde_json::from_value(
            json!({"type":"custom_tool_call","call_id":"same","name":"tool","input":"a"}),
        )
        .unwrap();
        let function_output: ResponseItem = serde_json::from_value(
            json!({"type":"function_call_output","call_id":"same","output":"a"}),
        )
        .unwrap();
        assert!(check_pairs(&[custom, function_output], &[0, 1]).is_err());
        let search: ResponseItem = serde_json::from_value(json!({"type":"tool_search_call","call_id":"search1","execution":"client","arguments":{}})).unwrap();
        let output: ResponseItem = serde_json::from_value(json!({"type":"tool_search_output","call_id":"search1","execution":"client","status":"completed","tools":[]})).unwrap();
        assert!(check_pairs(&[search.clone()], &[0]).is_err());
        assert!(check_pairs(&[search.clone(), output.clone()], &[1]).is_err());
        assert!(check_pairs(&[search, output], &[0, 1]).is_ok());
    }
}

#[cfg(test)]
mod computer_pair_tests {
    use super::check_pairs;
    use codex_protocol::models::ResponseItem;
    use serde_json::json;

    fn pair() -> Vec<ResponseItem> {
        vec![
            serde_json::from_value(json!({"type":"computer_call","id":"cu","call_id":"device","actions":[{"type":"wait"}]})).unwrap(),
            serde_json::from_value(json!({"type":"computer_call_output","call_id":"device","output":{"type":"computer_screenshot","image_url":"data:image/png;base64,aA=="},"acknowledged_safety_checks":[]})).unwrap(),
        ]
    }

    #[test]
    fn computer_compaction_retains_both_original_items_or_neither() {
        let items = pair();
        check_pairs(&items, &[]).unwrap();
        check_pairs(&items, &[0, 1]).unwrap();
        for indices in [&[0][..], &[1][..]] {
            assert!(
                check_pairs(&items, indices)
                    .unwrap_err()
                    .to_string()
                    .contains("retain both original")
            );
        }
    }

    #[test]
    fn computer_compaction_requires_original_identity_and_family() {
        let mut items = pair();
        assert!(check_pairs(&items[1..], &[0]).is_err());
        assert!(check_pairs(&items[..1], &[]).is_err());
        items[1] = serde_json::from_value(
            json!({"type":"function_call_output","call_id":"device","output":"not a screenshot"}),
        )
        .unwrap();
        assert!(
            check_pairs(&items, &[0, 1])
                .unwrap_err()
                .to_string()
                .contains("original family")
        );
    }
}
