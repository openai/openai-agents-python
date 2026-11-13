//! One application instruction owner, published through Core's native history.

use std::sync::{Arc, LazyLock, Mutex, PoisonError};

use anyhow::{Context, Result, ensure};
use codex_core_api::CodexThread;
use codex_extension_api::{
    ContextContributor, ExtensionFuture, PreviousWorldStateSection, RenderedWorldStateFragment,
    SdkApplicationInstructions, SdkTurnReceiptSeed, WorldStateContributionInput,
    WorldStateSectionContribution,
};
use codex_history::{CodexHarnessMetadata, ResponseItemEnvelope, RolloutItem};
use codex_protocol::models::{
    ContentItem, ContentItemKind, InternalChatMessageMetadataPassthrough, ResponseItem,
};
use codex_protocol::protocol::EventMsg;
use codex_protocol::{ResponseItemId, ThreadId};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

const SECTION: &str = "sdk_application";
const ACCEPTED: &str = "sdk_application.accepted";
const PROJECTED: &str = "sdk_application.instructions";
const ID_PREFIX: &str = "msg_sdk_application";
const MARKERS: (&str, &str) = ("<application_instructions>", "</application_instructions>");
const REVISION: &str = "Native instruction revision: ";
const NOTICE: &str = "The following application instructions replace all earlier application instructions. An empty value clears them.\n";

static CODEX_SOURCE_TREE: LazyLock<String> = LazyLock::new(|| {
    let manifest: Value = serde_json::from_str(include_str!("../patches/series.json"))
        .expect("embedded Codex patch manifest must be valid");
    manifest["result_tree"]
        .as_str()
        .expect("embedded Codex manifest must record the reviewed tree")
        .to_owned()
});

#[derive(Clone, Deserialize, Serialize)]
pub(crate) struct InstructionFile {
    path: String,
    source: String,
    sha256: String,
}

fn sha256(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}

#[derive(Clone)]
pub struct Accepted {
    pub(crate) id: ResponseItemId,
    pub(crate) text: String,
    pub(crate) files: Vec<InstructionFile>,
}

impl Accepted {
    pub(crate) fn with_current_files(mut self, files: Option<Vec<InstructionFile>>) -> Self {
        self.files = files.unwrap_or(self.files);
        self
    }
}

#[derive(Default)]
pub struct Instructions(Mutex<Option<Accepted>>);

impl Instructions {
    pub fn restored(accepted: Accepted) -> Self {
        Self(Mutex::new(Some(accepted)))
    }

    pub(crate) fn files(&self) -> Option<Vec<InstructionFile>> {
        self.0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .as_ref()
            .map(|accepted| accepted.files.clone())
    }

    pub(crate) fn realtime_accepted_snapshot(&self) -> Option<Accepted> {
        self.current()
    }

