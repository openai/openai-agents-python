//! Captured Agent declarations; Core alone owns the committed active profile.

use std::collections::{BTreeMap, HashSet};
use std::sync::{Arc, Mutex, Weak};

use anyhow::{Context, Result, ensure};
use codex_core_api::{CodexThread, StartThreadOptions};
use codex_extension_api::{
    AgentProfile, AgentProfileActivation, AgentProfileBoundaryInput, AgentProfileModelSettings,
    AgentProfileReceipt, CapturedAgentProfile, ExtensionData, ExtensionDataInit, ExtensionFuture,
    ToolContributor, TurnLifecycleContributor,
};
use codex_history::{ResponseItemEnvelope, RolloutItem};
use codex_protocol::dynamic_tools::{DynamicToolFunctionSpec, DynamicToolSpec};
use codex_thread_store::PersistContext;
use codex_tools::{
    FunctionCallError, ToolCall, ToolExecutor, ToolExecutorFuture, ToolName, ToolOutput, ToolSpec,
    dynamic_tool_to_responses_api_tool,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::application_instructions::{Accepted, InstructionFile};
use crate::python_tools::{ProfileCatalog, PythonTools};

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct OpeningProfiles {
    pub(crate) root_id: String,
    pub(crate) roster: Vec<ProfileSpec>,
    pub(crate) fingerprint: String,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ProfileSpec {
    id: String,
    model: AgentProfileModelSettings,
    instructions: String,
    tools: Vec<DynamicToolSpec>,
    #[serde(default)]
    manual_slots: Vec<usize>,
    #[serde(default)]
    terminal_slots: Vec<usize>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    input_error_slots: Vec<usize>,
    #[serde(default)]
    step_defaults: Option<Vec<usize>>,
    #[serde(default)]
    handoffs: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    handoff_callbacks: BTreeMap<String, usize>,
    #[serde(default)]
    transform_context: bool,
    #[serde(default)]
    before_compact: bool,
    #[serde(default)]
    before_complete: bool,
    #[serde(default)]
    selecting_steps: bool,
    output_fingerprint: String,
    #[serde(default)]
    mcp_servers: BTreeMap<String, Value>,
    #[serde(default)]
    mcp_result_tools: BTreeMap<String, BTreeMap<String, f64>>,
    #[serde(default)]
    mcp_metadata_tools: BTreeMap<String, Vec<String>>,
    #[serde(default)]
    mcp_tools: BTreeMap<String, McpToolDeclaration>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct McpToolDeclaration {
    pub(crate) tools: Vec<String>,
    defer_loading: bool,
    #[serde(default)]
    pub(crate) before: bool,
    #[serde(default)]
    pub(crate) approval: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ProfileToolUpdate {
    pub(crate) mcp_tools: BTreeMap<String, McpToolDeclaration>,
    pub(crate) default_handoffs: Option<Vec<String>>,
}

fn handoff_defaults(
    declaration: &ProfileSpec,
    names: Option<&[String]>,
) -> Result<Option<Vec<ToolName>>> {
    names
        .map(|names| {
            let mut seen = HashSet::new();
            names
                .iter()
                .map(|name| {
                    ensure!(
                        declaration.handoffs.contains_key(name) && seen.insert(name),
                        "Handoff defaults must name distinct captured handoffs"
                    );
                    Ok(ToolName::new(None, name.clone()).with_default_namespace())
                })
                .collect()
        })
        .transpose()
}

struct OpeningFiles(Vec<InstructionFile>);

impl OpeningProfiles {
    pub(crate) fn validate_restored_handoff(
        &self,
        tool_name: &ToolName,
        receipt: &codex_protocol::agent_profile::AgentProfileHandoff,
    ) -> Result<()> {
        let source = self
            .roster
            .iter()
            .find(|profile| profile.id == receipt.source.profile_id)
            .context("Restored handoff source is outside the declared graph")?;
        ensure!(
            source.handoffs.iter().any(|(name, target)| {
                ToolName::new(None, name.clone()).with_default_namespace()
                    == tool_name.clone().with_default_namespace()
                    && target == &receipt.target_profile_id
            }),
            "Restored handoff is outside its source declaration"
        );
        Ok(())
    }

    pub(crate) fn manual_restore_declaration(
        &self,
        profile_id: &str,
    ) -> Result<(&str, &[DynamicToolSpec], &[usize])> {
        let profile = self
            .roster
            .iter()
            .find(|profile| profile.id == profile_id)
            .context("Manual continuation source is outside the captured graph")?;
        Ok((&profile.instructions, &profile.tools, &profile.manual_slots))
    }

    /// Rebind a saved ordered subset only to the exact fresh source declarations.
    pub(crate) fn restore_catalog(
        &self,
        history: &[RolloutItem],
        saved: &Value,
    ) -> Result<(Vec<DynamicToolSpec>, Vec<usize>)> {
        ensure!(
            roster_fingerprint(&self.roster, &self.root_id)? == self.fingerprint,
            "Captured Agent graph fingerprint does not match its declarations"
        );
        let saved: Vec<DynamicToolSpec> = serde_json::from_value(saved.clone())?;
        let (_, source) = restored_envelope(self, history)?
            .context("Managed Agent checkpoint has no source receipt")?;
        let (_, declarations, manual) = self.manual_restore_declaration(&source.profile_id)?;
        let mut seen = HashSet::new();
        let mut slots = Vec::new();
        for (slot, spec) in saved.iter().enumerate() {
            let value = serde_json::to_value(spec)?;
            let original = declarations
                .iter()
                .position(|candidate| {
                    serde_json::to_value(candidate).is_ok_and(|candidate| candidate == value)
                })
                .context("Saved Agent tool is outside the fresh source declaration")?;
            ensure!(
                seen.insert(original),
                "Duplicate saved Agent tool declaration"
            );
            if manual.contains(&original) {
                slots.push(slot);
            }
        }
        Ok((saved, slots))
    }

    pub(crate) fn capabilities(&self) -> [bool; 4] {
        [
            self.roster.iter().any(|profile| profile.selecting_steps),
            self.roster.iter().any(|profile| profile.transform_context),
            self.roster.iter().any(|profile| profile.before_compact),
            self.roster.iter().any(|profile| profile.before_complete),
        ]
    }
}

fn decode_roster(raw: &str) -> Result<Vec<ProfileSpec>> {
    serde_json::from_str(raw).map_err(|_| anyhow::anyhow!("Invalid captured Agent roster"))
}

fn validate(roster: &[ProfileSpec], root_id: &str) -> Result<()> {
    let ids = roster
        .iter()
        .map(|profile| profile.id.as_str())
        .collect::<HashSet<_>>();
    ensure!(
        ids.len() == roster.len() && ids.contains(root_id),
        "Invalid captured Agent identities"
    );
    for profile in roster {
        ensure!(
            !profile.id.is_empty() && !profile.output_fingerprint.is_empty(),
            "Incomplete captured Agent declaration"
        );
        let mut all_declarations = profile.tools.clone();
        for name in profile.handoffs.keys() {
            if profile.handoff_callbacks.contains_key(name) {
                continue;
            }
            all_declarations.push(DynamicToolSpec::Function(DynamicToolFunctionSpec {
                name: name.clone(),
                description: "Native handoff".into(),
                input_schema: json!({"type":"object","properties":{},"additionalProperties":false}),
                defer_loading: false,
            }));
        }
        crate::python_tools::validate_declarations(&all_declarations)?;
        for (name, slot) in &profile.handoff_callbacks {
            ensure!(
                profile.handoffs.contains_key(name)
                    && matches!(profile.tools.get(*slot), Some(DynamicToolSpec::Function(spec)) if spec.name == *name)
                    && !profile.manual_slots.contains(slot)
                    && !profile.terminal_slots.contains(slot),
                "Handoff callback must name its exact automatic catalog slot"
            );
        }
        for (name, target) in &profile.handoffs {
            ensure!(
                !name.is_empty()
                    && name.len() <= 64
                    && name
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
                    && ids.contains(target.as_str()),
                "Invalid captured handoff declaration"
            );
        }
    }
    Ok(())
}

fn roster_fingerprint(roster: &[ProfileSpec], root_id: &str) -> Result<String> {
    validate(roster, root_id)?;
    let declarations = roster
        .iter()
        .map(|profile| {
            let mut descriptor = serde_json::to_value(profile)?;
            // Endpoint credentials, process configuration and refreshed policies are not
            // declaration compatibility. Their fresh native owners remain authority.
            descriptor["mcp_servers"] = json!(profile.mcp_servers.keys().collect::<Vec<_>>());
            Ok(descriptor)
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&json!({
            "root_id": root_id, "roster": declarations,
        }))?)
    ))
}

/// Pure normalization shared by opening, restoration and Python's graph capture.
/// Hashes declaration/model/instruction/output/callback shapes and MCP names, never credentials.
pub(crate) fn fingerprint(roster_json: &str, root_id: &str) -> Result<String> {
    roster_fingerprint(&decode_roster(roster_json)?, root_id)
}

pub(crate) fn opening(thread: &CodexThread) -> Option<(OpeningProfiles, Vec<InstructionFile>)> {
    let store = thread.thread_extension_data();
    Some((
        (*store.get::<OpeningProfiles>()?).clone(),
        store
            .get::<OpeningFiles>()
            .map(|files| files.0.clone())
            .unwrap_or_default(),
    ))
}

pub(crate) fn install_opening(
    startup: &mut StartThreadOptions,
    opening: OpeningProfiles,
    files: Vec<InstructionFile>,
) -> Result<()> {
    ensure!(
        roster_fingerprint(&opening.roster, &opening.root_id)? == opening.fingerprint,
        "Captured Agent graph fingerprint does not match its declarations"
    );
    ensure!(
        startup
            .thread_extension_init
            .get::<OpeningProfiles>()
            .is_none(),
        "Captured Agent graph is already installed"
    );
    startup.thread_extension_init.insert(OpeningFiles(files));
    startup.thread_extension_init.insert(opening);
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PersistedProfile {
    profile_id: String,
    epoch: u64,
    model: AgentProfileModelSettings,
}

pub(crate) fn profile_receipt(value: &Value) -> Result<AgentProfileReceipt> {
    let source: PersistedProfile = serde_json::from_value(value.clone())
        .map_err(|_| anyhow::anyhow!("Invalid restored Agent receipt"))?;
    Ok(AgentProfileReceipt {
        profile_id: source.profile_id,
        epoch: source.epoch,
    })
}

fn restored_envelope<'a>(
    opening: &OpeningProfiles,
    history: &'a [RolloutItem],
) -> Result<Option<(&'a ResponseItemEnvelope, PersistedProfile)>> {
    let latest = history
        .iter()
        .filter_map(|item| match item {
            RolloutItem::ResponseItem(envelope)
                if envelope
                    .metadata
                    .as_ref()
                    .is_some_and(|metadata| metadata.sdk_agent_profile.is_some()) =>
            {
                Some(envelope)
            }
            _ => None,
        })
        .last();
    let Some(envelope) = latest else {
        return Ok(None);
    };
    let metadata = envelope
        .metadata
        .as_ref()
        .context("Missing restored Agent metadata")?;
    ensure!(
        metadata.sdk_agent_graph.as_deref() == Some(opening.fingerprint.as_str()),
        "Restore requires the exact captured Agent declaration graph"
    );
    let receipt: PersistedProfile = serde_json::from_value(
        metadata
            .sdk_agent_profile
            .clone()
            .context("Missing restored Agent receipt")?,
    )
    .map_err(|_| anyhow::anyhow!("Invalid restored Agent receipt"))?;
    ensure!(
        opening
            .roster
            .iter()
            .any(|profile| profile.id == receipt.profile_id),
        "Restored Agent is outside the captured graph"
    );
    ensure!(
        !receipt.model.model.is_empty(),
        "Invalid restored Agent settings"
    );
    Ok(Some((envelope, receipt)))
}

#[derive(Clone)]
pub(crate) struct OperationDeclarationSource(pub(crate) Option<AgentProfileReceipt>);

/// Recover the declaration owner frozen before queue admission, never the sampled actor.
pub(crate) fn operation_source(
    opening: &OpeningProfiles,
    history: &[RolloutItem],
    turn_id: &str,
) -> Result<AgentProfileReceipt> {
    validate_restored(opening, history)?;
    ensure!(!turn_id.is_empty(), "Missing operation turn identity");
    let mut original = None;
    let mut first = None;
    for (index, item) in history.iter().enumerate() {
        let RolloutItem::TurnContext(context) = item else {
            continue;
        };
        if context.turn_id.as_deref() != Some(turn_id) {
            continue;
        }
        let Some(receipt) = &context.sdk_receipt else {
            continue;
        };
        let value = receipt
            .get("operation_declaration_source")
            .context("Missing original operation declaration provenance")?;
        let source: AgentProfileReceipt = serde_json::from_value(value.clone())
            .context("Invalid original operation declaration provenance")?;
        ensure!(
            original.as_ref().is_none_or(|previous| previous == &source),
            "Operation declaration provenance changed within its owning turn"
        );
        first.get_or_insert(index);
        original = Some(source);
    }
    let source = original.context("Missing original operation declaration provenance")?;
    let first = first.expect("an original source has an owning receipt");
    let mut matching = 0;
    for item in &history[..first] {
        let RolloutItem::ResponseItem(envelope) = item else {
            continue;
        };
        let Some(metadata) = &envelope.metadata else {
            continue;
        };
        let Some(value) = &metadata.sdk_agent_profile else {
            continue;
        };
        if profile_receipt(value)? == source {
            ensure!(
                metadata.sdk_agent_graph.as_deref() == Some(opening.fingerprint.as_str()),
                "Original operation belongs to a different Agent graph"
            );
            ensure!(
                opening
                    .roster
                    .iter()
                    .any(|profile| profile.id == source.profile_id),
                "Original operation is outside the fresh Agent declaration graph"
            );
            matching += 1;
        }
    }
    ensure!(
        matching == 1,
        "Original operation has no unique canonical accepted Agent envelope"
    );
    Ok(source)
}

pub(crate) fn restore_source(
    opening: &OpeningProfiles,
    history: &[RolloutItem],
) -> Result<AgentProfileReceipt> {
    validate_restored(opening, history)?;
    let (_, source) = restored_envelope(opening, history)?
        .context("Managed Agent checkpoint has no source receipt")?;
    Ok(AgentProfileReceipt {
        profile_id: source.profile_id,
        epoch: source.epoch,
    })
}

pub(crate) fn validate_restored(
    opening: &OpeningProfiles,
    history: &[RolloutItem],
) -> Result<Accepted> {
    ensure!(
        roster_fingerprint(&opening.roster, &opening.root_id)? == opening.fingerprint,
        "Captured Agent graph fingerprint does not match its declarations"
    );
    let (envelope, _) = restored_envelope(opening, history)?
        .context("Managed Agent checkpoint has no native profile receipt")?;
    let accepted = crate::application_instructions::latest(history)?;
    ensure!(
        Some(&accepted.id) == envelope.item.id(),
        "Latest accepted instructions have no matching Agent profile receipt"
    );
    Ok(accepted)
}

#[derive(Clone)]
struct PreparedProfile {
    declaration: ProfileSpec,
    model: AgentProfileModelSettings,
    catalog: ProfileCatalog,
    mcp: Arc<codex_mcp::McpBinding>,
    plugins: codex_extension_api::SelectedPluginSnapshot,
    result_selection: crate::mcp_result_control::Selection,
    mcp_policy: crate::native_interception::ProfileMcpPolicy,
}

struct Reservation {
    turn: String,
    expected: AgentProfileReceipt,
    target: String,
    calls: HashSet<String>,
    accepted: bool,
    restored: bool,
}

struct AgentProfiles {
    opening: Arc<OpeningProfiles>,
    thread: Weak<CodexThread>,
    tools: Arc<PythonTools>,
    profiles: BTreeMap<String, PreparedProfile>,
    files: Vec<InstructionFile>,
    reservation: Mutex<Option<Reservation>>,
}

struct VoiceProfileRegistry {
    generation: String,
    previous: Option<Arc<AgentProfiles>>,
    last_admission: Option<VoiceGraphAdmission>,
}

struct VoiceGraphAdmission {
    receipt: AgentProfileReceipt,
    previous: Option<Arc<AgentProfiles>>,
}

fn set_profiles(thread: &CodexThread, profiles: Option<Arc<AgentProfiles>>) {
    match profiles {
        Some(profiles) => {
            thread.thread_extension_data().insert(profiles);
        }
        None => {
            thread
                .thread_extension_data()
                .remove::<Arc<AgentProfiles>>();
        }
    }
}

async fn prepare_profile(
    thread: &CodexThread,
    tools: &PythonTools,
    descriptor: &ProfileSpec,
    mut model: AgentProfileModelSettings,
    default_model: &str,
    startup: bool,
    voice: bool,
) -> Result<PreparedProfile> {
    if model.model.is_empty() {
        model.model = default_model.to_owned();
    }
    let flags = [
        descriptor.selecting_steps,
        descriptor.transform_context,
        descriptor.before_compact,
        descriptor.before_complete,
    ];
    let catalog = if voice {
        tools.voice_profile_catalog(
            descriptor.tools.clone(),
            &descriptor.manual_slots,
            &descriptor.terminal_slots,
            &descriptor.input_error_slots,
            descriptor.step_defaults.as_deref(),
            flags,
        )?
    } else {
        tools.profile_catalog(
            descriptor.tools.clone(),
            &descriptor.manual_slots,
            &descriptor.terminal_slots,
            &descriptor.input_error_slots,
            descriptor.step_defaults.as_deref(),
            flags,
            startup,
        )?
    };
    let servers = crate::mcp_roster::decode(json!(descriptor.mcp_servers))?;
    let result_selection: crate::mcp_result_control::Selection = serde_json::from_value(json!({
        "generation": catalog.generation.id, "servers": descriptor.mcp_result_tools,
        "metadata_servers": descriptor.mcp_metadata_tools,
    }))?;
    result_selection.validate(&servers)?;
    let (mcp, plugins) = thread.prepare_agent_profile_mcp(servers).await?;
    let mcp_policy = crate::native_interception::ProfileMcpPolicy::prepare(
        &mcp,
        &descriptor.mcp_tools,
        catalog.generation.id,
    )?;
    Ok(PreparedProfile {
        declaration: descriptor.clone(),
        model,
        catalog,
        mcp,
        plugins,
        result_selection,
        mcp_policy,
    })
}

pub(crate) async fn admit_realtime_graph(
    state: &crate::engine::ThreadState,
    generation: &str,
    roster_json: &str,
    root_id: &str,
    expected: Option<AgentProfileReceipt>,
) -> Result<Value> {
    let thread = &state.thread;
    let current = thread
        .thread_extension_data()
        .get::<Arc<AgentProfiles>>()
        .map(|current| current.as_ref().clone());
    let original = match thread.thread_extension_data().get::<VoiceProfileRegistry>() {
        Some(registry) => {
            ensure!(
                registry.generation == generation,
                "Voice graph belongs to another generation"
            );
            registry.previous.clone()
        }
        None => current.clone(),
    };
    let tools = thread
        .thread_extension_data()
        .get::<PythonTools>()
        .context("Voice update requires the native tool owner")?;
    let roster = decode_roster(roster_json)?;
    validate(&roster, root_id)?;
    let mut prepared = current
        .as_ref()
        .map(|current| current.profiles.clone())
        .unwrap_or_default();
    ensure!(
        prepared.len() + roster.len() <= 1024,
        "Voice Agent graph capacity exceeded"
    );
    ensure!(
        roster
            .iter()
            .all(|profile| !prepared.contains_key(&profile.id)),
        "Voice Agent identities cannot be reused"
    );
    let default_model = thread.thread_settings_snapshot().await.model;
    for descriptor in &roster {
        prepared.insert(
            descriptor.id.clone(),
            prepare_profile(
                thread,
                &tools,
                descriptor,
                descriptor.model.clone(),
                &default_model,
                false,
                true,
            )
            .await?,
        );
    }
    let opening = match &current {
        Some(current) => current.opening.clone(),
        None => Arc::new(OpeningProfiles {
            root_id: root_id.to_owned(),
            roster: roster.clone(),
            fingerprint: roster_fingerprint(&roster, root_id)?,
        }),
    };
    let profiles = Arc::new(AgentProfiles {
        opening,
        thread: Arc::downgrade(thread),
        tools,
        profiles: prepared,
        files: current
            .as_ref()
            .map(|current| current.files.clone())
            .unwrap_or_default(),
        reservation: Mutex::new(None),
    });
    let target = profiles.candidate(thread, root_id)?;
    thread.thread_extension_data().insert(profiles.clone());
    let captured = thread
        .capture_realtime_agent_profile_source(generation, expected.as_ref(), target)
        .await;
    let receipt = match captured {
        Ok(receipt) => receipt,
        Err(error) => {
            set_profiles(thread, current);
            return Err(error.into());
        }
    };
    let (selection_id, function_tools) = match thread
        .realtime_agent_source_selection(Some(generation), Some(&receipt))
        .await
    {
        Ok(selection) => selection,
        Err(error) => {
            thread.release_realtime_agent_profile_source(generation, &receipt);
            set_profiles(thread, current);
            return Err(error.into());
        }
    };
    thread.thread_extension_data().insert(VoiceProfileRegistry {
        generation: generation.to_owned(),
        previous: original,
        last_admission: Some(VoiceGraphAdmission {
            receipt: receipt.clone(),
            previous: current,
        }),
    });
    let catalogs = roster
        .iter()
        .map(|profile| {
            (
                profile.id.clone(),
                profiles.profiles[&profile.id].catalog.generation.id,
            )
        })
        .collect::<BTreeMap<_, _>>();
    Ok(
        json!({"active": receipt, "catalogs": catalogs, "selection_id": selection_id, "function_tools": function_tools, "generation_high_water": profiles.tools.generation_high_water()}),
    )
}

pub(crate) fn discard_realtime_graph(
    thread: &CodexThread,
    generation: &str,
    receipt: &AgentProfileReceipt,
) -> Result<()> {
    let registry = thread
        .thread_extension_data()
        .get::<VoiceProfileRegistry>()
        .context("Voice graph is unavailable")?;
    ensure!(
        registry.generation == generation,
        "Voice graph belongs to another generation"
    );
    let admission = registry
        .last_admission
        .as_ref()
        .filter(|admission| &admission.receipt == receipt)
        .context("Voice graph does not match the last admission")?;
    thread.release_realtime_agent_profile_source(generation, receipt);
    set_profiles(thread, admission.previous.clone());
    thread.thread_extension_data().insert(VoiceProfileRegistry {
        generation: generation.to_owned(),
        previous: registry.previous.clone(),
        last_admission: None,
    });
    Ok(())
}

pub(crate) async fn restore_realtime_graph(
    thread: &CodexThread,
    generation: &str,
) -> Result<Value> {
    if let Some(registry) = thread.thread_extension_data().get::<VoiceProfileRegistry>() {
        ensure!(
            registry.generation == generation,
            "Voice graph belongs to another generation"
        );
        set_profiles(thread, registry.previous.clone());
        thread.release_realtime_agent_profile_sources(generation);
        thread
            .thread_extension_data()
            .remove::<VoiceProfileRegistry>();
    }
    if let Some(profiles) = thread.thread_extension_data().get::<Arc<AgentProfiles>>() {
        profiles.receipt(thread).await
    } else {
        let tools = thread
            .thread_extension_data()
            .get::<PythonTools>()
            .context("Native tool owner is unavailable")?;
        Ok(
            json!({"active":null,"catalogs":{},"generation_high_water":tools.generation_high_water()}),
        )
    }
}

pub(crate) async fn realtime_source(
    thread: &CodexThread,
    generation: &str,
    receipt: AgentProfileReceipt,
) -> Result<Value> {
    let profiles = thread
        .thread_extension_data()
        .get::<Arc<AgentProfiles>>()
        .context("Captured Agent graph is unavailable")?;
    let catalog = &profiles
        .profiles
        .get(&receipt.profile_id)
        .context("Voice Agent source is outside its retained graph")?
        .catalog;
    let (selection_id, function_tools) = thread
        .realtime_agent_source_selection(Some(generation), Some(&receipt))
        .await?;
    Ok(
        json!({"profile_receipt": receipt, "catalog_generation": catalog.generation.id,
        "selection_id": selection_id, "function_tools": function_tools}),
    )
}

impl AgentProfiles {
    fn candidate(&self, thread: &CodexThread, id: &str) -> Result<Arc<AgentProfile>> {
        let prepared = self
            .profiles
            .get(id)
            .context("Agent is outside the captured graph")?;
        let files = if id == self.opening.root_id {
            self.files.clone()
        } else {
            Vec::new()
        };
        let (mut envelope, accepted) = crate::application_instructions::prepare(
            thread,
            &prepared.declaration.instructions,
            files,
        )?;
        envelope
            .metadata
            .get_or_insert_with(Default::default)
            .sdk_agent_graph = Some(self.opening.fingerprint.clone());
        let mut data = ExtensionDataInit::new();
        data.insert(codex_extension_api::AgentProfileInstructions {
            text: Some(accepted.text.clone()),
        });
        data.insert(accepted);
        data.insert(prepared.catalog.clone());
        data.insert(prepared.plugins.clone());
        data.insert(codex_extension_api::AgentProfileHandoffTools {
            targets: prepared
                .declaration
                .handoffs
                .iter()
                .map(|(name, target)| {
                    (
                        ToolName::new(None, name.clone()).with_default_namespace(),
                        target.clone(),
                    )
                })
                .collect(),
        });
        data.insert(prepared.result_selection.clone());
        data.insert(prepared.mcp_policy.clone());
        Ok(Arc::new(AgentProfile {
            profile_id: id.to_owned(),
            model: prepared.model.clone(),
            application_instructions: envelope,
            data,
            mcp: prepared.mcp.clone(),
        }))
    }

    async fn receipt(&self, thread: &CodexThread) -> Result<Value> {
        let captured = thread
            .captured_agent_profile()
            .await?
            .context("Captured Agent graph has no committed native profile")?;
        let active = &captured.receipt;
        let catalogs = self
            .profiles
            .iter()
            .map(|(id, profile)| (id.clone(), profile.catalog.generation.id))
            .collect::<BTreeMap<_, _>>();
        let catalog = captured
            .profile
            .data
            .get::<ProfileCatalog>()
            .context("Captured Agent profile has no tool catalog")?;
        Ok(json!({"active": active, "catalogs": catalogs,
            "active_catalog":{"generation":catalog.generation.id,"specs":catalog.checkpoint_specs()?},
            "generation_high_water": self.tools.generation_high_water()}))
    }
}

pub(crate) async fn active_specs(thread: &CodexThread) -> Result<Option<Value>> {
    if thread
        .thread_extension_data()
        .get::<Arc<AgentProfiles>>()
        .is_none()
    {
        return Ok(None);
    }
    let Some(captured) = thread.captured_agent_profile().await? else {
        return Ok(None);
    };
    let catalog = captured
        .profile
        .data
        .get::<ProfileCatalog>()
        .context("Managed Agent checkpoint has no captured catalog")?;
    Ok(Some(catalog.checkpoint_specs()?))
}

pub(crate) async fn active(thread: &CodexThread) -> Result<Option<AgentProfileReceipt>> {
    Ok(thread.agent_profile_receipt().await?)
}

pub(crate) async fn configure(
    state: &crate::engine::ThreadState,
    roster_json: &str,
    root_id: &str,
    default_slots: Option<&[usize]>,
    default_handoffs: Option<&[String]>,
) -> Result<Value> {
    let thread = &state.thread;
    let opening = thread
        .thread_extension_data()
        .get::<OpeningProfiles>()
        .context("Captured Agent graph was not installed before native opening")?;
    let supplied = decode_roster(roster_json)?;
    ensure!(
        root_id == opening.root_id
            && roster_fingerprint(&supplied, root_id)? == opening.fingerprint,
        "Captured Agent graph changed after native opening"
    );
    if let Some(profiles) = thread.thread_extension_data().get::<Arc<AgentProfiles>>() {
        if let Some(slots) = default_slots {
            let captured = thread
                .captured_agent_profile()
                .await?
                .context("Managed Agent has no committed source")?;
            let catalog = captured
                .profile
                .data
                .get::<ProfileCatalog>()
                .context("Managed Agent has no captured catalog")?;
            ensure!(
                catalog.default_names(Some(slots))? == catalog.defaults,
                "Restored Agent defaults changed after configuration"
            );
        }
        if let Some(names) = default_handoffs {
            let captured = thread
                .captured_agent_profile()
                .await?
                .context("Managed Agent has no committed source")?;
            let declaration = &profiles
                .profiles
                .get(&captured.receipt.profile_id)
                .context("Managed Agent is outside the captured graph")?
                .declaration;
            let catalog = captured
                .profile
                .data
                .get::<ProfileCatalog>()
                .context("Managed Agent has no captured catalog")?;
            ensure!(
                handoff_defaults(declaration, Some(names))? == catalog.handoff_defaults,
                "Restored handoff defaults changed after configuration"
            );
        }
        return profiles.receipt(thread).await;
    }
    let tools = thread
        .thread_extension_data()
        .get::<PythonTools>()
        .context("Agent profiles require the admitted Python tool owner")?;
    tools.check_idle(thread).await?;
    // A fresh local thread has no rollout until persistence is materialized.
    // Read only after initialization and its writes have completed.
    state
        .engine
        .store
        .persist_thread(state.id, PersistContext::Standard)
        .await?;
    thread.flush_rollout().await?;
    let files = thread
        .thread_extension_data()
        .get::<OpeningFiles>()
        .map(|files| files.0.clone())
        .unwrap_or_default();
    let config = thread.config().await;
    let settings = thread.thread_settings_snapshot().await;
    let history = thread.load_history(true).await?;
    let restored = restored_envelope(&opening, &history.items)?.is_some();
    ensure!(
        restored || (default_slots.is_none() && default_handoffs.is_none()),
        "Restored Agent defaults require checkpoint history"
    );
    let opening_model = history.items.iter().find_map(|item| {
        let RolloutItem::ResponseItem(envelope) = item else {
            return None;
        };
        let metadata = envelope.metadata.as_ref()?;
        if metadata.sdk_agent_graph.as_deref() != Some(opening.fingerprint.as_str()) {
            return None;
        }
        let source: PersistedProfile =
            serde_json::from_value(metadata.sdk_agent_profile.clone()?).ok()?;
        (source.profile_id == opening.root_id).then_some(source.model)
    });
    ensure!(
        !restored || opening_model.is_some(),
        "Restored Agent graph has no captured root defaults"
    );
    let opening_model = opening_model.unwrap_or(AgentProfileModelSettings {
        model: settings.model.clone(),
        reasoning_effort: settings.reasoning_effort,
        reasoning_summary: settings.reasoning_summary,
        service_tier: settings.service_tier.clone(),
        tool_choice: settings.tool_choice,
        personality: config.personality,
        request_controls: settings.request_controls.clone(),
        invocation_timeout_ms: settings.invocation_timeout_ms,
    });
    let mut profiles = BTreeMap::new();
    for descriptor in &opening.roster {
        let model = if descriptor.id == opening.root_id {
            opening_model.clone()
        } else {
            descriptor.model.clone()
        };
        profiles.insert(
            descriptor.id.clone(),
            prepare_profile(
                thread,
                &tools,
                descriptor,
                model,
                &opening_model.model,
                !restored && descriptor.id == opening.root_id,
                false,
            )
            .await?,
        );
    }
    let profiles = Arc::new(AgentProfiles {
        opening,
        thread: Arc::downgrade(thread),
        tools,
        profiles,
        files,
        reservation: Mutex::new(None),
    });
    let restored = restored_envelope(&profiles.opening, &history.items)?;
    let target = if let Some((envelope, receipt)) = restored {
        let prepared = profiles
            .profiles
            .get(&receipt.profile_id)
            .context("Restored Agent is outside the captured graph")?;
        let accepted = validate_restored(&profiles.opening, &history.items)?;
        let mut catalog = profiles.tools.startup_profile_catalog(
            [
                prepared.catalog.selecting_steps,
                prepared.catalog.transform_context,
                prepared.catalog.before_compact,
                prepared.catalog.before_complete,
            ],
            default_slots,
        )?;
        catalog.handoff_defaults = handoff_defaults(&prepared.declaration, default_handoffs)?;
        let generation = catalog.generation.id;
        let mut data = ExtensionDataInit::new();
        data.insert(codex_extension_api::AgentProfileInstructions {
            text: Some(accepted.text.clone()),
        });
        data.insert(accepted);
        data.insert(catalog);
        data.insert(codex_extension_api::AgentProfileHandoffTools {
            targets: prepared
                .declaration
                .handoffs
                .iter()
                .map(|(name, target)| {
                    (
                        ToolName::new(None, name.clone()).with_default_namespace(),
                        target.clone(),
                    )
                })
                .collect(),
        });
        let mut target = AgentProfile {
            profile_id: receipt.profile_id,
            model: receipt.model,
            application_instructions: envelope.clone(),
            data,
            mcp: prepared.mcp.clone(),
        };
        bind_accepted_mcp(
            thread,
            &mut target,
            &prepared.declaration.mcp_tools,
            generation,
        )
        .await?;
        Arc::new(target)
    } else {
        profiles.candidate(thread, &profiles.opening.root_id)?
    };
    thread.activate_agent_profile(None, target).await?;
    thread.thread_extension_data().insert(profiles.clone());
    thread
        .thread_extension_data()
        .get::<crate::native_interception::Control>()
        .context("Agent profiles require the native source policy owner")?
        .enable_profile_policies();
    profiles.receipt(thread).await
}

/// The checkpoint owner validates this source against canonical pending-call history before entry.
/// This helper binds a fresh waiter to that declaration, never to the current root catalog.
pub(crate) fn enqueue_manual_reentry(
    state: &crate::engine::ThreadState,
    source: &AgentProfileReceipt,
    captured: &CapturedAgentProfile,
    turn: &str,
    item: &codex_protocol::models::ResponseItem,
    model: Option<&str>,
) -> Result<tokio::sync::oneshot::Receiver<crate::python_tools::ToolReply>> {
    ensure!(
        &captured.receipt == source,
        "Manual continuation Agent source changed"
    );
    let catalog = captured
        .profile
        .data
        .get::<ProfileCatalog>()
        .context("Manual continuation Agent catalog is absent")?;
    let profiles = state
        .thread
        .thread_extension_data()
        .get::<Arc<AgentProfiles>>()
        .context("Manual continuation Agent graph is absent")?;
    ensure!(
        profiles.profiles.contains_key(&source.profile_id),
        "Manual continuation Agent source is outside the graph"
    );
    profiles.tools.enqueue_manual_reentry_source(
        &state.id.to_string(),
        turn,
        item,
        model,
        catalog.generation.clone(),
        Some(source.clone()),
    )
}

/// An idle update replaces only Core's current immutable snapshot. A later activation
/// intentionally returns to the captured declaration defaults.
pub(crate) async fn update_active(
    thread: &CodexThread,
    settings: Option<&codex_protocol::protocol::ThreadSettingsOverrides>,
    instructions: Option<(&str, Vec<InstructionFile>)>,
    catalog: Option<(ProfileCatalog, BTreeMap<String, McpToolDeclaration>)>,
) -> Result<bool> {
    let Some(profiles) = thread.thread_extension_data().get::<Arc<AgentProfiles>>() else {
        return Ok(false);
    };
    // Voice-only graphs can populate the registry without a backend profile.
    let Some(captured) = thread.captured_agent_profile().await? else {
        return Ok(false);
    };
    profiles.tools.check_idle(thread).await?;
    let mut target = (*captured.profile).clone();
    if catalog.is_some() {
        if let Some(Some(prompt)) = &mut target.model.request_controls.hosted_prompt {
            prompt.omit_tools = false;
        }
    }
    if let Some(settings) = settings {
        if let Some(model) = &settings.model {
            target.model.model = model.clone();
            if settings
                .request_controls
                .as_ref()
                .is_none_or(|controls| controls.hosted_prompt.is_none())
            {
                if let Some(Some(prompt)) = &mut target.model.request_controls.hosted_prompt {
                    prompt.omit_model = false;
                }
            }
        }
        if let Some(effort) = &settings.effort {
            target.model.reasoning_effort = effort.clone();
        }
        if let Some(summary) = settings.summary {
            target.model.reasoning_summary = Some(summary);
        }
        if let Some(tier) = &settings.service_tier {
            target.model.service_tier = tier.clone();
        }
        if let Some(choice) = settings.tool_choice {
            target.model.tool_choice = Some(choice);
            target.model.request_controls.named_tool_choice = Some(None);
        }
        if let Some(controls) = &settings.request_controls {
            target.model.request_controls.apply_patch(controls);
        }
        if let Some(timeout) = settings.invocation_timeout_ms {
            target.model.invocation_timeout_ms = timeout;
        }
        if let Some(personality) = settings.personality {
            target.model.personality = Some(personality);
        }
    }
    let accepted = captured
        .profile
        .data
        .get::<Accepted>()
        .context("Managed Agent update has no captured instructions")?;
    let (text, files) = instructions.unwrap_or((&accepted.text, accepted.files.clone()));
    let (mut envelope, accepted) = crate::application_instructions::prepare(thread, text, files)?;
    envelope
        .metadata
        .get_or_insert_with(Default::default)
        .sdk_agent_graph = Some(profiles.opening.fingerprint.clone());
    target.application_instructions = envelope;
    target
        .data
        .insert(codex_extension_api::AgentProfileInstructions {
            text: Some(accepted.text.clone()),
        });
    target.data.insert(accepted);
    if let Some((catalog, mcp_tools)) = catalog {
        bind_accepted_mcp(thread, &mut target, &mcp_tools, catalog.generation.id).await?;
        target.data.insert(catalog);
    }
    thread
        .activate_agent_profile(Some(captured.receipt.clone()), Arc::new(target))
        .await?;
    Ok(true)
}

/// Bind an unpublished active candidate to the exact roster already accepted by
/// the SDK mutation. Declaration templates and existing captured sources stay owned.
async fn bind_accepted_mcp(
    thread: &CodexThread,
    target: &mut AgentProfile,
    declarations: &BTreeMap<String, McpToolDeclaration>,
    generation: u64,
) -> Result<()> {
    let current = thread.config().await;
    let (mcp, plugins) = thread
        .prepare_agent_profile_mcp(current.mcp_servers.get().clone())
        .await?;
    let policy =
        crate::native_interception::ProfileMcpPolicy::prepare(&mcp, declarations, generation)?;
    let mut selection = thread
        .thread_extension_data()
        .get::<crate::mcp_result_control::McpResultControl>()
        .and_then(|control| control.capture_source_selection())
        .unwrap_or(serde_json::from_value(
            json!({"generation":generation,"servers":{}}),
        )?);
    selection.generation = generation;
    target.mcp = mcp;
    target.data.insert(plugins);
    target.data.insert(policy);
    target.data.insert(selection);
    Ok(())
}

pub(crate) async fn replace_tools(
    thread: &CodexThread,
    requested: u64,
    specs: Vec<DynamicToolSpec>,
    manual_slots: &[usize],
    default_slots: Option<&[usize]>,
    profile_update: Option<&ProfileToolUpdate>,
    terminal_slots: &[usize],
    input_error_slots: &[usize],
) -> Result<Option<u64>> {
    let Some(profiles) = thread.thread_extension_data().get::<Arc<AgentProfiles>>() else {
        return Ok(None);
    };
    let Some(captured) = thread.captured_agent_profile().await? else {
        return Ok(None);
    };
    profiles.tools.check_idle(thread).await?;
    let previous = captured
        .profile
        .data
        .get::<ProfileCatalog>()
        .context("Managed Agent replacement has no captured catalog")?;
    let profile_update = profile_update
        .context("Managed Agent tool replacement requires captured source policies")?;
    let declaration = &profiles
        .profiles
        .get(&captured.receipt.profile_id)
        .context("Active Agent is outside the captured declaration graph")?
        .declaration;
    let handoffs = handoff_defaults(declaration, profile_update.default_handoffs.as_deref())?;
    let mut catalog = profiles.tools.profile_catalog(
        specs,
        manual_slots,
        terminal_slots,
        input_error_slots,
        default_slots,
        [
            previous.selecting_steps,
            previous.transform_context,
            previous.before_compact,
            previous.before_complete,
        ],
        false,
    )?;
    catalog.handoff_defaults = handoffs;
    let generation = catalog.generation.id;
    ensure!(
        requested == generation,
        "Python tool generation is out of sequence"
    );
    update_active(
        thread,
        None,
        None,
        Some((catalog, profile_update.mcp_tools.clone())),
    )
    .await?;
    Ok(Some(generation))
}

/// Restore only a typed accepted result paired by the checkpoint owner with its original call.
pub(crate) fn restore_handoff(
    state: &crate::engine::ThreadState,
    captured: &CapturedAgentProfile,
    turn: &str,
    tool_name: &ToolName,
    receipt: &codex_protocol::agent_profile::AgentProfileHandoff,
) -> Result<()> {
    ensure!(
        captured.receipt == receipt.source,
        "Restored handoff source changed"
    );
    let declarations = captured
        .profile
        .data
        .get::<codex_extension_api::AgentProfileHandoffTools>()
        .context("Restored handoff has no admitted source declarations")?;
    ensure!(
        declarations
            .targets
            .get(&tool_name.clone().with_default_namespace())
            == Some(&receipt.target_profile_id),
        "Restored handoff is outside its source declaration"
    );
    let profiles = state
        .thread
        .thread_extension_data()
        .get::<Arc<AgentProfiles>>()
        .context("Restored handoff graph is absent")?;
    let mut pending = profiles
        .reservation
        .lock()
        .expect("Agent reservation poisoned");
    if let Some(previous) = pending.as_mut() {
        ensure!(
            previous.turn == turn
                && previous.expected == receipt.source
                && previous.target == receipt.target_profile_id,
            "Conflicting restored Agent handoffs"
        );
        previous.calls.insert(receipt.call_id.clone());
        previous.accepted = true;
        previous.restored = true;
    } else {
        *pending = Some(Reservation {
            turn: turn.into(),
            expected: receipt.source.clone(),
            target: receipt.target_profile_id.clone(),
            calls: HashSet::from([receipt.call_id.clone()]),
            accepted: true,
            restored: true,
        });
    }
    Ok(())
}

pub(crate) async fn activate(
    state: &crate::engine::ThreadState,
    target: &str,
    expected: AgentProfileReceipt,
) -> Result<AgentProfileReceipt> {
    let profiles = state
        .thread
        .thread_extension_data()
        .get::<Arc<AgentProfiles>>()
        .context("Captured Agent graph is not configured")?;
    profiles.tools.check_idle(&state.thread).await?;
    let candidate = profiles.candidate(&state.thread, target)?;
    Ok(state
        .thread
        .activate_agent_profile(Some(expected), candidate)
        .await?)
}

pub(crate) struct Contributor;

impl TurnLifecycleContributor for Contributor {
    fn before_step_agent_activation<'a>(
        &'a self,
        input: AgentProfileBoundaryInput<'a>,
    ) -> ExtensionFuture<'a, std::result::Result<Option<AgentProfileActivation>, String>> {
        Box::pin(async move {
            let Some(profiles) = input.thread_store.get::<Arc<AgentProfiles>>() else {
                return Ok(None);
            };
            let restored = profiles
                .reservation
                .lock()
                .expect("Agent reservation poisoned")
                .take()
                .filter(|reservation| {
                    reservation.restored
                        && reservation.accepted
                        && reservation.turn == input.turn_id
                });
            let handoff = input.accepted_handoff;
            let (expected, target_id) = match (handoff, restored) {
                (Some(handoff), Some(restored)) => {
                    if handoff.source != restored.expected
                        || handoff.target_profile_id != restored.target
                    {
                        return Err("Conflicting accepted Agent handoffs".into());
                    }
                    (handoff.source.clone(), handoff.target_profile_id.clone())
                }
                (Some(handoff), None) => {
                    (handoff.source.clone(), handoff.target_profile_id.clone())
                }
                (None, Some(restored)) => (restored.expected, restored.target),
                (None, None) => return Ok(None),
            };
            if input.active != Some(&expected) {
                return Err("Captured Agent handoff source is stale".into());
            }
            let source = profiles
                .profiles
                .get(&expected.profile_id)
                .ok_or("Accepted handoff source is outside the graph")?;
            if !source
                .declaration
                .handoffs
                .values()
                .any(|target| target == &target_id)
            {
                return Err("Accepted handoff target is outside its source declaration".into());
            }
            let thread = profiles
                .thread
                .upgrade()
                .ok_or("Native Agent thread is closed")?;
            let target = profiles
                .candidate(&thread, &target_id)
                .map_err(|error| error.to_string())?;
            Ok(Some(AgentProfileActivation { expected, target }))
        })
    }
}

