//! Live child turns projected by the existing native event owner.

use anyhow::{Context, ensure};
use codex_core::context::{
    ContextualUserFragment, InternalContextSource, InternalModelContextFragment,
};
use codex_core_api::{CodexThread, Config, Feature, ThreadId, WithTurnExtensionData};
use codex_extension_api::ToolPolicy;
use codex_models_manager::manager::{RefreshStrategy, SharedModelsManager};
use codex_protocol::models::{ContentItem, MessagePhase, ResponseItem};
use codex_protocol::openai_models::{ConfigShellToolType, ModelInfo, ReasoningEffort, ToolMode};
use codex_protocol::protocol::{Event, EventMsg, TokenUsageInfo};
use codex_protocol::turn_input::{
    NotSubmittedReason, StartIfIdleSubmission, SteerSubmission, TurnInput, TurnInputRequest,
    TurnInputSubmission,
};
use codex_protocol::user_input::UserInput;
use codex_tools::ToolName;
use serde::Deserialize;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, OnceLock, Weak};
use tokio::sync::watch;

use crate::engine::ThreadState;

// Freeze the root and all current application-declared role models before an
// environment is attached. Role files are created by the SDK, never restored
// from historical instructions or model-selected overrides.
pub async fn validated_catalog(
    config: &Config,
    models: &SharedModelsManager,
) -> anyhow::Result<Vec<ModelInfo>> {
    #[derive(serde::Deserialize)]
    struct DeclaredRole {
        model: Option<String>,
        model_reasoning_effort: Option<ReasoningEffort>,
    }

    let root_model = config
        .model
        .as_ref()
        .context("NativeAgents requires an explicit model")?;
    let mut catalog = vec![validate_model(config, models, root_model).await?];
    let mut roles = Vec::new();
    for role in config.agent_roles.values() {
        if let Some(path) = &role.config_file {
            let source = tokio::fs::read_to_string(path).await?;
            let declaration: DeclaredRole = toml::from_str(&source)?;
            if let Some(model) = &declaration.model
                && !catalog.iter().any(|info| &info.slug == model)
            {
                let info = validate_model(config, models, model).await?;
                ensure!(
                    !config.features.enabled(Feature::CodeMode)
                        || matches!(
                            info.tool_mode,
                            Some(ToolMode::CodeMode | ToolMode::CodeModeOnly)
                        ),
                    "Native role model metadata is incompatible with Session Code Mode"
                );
                catalog.push(info);
            }
            roles.push(declaration);
        }
    }
    for role in roles {
        if let Some(effort) = role.model_reasoning_effort {
            // A role without its own model inherits whichever admitted parent
            // delegated to it, including another role at a deeper level.
            ensure!(
                effort != ReasoningEffort::Persistent
                    && catalog
                        .iter()
                        .filter(|info| role.model.as_ref().is_none_or(|model| model == &info.slug))
                        .all(|info| info
                            .supported_reasoning_levels
                            .iter()
                            .any(|preset| preset.effort == effort)),
                "Native role reasoning effort is incompatible with its possible models"
            );
        }
    }
    Ok(catalog)
}

async fn validate_model(
    config: &Config,
    models: &SharedModelsManager,
    model: &str,
) -> anyhow::Result<ModelInfo> {
    let info = models
        .get_model_info(model, &config.to_models_manager_config())
        .await;
    // Root and every declared role are validated before the environment opens.
    // Model-supplied overrides cannot expand this frozen native catalogue.
    crate::engine::EngineState::validate_hosted_search(config, &info)?;
    ensure!(
        !info.used_fallback_model_metadata
            && !matches!(info.shell_type, ConfigShellToolType::Disabled)
            && info.apply_patch_tool_type.is_some()
            && (matches!(info.tool_mode, None | Some(ToolMode::Direct))
                || (matches!(
                    info.tool_mode,
                    Some(ToolMode::CodeMode | ToolMode::CodeModeOnly)
                ) && (config.features.enabled(Feature::CodeMode)
                    || (config
                        .code_mode
                        .direct_only_tool_namespaces
                        .iter()
                        .any(|name| name == "functions")
                        && config
                            .code_mode
                            .direct_only_tool_namespaces
                            .iter()
                            .any(|name| name == "collaboration"))))),
        "The selected model metadata is incompatible with NativeAgents"
    );
    // No online refresh or provider fallback expands this admission decision.
    let selected = models
        .get_default_model(
            &Some(model.to_owned()),
            false,
            RefreshStrategy::Offline,
            config.http_client_factory(),
        )
        .await;
    ensure!(
        selected == model,
        "NativeAgents model selection did not match"
    );
    Ok(info)
}

pub struct Policy {
    pub roles: Vec<String>,
    pub max_depth: usize,
    pub(crate) instruction_files: Vec<crate::application_instructions::InstructionFile>,
    // Current startup policy is host authority. Core reads its own copy before
    // child lifecycle callbacks, so every direct dispatch checks this ceiling.
    pub allowed: OnceLock<Arc<ToolPolicy>>,
}

impl Policy {
    fn explicitly_allows(&self, name: &ToolName) -> bool {
        self.allowed
            .get()
            .is_some_and(|policy| policy.allowed_tools.is_some() && policy.allows(name))
    }

    pub fn admits(&self, depth: usize, item: &codex_protocol::models::ResponseItem) -> bool {
        use codex_protocol::models::ResponseItem;
        match item {
            ResponseItem::FunctionCall {
                name,
                namespace,
                arguments,
                ..
            } => {
                let allowed =
                    self.explicitly_allows(&ToolName::new(namespace.clone(), name.clone()));
                if !allowed {
                    return false;
                }
                if namespace.as_deref() != Some("collaboration") {
                    // The Code Mode wrapper requires an explicit current
                    // ToolPolicy grant, just like each nested dispatch.
                    return true;
                }
                if (depth != 0 && self.max_depth == 1)
                    || (name == "spawn_agent" && depth >= self.max_depth)
                {
                    return false;
                }
                if name != "spawn_agent" {
                    return matches!(
                        name.as_str(),
                        "wait_agent"
                            | "send_message"
                            | "followup_task"
                            | "interrupt_agent"
                            | "list_agents"
                    );
                }
                let Ok(serde_json::Value::Object(arguments)) = serde_json::from_str(arguments)
                else {
                    return false;
                };
                if arguments.contains_key("model") || arguments.contains_key("reasoning_effort") {
                    return false;
                }
                let role = match arguments.get("agent_type") {
                    None | Some(serde_json::Value::Null) => "default",
                    Some(serde_json::Value::String(role)) => role,
                    _ => return false,
                };
                matches!(role, "default" | "explorer" | "worker")
                    || self.roles.iter().any(|allowed| allowed == role)
            }
            ResponseItem::CustomToolCall {
                name, namespace, ..
            } => {
                namespace
                    .as_deref()
                    .is_none_or(|ns| matches!(ns, "functions" | ""))
                    && matches!(name.as_str(), "apply_patch" | "exec")
                    && self.explicitly_allows(&ToolName::new(namespace.clone(), name.clone()))
            }
            _ => false,
        }
    }