    fn current(&self) -> Option<Accepted> {
        self.0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
}

fn body(accepted: &Accepted) -> String {
    format!("{REVISION}{}\n{NOTICE}{}", accepted.id, accepted.text)
}

fn rendered(accepted: &Accepted) -> String {
    format!("{}{}{}", MARKERS.0, body(accepted), MARKERS.1)
}

fn decode(text: &str) -> Result<(&str, &str)> {
    let (id, text) = text
        .strip_prefix(MARKERS.0)
        .and_then(|text| text.strip_prefix(REVISION))
        .and_then(|text| text.split_once('\n'))
        .context("invalid revision for native application instructions")?;
    let text = text
        .strip_prefix(NOTICE)
        .and_then(|text| text.strip_suffix(MARKERS.1))
        .context("invalid native application instructions")?;
    Ok((id, text))
}

fn accepted(item: &ResponseItem) -> Result<Option<Accepted>> {
    let has_id = item
        .id()
        .is_some_and(|id| id.starts_with(&format!("{ID_PREFIX}_")));
    let ResponseItem::Message {
        id,
        role,
        content,
        phase,
        internal_chat_message_metadata_passthrough: metadata,
    } = item
    else {
        ensure!(!has_id, "invalid native application instructions item");
        return Ok(None);
    };
    let kinds = metadata
        .as_ref()
        .and_then(|metadata| metadata.content_item_kinds.as_deref());
    let has_kind = kinds.is_some_and(|kinds| kinds.iter().any(|kind| kind.0 == ACCEPTED));
    if !has_id && !has_kind {
        return Ok(None);
    }
    ensure!(
        has_id && id.is_some() && role == "developer" && phase.is_none(),
        "invalid identity for native application instructions"
    );
    ensure!(
        kinds == Some(&[ContentItemKind(ACCEPTED.to_owned())]),
        "invalid provenance for native application instructions"
    );
    let [ContentItem::InputText { text }] = content.as_slice() else {
        anyhow::bail!("invalid content for native application instructions");
    };
    let (body_id, text) = decode(text)?;
    let id = id
        .as_ref()
        .context("missing native application instructions identity")?;
    ensure!(
        id.as_str() == body_id,
        "application instructions body revision differs from item identity"
    );
    Ok(Some(Accepted {
        id: id.clone(),
        text: text.to_owned(),
        files: Vec::new(),
    }))
}

pub fn validate(history: &[RolloutItem], expected: &str) -> Result<Accepted> {
    let has_profile = |item: &ResponseItemEnvelope| {
        item.metadata.as_ref().is_some_and(|metadata| {
            metadata.sdk_agent_graph.is_some() || metadata.sdk_agent_profile.is_some()
        })
    };
    ensure!(
        !history.iter().any(|row| match row {
            RolloutItem::ResponseItem(item) => has_profile(item),
            RolloutItem::Compacted(item) => item
                .replacement_history
                .as_ref()
                .is_some_and(|history| history.iter().any(has_profile)),
            _ => false,
        }),
        "Restore requires the captured Agent graph for native profile history"
    );
    let latest = latest(history)?;
    ensure!(
        latest.text == expected,
        "Restore requires Agent.instructions to match the checkpoint's latest accepted instructions"
    );
    Ok(latest)
}

fn accepted_envelope(envelope: &ResponseItemEnvelope) -> Result<Option<Accepted>> {
    let Some(mut accepted) = accepted(&envelope.item)? else {
        return Ok(None);
    };
    if let Some(files) = envelope
        .metadata
        .as_ref()
        .and_then(|metadata| metadata.sdk_instruction_files.clone())
    {
        accepted.files =
            serde_json::from_value(files).context("invalid native instruction file receipt")?;
    }
    Ok(Some(accepted))
}

// Detached originals can rebuild model context during pop. Retain legacy
// instruction copies only when validated canonical history already owns them.
fn validate_detached_instruction_projections(history: &[RolloutItem]) -> Result<()> {
    use codex_history::sdk_originals::SdkOriginalHistoryCheckpoint;

    let reserved = |item: &ResponseItem| {
        let reserved_id = item.id().is_some_and(|id| {
            id.strip_prefix(ID_PREFIX)
                .is_some_and(|suffix| suffix.starts_with('_'))
        });
        let reserved_tag = matches!(item,
            ResponseItem::Message { internal_chat_message_metadata_passthrough: Some(metadata), .. }
            if metadata.content_item_kinds.as_ref().is_some_and(|kinds|
                kinds.iter().any(|kind| matches!(kind.0.as_str(), ACCEPTED | PROJECTED)))
        );
        reserved_id || reserved_tag
    };
    let key = |envelope: &ResponseItemEnvelope| -> serde_json::Result<Value> {
        let mut normalized = envelope.clone();
        if let ResponseItem::Message {
            internal_chat_message_metadata_passthrough: Some(metadata),
            ..
        } = &mut normalized.item
        {
            metadata.turn_id = None;
            metadata.create_time = None;
        }
        normalized.metadata = Some(normalized.metadata.take().unwrap_or_default());
        serde_json::to_value(normalized)
    };
    let mut canonical = std::collections::HashMap::<Value, Vec<&ResponseItemEnvelope>>::new();
    for record in history {
        match record {
            RolloutItem::ResponseItem(item) => {
                if reserved(&item.item) {
                    canonical.entry(key(item)?).or_default().push(item);
                }
            }
            RolloutItem::Compacted(compacted) => {
                for item in compacted.replacement_history.iter().flatten() {
                    if reserved(&item.item) {
                        canonical.entry(key(item)?).or_default().push(item);
                    }
                }
            }
            _ => {}
        }
    }
    for record in history {
        let RolloutItem::Compacted(compacted) = record else {
            continue;
        };
        let Some(SdkOriginalHistoryCheckpoint::Replace(originals)) = &compacted.sdk_originals
        else {
            continue;
        };
        for original in &originals.items {
            let Some(projection) = &original.projection else {
                continue;
            };
            if !reserved(&projection.item) {
                continue;
            }
            let same = |canonical: &ResponseItemEnvelope| {
                same_receipt(&projection.item, &canonical.item)
                    && projection.metadata.clone().unwrap_or_default()
                        == canonical.metadata.clone().unwrap_or_default()
            };
            let admitted = canonical
                .get(&key(projection)?)
                .is_some_and(|items| items.iter().any(|item| same(item)));
            ensure!(
                admitted,
                "SDK original instruction projection has no admitted canonical receipt"
            );
        }
    }
    Ok(())
}

/// Only original accepted items change authority. Compaction copies never do.
pub(crate) fn latest(history: &[RolloutItem]) -> Result<Accepted> {
    let mut originals: Vec<(&ResponseItem, Accepted)> = Vec::new();
    for row in history {
        if let RolloutItem::ResponseItem(item) = row {
            if let Some(accepted) = accepted_envelope(item)? {
                ensure!(
                    !originals
                        .iter()
                        .any(|(original, _)| original.id() == item.item.id()),
                    "duplicate accepted application instructions identity"
                );
                originals.push((&item.item, accepted));
            }
        }
    }
    let (_, latest) = originals.last().context(
        "checkpoint has no accepted application instructions; legacy checkpoints are unsupported",
    )?;
    for row in history {
        let RolloutItem::Compacted(compacted) = row else {
            continue;
        };
        let replacement = compacted
            .replacement_history
            .as_ref()
            .context("compacted native history has no verifiable replacement instructions")?;
        let mut contains_latest = false;
        let mut latest_is_last = false;
        for envelope in replacement {
            let item = &envelope.item;
            if let Some(accepted) = accepted(item)? {
                ensure!(
                    originals
                        .iter()
                        .any(|(original, _)| same_receipt(item, original)),
                    "compacted application instructions differ from their accepted original"
                );
                latest_is_last = accepted.id == latest.id;
                contains_latest |= latest_is_last;
                continue;
            }
            if let ResponseItem::Message {
                role,
                content,
                internal_chat_message_metadata_passthrough: metadata,
                ..
            } = item
            {
                let kinds = metadata
                    .as_ref()
                    .and_then(|metadata| metadata.content_item_kinds.as_deref());
                if kinds.is_some_and(|kinds| kinds.iter().any(|kind| kind.0 == PROJECTED)) {
                    ensure!(
                        kinds.is_some_and(|kinds| kinds.len() == content.len()),
                        "invalid projected application instructions"
                    );
                }
                for (index, part) in content.iter().enumerate() {
                    let tagged = kinds
                        .and_then(|kinds| kinds.get(index))
                        .is_some_and(|kind| kind.0 == PROJECTED);
                    let marked = role == "developer"
                        && matches!(part, ContentItem::InputText { text } if text.starts_with(MARKERS.0));
                    if tagged || marked {
                        ensure!(
                            role == "developer"
                                && tagged
                                && kinds.is_some_and(|kinds| kinds.len() == content.len()),
                            "invalid projected application instructions"
                        );
                        let ContentItem::InputText { text } = part else {
                            anyhow::bail!("invalid projected application instructions")
                        };
                        let (id, text) = decode(text)?;
                        ensure!(
                            originals
                                .iter()
                                .any(|(_, accepted)| accepted.id.as_str() == id
                                    && accepted.text == text),
                            "projected application instructions have no accepted original"
                        );
                        latest_is_last = latest.id.as_str() == id;
                        contains_latest |= latest_is_last;
                    }
                }
            }
        }
        // Core's retained matcher finds any exact copy. If that copy is present,
        // an older fragment after it would suppress the necessary current update.
        ensure!(
            !contains_latest || latest_is_last,
            "compacted history places older application instructions after the latest accepted instructions"
        );
    }
    validate_detached_instruction_projections(history)?;
    Ok(latest.clone())
}

/// Transfer only the validated initial SDK receipt of an otherwise empty
/// checkpoint. No Core context, response provenance or other history is moved.
pub(crate) fn take_initial_receipt(
    history: &mut Vec<RolloutItem>,
    latest: &Accepted,
    root: ThreadId,
) -> Option<ResponseItemEnvelope> {
    let mut receipt_index = None;
    for (index, row) in history.iter().enumerate() {
        match row {
            RolloutItem::SessionMeta(_) => {}
            RolloutItem::EventMsg(EventMsg::ThreadSettingsApplied(event))
                if event.thread_id == Some(root) => {}
            RolloutItem::ResponseItem(envelope)
                if receipt_index.is_none()
                    && envelope.item.id() == Some(&latest.id)
                    && envelope.metadata.as_ref().is_none_or(|metadata| {
                        metadata.sdk_instruction_files.is_some()
                            && *metadata
                                == CodexHarnessMetadata {
                                    sdk_instruction_files: metadata.sdk_instruction_files.clone(),
                                    ..Default::default()
                                }
                    }) =>
            {
                receipt_index = Some(index);
            }
            _ => return None,
        }
    }
    match history.remove(receipt_index?) {
        RolloutItem::ResponseItem(envelope) => Some(envelope),
        _ => unreachable!("receipt_index identifies a response item"),
    }
}

pub(crate) fn publication(text: &str) -> ResponseItem {
    let accepted = Accepted {
        id: ResponseItemId::new(ID_PREFIX),
        text: text.to_owned(),
        files: Vec::new(),
    };
    ResponseItem::Message {
        id: Some(accepted.id.clone()),
        role: "developer".to_owned(),
        content: vec![ContentItem::InputText {
            text: rendered(&accepted),
        }],
        phase: None,
        internal_chat_message_metadata_passthrough: Some(InternalChatMessageMetadataPassthrough {
            content_item_kinds: Some(vec![ContentItemKind(ACCEPTED.to_owned())]),
            ..Default::default()
        }),
    }
}

fn same_receipt(actual: &ResponseItem, expected: &ResponseItem) -> bool {
    let normalize = |item: &ResponseItem| {
        let mut item = item.clone();
        if let ResponseItem::Message {
            internal_chat_message_metadata_passthrough: Some(metadata),
            ..
        } = &mut item
        {
            metadata.turn_id = None;
            metadata.create_time = None;
        }
        item
    };
    normalize(actual) == normalize(expected)
}

fn receipt<'a>(
    items: impl Iterator<Item = &'a ResponseItem>,
    expected: &ResponseItem,
) -> Result<()> {
    let matches = items
        .filter(|item| item.id() == expected.id())
        .collect::<Vec<_>>();
    ensure!(
        matches.len() == 1 && same_receipt(matches[0], expected),
        "native application instruction publication was not confirmed"
    );
    Ok(())
}