struct HandoffExecutor {
    profiles: Arc<AgentProfiles>,
    source: AgentProfileReceipt,
    name: String,
    target: String,
    spec: ToolSpec,
    callback: Option<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>>,
}

/// A callback failure retires only its own provisional reservation. Accepted or
/// still-running siblings retain their original native reservation owner.
struct ProvisionalHandoff<'a> {
    executor: &'a HandoffExecutor,
    turn: String,
    call: String,
    keep: bool,
}

impl Drop for ProvisionalHandoff<'_> {
    fn drop(&mut self) {
        if self.keep {
            return;
        }
        let mut pending = self
            .executor
            .profiles
            .reservation
            .lock()
            .expect("Agent reservation poisoned");
        if let Some(reservation) = pending.as_mut() {
            if reservation.turn == self.turn
                && reservation.expected == self.executor.source
                && reservation.target == self.executor.target
            {
                reservation.calls.remove(&self.call);
                if reservation.calls.is_empty() && !reservation.accepted {
                    *pending = None;
                }
            }
        }
    }
}

impl<'call> ToolExecutor<ToolCall<'call>> for HandoffExecutor {
    fn tool_name(&self) -> ToolName {
        ToolName::new(None, self.name.clone())
    }
    fn spec(&self) -> ToolSpec {
        self.spec.clone()
    }
    fn exposure(&self) -> codex_tools::ToolExposure {
        codex_tools::ToolExposure::DirectModelOnly
    }
    fn supports_parallel_tool_calls(&self) -> bool {
        true
    }
    fn on_result_accepted(
        &self,
        turn_id: &str,
        call_id: &str,
        result: &dyn ToolOutput,
    ) -> codex_tools::AcceptedToolOutcome {
        let mut outcome = self
            .callback
            .as_ref()
            .map_or_else(Default::default, |callback| {
                callback.on_result_accepted(turn_id, call_id, result)
            });
        let handoff = (|| {
            if !result.success_for_logging() {
                return None;
            }
            let mut pending = self
                .profiles
                .reservation
                .lock()
                .expect("Agent reservation poisoned");
            let pending = pending.as_mut()?;
            if pending.turn != turn_id
                || pending.expected != self.source
                || pending.target != self.target
                || !pending.calls.contains(call_id)
            {
                return None;
            }
            pending.accepted = true;
            Some(codex_protocol::agent_profile::AgentProfileHandoff {
                source: self.source.clone(),
                target_profile_id: self.target.clone(),
                call_id: call_id.into(),
            })
        })();
        // This wrapper alone owns the transfer receipt; retain other accepted metadata.
        outcome.handoff = handoff;
        outcome
    }
    fn handle<'a>(&'a self, call: ToolCall<'call>) -> ToolExecutorFuture<'a>
    where
        ToolCall<'call>: 'a,
    {
        Box::pin(async move {
            if self.callback.is_none() {
                let arguments: Value =
                    serde_json::from_str(call.function_arguments()?).map_err(|_| {
                        FunctionCallError::RespondToModel("Invalid handoff arguments".into())
                    })?;
                if !arguments
                    .as_object()
                    .is_some_and(|arguments| arguments.is_empty())
                {
                    return Err(FunctionCallError::RespondToModel(
                        "Handoffs accept no arguments".into(),
                    ));
                }
            }
            {
                let mut pending = self
                    .profiles
                    .reservation
                    .lock()
                    .expect("Agent reservation poisoned");
                if pending
                    .as_ref()
                    .is_some_and(|previous| previous.turn != call.turn_id)
                {
                    *pending = None;
                }
                if let Some(previous) = pending.as_mut() {
                    if previous.turn != call.turn_id
                        || previous.expected != self.source
                        || previous.target != self.target
                    {
                        return Err(FunctionCallError::RespondToModel(
                            "Conflicting Agent handoffs in one tool batch".into(),
                        ));
                    }
                    previous.calls.insert(call.call_id.clone());
                } else {
                    *pending = Some(Reservation {
                        turn: call.turn_id.to_owned(),
                        expected: self.source.clone(),
                        target: self.target.clone(),
                        calls: HashSet::from([call.call_id.clone()]),
                        accepted: false,
                        restored: false,
                    });
                }
            }
            let mut reservation = ProvisionalHandoff {
                executor: self,
                turn: call.turn_id.to_owned(),
                call: call.call_id.clone(),
                keep: false,
            };
            let output = if let Some(callback) = &self.callback {
                callback.handle(call).await?
            } else {
                Box::new(crate::python_tools::PythonOutput(
                codex_protocol::dynamic_tools::DynamicToolResponse {
                    content_items: vec![codex_protocol::dynamic_tools::DynamicToolCallOutputContentItem::InputText { text: "Handoff reserved.".into() }],
                    success: true,
                },
                )) as Box<dyn ToolOutput>
            };
            reservation.keep = output.success_for_logging();
            Ok(output)
        })
    }
}