    pub(crate) fn admits_nested(
        &self,
        depth: usize,
        invocation: &codex_code_mode::CodeModeNestedToolCall,
    ) -> bool {
        let name = &invocation.tool_name;
        if !self.explicitly_allows(name) {
            return false;
        }
        match invocation.tool_kind {
            codex_code_mode::CodeModeToolKind::Freeform => {
                name.is_default_namespace() && name.name == "apply_patch"
            }
            codex_code_mode::CodeModeToolKind::Function => {
                if name.namespace.as_deref() != Some("collaboration") {
                    return true;
                }
                if (depth != 0 && self.max_depth == 1)
                    || (name.name == "spawn_agent" && depth >= self.max_depth)
                {
                    return false;
                }
                if name.name != "spawn_agent" {
                    return matches!(
                        name.name.as_str(),
                        "wait_agent"
                            | "send_message"
                            | "followup_task"
                            | "interrupt_agent"
                            | "list_agents"
                    );
                }
                let Some(serde_json::Value::Object(args)) = invocation.input.as_ref() else {
                    return false;
                };
                if args.contains_key("model") || args.contains_key("reasoning_effort") {
                    return false;
                }
                let role = match args.get("agent_type") {
                    None | Some(serde_json::Value::Null) => "default",
                    Some(serde_json::Value::String(role)) => role,
                    _ => return false,
                };
                matches!(role, "default" | "explorer" | "worker")
                    || self.roles.iter().any(|allowed| allowed == role)
            }
        }
    }
}

pub struct Instance {
    pub id: u64,
    pub incarnation: Option<String>,
    pub thread: Weak<CodexThread>,
    pub turn: Option<Arc<Turn>>,
    pub usage: Option<TokenUsageInfo>,
    // Admitted Core receipts awaiting this instance's sole event reader. Never
    // replace current with a new receipt before earlier queued events are read.
    pub submitted: HashMap<String, Arc<Turn>>,
    // The event reader can beat Core's admission acknowledgement. Retain its
    // receipts only while the serialized SDK submit is still in flight.
    pub during_submission: Option<HashMap<String, Arc<Turn>>>,
}

pub struct Turn {
    pub child_id: ThreadId,
    pub instance_id: u64,
    pub incarnation: Option<String>,
    pub turn_id: String,
    pub thread: Weak<CodexThread>,
    pub owner: Weak<ThreadState>,
    result: watch::Sender<Option<Outcome>>,
    sdk_admission: OnceLock<Arc<crate::python_tools::TurnAdmission>>,
    // Core's projected AgentMessage normalizes both no parts and blank text to
    // Text(""). Retain only whether the last raw assistant item had real blank
    // text; an unexplained None after nonblank text must not become success.
    last_assistant_had_blank_text: AtomicBool,
    // An explicit final blank also replaces cached commentary through legacy tails.
    last_assistant_had_blank_final: AtomicBool,
}

#[derive(Clone)]
struct Outcome {
    result: Result<String, String>,
    terminal: bool,
    usage: Option<TokenUsageInfo>,
}

impl Turn {
    fn new(
        owner: &Arc<ThreadState>,
        child_id: ThreadId,
        instance: &Instance,
        turn_id: String,
    ) -> Self {
        Self {
            child_id,
            instance_id: instance.id,
            incarnation: instance.incarnation.clone(),
            turn_id,
            thread: instance.thread.clone(),
            owner: Arc::downgrade(owner),
            result: watch::channel(None).0,
            sdk_admission: OnceLock::new(),
            last_assistant_had_blank_text: AtomicBool::new(false),
            last_assistant_had_blank_final: AtomicBool::new(false),
        }
    }
    fn finish(
        &self,
        result: Result<String, String>,
        terminal: bool,
        usage: Option<TokenUsageInfo>,
    ) {
        self.result.send_if_modified(|current| {
            if current.is_some() {
                return false;
            }
            *current = Some(Outcome {
                // A retained steer belongs to a new native turn. It cannot
                // alter the result of this exact, already completed turn.
                result,
                terminal,
                usage,
            });
            true
        });
    }

    async fn outcome(&self) -> anyhow::Result<Outcome> {
        let mut result = self.result.subscribe();
        loop {
            let current = result.borrow_and_update().clone();
            if let Some(current) = current {
                return Ok(current);
            }
            result
                .changed()
                .await
                .context("Native child result is unavailable")?;
        }
    }
    pub async fn wait(&self) -> anyhow::Result<(String, Option<TokenUsageInfo>)> {
        let outcome = self.outcome().await?;
        Ok((outcome.result.map_err(anyhow::Error::msg)?, outcome.usage))
    }
}

impl Instance {
    fn submission_turn(
        &mut self,
        owner: &Arc<ThreadState>,
        child_id: ThreadId,
        turn_id: &str,
    ) -> Arc<Turn> {
        if let Some(turn) = self.turn.as_ref().filter(|turn| turn.turn_id == turn_id) {
            return turn.clone();
        }
        if let Some(turn) = self
            .during_submission
            .as_ref()
            .and_then(|capture| capture.get(turn_id))
        {
            return turn.clone();
        }
        if let Some(turn) = self.submitted.get(turn_id) {
            return turn.clone();
        }
        let turn = Arc::new(Turn::new(owner, child_id, self, turn_id.to_owned()));
        // Before Core acknowledges input, the existing capture owns this
        // slot. Drop removes rejected or cancelled pre-submit work.
        self.during_submission
            .as_mut()
            .expect("child submission capture must be open")
            .insert(turn_id.to_owned(), turn.clone());
        turn
    }

    fn acknowledge_submission(
        &mut self,
        owner: &Arc<ThreadState>,
        child_id: ThreadId,
        turn_id: &str,
    ) -> Arc<Turn> {
        let turn = self.submission_turn(owner, child_id, turn_id);
        // Promote only an acknowledged turn whose TurnStarted the reader has
        // not yet observed. Completed captures can be returned without lingering.
        if !self
            .turn
            .as_ref()
            .is_some_and(|live| Arc::ptr_eq(live, &turn))
            && turn.result.borrow().is_none()
        {
            self.submitted.insert(turn_id.to_owned(), turn.clone());
        }
        turn
    }