/// The caller holds the existing native operation and idle mutation gates.
pub async fn publish(thread: &CodexThread, text: String) -> Result<()> {
    publish_with_files(thread, text, Vec::new()).await
}

pub(crate) async fn publish_with_files(
    thread: &CodexThread,
    text: String,
    files: Vec<InstructionFile>,
) -> Result<()> {
    publish_with_settings(thread, text, files, None).await
}

pub(crate) async fn publish_explicit_with_files(
    thread: &CodexThread,
    text: String,
    files: Vec<InstructionFile>,
) -> Result<()> {
    let settings = crate::hosted_prompt::explicit_inputs(thread, true, false).await?;
    publish_with_settings(thread, text, files, settings).await
}

async fn publish_with_settings(
    thread: &CodexThread,
    text: String,
    files: Vec<InstructionFile>,
    settings: Option<codex_protocol::protocol::ThreadSettingsOverrides>,
) -> Result<()> {
    if crate::agent_profiles::update_active(
        thread,
        settings.as_ref(),
        Some((&text, files.clone())),
        None,
    )
    .await?
    {
        return Ok(());
    }
    let (envelope, _) = prepare(thread, &text, files)?;
    publish_item(thread, envelope, None).await?;
    if let Some(settings) = settings {
        thread.update_thread_settings(settings).await?;
    }
    Ok(())
}