/// The ordinary Python factory owns exact callback admission and result fencing.
pub(crate) fn wrap_handoff_callback(
    thread: &ExtensionData,
    source: Option<&AgentProfileReceipt>,
    callback: Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>,
) -> Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>> {
    let Some(source) = source else {
        return callback;
    };
    let Some(profiles) = thread.get::<Arc<AgentProfiles>>() else {
        return callback;
    };
    let Some(profile) = profiles.profiles.get(&source.profile_id) else {
        return callback;
    };
    let callback_name = callback.tool_name().with_default_namespace();
    let Some((name, _)) = profile
        .declaration
        .handoff_callbacks
        .iter()
        .find(|(name, _)| {
            ToolName::new(None, name.to_string()).with_default_namespace() == callback_name
        })
    else {
        return callback;
    };
    let Some(target) = profile.declaration.handoffs.get(name) else {
        return callback;
    };
    Arc::new(HandoffExecutor {
        profiles: profiles.as_ref().clone(),
        source: source.clone(),
        name: name.clone(),
        target: target.clone(),
        spec: callback.spec(),
        callback: Some(callback),
    })
}

fn handoff_declaration(name: &str, target: &str) -> DynamicToolFunctionSpec {
    DynamicToolFunctionSpec {
        name: name.to_owned(),
        description: format!("Transfer this conversation to captured Agent {target}."),
        input_schema: json!({"type":"object","properties":{},"required":[],"additionalProperties":false}),
        defer_loading: false,
    }
}