    pub fn retire(&self) {
        if let Some(turn) = &self.turn {
            turn.finish(
                Err("Native child runtime closed or was replaced".into()),
                false,
                self.usage.clone(),
            );
        }
        for turn in self.submitted.values() {
            turn.finish(
                Err("Native child runtime closed or was replaced".into()),
                false,
                None,
            );
        }
    }

    pub fn observe(
        &mut self,
        owner: &Arc<ThreadState>,
        id: ThreadId,
        event: &Event,
    ) -> Option<bool> {
        if let EventMsg::TurnStarted(start) = &event.msg {
            if let Some(previous) = &self.turn {
                previous.finish(
                    Err("Native child turn ended without an observed terminal".into()),
                    false,
                    self.usage.clone(),
                );
            }
            self.usage = None;
            self.turn = Some(
                self.submitted
                    .remove(&start.turn_id)
                    .or_else(|| {
                        self.during_submission
                            .as_ref()
                            .and_then(|capture| capture.get(&start.turn_id))
                            .cloned()
                    })
                    .unwrap_or_else(|| Arc::new(Turn::new(owner, id, self, start.turn_id.clone()))),
            );
            if let Some(capture) = self.during_submission.as_mut()
                && let Some(turn) = &self.turn
            {
                capture.insert(turn.turn_id.clone(), turn.clone());
            }
            return None;
        }
        let Some(turn) = &self.turn else {
            return None;
        };
        match &event.msg {
            EventMsg::TokenCount(count) if event.id == turn.turn_id => {
                self.usage = count.info.clone();
            }
            EventMsg::RawResponseItem(raw) if event.id == turn.turn_id => {
                if let ResponseItem::Message {
                    role,
                    content,
                    phase,
                    ..
                } = &raw.item
                    && role == "assistant"
                {
                    let mut texts = content
                        .iter()
                        .filter_map(|part| match part {
                            ContentItem::InputText { text } | ContentItem::OutputText { text } => {
                                Some(text.as_str())
                            }
                            _ => None,
                        })
                        .peekable();
                    let blank = texts.peek().is_some() && texts.all(|text| text.trim().is_empty());
                    turn.last_assistant_had_blank_text
                        .store(blank, Ordering::Relaxed);
                    // Unknown-phase blank tails keep the previous final answer.
                    if !blank || matches!(phase, Some(MessagePhase::FinalAnswer)) {
                        turn.last_assistant_had_blank_final
                            .store(blank, Ordering::Relaxed);
                    }
                }
            }
            EventMsg::TurnComplete(end) if end.turn_id == turn.turn_id => {
                let mcp_failed = self
                    .thread
                    .upgrade()
                    .and_then(|thread| {
                        thread
                            .thread_extension_data()
                            .get::<crate::mcp_result_control::McpResultControl>()
                    })
                    .is_some_and(|control| control.take_unresolved(&turn.turn_id));
                let python_failed = self
                    .thread
                    .upgrade()
                    .and_then(|thread| {
                        thread
                            .thread_extension_data()
                            .get::<crate::python_tools::PythonTools>()
                    })
                    .is_some_and(|tools| tools.take_failed(&turn.turn_id));
                turn.finish(
                    if end.error.is_some() || mcp_failed || python_failed {
                        Err("Native child turn failed".into())
                    } else if turn.last_assistant_had_blank_final.load(Ordering::Relaxed) {
                        Ok(String::new())
                    } else {
                        end.last_agent_message
                            .clone()
                            .or_else(|| {
                                turn.last_assistant_had_blank_text
                                    .load(Ordering::Relaxed)
                                    .then(String::new)
                            })
                            .ok_or_else(|| {
                                "Native child completed without an assistant output".into()
                            })
                    },
                    true,
                    self.usage.clone(),
                );
                return turn
                    .result
                    .borrow()
                    .as_ref()
                    .filter(|outcome| outcome.terminal)
                    .map(|outcome| outcome.result.is_err());
            }
            EventMsg::TurnAborted(end) if end.turn_id.as_ref() == Some(&turn.turn_id) => {
                turn.finish(
                    Err("Native child turn was interrupted".into()),
                    true,
                    self.usage.clone(),
                );
            }
            EventMsg::ShutdownComplete => self.retire(),
            _ => {}
        }
        None
    }
}

struct SubmissionCapture {
    owner: Weak<ThreadState>,
    child_id: ThreadId,
    instance_id: u64,
}

impl Drop for SubmissionCapture {
    fn drop(&mut self) {
        if let Some(owner) = self.owner.upgrade()
            && let Ok(mut instances) = owner.child_instances.write()
            && let Some(current) = instances.get_mut(&self.child_id)
            && current.id == self.instance_id
        {
            current.during_submission.take();
        }
    }
}