/// Prepare a fresh accepted instruction envelope without publishing or changing authority.
/// Agent profile activation commits this envelope and its captured Accepted value together.
pub(crate) fn prepare(
    thread: &CodexThread,
    text: &str,
    files: Vec<InstructionFile>,
) -> Result<(ResponseItemEnvelope, Accepted)> {
    let mut envelope = thread.annotate_client_response_item(publication(text));
    if !files.is_empty() {
        envelope
            .metadata
            .get_or_insert_with(Default::default)
            .sdk_instruction_files = Some(serde_json::to_value(files)?);
    }
    let accepted = accepted_envelope(&envelope)?
        .context("missing prepared application instruction publication")?;
    Ok((envelope, accepted))
}

/// Stage a first-turn publication. Core commits and flushes before invoking the
/// receipt callback; pending instructions never contribute to world state.
pub(crate) fn initial_turn(
    thread: &Arc<CodexThread>,
    text: String,
    files: Vec<InstructionFile>,
) -> Result<Arc<codex_core::InitialTurnInitialization>> {
    let (envelope, _) = prepare(thread, &text, files)?;
    let committed = envelope.clone();
    let weak = Arc::downgrade(thread);
    let commit: Arc<
        dyn Fn() -> futures_util::future::BoxFuture<'static, Result<()>> + Send + Sync,
    > = Arc::new(move || {
        let thread = weak.upgrade();
        let envelope = committed.clone();
        Box::pin(async move {
            let thread = thread.context("initial instruction owner was closed")?;
            confirm_publication(&thread, envelope, None).await
        })
    });
    Ok(Arc::new(codex_core::InitialTurnInitialization::new(
        vec![envelope],
        commit,
    )))
}