/// Project only the handoffs admitted by this immutable producing Agent profile.
pub(crate) fn is_handoff_callback(
    thread: &ExtensionData,
    source: Option<&AgentProfileReceipt>,
    name: &ToolName,
) -> bool {
    source
        .and_then(|source| {
            thread.get::<Arc<AgentProfiles>>().and_then(|profiles| {
                profiles.profiles.get(&source.profile_id).map(|profile| {
                    profile
                        .declaration
                        .handoff_callbacks
                        .contains_key(&name.name)
                })
            })
        })
        .unwrap_or(false)
}

pub(crate) fn realtime_handoff_declarations(
    handoffs: &codex_extension_api::AgentProfileHandoffTools,
    thread: &ExtensionData,
    source: Option<&AgentProfileReceipt>,
) -> Result<Vec<codex_extension_api::RealtimeToolDeclaration>> {
    handoffs
        .targets
        .iter()
        .filter(|(name, _)| !is_handoff_callback(thread, source, name))
        .map(|(name, target)| {
            let raw = handoff_declaration(&name.name, target);
            let native = dynamic_tool_to_responses_api_tool(&raw)?;
            Ok(codex_extension_api::RealtimeToolDeclaration {
                native_name: name.clone(),
                spec: codex_extension_api::PreparedToolPresentation {
                    namespace: name.namespace.clone(),
                    name: native.name,
                    kind: "function".into(),
                    description: native.description,
                    parameters: raw.input_schema,
                    strict: native.strict,
                    defer_loading: None,
                },
            })
        })
        .collect()
}

