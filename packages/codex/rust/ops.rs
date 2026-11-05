//! JSON adapters for native operations that contain Rust-only channels or values.
use crate::config::{decode, optional};
use anyhow::{Context, Result, bail, ensure};
use codex_core::context::{
    ContextualUserFragment, InternalContextSource, InternalModelContextFragment,
};
use codex_core_api::{AbsolutePathBuf, CodexThread, WithTurnExtensionData};
use codex_extension_api::ExtensionDataInit;
use codex_protocol::config_types::{
    ApprovalsReviewer, CollaborationMode, Personality, ReasoningSummary, WindowsSandboxLevel,
};
use codex_protocol::dynamic_tools::DynamicToolResponse;
use codex_protocol::mcp::RequestId;
use codex_protocol::mcp_approval_meta::{PERSIST_ALWAYS, PERSIST_KEY, PERSIST_SESSION};
use codex_protocol::models::{
    ActivePermissionProfile, PermissionProfile, PermissionProfileSnapshot, ProfileWorkspaceRoot,
    ResponseItem,
};
use codex_protocol::openai_models::ReasoningEffort;
use codex_protocol::protocol::*;
use codex_protocol::request_permissions::RequestPermissionsResponse;
use codex_protocol::request_user_input::RequestUserInputResponse;
use codex_protocol::sandbox::SandboxType;
use codex_protocol::turn_input::*;
use codex_protocol::user_input::UserInput;
use codex_utils_path_uri::PathUri;
use serde::{Deserialize, Deserializer};
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap};
use tokio::sync::oneshot;

const PERSISTENT_APPROVAL_ERROR: &str =
    "persistent approvals write host configuration; use a one-time or session approval";
// Core's legacy approval form identifiers and choices are not public constants.
const MCP_APPROVAL_PREFIX: &str = "mcp_tool_call_approval_";

fn check_approval(decision: &ReviewDecision) -> Result<()> {
    anyhow::ensure!(
        !matches!(
            decision,
            ReviewDecision::ApprovedExecpolicyAmendment { .. }
                | ReviewDecision::ApprovedMcpPolicyAmendment
                | ReviewDecision::NetworkPolicyAmendment { .. }
        ),
        PERSISTENT_APPROVAL_ERROR
    );
    Ok(())
}

fn mcp_answer_persists<'a>(answers: impl Iterator<Item = &'a str>) -> bool {
    let mut persistent = false;
    for answer in answers {
        // Match Core's precedence if a response supplies multiple choices.
        if answer == "Allow for this session" {
            return false;
        }
        persistent |= answer == "Allow and don't ask me again";
    }
    persistent
}

pub const OPERATIONS: &[&str] = &[
    "interrupt",
    "interrupt_if_no_pending_input",
    "clean_background_terminals",
    "realtime_conversation_start",
    "realtime_conversation_audio",
    "realtime_conversation_text",
    "realtime_conversation_speech",
    "realtime_conversation_close",
    "realtime_conversation_list_voices",
    "turn_input",
    "recover_turn",
    "suspend_turn_and_shutdown",
    "thread_settings",
    "turn_settings",
    "inter_agent_communication",
    "exec_approval",
    "patch_approval",
    "resolve_elicitation",
    "cancel_url_completion",
    "user_input_answer",
    "request_permissions_response",
    "dynamic_tool_response",
    "refresh_mcp_servers",
    "reload_user_config",
    "compact",
    "set_thread_memory_mode",
    "review",
    "approve_guardian_denied_action",
    "shutdown",
    "run_user_shell_command",
    "start_turn",
    "recover_turn_if_idle",
    "token_usage_info",
    "load_history",
    "read_thread",
    "list_subagents",
    "agent_status",
    "thread_settings_snapshot",
    "list_background_terminals",
    "terminate_background_terminal",
    "inject_response_items",
    "inject_if_running",
    "flush_rollout",
    "checkpoint_preparation",
    "ensure_rollout_materialized",
    "checkpoint_thread_settings",
    "prewarm",
    "prewarm_with_history",
    "wait_until_terminated",
    "set_app_server_client_info",
    "call_mcp_tool",
    "read_mcp_resource",
    "read_mcp_resource_for_call",
    "environment_ready",
    "environment_failed",
    "connect_environment",
    "environment_attached",
    "increment_out_of_band_elicitation_count",
    "decrement_out_of_band_elicitation_count",
    "update_thread_metadata",
    "append_rollout_items",
    "current_turn_model",
    "active_turn_root",
    "approvals_reviewer_for_turn",
    "rollout_path",
    "instruction_sources",
    "queued_event_count",
    "record_user_goal_update",
];