/// Confirm the published envelope before assigning the file sources for future sampling.
pub(crate) async fn publish_item(
    thread: &CodexThread,
    envelope: ResponseItemEnvelope,
    current_files: Option<Vec<InstructionFile>>,
) -> Result<()> {
    thread
        .thread_extension_data()
        .get::<Instructions>()
        .context("application instruction updates require a managed Session")?;
    accepted_envelope(&envelope)?.context("missing application instructions publication")?;
    thread
        .inject_response_item_envelopes(vec![envelope.clone()])
        .await?;
    thread.flush_rollout().await?;
    confirm_publication(thread, envelope, current_files).await
}

async fn confirm_publication(
    thread: &CodexThread,
    envelope: ResponseItemEnvelope,
    current_files: Option<Vec<InstructionFile>>,
) -> Result<()> {
    let owner = thread
        .thread_extension_data()
        .get::<Instructions>()
        .context("application instruction updates require a managed Session")?;
    let accepted =
        accepted_envelope(&envelope)?.context("missing application instructions publication")?;
    let live = thread.conversation_history_snapshot().await;
    receipt(live.items(), &envelope.item)?;
    if thread
        .thread_extension_data()
        .get::<codex_core::PrivateDisposableRun>()
        .is_some()
    {
        ensure!(
            accepted.files.is_empty() && current_files.as_ref().is_none_or(Vec::is_empty),
            "private instruction receipt cannot contain files"
        );
    } else {
        let stored = thread.load_history(true).await?;
        let mut stored_items = stored.items.iter().filter_map(|row| match row {
            RolloutItem::ResponseItem(item) => Some(item),
            _ => None,
        });
        receipt(stored_items.clone().map(|item| &item.item), &envelope.item)?;
        let recorded = stored_items
            .find(|item| item.item.id() == envelope.item.id())
            .context("missing native application instruction receipt")?;
        ensure!(
            recorded
                .metadata
                .as_ref()
                .and_then(|value| value.sdk_instruction_files.as_ref())
                == envelope
                    .metadata
                    .as_ref()
                    .and_then(|value| value.sdk_instruction_files.as_ref()),
            "native application instruction file receipt was not confirmed"
        );
    }
    *owner.0.lock().unwrap_or_else(PoisonError::into_inner) =
        Some(accepted.with_current_files(current_files));
    Ok(())
}

pub(crate) fn captured_prompt_text(
    step: Option<&codex_extension_api::ExtensionData>,
) -> Result<String> {
    let seed = step
        .and_then(|step| step.get::<SdkTurnReceiptSeed>())
        .context("Missing captured hosted prompt instruction owner")?;
    let application = seed
        .application
        .as_ref()
        .context("Missing accepted hosted prompt instructions")?;
    Ok(decode(&application.rendered)?.1.to_owned())
}

/// Remove only SDK-owned instruction parts from the detached provider projection.
/// Accepted canonical envelopes and unrelated parts stay with their original owners.
pub(crate) fn remove_prompt_fragments(items: &mut Vec<ResponseItem>) -> Result<()> {
    let mut invalid_metadata = false;
    items.retain_mut(|item| {
        let ResponseItem::Message {
            content,
            internal_chat_message_metadata_passthrough: Some(metadata),
            ..
        } = item
        else {
            return true;
        };
        let Some(kinds) = &mut metadata.content_item_kinds else {
            return true;
        };
        if !kinds
            .iter()
            .any(|kind| kind.0 == ACCEPTED || kind.0 == PROJECTED)
        {
            return true;
        }
        if kinds.len() != content.len() {
            invalid_metadata = true;
            return true;
        }
        let mut index = 0;
        content.retain(|_| {
            let keep = !matches!(kinds[index].0.as_str(), ACCEPTED | PROJECTED);
            index += 1;
            keep
        });
        kinds.retain(|kind| !matches!(kind.0.as_str(), ACCEPTED | PROJECTED));
        !content.is_empty()
    });
    ensure!(!invalid_metadata, "Invalid SDK instruction part metadata");
    Ok(())
}