// Only a live, already observed descendant is an input destination. Capture
// its authority and optional observed turn together, before releasing the lock.
// A reader without SDK Control can be observed but cannot grant SDK input.
pub async fn instance(
    state: &Arc<ThreadState>,
    id: ThreadId,
) -> anyhow::Result<Option<(u64, String, Option<Arc<Turn>>)>> {
    ensure!(
        id != state.id
            && state
                .engine
                .manager
                .list_agent_subtree_thread_ids(state.id)
                .await?
                .contains(&id),
        "Thread is not a child of this Session"
    );
    let Ok(thread) = state.engine.manager.get_thread(id).await else {
        return Ok(None);
    };
    let instances = state
        .child_instances
        .read()
        .map_err(|_| anyhow::anyhow!("Native child state unavailable"))?;
    Ok(instances
        .get(&id)
        .filter(|current| Weak::ptr_eq(&current.thread, &Arc::downgrade(&thread)))
        .and_then(|current| {
            current
                .incarnation
                .clone()
                .map(|incarnation| (current.id, incarnation, current.turn.clone()))
        }))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ChildInput {
    input: Vec<UserInput>,
}

pub async fn submit(
    state: &Arc<ThreadState>,
    child_id: ThreadId,
    instance_id: u64,
    value: serde_json::Value,
    expected: Option<&Arc<Turn>>,
) -> anyhow::Result<Arc<Turn>> {
    let input: ChildInput = crate::config::decode(value)?;
    ensure!(
        !input.input.is_empty()
            && input.input.iter().all(|part| matches!(
                part,
                UserInput::Text { .. } | UserInput::Image { .. } | UserInput::LocalImage { .. }
            )),
        "Child submission requires text or image user input"
    );
    // Serialize SDK admission and close operations; Core still owns admission
    // against concurrently submitted native followups and steers.
    let _mutation = state.mutations.lock().await;
    ensure!(
        instance(state, child_id)
            .await?
            .is_some_and(|(current, _, _)| current == instance_id),
        "Native child instance changed or is no longer live"
    );
    if let Some(turn) = expected {
        ensure!(
            turn.child_id == child_id
                && turn.instance_id == instance_id
                && Weak::ptr_eq(&turn.owner, &Arc::downgrade(state))
                && owns_turn(state, turn)?,
            "Native child turn is no longer current"
        );
    }
    let thread = {
        let mut instances = state
            .child_instances
            .write()
            .map_err(|_| anyhow::anyhow!("Native child state unavailable"))?;
        let current = instances
            .get_mut(&child_id)
            .filter(|current| current.id == instance_id)
            .context("Native child is no longer live")?;
        let thread = current
            .thread
            .upgrade()
            .context("Native child is no longer live")?;
        // Retain an already observed steer target in the same capture. A
        // native followup may replace current.turn before acknowledgement.
        current.during_submission = Some(
            expected
                .map(|turn| (turn.turn_id.clone(), turn.clone()))
                .into_iter()
                .collect(),
        );
        thread
    };
    let _capture = SubmissionCapture {
        owner: Arc::downgrade(state),
        child_id,
        instance_id,
    };
    let control = thread
        .thread_extension_data()
        .get::<crate::native_interception::Control>()
        .context("Native child admission is unavailable")?;
    ensure!(
        control.has_native_agents(),
        "Child submission requires current NativeAgents ownership"
    );
    let request = TurnInputRequest::new(TurnInput::UserInput {
        content: input.input,
        client_id: None,
    });
    let tools = thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>()
        .context("Native child submission lost its tool owner")?;
    let turn_id = match expected {
        Some(turn) => {
            // Attach before Core can record input or publish completion. The
            // sole event reader may beat this admission's acknowledgement.
            let receipt = tools.child_steer_admission(&turn.turn_id)?;
            let mut request = request;
            if let TurnInput::UserInput { client_id, .. } = &mut request.input {
                *client_id = Some(receipt.steering_client_id());
            }
            let owner = turn.sdk_admission.get_or_init(|| receipt.clone());
            ensure!(
                Arc::ptr_eq(owner, &receipt),
                "Native child turn ownership changed"
            );
            // Core enforces the captured identity at mutation even if the
            // child completes and a native followup arrives after our checks.
            match thread.steer_turn(request, turn.turn_id.clone()).await? {
                SteerSubmission::Steered { turn_id } => turn_id,
                SteerSubmission::NotSubmitted { reason } => {
                    anyhow::bail!("Native child steer was not submitted: {reason:?}")
                }
            }
        }
        None => {
            tools.resume_graph(&state.engine.manager).await?;
            {
                let instances = state
                    .child_instances
                    .read()
                    .map_err(|_| anyhow::anyhow!("Native child state unavailable"))?;
                instances
                    .get(&child_id)
                    .filter(|current| {
                        current.id == instance_id
                            && Weak::ptr_eq(&current.thread, &Arc::downgrade(&thread))
                    })
                    .context("Native child changed before start")?;
            }
            // A terminal task may still own Core's slot; only a fresh start
            // can settle that receipt. Steering never finishes another turn.
            tools.finish_child_before_submit(&thread).await?;
            {
                let instances = state
                    .child_instances
                    .read()
                    .map_err(|_| anyhow::anyhow!("Native child state unavailable"))?;
                ensure!(
                    instances.get(&child_id).is_some_and(|current| {
                        current.id == instance_id
                            && Weak::ptr_eq(&current.thread, &Arc::downgrade(&thread))
                    }),
                    "Native child changed while finishing its previous turn"
                );
            }
            match thread.start_turn_if_idle(request).await? {
                StartIfIdleSubmission::Started { turn_id } => turn_id,
                StartIfIdleSubmission::NotSubmitted { reason } => {
                    anyhow::bail!("Native child start was not submitted: {reason:?}")
                }
            }
        }
    };
    let mut instances = state
        .child_instances
        .write()
        .map_err(|_| anyhow::anyhow!("Native child state unavailable"))?;
    let current = instances
        .get_mut(&child_id)
        .filter(|current| {
            current.id == instance_id && Weak::ptr_eq(&current.thread, &Arc::downgrade(&thread))
        })
        .context("Native child closed before its admission receipt")?;
    Ok(current.acknowledge_submission(state, child_id, &turn_id))
}

pub async fn current(state: &Arc<ThreadState>, id: ThreadId) -> anyhow::Result<Option<Arc<Turn>>> {
    ensure!(
        id != state.id
            && state
                .engine
                .manager
                .list_agent_subtree_thread_ids(state.id)
                .await?
                .contains(&id),
        "Thread is not a child of this Session"
    );
    let instances = state
        .child_instances
        .read()
        .map_err(|_| anyhow::anyhow!("Native child state unavailable"))?;
    Ok(instances
        .get(&id)
        .and_then(|instance| instance.turn.clone()))
}

// Neither a matching turn ID nor a durable child ID grants continuation after
// a native reload. Check the original live thread and receipt at each boundary.
fn owns_turn(state: &ThreadState, turn: &Arc<Turn>) -> anyhow::Result<bool> {
    let instances = state
        .child_instances
        .read()
        .map_err(|_| anyhow::anyhow!("Native child state unavailable"))?;
    Ok(!state.is_closed()
        && instances.get(&turn.child_id).is_some_and(|current| {
            current.id == turn.instance_id
                && current.incarnation == turn.incarnation
                && Weak::ptr_eq(&current.thread, &turn.thread)
                && current
                    .turn
                    .as_ref()
                    .is_some_and(|current| Arc::ptr_eq(current, turn))
        }))
}

pub async fn continue_retained(
    state: &Arc<ThreadState>,
    turn: &Arc<Turn>,
) -> anyhow::Result<Option<Arc<Turn>>> {
    ensure!(
        Weak::ptr_eq(&turn.owner, &Arc::downgrade(state)),
        "Child turn belongs to another Session"
    );
    let Some(receipt) = turn.sdk_admission.get() else {
        return Ok(None);
    };
    // This is invoked only after Python callback settlement, never by wait().
    // Do not wait for a result while holding the SDK admission lock.
    if !turn
        .result
        .borrow()
        .as_ref()
        .is_some_and(|outcome| outcome.terminal && outcome.result.is_ok())
    {
        return Ok(None);
    }
    let _mutation = state.mutations.lock().await;
    if !owns_turn(state, turn)?
        || !instance(state, turn.child_id)
            .await?
            .is_some_and(|(id, incarnation, _)| {
                id == turn.instance_id && Some(incarnation.as_str()) == turn.incarnation.as_deref()
            })
    {
        return Ok(None);
    }
    let Some(thread) = turn.thread.upgrade() else {
        return Ok(None);
    };
    let tools = thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>()
        .context("Native child continuation lost its tool owner")?;
    if !tools.can_finish_child_steering(receipt) {
        return Ok(None);
    }
    let finished = tools.finish_child_before_submit(&thread).await;
    if !owns_turn(state, turn)? || !tools.can_finish_child_steering(receipt) {
        return Ok(None);
    }
    finished?;
    if !tools.can_continue_steering(&turn.turn_id) {
        return Ok(None);
    }
    let init = thread.config_snapshot().await.turn_extension_init;
    if !owns_turn(state, turn)?
        || !tools.can_finish_child_steering(receipt)
        || !tools.can_continue_steering(&turn.turn_id)
    {
        return Ok(None);
    }
    {
        let mut instances = state
            .child_instances
            .write()
            .map_err(|_| anyhow::anyhow!("Native child state unavailable"))?;
        let current = instances
            .get_mut(&turn.child_id)
            .context("Native child is no longer live")?;
        current.during_submission = Some(HashMap::new());
    }
    let _capture = SubmissionCapture {
        owner: Arc::downgrade(state),
        child_id: turn.child_id,
        instance_id: turn.instance_id,
    };
    let request = TurnInputRequest::new(TurnInput::ResponseItem(ContextualUserFragment::into(
        InternalModelContextFragment::new(
            InternalContextSource::from_static("agents_steering"),
            "Continue with the latest user input already in the conversation. Do not repeat completed work.",
        ),
    )));
    let submitted = thread
        .continue_turn_if_idle(
            WithTurnExtensionData::new(request, init),
            turn.turn_id.clone(),
        )
        .await?;
    let turn_id = match submitted {
        TurnInputSubmission::Started { turn_id } => turn_id,
        TurnInputSubmission::NotSubmitted {
            reason: NotSubmittedReason::Superseded | NotSubmittedReason::PendingTriggerTurn,
        } => return Ok(None),
        TurnInputSubmission::NotSubmitted {
            reason: NotSubmittedReason::NotIdle,
        } if !owns_turn(state, turn)? || !tools.can_finish_child_steering(receipt) => {
            return Ok(None);
        }
        _ => anyhow::bail!("Native child continuation was not admitted"),
    };
    let mut instances = state
        .child_instances
        .write()
        .map_err(|_| anyhow::anyhow!("Native child state unavailable"))?;
    let current = instances
        .get_mut(&turn.child_id)
        .filter(|current| {
            current.id == turn.instance_id
                && current.incarnation == turn.incarnation
                && Weak::ptr_eq(&current.thread, &turn.thread)
        })
        .context("Native child closed before its continuation receipt")?;
    Ok(Some(current.acknowledge_submission(
        state,
        turn.child_id,
        &turn_id,
    )))
}

// None means interruption was confirmed but native cleanup failed. Keep that
// receipt distinct from both a no-op and an error before confirmation.
pub async fn cancel(state: &Arc<ThreadState>, turn: &Arc<Turn>) -> anyhow::Result<Option<bool>> {
    ensure!(
        Weak::ptr_eq(&turn.owner, &Arc::downgrade(state)),
        "Child turn belongs to another Session"
    );
    ensure!(
        state
            .engine
            .manager
            .list_agent_subtree_thread_ids(state.id)
            .await?
            .contains(&turn.child_id),
        "Thread is not a child of this Session"
    );
    let Some(thread) = turn.thread.upgrade() else {
        return Ok(Some(false));
    };
    {
        let instances = state
            .child_instances
            .read()
            .map_err(|_| anyhow::anyhow!("Native child state unavailable"))?;
        if !instances.get(&turn.child_id).is_some_and(|current| {
            current.id == turn.instance_id
                && Weak::ptr_eq(&current.thread, &turn.thread)
                && current
                    .turn
                    .as_ref()
                    .is_some_and(|current| Arc::ptr_eq(current, turn))
        }) {
            return Ok(Some(false));
        }
    }
    let receipt = thread
        .thread_extension_data()
        .get::<crate::python_tools::PythonTools>()
        .and_then(|tools| tools.admission_ready(&turn.turn_id).ok());
    let interrupted = thread
        .interrupt_if_no_pending_input(&turn.turn_id)
        .await
        .context("Native child cancellation did not return a confirmed decision")?;
    if interrupted {
        ensure!(
            turn.outcome().await?.terminal,
            "Native child closed before its terminal event was observed"
        );
        if let Some(cleanup) = thread
            .thread_extension_data()
            .get::<crate::native_interception::Control>()
            .and_then(|control| control.capture_turn_stop(&turn.turn_id))
        {
            cleanup.stop();
            if cleanup.finish().await.is_err() {
                // Host error text is private. Python must still join callbacks
                // belonging to this confirmed cancellation before reporting it.
                return Ok(None);
            }
        }
        if receipt.is_some_and(|receipt| receipt.check_local_command_cleanup().is_err()) {
            return Ok(None);
        }
    }
    Ok(Some(interrupted))
}

#[cfg(test)]
mod steering_capture_tests {
    use super::*;
    use crate::python_tools::managed_tests::{real_engine, sdk_turn, steering_item};
    use codex_protocol::protocol::{
        TurnAbortReason, TurnAbortedEvent, TurnCompleteEvent, TurnStartedEvent,
    };

    fn started(turn_id: &str) -> Event {
        Event {
            id: turn_id.to_owned(),
            msg: EventMsg::TurnStarted(TurnStartedEvent {
                turn_id: turn_id.to_owned(),
                root_turn_id: None,
                trace_id: None,
                started_at: None,
                model_context_window: None,
                collaboration_mode_kind: Default::default(),
            }),
        }
    }
    fn completed(turn_id: &str) -> Event {
        Event {
            id: turn_id.to_owned(),
            msg: EventMsg::TurnComplete(TurnCompleteEvent {
                turn_id: turn_id.to_owned(),
                last_agent_message: Some("original answer".into()),
                error: None,
                started_at: None,
                completed_at: None,
                duration_ms: None,
                time_to_first_token_ms: None,
            }),
        }
    }

    fn install_instance(state: &Arc<ThreadState>, child_id: ThreadId) {
        state.child_instances.write().unwrap().insert(
            child_id,
            Instance {
                id: 1,
                incarnation: Some("test-child".into()),
                thread: Arc::downgrade(&state.thread),
                turn: None,
                usage: None,
                submitted: HashMap::new(),
                during_submission: Some(HashMap::new()),
            },
        );
    }

    fn capture(state: &Arc<ThreadState>, child_id: ThreadId) -> SubmissionCapture {
        SubmissionCapture {
            owner: Arc::downgrade(state),
            child_id,
            instance_id: 1,
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 3)]
    async fn real_owned_child_gap_continues_once_or_yields_to_graph_stop() {
        use crate::engine::settlement_tests::{ChildSampleHold, Provider};
        use crate::storage::{CompletionHold, MemoryThreadStore};
        use codex_protocol::user_input::UserInput;
        use tokio::sync::{Semaphore, mpsc};

        tokio::time::timeout(std::time::Duration::from_secs(45), async {
            for stop in [false, true] {
                let spawn = serde_json::json!({
                    "type": "function_call", "call_id": "spawn-alpha",
                    "name": "spawn_agent", "namespace": "collaboration",
                    "arguments": serde_json::json!({
                        "task_name": "alpha", "message": "Answer with one short word.",
                        "agent_type": "default", "fork_turns": "none",
                    }).to_string(),
                });
                let provider = Provider::with_first_item(Some(spawn)).await;
                let sample = Arc::new(ChildSampleHold {
                    entered: Semaphore::new(0),
                    release: Semaphore::new(0),
                });
                *provider.first_child_sample.lock().unwrap() = Some(sample.clone());
                let directory = tempfile::tempdir().unwrap();
                let config = serde_json::json!({
                    "model": "gpt-5.5", "model_provider": "fixture",
                    "model_providers.fixture": {
                        "name": "Receipt fixture", "base_url": provider.url,
                        "wire_api": "responses", "requires_openai_auth": false,
                        "supports_websockets": false, "request_max_retries": 0,
                        "stream_max_retries": 0,
                    },
                    "approval_policy": "never", "sandbox_mode": "read-only",
                    "web_search": "disabled", "agents.enabled": true,
                    "features.multi_agent_v2": {
                        "enabled": true, "tool_namespace": "collaboration",
                        "subagent_developer_instructions": "Answer briefly.",
                        "expose_spawn_agent_model_overrides": false,
                    },
                    "features.code_mode": {
                        "enabled": false,
                        "direct_only_tool_namespaces": ["functions", "collaboration"],
                    },
                    "features.code_mode_interrupt": false,
                    "features.instant_interrupt": false,
                    "features.shell_snapshot": false, "features.goals": false,
                    "analytics.enabled": false,
                });
                let runtime = serde_json::json!({
                    "codex_home": directory.path(), "cwd": directory.path(),
                    "_native_agents": true, "_native_turn_control": true,
                });
                let engine = Arc::new(
                    crate::engine::EngineState::new(
                        config.to_string(),
                        runtime.to_string(),
                        true,
                        Some("fixture-unused-key".into()),
                    )
                    .await
                    .unwrap(),
                );
                let options = serde_json::from_value(serde_json::json!({
                    "_native_agent_roles": [], "_native_agent_max_depth": 1,
                    "tool_policy": { "allowed_tools": [
                        {"name": "spawn_agent", "namespace": "collaboration"},
                    ]},
                }))
                .unwrap();
                let native = engine
                    .start_with_tools(options, None, Some(vec![]), false)
                    .await
                    .unwrap();
                let state = Arc::clone(&native.current().unwrap());
                let (sender, mut observations) = mpsc::unbounded_channel();
                let read_state = state.clone();
                let reader = tokio::spawn(async move {
                    while let Ok(event) = read_state.next_session_event().await {
                        if sender.send(event).is_err() {
                            break;
                        }
                    }
                });
                let text = |text: &str| {
                    serde_json::json!({
                        "input": [UserInput::Text { text: text.into(), text_elements: vec![] }],
                    })
                };
                let root = crate::ops::submit_replaceable(&state, text("Start one child"))
                    .await
                    .unwrap();
                assert_eq!(root["status"], "started");
                // This first-ever child has no previous task whose post-terminal
                // flush could steal the next-flush hold. Its provider request
                // proves startup reached native sampling before the hold is armed.
                sample.entered.acquire().await.unwrap().forget();
                let child_requests = || -> Vec<serde_json::Value> {
                    provider
                        .requests
                        .lock()
                        .unwrap()
                        .iter()
                        .filter(|body| {
                            let metadata: serde_json::Value = serde_json::from_str(
                                body["client_metadata"]["x-codex-turn-metadata"]
                                    .as_str()
                                    .unwrap(),
                            )
                            .unwrap();
                            metadata["agent_name"] == "/root/alpha"
                        })
                        .cloned()
                        .collect()
                };
                let initial = child_requests();
                assert_eq!(initial.len(), 1);
                let metadata: serde_json::Value = serde_json::from_str(
                    initial[0]["client_metadata"]["x-codex-turn-metadata"]
                        .as_str()
                        .unwrap(),
                )
                .unwrap();
                let child_id =
                    ThreadId::from_string(metadata["thread_id"].as_str().unwrap()).unwrap();
                let original_id = metadata["turn_id"].as_str().unwrap();
                loop {
                    let event = observations.recv().await.expect("SDK root reader ended");
                    if event["subagent_id"] == child_id.to_string()
                        && event["msg"]["turn_id"] == original_id
                        && matches!(
                            event["msg"]["type"].as_str(),
                            Some("task_started" | "turn_started")
                        )
                    {
                        break;
                    }
                }
                let (instance_id, _, original) = instance(&state, child_id).await.unwrap().unwrap();
                let original = original.unwrap();
                assert_eq!(original.turn_id, original_id);
                let store = engine
                    .store
                    .as_any()
                    .downcast_ref::<MemoryThreadStore>()
                    .unwrap();
                let hold = Arc::new(CompletionHold {
                    parent: child_id,
                    entered: Semaphore::new(0),
                    release: Semaphore::new(0),
                });
                *store.next_flush_hold.lock().unwrap() = Some(hold.clone());
                sample.release.add_permits(1);
                hold.entered.acquire().await.unwrap().forget();
                assert_eq!(
                    child_requests().len(),
                    1,
                    "hold must follow the original sample"
                );
                assert!(
                    original.result.borrow().is_none(),
                    "hold must precede actual terminal"
                );
                let first = submit(
                    &state,
                    child_id,
                    instance_id,
                    text("retained child input A"),
                    Some(&original),
                )
                .await
                .unwrap();
                let second = submit(
                    &state,
                    child_id,
                    instance_id,
                    text("retained child input B"),
                    Some(&original),
                )
                .await
                .unwrap();
                assert!(Arc::ptr_eq(&first, &original));
                assert!(Arc::ptr_eq(&second, &original));
                hold.release.add_permits(1);
                assert_eq!(original.outcome().await.unwrap().result, Ok("done".into()));
                let receipt = original
                    .sdk_admission
                    .get()
                    .expect("actual SDK steering receipt");
                assert!(receipt.has_steering_tail());
                assert_eq!(child_requests().len(), 1);
                let root_tools = state
                    .thread
                    .thread_extension_data()
                    .get::<crate::python_tools::PythonTools>()
                    .unwrap();
                let _stop = stop.then(|| root_tools.request_graph_interrupt(false).unwrap());
                let successor = continue_retained(&state, &original).await.unwrap();
                if stop {
                    assert!(successor.is_none());
                    assert_eq!(child_requests().len(), 1);
                } else {
                    let successor = successor.expect("genuine child tail must start one successor");
                    assert_ne!(successor.turn_id, original.turn_id);
                    assert_eq!(successor.child_id, original.child_id);
                    assert_eq!(successor.outcome().await.unwrap().result, Ok("done".into()));
                    let requests = child_requests();
                    assert_eq!(requests.len(), 2);
                    let metadata: serde_json::Value = serde_json::from_str(
                        requests[1]["client_metadata"]["x-codex-turn-metadata"]
                            .as_str()
                            .unwrap(),
                    )
                    .unwrap();
                    assert_eq!(metadata["turn_id"], successor.turn_id);
                    let input = requests[1]["input"].to_string();
                    assert_eq!(input.matches("retained child input A").count(), 1);
                    assert_eq!(input.matches("retained child input B").count(), 1);
                    assert!(
                        continue_retained(&state, &original)
                            .await
                            .unwrap()
                            .is_none()
                    );
                    assert_eq!(child_requests().len(), 2);
                }
                assert_eq!(original.outcome().await.unwrap().result, Ok("done".into()));
                state.close().await.unwrap();
                reader.await.unwrap();
                engine.close().await.unwrap();
            }
        })
        .await
        .expect("real owned child steering did not reach native settlement");
    }

    #[tokio::test]
    async fn observed_child_abort_returns_original_error_without_idle_notification() {
        let provider = crate::engine::settlement_tests::Provider::start().await;
        let directory = tempfile::tempdir().unwrap();
        let engine = real_engine(&directory, &provider.url).await;
        let native = engine
            .start_with_tools(Default::default(), None, Some(vec![]), false)
            .await
            .unwrap();
        let state = Arc::clone(&native.current().unwrap());
        let child_id = ThreadId::new();
        install_instance(&state, child_id);
        let _store = sdk_turn(&state, "explicitly-aborted").await;
        let original = {
            let mut rows = state.child_instances.write().unwrap();
            let row = rows.get_mut(&child_id).unwrap();
            row.observe(&state, child_id, &started("explicitly-aborted"));
            let event = Event {
                id: "explicitly-aborted".into(),
                msg: EventMsg::TurnAborted(TurnAbortedEvent {
                    turn_id: Some("explicitly-aborted".into()),
                    reason: TurnAbortReason::Interrupted,
                    error: None,
                    started_at: None,
                    completed_at: None,
                    duration_ms: None,
                }),
            };
            row.observe(&state, child_id, &event);
            row.turn.clone().unwrap()
        };
        // Core takes the active slot before explicit/conditional cancellation
        // and emits TurnAborted, but deliberately never notifies idle.
        let tools = state
            .thread
            .thread_extension_data()
            .get::<crate::python_tools::PythonTools>()
            .unwrap();
        let receipt = tools.admission_ready("explicitly-aborted").unwrap();
        assert!(futures_util::poll!(Box::pin(receipt.wait())).is_pending());
        let mut waiting = Box::pin(original.wait());
        let std::task::Poll::Ready(result) = futures_util::poll!(&mut waiting) else {
            panic!("a native abort cannot wait on an idle notification Core never emits");
        };
        assert!(result.unwrap_err().to_string().contains("was interrupted"));
        state.close().await.unwrap();
        assert!(
            original
                .wait()
                .await
                .unwrap_err()
                .to_string()
                .contains("was interrupted")
        );
        engine.close().await.unwrap();
    }

    #[tokio::test]
    async fn ordinary_child_wait_is_passive_and_preserves_old_outcome() {
        let provider = crate::engine::settlement_tests::Provider::start().await;
        let directory = tempfile::tempdir().unwrap();
        let engine = real_engine(&directory, &provider.url).await;
        let native = engine
            .start_with_tools(Default::default(), None, Some(vec![]), false)
            .await
            .unwrap();
        let state = Arc::clone(&native.current().unwrap());
        let child_id = ThreadId::new();
        install_instance(&state, child_id);
        // Install the actual SDK contributors, then let the event owner see a
        // terminal while the admitted Core slot is still held.
        let _original_store = sdk_turn(&state, "same-id").await;
        let original = {
            let mut rows = state.child_instances.write().unwrap();
            let row = rows.get_mut(&child_id).unwrap();
            row.observe(&state, child_id, &started("same-id"));
            row.observe(&state, child_id, &completed("same-id"));
            row.turn.clone().unwrap()
        };
        let tools = state
            .thread
            .thread_extension_data()
            .get::<crate::python_tools::PythonTools>()
            .unwrap();
        let receipt = tools.admission_ready("same-id").unwrap();
        assert_eq!(original.wait().await.unwrap().0, "original answer");
        assert!(futures_util::poll!(Box::pin(receipt.wait())).is_pending());
        // Replacement closes the original slot but cannot make its passive
        // readers wait on an unrelated, still-pending turn with the same ID.
        let _successor_store = sdk_turn(&state, "same-id").await;
        let successor = tools.admission_ready("same-id").unwrap();
        assert!(futures_util::poll!(Box::pin(successor.wait())).is_pending());
        assert_eq!(original.wait().await.unwrap().0, "original answer");
        state.close().await.unwrap();
        assert_eq!(original.wait().await.unwrap().0, "original answer");
        engine.close().await.unwrap();
    }

    #[tokio::test]
    async fn child_steer_rejects_native_successor_before_observer_advances() {
        let provider = crate::engine::settlement_tests::Provider::start().await;
        let directory = tempfile::tempdir().unwrap();
        let engine = real_engine(&directory, &provider.url).await;
        let native = engine
            .start_with_tools(Default::default(), None, Some(vec![]), false)
            .await
            .unwrap();
        let state = Arc::clone(&native.current().unwrap());
        let child_id = ThreadId::new();
        install_instance(&state, child_id);
        let _original_store = sdk_turn(&state, "original").await;
        let original = {
            let mut rows = state.child_instances.write().unwrap();
            let row = rows.get_mut(&child_id).unwrap();
            row.observe(&state, child_id, &started("original"));
            row.turn.clone().unwrap()
        };
        let tools = state
            .thread
            .thread_extension_data()
            .get::<crate::python_tools::PythonTools>()
            .unwrap();
        assert!(owns_turn(&state, &original).unwrap());
        assert!(tools.child_steer_admission(&original.turn_id).is_ok());
        // Run the actual contributors for the successor but intentionally
        // withhold its observer event. No timing or fabricated error involved.
        let _successor_store = sdk_turn(&state, "successor").await;
        assert!(owns_turn(&state, &original).unwrap());
        assert_eq!(
            tools
                .child_steer_admission(&original.turn_id)
                .err()
                .unwrap()
                .to_string(),
            "Native child turn is no longer current"
        );
        let successor = tools.child_steer_admission("successor").unwrap();
        assert!(!successor.has_steering_tail());
        {
            let mut rows = state.child_instances.write().unwrap();
            rows.get_mut(&child_id)
                .unwrap()
                .observe(&state, child_id, &started("successor"));
        }
        assert!(!owns_turn(&state, &original).unwrap());
        assert!(!successor.has_steering_tail());
        state.close().await.unwrap();
        engine.close().await.unwrap();
    }

    #[tokio::test]
    async fn steering_tentative_capture_unwinds_for_rejected_and_cancelled_submitters() {
        let provider = crate::engine::settlement_tests::Provider::start().await;
        let directory = tempfile::tempdir().unwrap();
        let engine = real_engine(&directory, &provider.url).await;
        let native = engine
            .start_with_tools(Default::default(), None, Some(vec![]), false)
            .await
            .unwrap();
        let state = Arc::clone(&native.current().unwrap());
        let child_id = ThreadId::new();
        install_instance(&state, child_id);
        let before_rejection = {
            let _capture = capture(&state, child_id);
            let mut rows = state.child_instances.write().unwrap();
            let row = rows.get_mut(&child_id).unwrap();
            let tentative = row.submission_turn(&state, child_id, "turn-before-observation");
            assert!(row.submitted.is_empty());
            Arc::downgrade(&tentative)
        };
        assert!(before_rejection.upgrade().is_none());
        assert!(
            state
                .child_instances
                .read()
                .unwrap()
                .get(&child_id)
                .unwrap()
                .during_submission
                .is_none()
        );

        install_instance(&state, child_id);
        let (seen, received) = tokio::sync::oneshot::channel();
        let pending = tokio::spawn({
            let state = state.clone();
            async move {
                let _capture = capture(&state, child_id);
                let weak = {
                    let mut rows = state.child_instances.write().unwrap();
                    let row = rows.get_mut(&child_id).unwrap();
                    let tentative = row.submission_turn(&state, child_id, "cancelled");
                    Arc::downgrade(&tentative)
                };
                seen.send(weak).unwrap();
                std::future::pending::<()>().await;
            }
        });
        let before_cancel = received.await.unwrap();
        assert!(before_cancel.upgrade().is_some());
        pending.abort();
        assert!(pending.await.unwrap_err().is_cancelled());
        assert!(
            before_cancel.upgrade().is_none(),
            "cancelled admission must not occupy submitted"
        );
        state.close().await.unwrap();
        engine.close().await.unwrap();
    }

    #[tokio::test]
    async fn steering_capture_observes_terminal_before_ack_and_freezes_original_result() {
        let provider = crate::engine::settlement_tests::Provider::start().await;
        let directory = tempfile::tempdir().unwrap();
        let engine = real_engine(&directory, &provider.url).await;
        let native = engine
            .start_with_tools(Default::default(), None, Some(vec![]), false)
            .await
            .unwrap();
        let state = Arc::clone(&native.current().unwrap());
        let child_id = ThreadId::new();
        install_instance(&state, child_id);
        let capture = capture(&state, child_id);
        let store = sdk_turn(&state, "receipt-a").await;
        let tools = state
            .thread
            .thread_extension_data()
            .get::<crate::python_tools::PythonTools>()
            .unwrap();
        let receipt = tools.admission_ready("receipt-a").unwrap();
        let tentative = {
            let mut rows = state.child_instances.write().unwrap();
            let row = rows.get_mut(&child_id).unwrap();
            let turn = row.submission_turn(&state, child_id, "receipt-a");
            turn.sdk_admission
                .set(receipt.clone())
                .unwrap_or_else(|_| panic!("already admitted"));
            turn
        };
        steering_item(&state, &store, Some(receipt.steering_client_id())).await;
        {
            let mut rows = state.child_instances.write().unwrap();
            let row = rows.get_mut(&child_id).unwrap();
            assert_eq!(row.observe(&state, child_id, &started("receipt-a")), None);
            assert!(Arc::ptr_eq(row.turn.as_ref().unwrap(), &tentative));
            assert_eq!(
                row.observe(&state, child_id, &completed("receipt-a")),
                Some(false)
            );
            assert_eq!(row.observe(&state, child_id, &started("successor")), None);
            let ack = row.acknowledge_submission(&state, child_id, "receipt-a");
            assert!(Arc::ptr_eq(&ack, &tentative));
            assert!(
                row.submitted.is_empty(),
                "already completed receipt must not be re-queued"
            );
        }
        drop(capture);
        assert_eq!(tentative.wait().await.unwrap().0, "original answer");
        assert!(futures_util::poll!(Box::pin(receipt.wait())).is_pending());
        let _next = sdk_turn(&state, "successor").await;
        // A new model request and later native completion cannot change the
        // already observed watch result of this exact older receipt.
        let mut metadata = None;
        use codex_extension_api::{ModelRequestContributor, ModelRequestInput, ModelRequestKind};
        drop(engine.interception.request(ModelRequestInput {
            kind: ModelRequestKind::Generation,
            thread_id: &state.id.to_string(),
            client_metadata: &mut metadata,
            model: "fixture",
        }));
        assert_eq!(tentative.wait().await.unwrap().0, "original answer");
        state.close().await.unwrap();
        engine.close().await.unwrap();
    }
}