macro_rules! params {
    ($name:ident { $($(#[$attr:meta])* $field:ident: $ty:ty),* $(,)? }) => {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct $name { $($(#[$attr])* $field: $ty),* }
    };
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EnvironmentSelection {
    pub environment_id: String,
    pub cwd: PathUri,
    #[serde(default)]
    pub workspace_roots: Vec<PathUri>,
    #[serde(default)]
    pub config: EnvironmentState,
}

/// Decode complete native environment selections for thread and turn startup.
pub fn environments(value: Value) -> Result<Vec<TurnEnvironmentSelection>> {
    decode::<Vec<EnvironmentSelection>>(value)?
        .into_iter()
        .map(TryInto::try_into)
        .collect()
}

#[derive(Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EnvironmentState {
    #[default]
    FromThread,
    Pending,
    Ready(EnvironmentOptions),
    Failed(String),
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EnvironmentOptions {
    allow_login_shell: bool,
    workspace_roots: Vec<PathUri>,
    permission_profile: PermissionSnapshot,
    // Reuse native config parsing for wildcard patterns and filter precedence.
    shell_environment_policy: codex_config::types::ShellEnvironmentPolicyToml,
    windows_sandbox_level: WindowsSandboxLevel,
    windows_sandbox_type: SandboxType,
    use_legacy_landlock: bool,
    exec_policy: Option<codex_config::RequirementsExecPolicyToml>,
    mcp_policy: Option<McpPolicy>,
    network_policy: Option<codex_core_api::EnvironmentNetworkPolicy>,
    #[serde(default)]
    selected_capability_roots: Vec<codex_protocol::capabilities::SelectedCapabilityRoot>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PermissionSnapshot {
    permission_profile: PermissionProfile,
    active_permission_profile: Option<ActivePermissionProfile>,
    #[serde(default)]
    profile_workspace_roots: Vec<PathUri>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct McpPolicy {
    servers: Option<BTreeMap<String, codex_protocol::mcp_policy::McpServerRequirement>>,
    plugins: Option<BTreeMap<String, codex_protocol::mcp_policy::PluginMcpRequirements>>,
}

impl TryFrom<EnvironmentOptions> for EnvironmentConfig {
    type Error = anyhow::Error;
    fn try_from(value: EnvironmentOptions) -> Result<Self> {
        let p = value.permission_profile;
        let permission_profile = match p.active_permission_profile {
            Some(active) => PermissionProfileSnapshot::active_with_profile_workspace_roots(
                p.permission_profile,
                active,
                p.profile_workspace_roots
                    .into_iter()
                    .map(Into::into)
                    .collect(),
            ),
            None if p.profile_workspace_roots.is_empty() => {
                PermissionProfileSnapshot::legacy(p.permission_profile)
            }
            None => bail!("profile_workspace_roots require an active_permission_profile"),
        };
        Ok(Self {
            allow_login_shell: value.allow_login_shell,
            workspace_roots: value.workspace_roots,
            permission_profile,
            shell_environment_policy: value.shell_environment_policy.into(),
            windows_sandbox_level: value.windows_sandbox_level,
            windows_sandbox_type: value.windows_sandbox_type,
            use_legacy_landlock: value.use_legacy_landlock,
            exec_policy: value
                .exec_policy
                .map(|p| {
                    p.to_policy()
                        .map(codex_execpolicy::RequirementsExecPolicy::new)
                })
                .transpose()?,
            mcp_policy: value
                .mcp_policy
                .map(|p| codex_core_api::EnvironmentMcpPolicy {
                    servers: p.servers,
                    plugins: p.plugins,
                }),
            network_policy: value.network_policy,
            selected_capability_roots: value.selected_capability_roots,
        })
    }
}

impl TryFrom<EnvironmentSelection> for TurnEnvironmentSelection {
    type Error = anyhow::Error;
    fn try_from(value: EnvironmentSelection) -> Result<Self> {
        let selection = Self {
            environment_id: value.environment_id,
            cwd: value.cwd,
            workspace_roots: value.workspace_roots,
            config: match value.config {
                EnvironmentState::FromThread => EnvironmentConfigState::FromThread,
                EnvironmentState::Pending => EnvironmentConfigState::Pending,
                EnvironmentState::Ready(config) => {
                    EnvironmentConfigState::Ready(config.try_into()?)
                }
                EnvironmentState::Failed(error) => EnvironmentConfigState::Failed(error),
            },
        };
        Ok(selection)
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Environments {
    legacy_fallback_cwd: AbsolutePathBuf,
    environments: Vec<EnvironmentSelection>,
}

fn deserialize_environments<'de, D: Deserializer<'de>>(
    d: D,
) -> std::result::Result<Option<TurnEnvironmentSelections>, D::Error> {
    Option::<Environments>::deserialize(d)?
        .map(|v| {
            Ok::<_, anyhow::Error>(TurnEnvironmentSelections {
                legacy_fallback_cwd: v.legacy_fallback_cwd,
                environments: v
                    .environments
                    .into_iter()
                    .map(TryInto::try_into)
                    .collect::<Result<_>>()?,
            })
        })
        .transpose()
        .map_err(serde::de::Error::custom)
}

fn environment_list<'de, D: Deserializer<'de>>(
    d: D,
) -> std::result::Result<Option<Vec<TurnEnvironmentSelection>>, D::Error> {
    Option::<Vec<EnvironmentSelection>>::deserialize(d)?
        .map(|v| v.into_iter().map(TryInto::try_into).collect::<Result<_>>())
        .transpose()
        .map_err(serde::de::Error::custom)
}

fn profile_roots<'de, D: Deserializer<'de>>(
    d: D,
) -> std::result::Result<Option<Vec<ProfileWorkspaceRoot>>, D::Error> {
    Ok(Option::<Vec<PathUri>>::deserialize(d)?
        .map(|roots| roots.into_iter().map(Into::into).collect()))
}

#[derive(Default, Deserialize)]
#[serde(remote = "ThreadSettingsOverrides", default, deny_unknown_fields)]
pub struct ThreadSettings {
    #[serde(deserialize_with = "deserialize_environments")]
    environments: Option<TurnEnvironmentSelections>,
    runtime_workspace_roots: Option<Vec<AbsolutePathBuf>>,
    #[serde(deserialize_with = "profile_roots")]
    profile_workspace_roots: Option<Vec<ProfileWorkspaceRoot>>,
    approval_policy: Option<AskForApproval>,
    approvals_reviewer: Option<ApprovalsReviewer>,
    sandbox_policy: Option<SandboxPolicy>,
    permission_profile: Option<PermissionProfile>,
    active_permission_profile: Option<ActivePermissionProfile>,
    windows_sandbox_level: Option<WindowsSandboxLevel>,
    model: Option<String>,
    #[serde(deserialize_with = "optional")]
    effort: Option<Option<ReasoningEffort>>,
    summary: Option<ReasoningSummary>,
    #[serde(deserialize_with = "optional")]
    service_tier: Option<Option<String>>,
    collaboration_mode: Option<CollaborationMode>,
    personality: Option<Personality>,
    disabled_plugin_ids: Option<Vec<String>>,
}

/// Restrict the managed Session API to model settings, before any native mutation.
pub fn model_settings(params: Value) -> Result<ThreadSettingsOverrides> {
    let fields = params
        .as_object()
        .context("model settings must be an object")?;
    for (key, value) in fields {
        match key.as_str() {
            "model" | "effort" | "summary" | "service_tier" => {
                if key == "service_tier" && value.is_null() {
                    continue;
                }
                let text = value.as_str().context("model settings must be strings")?;
                anyhow::ensure!(!text.trim().is_empty(), "model settings must not be empty");
            }
            _ => bail!("unsupported model setting: {key}"),
        }
    }
    let settings = ThreadSettings::deserialize(params)?;
    anyhow::ensure!(
        settings.effort != Some(Some(ReasoningEffort::Persistent)),
        "persistent reasoning is unsupported for replaceable sessions"
    );
    Ok(settings)
}

#[derive(Default, Deserialize)]
#[serde(remote = "TurnStartOptions", default, deny_unknown_fields)]
pub struct StartOptions {
    turn_trigger: Option<String>,
    final_output_json_schema: Option<Value>,
    service_tier: Option<String>,
    parent_turn_id: Option<String>,
    root_turn_id: Option<String>,
    cyber_access_program: Option<CyberAccessProgram>,
}

#[derive(Default, Deserialize)]
#[serde(remote = "TurnSettingsUpdate", default, deny_unknown_fields)]
struct TurnSettings {
    approvals_reviewer: Option<ApprovalsReviewer>,
    #[serde(deserialize_with = "environment_list")]
    environments: Option<Vec<TurnEnvironmentSelection>>,
    model: Option<String>,
    #[serde(deserialize_with = "optional")]
    effort: Option<Option<ReasoningEffort>>,
    summary: Option<ReasoningSummary>,
    #[serde(deserialize_with = "optional")]
    service_tier: Option<Option<String>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum ContextKind {
    Untrusted,
    Application,
}

params!(ContextEntry {
    value: String,
    kind: ContextKind
});

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum Mode {
    StartIfIdle,
    StartOrSteer,
    Steer,
    ContinueIfIdle,
}
impl Default for Mode {
    fn default() -> Self {
        Self::StartIfIdle
    }
}

params!(TurnParams {
    input: Value,
    observation_submission_id: Option<String>,
    client_id: Option<String>,
    #[serde(default, alias = "settings", with = "ThreadSettings")]
    thread_settings: ThreadSettingsOverrides,
    #[serde(default, with = "StartOptions")]
    start: TurnStartOptions,
    #[serde(default)]
    additional_context: BTreeMap<String, ContextEntry>,
    responsesapi_client_metadata: Option<HashMap<String, String>>,
    trace: Option<W3cTraceContext>,
    #[serde(default)] mode: Mode,
    expected_turn_id: Option<String>,
    expected_previous_turn_id: Option<String>,
    #[serde(default)] exact_steering: bool,
    writable_roots: Option<Vec<PathUri>>,
});

fn rejection(reason: NotSubmittedReason) -> Value {
    let reason = match reason {
        NotSubmittedReason::Superseded => json!({"type": "superseded"}),
        NotSubmittedReason::ServerDraining => json!({"type": "server_draining"}),
        NotSubmittedReason::NotIdle => json!({"type": "not_idle"}),
        NotSubmittedReason::PendingTriggerTurn => json!({"type": "pending_trigger_turn"}),
        NotSubmittedReason::PlanMode => json!({"type": "plan_mode"}),
        NotSubmittedReason::NoActiveTurn => json!({"type": "no_active_turn"}),
        NotSubmittedReason::ExpectedTurnMismatch { expected, actual } => {
            json!({"type": "expected_turn_mismatch", "expected": expected, "actual": actual})
        }
        NotSubmittedReason::ActiveTurnNotSteerable { turn_kind } => {
            json!({"type": "active_turn_not_steerable", "turn_kind": turn_kind})
        }
        NotSubmittedReason::ActiveTurnOutputSchemaMismatch => {
            json!({"type": "active_turn_output_schema_mismatch"})
        }
        NotSubmittedReason::EmptyInput => json!({"type": "empty_input"}),
    };
    json!({"status": "not_submitted", "reason": reason})
}

fn submission(result: TurnInputSubmission) -> Value {
    match result {
        TurnInputSubmission::Started { turn_id } => {
            json!({"status": "started", "turn_id": turn_id})
        }
        TurnInputSubmission::Steered { turn_id } => {
            json!({"status": "steered", "turn_id": turn_id})
        }
        TurnInputSubmission::NotSubmitted { reason } => rejection(reason),
    }
}

fn idle_submission(result: StartIfIdleSubmission) -> Value {
    match result {
        StartIfIdleSubmission::Started { turn_id } => {
            json!({"status": "started", "turn_id": turn_id})
        }
        StartIfIdleSubmission::NotSubmitted { reason } => rejection(reason),
    }
}

async fn start_turn(thread: &CodexThread, value: Value) -> Result<Value> {
    let p: TurnParams = decode(value)?;
    start_with_params(thread, p, None, None).await
}

pub async fn submit_replaceable(state: &crate::engine::ThreadState, value: Value) -> Result<Value> {
    let mut mutation = Some(state.mutations.lock().await);
    let thread = &state.thread;
    let mut p: TurnParams = decode(value)?;
    anyhow::ensure!(
        p.input.is_array()
            && (!matches!(p.mode, Mode::ContinueIfIdle)
                || p.input.as_array().is_some_and(Vec::is_empty)
                    && p.client_id.is_none()
                    && p.additional_context.is_empty())
            && p.start.parent_turn_id.is_none()
            && p.start.root_turn_id.is_none()
            && p.thread_settings.effort != Some(Some(ReasoningEffort::Persistent))
            && !p
                .thread_settings
                .collaboration_mode
                .as_ref()
                .is_some_and(|mode| mode.reasoning_effort() == Some(ReasoningEffort::Persistent)),
        "replaceable tools support ordinary submit/steer only, without persistent execution"
    );
    let writes = if let Some(roots) = p.writable_roots.take() {
        ensure!(
            matches!(p.mode, Mode::StartIfIdle) && p.thread_settings.environments.is_none(),
            "Writable roots require a new idle native turn"
        );
        match prepare_write_selection(state, roots).await {
            Ok((replacement, writes)) => {
                p.thread_settings.environments = Some(replacement);
                Some(writes)
            }
            Err(error) => {
                return Ok(json!({"status": "not_submitted", "reason": {
                    "type": "writable_roots_refused", "message": error.to_string()
                }}));
            }
        }
    } else {
        None
    };
    let result = start_with_params(thread, p, None, Some((state, &mut mutation))).await?;
    if let Some(writes) = writes
        && result["status"] == "started"
    {
        thread.thread_extension_data().insert(writes);
    }
    Ok(result)
}

async fn prepare_write_selection(
    state: &crate::engine::ThreadState,
    roots: Vec<PathUri>,
) -> Result<(
    TurnEnvironmentSelections,
    crate::environment::StrictExecutorWrites,
)> {
    let thread = &state.thread;
    let writes = thread
        .thread_extension_data()
        .get::<crate::environment::StrictExecutorWrites>()
        .context("Writable roots require a strict Session Environment")?;
    let current = thread.environment_selections().await;
    let [selection] = current.as_slice() else {
        bail!("Writable roots require one selected native Environment");
    };
    let config = thread.config().await;
    let (replacement, writes) = crate::environment::replace_writable_roots(
        selection,
        &config,
        &state.engine.manager.environment_manager(),
        &writes,
        roots,
        state.engine.local_private_paths(),
    )
    .await?;
    if selection.environment_id == codex_exec_server::LOCAL_ENVIRONMENT_ID
        && let Some(store) = &state.engine.local_checklist_store
    {
        writes.check_local_rollout_path(&store.live_rollout_path(state.id).await?)?;
    }
    if selection != &replacement {
        ensure!(
            thread.active_turn_environment_selections().await.is_none()
                && thread.list_background_terminals().await.is_empty()
                && !thread
                    .thread_extension_data()
                    .get::<crate::native_interception::Control>()
                    .is_some_and(|control| control.has_unsettled_code_cells()),
            "Cannot change writable roots while old executor work is still active"
        );
    }
    Ok((
        TurnEnvironmentSelections::new(
            thread.config_snapshot().await.cwd().clone(),
            vec![replacement],
        ),
        writes,
    ))
}

// The SDK's root input and stop paths share this native admission gate. Once
// it is held, no SDK start can replace the checked owner before the interrupt.
// A stale stop does not send an unqualified Op to a newer turn.
pub(crate) async fn interrupt_managed_root(
    state: &crate::engine::ThreadState,
    expected_turn_id: &str,
    queued_compaction: bool,
) -> Result<()> {
    let _mutation = state.mutations.lock().await;
    let tools = state
        .thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>()
        .context("Session interruption requires replaceable tools")?;
    // compact() is the sole producer of this trusted flag: Python retains
    // its active slot from submission until this stop joins Core. The queued
    // Op may not have installed its receipt yet. Public Turn stops still
    // require the current native admission before sending any raw Op.
    if !queued_compaction && tools.admission_ready(expected_turn_id).is_err() {
        return Ok(());
    }
    tools
        .interrupt_session(&state.thread, expected_turn_id)
        .await
}

params!(RecoveryOptions {
    observation_submission_id: Option<String>,
    #[serde(default)]
    output_schema: Option<serde_json::Map<String, Value>>,
});

pub async fn recover_prepared(
    state: &crate::engine::ThreadState,
    turn_id: String,
    original_call_ids: Vec<String>,
    value: Value,
) -> Result<Value> {
    let p: RecoveryOptions = decode(value)?;
    let thread = &state.thread;
    let mut init = thread.config_snapshot().await.turn_extension_init;
    if let Some(submission) = p.observation_submission_id {
        state
            .request_metadata
            .seed(&mut init, submission, state.id.to_string());
    }
    crate::recovery_guard::Seed::attach(&mut init, &turn_id, original_call_ids);
    let seed = crate::turn_settlement::Seed::attach(&mut init);
    let submission = thread
        .recover_turn_if_idle(WithTurnExtensionData::new(
            RecoverTurnRequest {
                turn_id,
                thread_settings: Default::default(),
                trace: None,
                cyber_access_program: None,
                final_output_json_schema: p.output_schema.map(Value::Object),
            },
            init,
        ))
        .await?;
    seed.confirm(&submission);
    Ok(idle_submission(submission))
}

async fn start_with_params<'a>(
    thread: &CodexThread,
    p: TurnParams,
    turn_init: Option<ExtensionDataInit>,
    managed: Option<(
        &'a crate::engine::ThreadState,
        &mut Option<tokio::sync::MutexGuard<'a, ()>>,
    )>,
) -> Result<Value> {
    ensure!(
        !p.exact_steering || (managed.is_some() && matches!(p.mode, Mode::Steer)),
        "exact steering requires a managed steer"
    );
    ensure!(
        p.writable_roots.is_none(),
        "Writable roots require a managed Session"
    );
    let turn_init = if let (Some((state, _)), Some(submission)) =
        (managed.as_ref(), p.observation_submission_id)
    {
        let mut init = match turn_init {
            Some(init) => init,
            None => thread.config_snapshot().await.turn_extension_init,
        };
        state
            .request_metadata
            .seed(&mut init, submission, state.id.to_string());
        Some(init)
    } else {
        turn_init
    };
    let input = if p.input.is_array() {
        TurnInput::UserInput {
            content: decode::<Vec<UserInput>>(p.input)?,
            client_id: p.client_id,
        }
    } else {
        if p.client_id.is_some() {
            bail!("client_id must be inside native TurnInput when input is not a list");
        }
        decode::<TurnInput>(p.input)?
    };
    let mut request = TurnInputRequest {
        input,
        thread_settings: p.thread_settings,
        start: p.start,
        additional_context: p
            .additional_context
            .into_iter()
            .map(|(key, entry)| {
                (
                    key,
                    AdditionalContextEntry {
                        value: entry.value,
                        kind: match entry.kind {
                            ContextKind::Untrusted => AdditionalContextKind::Untrusted,
                            ContextKind::Application => AdditionalContextKind::Application,
                        },
                    },
                )
            })
            .collect(),
        responsesapi_client_metadata: p.responsesapi_client_metadata,
        trace: p.trace,
    };
    match p.mode {
        Mode::StartIfIdle | Mode::StartOrSteer
            if p.expected_turn_id.is_some() || p.expected_previous_turn_id.is_some() =>
        {
            bail!("expected turn IDs require steer or continue_if_idle mode")
        }
        Mode::StartIfIdle => {
            if let Some((state, mutation)) = managed {
                if matches!(request.input, TurnInput::UserInput { .. }) {
                    let tools = thread
                        .thread_extension_data()
                        .get::<crate::python_tools::PythonTools>()
                        .context("Managed submission lost its tool owner")?;
                    tools.resume_graph(&state.engine.manager).await?;
                }
                let init = match turn_init {
                    Some(init) => init,
                    None => thread.config_snapshot().await.turn_extension_init,
                };
                return Ok(idle_submission(
                    crate::turn_settlement::start(state, mutation, request, init, None).await?,
                ));
            }
            let request = match turn_init {
                Some(init) => WithTurnExtensionData::new(request, init),
                None => request.into(),
            };
            Ok(idle_submission(thread.start_turn_if_idle(request).await?))
        }
        Mode::StartOrSteer => Ok(submission(thread.start_or_steer_turn(request).await?)),
        Mode::ContinueIfIdle => {
            if p.expected_turn_id.is_some() {
                bail!("expected_turn_id is only valid in steer mode");
            }
            let previous = p
                .expected_previous_turn_id
                .context("continue_if_idle requires expected_previous_turn_id")?;
            if let Some((state, mutation)) = managed {
                request.input = TurnInput::ResponseItem(ContextualUserFragment::into(
                    InternalModelContextFragment::new(
                        InternalContextSource::from_static("agents_steering"),
                        "Continue with the latest user input already in the conversation. Do not repeat completed work.",
                    ),
                ));
                let init = match turn_init {
                    Some(init) => init,
                    None => thread.config_snapshot().await.turn_extension_init,
                };
                return Ok(idle_submission(
                    crate::turn_settlement::start(state, mutation, request, init, Some(&previous))
                        .await?,
                ));
            }
            Ok(submission(
                thread.continue_turn_if_idle(request, previous).await?,
            ))
        }
        Mode::Steer => {
            if p.expected_previous_turn_id.is_some() {
                bail!("expected_previous_turn_id is only valid in continue_if_idle mode");
            }
            let expected = p
                .expected_turn_id
                .context("steer requires expected_turn_id")?;
            let mut input_id = None;
            if managed.is_some()
                && let TurnInput::UserInput { client_id, .. } = &mut request.input
            {
                let receipt = thread
                    .thread_extension_data()
                    .get::<crate::python_tools::PythonTools>()
                    .context("Managed steering lost its tool owner")?
                    .admission_ready(&expected)?;
                let id = receipt.root_steering_client_id(p.exact_steering)?;
                if p.exact_steering {
                    input_id = Some(id.clone());
                }
                *client_id = Some(id);
            }
            Ok(match thread.steer_turn(request, expected).await? {
                SteerSubmission::Steered { turn_id } => match input_id {
                    Some(input_id) => {
                        json!({"status": "steered", "turn_id": turn_id, "input_id": input_id})
                    }
                    None => json!({"status": "steered", "turn_id": turn_id}),
                },
                SteerSubmission::NotSubmitted { reason } => rejection(reason),
            })
        }
    }
}

params!(Empty {});
params!(TurnId { turn_id: String });
params!(ExecApprovalParams { id: String, turn_id: Option<String>, decision: ReviewDecision });
params!(PatchApprovalParams {
    id: String,
    decision: ReviewDecision
});
params!(ElicitationParams { server_name: String, request_id: RequestId, decision: ElicitationAction, content: Option<Value>, meta: Option<Value> });
params!(AnswerParams {
    id: String,
    response: RequestUserInputResponse
});
params!(PermissionsParams {
    id: String,
    response: RequestPermissionsResponse
});
params!(DynamicParams {
    id: String,
    response: DynamicToolResponse
});
params!(ReviewParams {
    review_request: ReviewRequest
});
params!(GuardianParams {
    event: GuardianAssessmentEvent
});
params!(ShellParams { command: String, timeout_ms: Option<u64> });
params!(MemoryParams {
    mode: ThreadMemoryMode
});
params!(SettingsParams {
    #[serde(alias = "settings", with = "ThreadSettings")]
    thread_settings: ThreadSettingsOverrides
});
params!(TurnSettingsParams {
    turn_id: String,
    #[serde(with = "TurnSettings")]
    update: TurnSettingsUpdate
});
params!(RecoverParams {
    #[serde(default, alias = "settings", with = "ThreadSettings")]
    thread_settings: ThreadSettingsOverrides,
    #[serde(default, with = "StartOptions")]
    start_options: TurnStartOptions
});
params!(RecoverIdleParams { turn_id: String, #[serde(default, alias = "settings", with = "ThreadSettings")] thread_settings: ThreadSettingsOverrides, trace: Option<W3cTraceContext>, cyber_access_program: Option<CyberAccessProgram>, final_output_json_schema: Option<Value> });
params!(CommunicationParams {
    communication: InterAgentCommunication,
    #[serde(default, with = "StartOptions")]
    start_options: TurnStartOptions
});
params!(HistoryParams {
    #[serde(default)]
    include_archived: bool
});
params!(ReadParams {
    #[serde(default)]
    include_archived: bool,
    #[serde(default)]
    include_history: bool
});
params!(TerminalParams { process_id: i32 });
params!(ItemsParams { items: Vec<ResponseItem> });
params!(ClientInfoParams { app_server_client_name: Option<String>, app_server_client_version: Option<String>, #[serde(default)] mcp_elicitations_auto_deny: bool });
params!(McpToolParams { server: String, tool: String, arguments: Option<Value>, meta: Option<Value> });
params!(McpReadParams {
    server: String,
    params: rmcp::model::ReadResourceRequestParams
});
params!(McpCallReadParams {
    call_id: String,
    uri: String
});
params!(EnvironmentReadyParams {
    selection: EnvironmentSelection,
    config: EnvironmentOptions
});
params!(EnvironmentFailedParams {
    selection: EnvironmentSelection,
    error: String
});
params!(MetadataParams {
    patch: codex_thread_store::ThreadMetadataPatch,
    #[serde(default)]
    include_archived: bool
});
params!(RolloutParams { items: Vec<codex_history::RolloutItem> });

pub(crate) fn question_response(operation: &str, params: Value) -> anyhow::Result<Op> {
    Ok(match operation {
        "exec_approval" => {
            let p: ExecApprovalParams = decode(params)?;
            check_approval(&p.decision)?;
            Op::ExecApproval {
                id: p.id,
                turn_id: p.turn_id,
                decision: p.decision,
            }
        }
        "patch_approval" => {
            let p: PatchApprovalParams = decode(params)?;
            check_approval(&p.decision)?;
            Op::PatchApproval {
                id: p.id,
                decision: p.decision,
            }
        }
        "resolve_elicitation" => {
            let p: ElicitationParams = decode(params)?;
            ensure!(
                p.meta.is_none(),
                "Owned questions cannot persist MCP approval"
            );
            Op::ResolveElicitation {
                server_name: p.server_name,
                request_id: p.request_id,
                decision: p.decision,
                content: p.content,
                meta: None,
            }
        }
        _ => anyhow::bail!("Unsupported owned question response"),
    })
}

pub async fn call(thread: &CodexThread, operation: &str, params: Value) -> Result<Value> {
    if !OPERATIONS.contains(&operation) {
        bail!("unknown native operation: {operation}");
    }
    let op = match operation {
        "start_turn" | "turn_input" => return start_turn(thread, params).await,
        "interrupt_if_no_pending_input" => {
            let p: TurnId = decode(params)?;
            let (reply, result) = oneshot::channel();
            thread
                .submit(Op::InterruptIfNoPendingInput {
                    turn_id: p.turn_id,
                    reply,
                })
                .await?;
            return Ok(json!(result.await?));
        }
        "recover_turn" => {
            let p: RecoverParams = decode(params)?;
            let (reply, result) = oneshot::channel();
            thread
                .submit(Op::RecoverTurn {
                    thread_settings: p.thread_settings,
                    start_options: p.start_options,
                    reply,
                })
                .await?;
            return Ok(submission(result.await??));
        }
        "recover_turn_if_idle" => {
            let p: RecoverIdleParams = decode(params)?;
            return Ok(idle_submission(
                thread
                    .recover_turn_if_idle(RecoverTurnRequest {
                        turn_id: p.turn_id,
                        thread_settings: p.thread_settings,
                        trace: p.trace,
                        cyber_access_program: p.cyber_access_program,
                        final_output_json_schema: p.final_output_json_schema,
                    })
                    .await?,
            ));
        }
        "suspend_turn_and_shutdown" => {
            decode::<Empty>(params)?;
            return Ok(match thread.suspend_turn_and_shutdown().await? {
                SuspendTurnOutcome::Suspended { turn_id } => {
                    json!({"status": "suspended", "turn_id": turn_id})
                }
                SuspendTurnOutcome::NotActive => json!({"status": "not_active"}),
                SuspendTurnOutcome::HasLiveDescendants => json!({"status": "has_live_descendants"}),
                SuspendTurnOutcome::UnsupportedTask => json!({"status": "unsupported_task"}),
            });
        }
        "thread_settings" => {
            let p: SettingsParams = decode(params)?;
            thread.update_thread_settings(p.thread_settings).await?;
            return Ok(Value::Null);
        }
        "turn_settings" => {
            let p: TurnSettingsParams = decode(params)?;
            let (reply, result) = oneshot::channel();
            thread
                .submit(Op::TurnSettings {
                    turn_id: p.turn_id,
                    update: p.update,
                    reply,
                })
                .await?;
            return Ok(match result.await? {
                TurnSettingsUpdateOutcome::Applied => json!({"status": "applied"}),
                TurnSettingsUpdateOutcome::TargetUnavailable => {
                    json!({"status": "target_unavailable"})
                }
                TurnSettingsUpdateOutcome::Rejected { reason } => {
                    json!({"status": "rejected", "reason": reason})
                }
            });
        }
        "inter_agent_communication" => {
            let p: CommunicationParams = decode(params)?;
            Op::InterAgentCommunication {
                communication: p.communication,
                start_options: p.start_options,
            }
        }
        "exec_approval" => {
            let p: ExecApprovalParams = decode(params)?;
            check_approval(&p.decision)?;
            Op::ExecApproval {
                id: p.id,
                turn_id: p.turn_id,
                decision: p.decision,
            }
        }
        "patch_approval" => {
            let p: PatchApprovalParams = decode(params)?;
            check_approval(&p.decision)?;
            Op::PatchApproval {
                id: p.id,
                decision: p.decision,
            }
        }
        "resolve_elicitation" | "cancel_url_completion" => {
            let p: ElicitationParams = decode(params)?;
            if operation == "cancel_url_completion" {
                anyhow::ensure!(
                    p.decision == ElicitationAction::Cancel
                        && p.content.is_none()
                        && p.meta.is_none(),
                    "URL cancellation requires a negative decision"
                );
            }
            if p.decision == ElicitationAction::Accept
                && let RequestId::String(id) = &p.request_id
                && id.starts_with(MCP_APPROVAL_PREFIX)
            {
                let persist = p
                    .meta
                    .as_ref()
                    .and_then(|meta| meta.get(PERSIST_KEY))
                    .and_then(Value::as_str);
                let answer = p.content.as_ref().and_then(|content| content.get(id));
                let persistent = match persist {
                    Some(PERSIST_ALWAYS) => true,
                    Some(PERSIST_SESSION) => false,
                    _ => match answer {
                        Some(Value::String(answer)) => {
                            mcp_answer_persists(std::iter::once(answer.as_str()))
                        }
                        Some(Value::Array(answers)) => {
                            mcp_answer_persists(answers.iter().filter_map(Value::as_str))
                        }
                        _ => false,
                    },
                };
                anyhow::ensure!(!persistent, PERSISTENT_APPROVAL_ERROR);
            }
            Op::ResolveElicitation {
                server_name: p.server_name,
                request_id: p.request_id,
                decision: p.decision,
                content: p.content,
                meta: p.meta,
            }
        }
        "user_input_answer" => {
            let p: AnswerParams = decode(params)?;
            anyhow::ensure!(
                !p.response.answers.iter().any(|(id, answer)| {
                    id.starts_with(MCP_APPROVAL_PREFIX)
                        && mcp_answer_persists(answer.answers.iter().map(String::as_str))
                }),
                PERSISTENT_APPROVAL_ERROR
            );
            Op::UserInputAnswer {
                id: p.id,
                response: p.response,
            }
        }
        "request_permissions_response" => {
            let p: PermissionsParams = decode(params)?;
            Op::RequestPermissionsResponse {
                id: p.id,
                response: p.response,
            }
        }
        "dynamic_tool_response" => {
            let p: DynamicParams = decode(params)?;
            Op::DynamicToolResponse {
                id: p.id,
                response: p.response,
            }
        }
        "review" => {
            let p: ReviewParams = decode(params)?;
            Op::Review {
                review_request: p.review_request,
            }
        }
        "approve_guardian_denied_action" => {
            let p: GuardianParams = decode(params)?;
            Op::ApproveGuardianDeniedAction { event: p.event }
        }
        "run_user_shell_command" => {
            let p: ShellParams = decode(params)?;
            Op::RunUserShellCommand {
                command: p.command,
                timeout_ms: p.timeout_ms,
            }
        }
        "set_thread_memory_mode" => {
            let p: MemoryParams = decode(params)?;
            thread.set_thread_memory_mode(p.mode).await?;
            return Ok(Value::Null);
        }
        "load_history" => {
            let p: HistoryParams = decode(params)?;
            return Ok(serde_json::to_value(
                thread.load_history(p.include_archived).await?,
            )?);
        }
        "read_thread" => {
            let p: ReadParams = decode(params)?;
            return Ok(serde_json::to_value(
                thread
                    .read_thread(p.include_archived, p.include_history)
                    .await?,
            )?);
        }
        "update_thread_metadata" => {
            let p: MetadataParams = decode(params)?;
            return Ok(serde_json::to_value(
                thread
                    .update_thread_metadata(p.patch, p.include_archived)
                    .await?,
            )?);
        }
        "append_rollout_items" => {
            let p: RolloutParams = decode(params)?;
            crate::checkpoint::validate_imported_history(&p.items)?;
            thread.append_rollout_items(&p.items).await?;
            return Ok(Value::Null);
        }
        "terminate_background_terminal" => {
            let p: TerminalParams = decode(params)?;
            return Ok(json!(
                thread.terminate_background_terminal(p.process_id).await
            ));
        }
        "inject_response_items" => {
            let p: ItemsParams = decode(params)?;
            thread.inject_response_items(p.items).await?;
            return Ok(Value::Null);
        }
        "inject_if_running" => {
            let p: ItemsParams = decode(params)?;
            return Ok(match thread.inject_if_running(p.items).await {
                Ok(()) => json!({"injected": true}),
                Err(items) => json!({"injected": false, "items": items}),
            });
        }
        "set_app_server_client_info" => {
            let p: ClientInfoParams = decode(params)?;
            thread
                .set_app_server_client_info(
                    p.app_server_client_name,
                    p.app_server_client_version,
                    p.mcp_elicitations_auto_deny,
                )
                .await?;
            return Ok(Value::Null);
        }
        "call_mcp_tool" => {
            let p: McpToolParams = decode(params)?;
            return Ok(serde_json::to_value(
                thread
                    .call_mcp_tool(&p.server, &p.tool, p.arguments, p.meta)
                    .await?,
            )?);
        }
        "read_mcp_resource" => {
            let p: McpReadParams = decode(params)?;
            return thread.read_mcp_resource(&p.server, p.params).await;
        }
        "read_mcp_resource_for_call" => {
            let p: McpCallReadParams = decode(params)?;
            return thread.read_mcp_resource_for_call(&p.call_id, &p.uri).await;
        }
        "environment_ready" => {
            let p: EnvironmentReadyParams = decode(params)?;
            let selection: TurnEnvironmentSelection = p.selection.try_into()?;
            let config = p.config.try_into()?;
            thread.environment_ready(&selection, config).await?;
            return Ok(Value::Null);
        }
        "environment_failed" => {
            let p: EnvironmentFailedParams = decode(params)?;
            thread
                .environment_failed(&p.selection.try_into()?, p.error)
                .await?;
            return Ok(Value::Null);
        }
        "current_turn_model" => {
            let p: TurnId = decode(params)?;
            return Ok(json!(thread.current_turn_model(&p.turn_id).await));
        }
        "active_turn_root" => {
            let p: TurnId = decode(params)?;
            return Ok(json!(thread.active_turn_root(&p.turn_id).await));
        }
        "approvals_reviewer_for_turn" => {
            let p: TurnId = decode(params)?;
            return Ok(json!(thread.approvals_reviewer_for_turn(&p.turn_id).await));
        }
        "record_user_goal_update" => {
            #[derive(Deserialize)]
            #[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
            enum Goal {
                Set {
                    objective: Option<String>,
                    status: Option<ThreadGoalStatus>,
                },
                Clear,
            }
            thread
                .record_user_goal_update(match decode::<Goal>(params)? {
                    Goal::Set { objective, status } => {
                        codex_core::context::UserGoalUpdate::Set { objective, status }
                    }
                    Goal::Clear => codex_core::context::UserGoalUpdate::Clear,
                })
                .await?;
            return Ok(Value::Null);
        }
        "realtime_conversation_start" => {
            Op::RealtimeConversationStart(decode::<RealtimeStart>(params)?.into())
        }
        "realtime_conversation_audio" => {
            let p: RealtimeAudio = decode(params)?;
            Op::RealtimeConversationAudio(ConversationAudioParams { frame: p.frame })
        }
        "realtime_conversation_text" => {
            Op::RealtimeConversationText(decode::<RealtimeText>(params)?.into())
        }
        "realtime_conversation_speech" => {
            let p: RealtimeSpeech = decode(params)?;
            Op::RealtimeConversationSpeech(ConversationSpeechParams { text: p.text })
        }
        no_args => {
            decode::<Empty>(params)?;
            match no_args {
                "interrupt" => return submit(thread, Op::Interrupt).await,
                "clean_background_terminals" => return submit(thread, Op::CleanBackgroundTerminals).await,
                "realtime_conversation_close" => return submit(thread, Op::RealtimeConversationClose).await,
                "realtime_conversation_list_voices" => return submit(thread, Op::RealtimeConversationListVoices).await,
                "refresh_mcp_servers" => return submit(thread, Op::RefreshMcpServers).await,
                "reload_user_config" => bail!("reload_user_config reads host configuration; supply config when creating the client or session"),
                "compact" => return submit(thread, Op::Compact).await,
                "shutdown" => { thread.shutdown_and_wait().await?; return Ok(Value::Null); }
                "token_usage_info" => return Ok(json!(thread.token_usage_info().await)),
                "agent_status" => return Ok(json!(thread.agent_status().await)),
                "thread_settings_snapshot" => return Ok(json!(thread.thread_settings_snapshot().await)),
                "rollout_path" => return Ok(json!(thread.rollout_path())),
                "instruction_sources" => return Ok(json!(thread.instruction_sources().await)),
                "queued_event_count" => return Ok(json!(thread.queued_event_count())),
                "list_background_terminals" => return Ok(json!(thread.list_background_terminals().await.into_iter().map(|p| json!({"item_id": p.item_id, "process_id": p.process_id, "command": p.command, "cwd": p.cwd})).collect::<Vec<_>>())),
                "increment_out_of_band_elicitation_count" => return Ok(json!(thread.increment_out_of_band_elicitation_count().await?)),
                "decrement_out_of_band_elicitation_count" => return Ok(json!(thread.decrement_out_of_band_elicitation_count().await?)),
                "flush_rollout" => thread.flush_rollout().await?,
                "checkpoint_preparation" => thread.checkpoint_preparation().await?,
                "ensure_rollout_materialized" => thread.ensure_rollout_materialized().await,
                "checkpoint_thread_settings" => thread.checkpoint_thread_settings().await?,
                "prewarm" => thread.prewarm().await,
                "prewarm_with_history" => thread.prewarm_with_history().await,
                "wait_until_terminated" => thread.wait_until_terminated().await,
                _ => bail!("unknown native operation: {operation}"),
            }
            return Ok(Value::Null);
        }
    };
    submit(thread, op).await
}

async fn submit(thread: &CodexThread, op: Op) -> Result<Value> {
    Ok(json!({"submission_id": thread.submit(op).await?}))
}

params!(RealtimeAudio {
    frame: RealtimeAudioFrame
});
params!(RealtimeSpeech { text: String });
params!(RealtimeText {
    text: String,
    #[serde(default)]
    role: ConversationTextRole
});
impl From<RealtimeText> for ConversationTextParams {
    fn from(p: RealtimeText) -> Self {
        Self {
            text: p.text,
            role: p.role,
        }
    }
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum RealtimeTransport {
    Websocket,
    Webrtc {
        sdp: String,
    },
    ExistingCall {
        call_id: String,
        sideband_base_url: Option<String>,
    },
}

params!(RealtimeStart {
    client_managed_handoffs: bool,
    delegation_ack_filler: Option<bool>,
    flush_transcript_tail_on_session_end: bool,
    codex_responses_as_items: bool,
    codex_response_item_prefix: Option<String>,
    codex_response_handoff_mode: CodexResponseHandoffMode,
    backend_reasoning_status: bool,
    codex_response_handoff_channel_prefixes: Option<BTreeMap<String, Vec<String>>>,
    model: Option<String>,
    output_modality: RealtimeOutputModality,
    include_startup_context: bool,
    #[serde(default)] initial_items: Vec<RealtimeText>,
    realtime_start_instructions: Option<String>,
    realtime_end_instructions: Option<String>,
    #[serde(default, deserialize_with = "optional")] prompt: Option<Option<String>>,
    realtime_session_id: Option<String>,
    transport: Option<RealtimeTransport>,
    version: Option<RealtimeConversationVersion>,
    voice: Option<RealtimeVoice>,
});

impl From<RealtimeStart> for ConversationStartParams {
    fn from(p: RealtimeStart) -> Self {
        Self {
            client_managed_handoffs: p.client_managed_handoffs,
            delegation_ack_filler: p.delegation_ack_filler,
            flush_transcript_tail_on_session_end: p.flush_transcript_tail_on_session_end,
            codex_responses_as_items: p.codex_responses_as_items,
            codex_response_item_prefix: p.codex_response_item_prefix,
            codex_response_handoff_mode: p.codex_response_handoff_mode,
            backend_reasoning_status: p.backend_reasoning_status,
            codex_response_handoff_channel_prefixes: p.codex_response_handoff_channel_prefixes,
            model: p.model,
            output_modality: p.output_modality,
            include_startup_context: p.include_startup_context,
            initial_items: p.initial_items.into_iter().map(Into::into).collect(),
            realtime_start_instructions: p.realtime_start_instructions,
            realtime_end_instructions: p.realtime_end_instructions,
            prompt: p.prompt,
            realtime_session_id: p.realtime_session_id,
            transport: p.transport.map(|t| match t {
                RealtimeTransport::Websocket => ConversationStartTransport::Websocket,
                RealtimeTransport::Webrtc { sdp } => ConversationStartTransport::Webrtc { sdp },
                RealtimeTransport::ExistingCall {
                    call_id,
                    sideband_base_url,
                } => ConversationStartTransport::ExistingCall {
                    call_id,
                    sideband_base_url,
                },
            }),
            version: p.version,
            voice: p.voice,
        }
    }
}