pub struct Contributor;

pub(crate) fn retain_child_files(
    store: &codex_extension_api::ExtensionData,
    config: &codex_core_api::Config,
    files: &[InstructionFile],
) {
    if !files.is_empty()
        && let Some(text) = config.developer_instructions.as_ref()
        && !text.is_empty()
        && config
            .multi_agent_v2
            .subagent_developer_instructions
            .as_ref()
            == Some(text)
    {
        store.insert(SdkApplicationInstructions {
            revision: None,
            rendered: text.clone(),
            developer_sha256: sha256(text),
            files: files.iter().map(|file| serde_json::json!(file)).collect(),
        });
    }
}

impl ContextContributor for Contributor {
    fn contribute_world_state<'a>(
        &'a self,
        input: WorldStateContributionInput<'a>,
    ) -> ExtensionFuture<'a, Vec<WorldStateSectionContribution>> {
        Box::pin(async move {
            // Initial publication must become durable before this cache can
            // contribute. Otherwise Core's injection preparation emits it twice.
            let omitted_prompt_instructions = input
                .request_controls
                .hosted_prompt
                .as_ref()
                .and_then(Option::as_ref)
                .is_some_and(|prompt| prompt.omit_instructions);
            let captured_realtime_instructions = input
                .step_store
                .get::<codex_extension_api::AgentProfileInstructions>()
                .is_some();
            let accepted = input
                .step_store
                .get::<Accepted>()
                .map(|accepted| accepted.as_ref().clone())
                .or_else(|| {
                    (!captured_realtime_instructions
                        && input
                            .step_store
                            .get::<codex_extension_api::CapturedAgentProfile>()
                            .is_none())
                    .then(|| {
                        input
                            .thread_store
                            .get::<Instructions>()
                            .and_then(|owner| owner.current())
                    })
                    .flatten()
                });
            input.step_store.insert(SdkTurnReceiptSeed {
                operation_declaration_source: input
                    .turn_store
                    .get::<crate::agent_profiles::OperationDeclarationSource>()
                    .and_then(|source| source.0.clone()),
                codex_revision: include_str!("../CODEX_REVISION").trim(),
                codex_source_tree: CODEX_SOURCE_TREE.as_str(),
                sha256,
                strict_executor_writes: input
                    .thread_store
                    .get::<crate::environment::StrictExecutorWrites>()
                    .is_some(),
                application: accepted
                    .as_ref()
                    .filter(|_| !omitted_prompt_instructions)
                    .map(|accepted| SdkApplicationInstructions {
                        revision: Some(accepted.id.to_string()),
                        rendered: rendered(accepted),
                        developer_sha256: sha256(&accepted.text),
                        files: accepted
                            .files
                            .iter()
                            .map(|file| serde_json::json!(file))
                            .collect(),
                    })
                    .or_else(|| {
                        (!captured_realtime_instructions && !omitted_prompt_instructions)
                            .then(|| {
                                input
                                    .thread_store
                                    .get::<SdkApplicationInstructions>()
                                    .as_deref()
                                    .cloned()
                            })
                            .flatten()
                    }),
            });
            let Some(accepted) = accepted.filter(|_| !omitted_prompt_instructions) else {
                return Vec::new();
            };
            let exact = rendered(&accepted);
            let retained = exact.clone();
            vec![
                WorldStateSectionContribution::new(SECTION, move |previous| {
                    let snapshot = Value::String(accepted.id.to_string());
                    // Both matchers require the *current* exact body. Known/Unknown
                    // therefore prove that body is already in retained native history.
                    let unchanged = !matches!(previous, PreviousWorldStateSection::Absent);
                    (
                        Some(snapshot),
                        (!unchanged).then(|| {
                            RenderedWorldStateFragment::new("developer", MARKERS, body(&accepted))
                        }),
                    )
                })
                .with_legacy_matcher(move |role, text| role == "developer" && text == exact)
                .with_retained_fragment_matcher(move |role, text| {
                    role == "developer" && text == retained
                }),
            ]
        })
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn detached_originals_validate_instructions_without_losing_caller_data() {
        use codex_history::sdk_originals::{
            SdkOriginalHistory, SdkOriginalHistoryCheckpoint, SdkOriginalItem,
        };
        let caller_original = serde_json::to_string(&json!({
            "type":"message", "role":"user", "content":"caller-owned audit value",
            "application_field":{"retained":true}
        }))
        .unwrap();
        let public: ResponseItem = serde_json::from_value(json!({
            "type":"message", "role":"developer",
            "content":[{"type":"input_text","text":"caller-owned projected value"}],
            "internal_chat_message_metadata_passthrough":{"content_item_kinds":["customer.note"]}
        }))
        .unwrap();
        let mut originals = SdkOriginalHistory::default();
        originals.append(SdkOriginalItem {
            original: caller_original.clone(),
            projection: Some(public.into()),
        });
        let compacted: RolloutItem = serde_json::from_value(json!({
            "type":"compacted", "payload":{"message":"summary", "replacement_history":[], "sdk_originals": originals}
        })).unwrap();
        let canonical: ResponseItemEnvelope = publication("A").into();
        let history = vec![RolloutItem::ResponseItem(canonical.clone()), compacted];
        assert!(latest(&history).is_ok());
        for tag in [ACCEPTED, PROJECTED] {
            let mut rejected = history.clone();
            let RolloutItem::Compacted(compacted) = &mut rejected[1] else {
                unreachable!()
            };
            let Some(SdkOriginalHistoryCheckpoint::Replace(originals)) =
                &mut compacted.sdk_originals
            else {
                unreachable!()
            };
            assert_eq!(originals.items[0].original, caller_original);
            let ResponseItem::Message {
                internal_chat_message_metadata_passthrough: Some(metadata),
                ..
            } = &mut originals.items[0].projection.as_mut().unwrap().item
            else {
                unreachable!()
            };
            metadata.content_item_kinds = Some(vec![ContentItemKind(tag.to_owned())]);
            assert!(
                latest(&rejected)
                    .err()
                    .unwrap()
                    .to_string()
                    .contains("no admitted canonical receipt")
            );
        }
        for projected in [false, true] {
            let mut legacy = history.clone();
            let mut admitted = canonical.clone();
            if projected {
                let ResponseItem::Message {
                    id,
                    internal_chat_message_metadata_passthrough: Some(metadata),
                    ..
                } = &mut admitted.item
                else {
                    unreachable!()
                };
                *id = None;
                metadata.content_item_kinds = Some(vec![ContentItemKind(PROJECTED.to_owned())]);
                legacy.push(RolloutItem::ResponseItem(admitted.clone()));
            }
            let RolloutItem::Compacted(compacted) = &mut legacy[1] else {
                unreachable!()
            };
            let Some(SdkOriginalHistoryCheckpoint::Replace(originals)) =
                &mut compacted.sdk_originals
            else {
                unreachable!()
            };
            originals.items[0].projection = Some(admitted);
            assert_eq!(originals.items[0].original, caller_original);
            assert!(latest(&legacy).is_ok());
        }
        let mut audit_only = history;
        let RolloutItem::Compacted(compacted) = &mut audit_only[1] else {
            unreachable!()
        };
        let Some(SdkOriginalHistoryCheckpoint::Replace(originals)) = &mut compacted.sdk_originals
        else {
            unreachable!()
        };
        originals.items[0].original =
            serde_json::to_string(&publication("opaque caller data")).unwrap();
        originals.items[0].projection = None;
        assert!(latest(&audit_only).is_ok());
    }

    use super::*;
    use serde_json::json;
    use std::sync::{Arc, atomic::Ordering};

    fn row(item: ResponseItem) -> RolloutItem {
        RolloutItem::ResponseItem(item.into())
    }

    #[test]
    fn receipts_require_new_identity_and_exact_content() {
        let old = publication("A");
        let next = publication("A");
        assert!(receipt([&old].into_iter(), &next).is_err());
        assert!(receipt([&next, &next].into_iter(), &next).is_err());
        let mut recorded = next.clone();
        if let ResponseItem::Message {
            internal_chat_message_metadata_passthrough: Some(metadata),
            ..
        } = &mut recorded
        {
            metadata.turn_id = Some("native-turn".into());
            metadata.create_time = Some(1.into());
        }
        receipt([&recorded].into_iter(), &next).unwrap();
        let mut changed = accepted(&next).unwrap().unwrap();
        changed.text = "B".to_owned();
        if let ResponseItem::Message { content, .. } = &mut recorded {
            *content = vec![ContentItem::InputText {
                text: rendered(&changed),
            }];
        }
        assert!(receipt([&recorded].into_iter(), &next).is_err());
    }

    #[test]
    fn latest_original_owns_restore_even_when_compaction_retains_older_text() {
        let old = publication("A");
        let mut rows = vec![row(old.clone()), row(publication("B"))];
        rows.push(
            serde_json::from_value(json!({
                "type":"compacted", "payload":{"message":"summary", "replacement_history":[old]}
            }))
            .unwrap(),
        );
        validate(&rows, "B").unwrap();
        assert_eq!(latest(&rows).unwrap().text, "B");
        assert!(validate(&rows, "A").is_err());
        let mut invalid = publication("C");
        if let ResponseItem::Message {
            internal_chat_message_metadata_passthrough,
            ..
        } = &mut invalid
        {
            *internal_chat_message_metadata_passthrough = None;
        }
        rows.push(row(invalid));
        assert!(validate(&rows, "B").is_err());
        assert!(latest(&rows).is_err());
        assert!(validate(&[], "").is_err());
        for text in ["", "  A\n", "</application_instructions>"] {
            assert_eq!(accepted(&publication(text)).unwrap().unwrap().text, text);
            assert_eq!(latest(&[row(publication(text))]).unwrap().text, text);
        }
    }

    #[test]
    fn current_compacted_instructions_must_follow_older_fragments() {
        let old = publication("A");
        let latest = publication("B");
        let mut projected = latest.clone();
        if let ResponseItem::Message {
            id,
            internal_chat_message_metadata_passthrough: Some(metadata),
            ..
        } = &mut projected
        {
            *id = Some(ResponseItemId::new("msg"));
            metadata.content_item_kinds = Some(vec![ContentItemKind(PROJECTED.to_owned())]);
        }
        for current in [latest.clone(), projected] {
            for (replacement, valid) in [
                (vec![current.clone(), old.clone()], false),
                (vec![old.clone(), current], true),
            ] {
                let rows = vec![
                    row(old.clone()),
                    row(latest.clone()),
                    serde_json::from_value(json!({
                        "type":"compacted", "payload":{"message":"summary", "replacement_history":replacement}
                    }))
                    .unwrap(),
                ];
                assert_eq!(validate(&rows, "B").is_ok(), valid);
            }
        }
    }

    #[tokio::test]
    async fn swallowed_native_append_does_not_acknowledge_old_equal_instructions() {
        let directory = tempfile::tempdir().unwrap();
        let home = directory.path().join("home");
        let work = directory.path().join("work");
        std::fs::create_dir(&home).unwrap();
        std::fs::create_dir(&work).unwrap();
        let engine = Arc::new(crate::engine::EngineState::new(
            json!({
                "model":"gpt-5.2", "model_provider":"fixture",
                "model_providers.fixture":{"name":"Instructions fixture", "base_url":"http://127.0.0.1:9/v1", "wire_api":"responses", "requires_openai_auth":false, "supports_websockets":false},
                "approval_policy":"never", "sandbox_mode":"read-only", "web_search":"disabled",
                "agents.enabled":false, "features.code_mode":false, "features.shell_snapshot":false
            }).to_string(),
            json!({"codex_home":home, "cwd":work, "codex_self_exe":null}).to_string(),
            false, None,
        ).await.unwrap());
        let probe = tokio::spawn({
            let engine = engine.clone();
            async move {
                let native = engine
                    .start_with_tools(
                        serde_json::from_value(json!({"_application_instructions":"A"})).unwrap(),
                        None,
                        Some(Vec::new()),
                        false,
                    )
                    .await
                    .unwrap();
                let state = native.current().unwrap();
                publish(&state.thread, "A".into()).await.unwrap();
                publish(&state.thread, "B".into()).await.unwrap();
                let store = engine
                    .store
                    .as_any()
                    .downcast_ref::<crate::storage::MemoryThreadStore>()
                    .unwrap();
                store.fail_appends.store(true, Ordering::SeqCst);
                let result = publish(&state.thread, "A".into()).await;
                assert!(result.is_err());
                let live = state.thread.conversation_history_snapshot().await;
                assert_eq!(
                    live.items()
                        .filter(|item| accepted(item)
                            .unwrap()
                            .is_some_and(|record| record.text == "A"))
                        .count(),
                    2
                );
                let saved = state.thread.load_history(true).await.unwrap();
                validate(&saved.items, "B").unwrap();
                assert_eq!(
                    state
                        .thread
                        .thread_extension_data()
                        .get::<Instructions>()
                        .unwrap()
                        .current()
                        .map(|record| record.text),
                    Some("B".to_owned())
                );
            }
        })
        .await;
        engine
            .store
            .as_any()
            .downcast_ref::<crate::storage::MemoryThreadStore>()
            .unwrap()
            .fail_appends
            .store(false, Ordering::SeqCst);
        engine.close().await.unwrap();
        probe.unwrap();
    }
}