impl ToolContributor for Contributor {
    fn tools(
        &self,
        _: &ExtensionData,
        _: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        // Handoff declarations require the exact producing native step.
        Vec::new()
    }

    fn tools_for_step(
        &self,
        _: &ExtensionData,
        thread: &ExtensionData,
        step: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        let Some(profiles) = thread.get::<Arc<AgentProfiles>>() else {
            return Vec::new();
        };
        let Some(source) = step.get::<CapturedAgentProfile>() else {
            return Vec::new();
        };
        let Some(profile) = profiles.profiles.get(&source.receipt.profile_id) else {
            return Vec::new();
        };
        profile
            .declaration
            .handoffs
            .iter()
            .filter(|(name, _)| !profile.declaration.handoff_callbacks.contains_key(*name))
            .filter_map(|(name, target)| {
                let declaration = handoff_declaration(name, target);
                let spec =
                    ToolSpec::Function(dynamic_tool_to_responses_api_tool(&declaration).ok()?);
                Some(Arc::new(HandoffExecutor {
                    profiles: profiles.as_ref().clone(),
                    source: source.receipt.clone(),
                    name: name.clone(),
                    target: target.clone(),
                    spec,
                    callback: None,
                })
                    as Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>)
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roster() -> Value {
        json!([
            {"id":"a","model":{"model":"","reasoning_effort":null,"reasoning_summary":null,
                "service_tier":null,"tool_choice":null,"personality":null},
                "instructions":"Root","tools":[{"type":"function","name":"lookup",
                    "description":"Lookup A","inputSchema":{"type":"object","properties":{},
                        "additionalProperties":false},"deferLoading":false}],
                "handoffs":{"transfer_to_b":"b"},"output_fingerprint":"output-a",
                "mcp_servers":{"remote":{"url":"https://first.invalid","headers":{"Authorization":"first"}}}},
            {"id":"b","model":{"model":"destination","reasoning_effort":null,"reasoning_summary":null,
                "service_tier":null,"tool_choice":null,"personality":null},
                "instructions":"Destination","tools":[],"output_fingerprint":"output-b"}
        ])
    }

    #[test]
    fn callback_handoff_uses_exact_catalog_schema_and_preserves_empty_fingerprints() {
        let original = roster();
        let mut empty = original.clone();
        empty[0]["handoff_callbacks"] = json!({});
        assert_eq!(
            fingerprint(&original.to_string(), "a").unwrap(),
            fingerprint(&empty.to_string(), "a").unwrap()
        );

        let mut callback = original;
        callback[0]["tools"].as_array_mut().unwrap().push(json!({
            "type":"function", "name":"transfer_to_b", "description":"Transfer with a reason",
            "inputSchema":{"type":"object","properties":{"reason":{"type":"string"}},"required":["reason"],"additionalProperties":false},
            "deferLoading":false,
        }));
        callback[0]["handoff_callbacks"] = json!({"transfer_to_b":1});
        assert!(fingerprint(&callback.to_string(), "a").is_ok());
        callback[0]["handoff_callbacks"] = json!({"transfer_to_b":0});
        assert!(fingerprint(&callback.to_string(), "a").is_err());
    }

    #[test]
    fn queued_operation_origin_is_distinct_from_admitted_and_restored_actor() {
        let raw = roster().to_string();
        let opening = OpeningProfiles {
            root_id: "a".into(),
            roster: serde_json::from_str(&raw).unwrap(),
            fingerprint: fingerprint(&raw, "a").unwrap(),
        };
        let envelope = |profile: &str, epoch: u64, text: &str| {
            let mut model = opening
                .roster
                .iter()
                .find(|item| item.id == profile)
                .unwrap()
                .model
                .clone();
            if model.model.is_empty() {
                model.model = "opening-model".into();
            }
            ResponseItemEnvelope {
                item: crate::application_instructions::publication(text),
                metadata: Some(codex_history::CodexHarnessMetadata {
                    sdk_agent_graph: Some(opening.fingerprint.clone()),
                    sdk_agent_profile: Some(
                        json!({"profile_id":profile,"epoch":epoch,"model":model}),
                    ),
                    client_authored: true,
                    ..Default::default()
                }),
            }
        };
        let a = envelope("a", 0, "Root");
        let b = envelope("b", 1, "Destination");
        let context = |source: Value| -> RolloutItem {
            serde_json::from_value(json!({"type":"turn_context","payload":{
                "turn_id":"queued-turn","sdk_receipt":{
                    "instruction_revision":b.item.id(),"operation_declaration_source":source
                },
                "cwd":std::env::current_dir().unwrap(),"approval_policy":"never",
                "sandbox_policy":codex_protocol::protocol::SandboxPolicy::new_read_only_policy(),
                "model":"destination","summary":"auto"
            }}))
            .unwrap()
        };
        let original = json!({"profile_id":"a","epoch":0});
        let mut history = vec![
            RolloutItem::ResponseItem(a),
            RolloutItem::ResponseItem(b.clone()),
            context(original.clone()),
        ];
        assert_eq!(restore_source(&opening, &history).unwrap().profile_id, "b");
        assert_eq!(
            operation_source(&opening, &history, "queued-turn")
                .unwrap()
                .profile_id,
            "a"
        );
        let mut compacted: codex_history::CompactedItem =
            serde_json::from_value(json!({"message":"summary"})).unwrap();
        compacted.replacement_history = Some(vec![b.clone()]);
        history.push(RolloutItem::Compacted(compacted));
        history.push(context(original));
        assert_eq!(
            operation_source(&opening, &history, "queued-turn")
                .unwrap()
                .profile_id,
            "a"
        );
        history.push(context(json!({"profile_id":"b","epoch":1})));
        assert!(operation_source(&opening, &history, "queued-turn").is_err());
        history.pop();
        history.push(context(json!({"profile_id":"a","epoch":0,"unknown":true})));
        assert!(operation_source(&opening, &history, "queued-turn").is_err());
        history.pop();
        assert!(operation_source(&opening, &history, "different-turn").is_err());
        if let RolloutItem::ResponseItem(source) = &mut history[0] {
            source.metadata.as_mut().unwrap().sdk_agent_graph = Some("different graph".into());
        }
        assert!(operation_source(&opening, &history, "queued-turn").is_err());
    }
    #[test]
    fn declaration_fingerprint_excludes_connection_credentials_but_keeps_server_identity() {
        let mut declaration = roster();
        let original = fingerprint(&declaration.to_string(), "a").unwrap();
        declaration[0]["mcp_servers"]["remote"] = json!({
            "url":"https://second.invalid","headers":{"Authorization":"second"}
        });
        assert_eq!(
            fingerprint(&declaration.to_string(), "a").unwrap(),
            original
        );
        declaration[0]["mcp_servers"] = json!({"different":{}});
        assert_ne!(
            fingerprint(&declaration.to_string(), "a").unwrap(),
            original
        );
    }

    #[test]
    fn declaration_fingerprint_pins_edges_schemas_output_and_result_policy() {
        let declaration = roster();
        let original = fingerprint(&declaration.to_string(), "a").unwrap();
        for (path, replacement) in [
            ("/0/handoffs/transfer_to_b", json!("a")),
            ("/0/tools/0/inputSchema/additionalProperties", json!(true)),
            ("/0/output_fingerprint", json!("changed retry policy")),
            ("/0/instructions", json!("Changed root")),
        ] {
            let mut changed = declaration.clone();
            *changed.pointer_mut(path).unwrap() = replacement;
            assert_ne!(
                fingerprint(&changed.to_string(), "a").unwrap(),
                original,
                "{path}"
            );
        }
        let mut changed = declaration;
        changed[0]["mcp_result_tools"] = json!({"remote":{"lookup":30.0}});
        assert_ne!(fingerprint(&changed.to_string(), "a").unwrap(), original);
    }

    #[test]
    fn persisted_receipt_accepts_initial_epoch_and_rejects_unknown_metadata() {
        let model = roster()[1]["model"].clone();
        let receipt = json!({"profile_id":"b","epoch":0,"model":model});
        assert_eq!(
            profile_receipt(&receipt).unwrap(),
            AgentProfileReceipt {
                profile_id: "b".into(),
                epoch: 0,
            }
        );
        let mut invalid = receipt;
        invalid["unrecognized_authority"] = json!(true);
        assert!(profile_receipt(&invalid).is_err());
    }
}
