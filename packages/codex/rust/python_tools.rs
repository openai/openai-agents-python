#[path = "model_producer.rs"]
mod model_producer;
pub(crate) use model_producer::Contributor as ModelProducerContributor;

#[path = "prepared_requests.rs"]
mod prepared_requests;
pub(crate) use prepared_requests::Contributor as PreparedRequestContributor;

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock, RwLock, Weak};

use anyhow::{Context, Result, ensure};
use codex_core_api::{
    CodexThread, Config, Feature, ManagedLocalExecCancelFailure, StartThreadOptions, ThreadManager,
};
use codex_extension_api::{
    ContextContributor, ExtensionData, ExtensionFuture, ThreadIdleInput,
    ThreadLifecycleContributor, ThreadStartInput, ThreadStopInput, ToolCallOutcome,
    ToolContributor, ToolFinishInput, ToolLifecycleContributor, ToolLifecycleFuture, ToolPolicy,
    TurnErrorInput, TurnLifecycleContributor, TurnSamplingStop, TurnStartInput, TurnStartPhase,
    WorldStateContributionInput, WorldStateSectionContribution,
};
use codex_protocol::dynamic_tools::{
    DynamicToolFunctionSpec, DynamicToolNamespaceSpec, DynamicToolNamespaceTool,
    DynamicToolResponse, DynamicToolSpec,
};
use codex_protocol::models::{
    FunctionCallOutputBody, FunctionCallOutputContentItem, FunctionCallOutputPayload,
    ResponseInputItem, ResponseItem,
};
use codex_protocol::openai_models::ToolMode;
use codex_protocol::protocol::{AgentStatus, Op, SessionSource, ThreadSource};
use codex_tools::{
    FunctionCallError, ResponsesApiNamespace, ResponsesApiNamespaceTool, ToolCall, ToolCallSource,
    ToolExecutor, ToolExecutorFuture, ToolExposure, ToolName, ToolOutput, ToolPayload,
    ToolSearchInfo, ToolSearchSourceInfo, ToolSpec, default_namespace_description,
    dynamic_tool_to_responses_api_tool,
};
use pyo3::prelude::*;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::sync::{Mutex as AsyncMutex, mpsc, oneshot, watch};

struct Entry {
    name: ToolName,
    spec: ToolSpec,
    exposure: ToolExposure,
    manual: bool,
    finish_turn: bool,
    input_error_recovery: bool,
}

pub(crate) struct Generation {
    pub(crate) id: u64,
    entries: Vec<Entry>,
    specs: Vec<DynamicToolSpec>,
}

/// Immutable per-profile declarations captured before Core advertises a step.
#[derive(Clone)]
pub(crate) struct ProfileCatalog {
    pub(crate) generation: Arc<Generation>,
    pub(crate) defaults: Option<Vec<ToolName>>,
    pub(crate) handoff_defaults: Option<Vec<ToolName>>,
    pub(crate) selecting_steps: bool,
    pub(crate) transform_context: bool,
    pub(crate) before_compact: bool,
    pub(crate) before_complete: bool,
    pub(crate) tool_batch_completion: bool,
}

impl ProfileCatalog {
    pub(crate) fn default_names(&self, slots: Option<&[usize]>) -> Result<Option<Vec<ToolName>>> {
        self.generation.default_names(slots)
    }
    pub(crate) fn checkpoint_specs(&self) -> Result<Value> {
        checkpoint_specs(&self.generation.specs)
    }
}

struct StepReply {
    reply: oneshot::Sender<Option<Vec<ToolName>>>,
    stop: crate::native_interception::CapturedTurnStop,
}

pub(crate) struct FinishToolSlots(pub(crate) Vec<usize>);
pub(crate) struct InputErrorToolSlots(pub(crate) Vec<usize>);

impl Generation {
    fn default_names(&self, defaults: Option<&[usize]>) -> Result<Option<Vec<ToolName>>> {
        defaults
            .map(|slots| {
                let mut seen = HashSet::new();
                slots
                    .iter()
                    .map(|slot| {
                        ensure!(seen.insert(*slot), "Duplicate profile default tool slot");
                        self.entries
                            .get(*slot)
                            .map(|entry| entry.name.clone())
                            .context("Invalid profile default tool slot")
                    })
                    .collect::<Result<Vec<_>>>()
            })
            .transpose()
    }

    fn set_finish(&mut self, slots: &[usize]) -> Result<()> {
        for &slot in slots {
            let entry = self
                .entries
                .get_mut(slot)
                .context("Invalid terminal tool slot")?;
            ensure!(!entry.finish_turn, "Duplicate terminal tool slot");
            entry.finish_turn = true;
        }
        Ok(())
    }

    fn set_input_errors(&mut self, slots: &[usize]) -> Result<()> {
        for &slot in slots {
            let entry = self
                .entries
                .get_mut(slot)
                .context("Invalid input recovery slot")?;
            ensure!(!entry.input_error_recovery, "Duplicate input recovery slot");
            entry.input_error_recovery = true;
        }
        Ok(())
    }

    fn set_manual(&mut self, slots: &[usize]) -> Result<()> {
        for &slot in slots {
            let entry = self
                .entries
                .get_mut(slot)
                .ok_or_else(|| anyhow::anyhow!("Invalid manual tool slot"))?;
            ensure!(!entry.manual, "Duplicate manual tool slot");
            entry.manual = true;
        }
        Ok(())
    }
}

// No Python constructor or serialization. The weak owner avoids retaining a
// session through application-held requests after its native waiter is gone.
#[derive(Clone)]
#[pyclass(frozen, module = "agents._native")]
pub(crate) struct ManualToolGrant {
    pub(crate) owner: ManualOwner,
    pub(crate) key: PendingKey,
    pub(crate) invocation: u64,
}

#[derive(Clone)]
pub(crate) enum ManualOwner {
    Core(Weak<PythonTools>),
}

#[pymethods]
impl ManualToolGrant {
    fn __repr__(&self) -> &'static str {
        "<native manual tool grant>"
    }
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, Deserialize, Serialize)]
pub struct ReplyKey {
    pub thread: String,
    pub generation: u64,
    pub slot: usize,
    // A durable child thread may have a new runtime after eviction or reopen.
    // Zero preserves the root's existing wire format.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub instance: Option<String>,
}

// turn/call come from the original request, not a name lookup at response time.
#[derive(Clone, Debug, Hash, PartialEq, Eq, Deserialize, Serialize)]
pub(crate) struct PendingKey {
    #[serde(rename = "_sdk_python_tool")]
    pub(crate) reply: ReplyKey,
    #[serde(rename = "turnId")]
    pub(crate) turn: String,
    #[serde(rename = "callId")]
    pub(crate) call: String,
}

/// Source attribution retained by a child; it conveys no native turn or reply grants.
#[derive(Clone)]
struct DescendantSource {
    catalog: Option<Arc<ProfileCatalog>>,
    profile: Option<codex_extension_api::AgentProfileReceipt>,
    mcp_selection: Option<Arc<crate::mcp_result_control::Selection>>,
    mcp_policy: Option<Arc<crate::native_interception::ProfileMcpPolicy>>,
}

#[derive(Clone)]
struct DescendantProfileSource(codex_extension_api::AgentProfileReceipt);

pub struct PythonTools {
    current: RwLock<Arc<Generation>>,
    generation_high_water: AtomicU64,
    profile_catalogs: RwLock<HashMap<u64, Weak<Generation>>>,
    managed_profiles: AtomicBool,
    descendant_source: Option<DescendantSource>,
    fixed: HashSet<ToolName>,
    policy: Arc<ToolPolicy>,
    pending: Mutex<HashMap<PendingKey, PendingCall>>,
    failed_turns: Mutex<HashSet<String>>,
    family: Arc<PythonFamily>,
    instance: Option<String>,
    suspended: AtomicBool,
    manual_restore: Mutex<Option<crate::manual_checkpoint::WeakOwner>>,
    manual_progress: watch::Sender<u64>,
    admission: Mutex<Option<Arc<TurnAdmission>>>,
    // A live native thread owns both. Keep no family-to-thread ownership cycle.
    interception: OnceLock<Weak<crate::native_interception::Control>>,
    model_producer: Mutex<model_producer::State>,
    prepared_requests: Mutex<prepared_requests::State>,
    selecting_steps: AtomicBool,
    selecting_children: AtomicBool,
    context_transform_enabled: bool,
    before_compact_enabled: bool,
    before_complete_enabled: bool,
    pub(crate) tool_batch_completion_enabled: bool,
    step_defaults: RwLock<Option<Vec<ToolName>>>,
    // None retires this native thread's selectors. Registration and retirement
    // share the lock so a late step cannot wait on an already closed reader.
    preparation_replies: Mutex<Option<HashMap<u64, PreparationSlot>>>,
    preparations_stopped: AtomicBool,
    #[cfg(test)]
    pub(crate) before_result: Mutex<Option<Arc<manual_checkpoint_tests::BeforeResult>>>,
}

pub(crate) enum ModelContextUpdate {
    Keep,
    Replace(Vec<ResponseItem>),
    Hosted(HostedContextProjection),
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct HostedContextProjection {
    pub(crate) hosted_prompt: codex_protocol::model_request_controls::HostedPromptReference,
    #[serde(default)]
    pub(crate) items: Option<Vec<ResponseItem>>,
    #[serde(default)]
    pub(crate) explicit_tools: bool,
}

#[derive(Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum ToolCompletionReply {
    Accept { final_output: Value },
    Continue { feedback: Option<String> },
}

enum PreparationReply {
    ModelRetry(
        oneshot::Sender<std::result::Result<codex_protocol::model_retry::ModelRetryResolution, ()>>,
    ),
    PreparedRequest {
        ticket: Arc<prepared_requests::Ticket>,
        reply: oneshot::Sender<prepared_requests::Reply>,
    },
    ModelProducer {
        ticket: Arc<model_producer::Ticket>,
        reply: model_producer::Delivery,
    },
    ToolCompletion {
        reply: oneshot::Sender<std::result::Result<ToolCompletionReply, ()>>,
        stop: crate::native_interception::CapturedTurnStop,
    },
    Step(StepReply),
    Context(oneshot::Sender<Option<ModelContextUpdate>>),
    Compaction(oneshot::Sender<std::result::Result<Option<String>, ()>>),
    Completion {
        reply: oneshot::Sender<std::result::Result<Option<String>, ()>>,
        stop: crate::native_interception::CapturedTurnStop,
    },
}

impl PreparationReply {
    fn is_closed(&self) -> bool {
        match self {
            Self::ModelRetry(reply) => reply.is_closed(),
            Self::PreparedRequest { ticket, reply } => reply.is_closed() || !ticket.admits(),
            Self::ModelProducer { ticket, reply } => reply.is_closed() || !ticket.admits(),
            Self::ToolCompletion { reply, .. } => reply.is_closed(),
            Self::Step(reply) => reply.reply.is_closed(),
            Self::Context(reply) => reply.is_closed(),
            Self::Compaction(reply) => reply.is_closed(),
            Self::Completion { reply, .. } => reply.is_closed(),
        }
    }
}

struct PreparationSlot {
    turn: String,
    reply: Option<PreparationReply>,
    callback: Arc<CallbackRelease>,
}
impl PreparationSlot {
    fn is_closed(&self) -> bool {
        !self.callback.admits_reply() || self.reply.as_ref().is_none_or(PreparationReply::is_closed)
    }
}

/// Add only native identities retained by the producing profile, never all candidates.
fn merge_profile_native_defaults(
    code_mode_enabled: bool,
    mut ordinary: Vec<ToolName>,
    native: impl IntoIterator<Item = ToolName>,
    candidates: &[ToolName],
    policy: &ToolPolicy,
) -> Vec<ToolName> {
    let candidates = candidates
        .iter()
        .cloned()
        .map(ToolName::with_default_namespace)
        .collect::<HashSet<_>>();
    let mut seen = ordinary
        .iter()
        .cloned()
        .map(ToolName::with_default_namespace)
        .collect::<HashSet<_>>();
    let mut native = native
        .into_iter()
        .chain(
            code_mode_enabled
                .then_some([ToolName::plain("exec"), ToolName::plain("wait")])
                .into_iter()
                .flatten(),
        )
        .map(ToolName::with_default_namespace)
        .collect::<Vec<_>>();
    native.sort_by(|a, b| a.namespace.cmp(&b.namespace).then(a.name.cmp(&b.name)));
    for name in native {
        if policy.allows(&name) && candidates.contains(&name) && seen.insert(name.clone()) {
            ordinary.push(name);
        }
    }
    ordinary
}

struct PendingPreparation {
    tools: Arc<PythonTools>,
    id: u64,
}

impl Drop for PendingPreparation {
    fn drop(&mut self) {
        self.tools.retire_preparation(self.id, false);
    }
}

enum QueuedRequest {
    Callback(Value),
    Step(Value),
    Call(Weak<PythonTools>, PendingKey, u64, Value),
    Retired {
        thread: String,
        incarnation: String,
    },
    GracefulFailure(Weak<PythonTools>, Weak<TurnAdmission>),
    ToolCompletionFailure(Weak<PythonTools>, Weak<TurnAdmission>),
    ModelFailure {
        owner: Weak<PythonTools>,
        receipt: Weak<TurnAdmission>,
        thread: String,
        policy_message: Option<&'static str>,
        response_integrity_reason: Option<codex_protocol::error::ResponseIntegrityReason>,
    },
}

// This inbox carries requests, never turn admission or response senders.
// Those remain scoped to the individual thread's PythonTools.
struct PythonFamily {
    members: RwLock<HashMap<String, Weak<PythonTools>>>,
    // This lock also serializes the turn-receipt installation against an
    // all-scope interrupt. No native await is allowed while holding it.
    graph: Mutex<GraphInterruption>,
    graph_changed: watch::Sender<()>,
    next_invocation: AtomicU64,
    requests: mpsc::UnboundedSender<QueuedRequest>,
    receiver: AsyncMutex<mpsc::UnboundedReceiver<QueuedRequest>>,
}

#[derive(Default)]
struct GraphInterruption {
    fenced: bool,
    pending_stops: usize,
    failure: Option<&'static str>,
}

pub(crate) struct GraphStopRequest {
    family: Arc<PythonFamily>,
}

impl Drop for GraphStopRequest {
    fn drop(&mut self) {
        self.family
            .graph
            .lock()
            .expect("Python graph poisoned")
            .pending_stops -= 1;
        self.family.graph_changed.send_replace(());
    }
}

impl Default for PythonFamily {
    fn default() -> Self {
        let (requests, receiver) = mpsc::unbounded_channel();
        Self {
            members: RwLock::new(HashMap::new()),
            graph: Mutex::new(GraphInterruption::default()),
            graph_changed: watch::channel(()).0,
            next_invocation: AtomicU64::new(1),
            requests,
            receiver: AsyncMutex::new(receiver),
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum AdmissionState {
    Pending,
    Ready,
    Closed,
}

const LOCAL_COMMAND_CLEANUP_FAILED: &str = "Interrupted local command cleanup did not finish";

// Core emits TurnComplete before releasing its active-turn slot. This receipt
// proves only that another turn may be admitted, not that the thread has closed.
pub(crate) struct TurnAdmission {
    model_attempt: Mutex<Option<Arc<dyn codex_extension_api::ModelAttemptAdmission>>>,
    thread_id: String,
    pub(crate) tracing: Option<Arc<codex_otel::sdk_trace::ContextLease>>,
    turn_id: String,
    owner: Weak<PythonTools>,
    use_deferred_specs: bool,
    prepared_operation: Option<u64>,
    state: watch::Sender<AdmissionState>,
    completed_calls: Mutex<HashSet<String>>,
    held_tool_outputs: Arc<codex_extension_api::HeldToolCompletionResults>,
    graceful_requested: AtomicBool,
    sampling_stop: Arc<TurnSamplingStop>,
    // A turn born while the graph is stopped must stay fenced even if a
    // later explicit submission starts a new graph generation.
    graph_fenced: bool,
    // Idle notification and terminal delivery are different native boundaries.
    // Reopening must never trust an idle hint alone for a fenced new turn.
    terminal: AtomicBool,
    tool_completion_proposed: AtomicBool,
    tool_completion_failed: AtomicBool,
    local_command_cleanup_failed: AtomicBool,
    input_recorded: AtomicBool,
    // Private namespace for SDK steers of this exact runtime, including same-ID recovery.
    steering_namespace: String,
    steering_tail: AtomicBool,
    exact_steering: OnceLock<bool>,
    provider_error: Mutex<Option<(Value, Value)>>,
    // An unseeded event cannot distinguish a queued old same-ID lifecycle.
    same_id_recovery: bool,
    policy_failure: OnceLock<&'static str>,
}

tokio::task_local! {
    // Core checks idle and then awaits contributors on this same task.
    // A new receipt can arrive in between; explicit probes never own it.
    static IDLE_PROBE_RECEIPT: Arc<TurnAdmission>;
}

impl TurnAdmission {
    pub(crate) fn callback_tools(&self) -> Option<Arc<PythonTools>> {
        self.owner.upgrade()
    }

    pub(crate) fn model_attempt_snapshot(
        &self,
    ) -> Result<Option<codex_protocol::model_retry::ModelAttemptSnapshot>> {
        let attempt = self
            .model_attempt
            .lock()
            .expect("Model attempt mirror poisoned")
            .clone();
        attempt
            .map(|attempt| attempt.snapshot().map_err(anyhow::Error::msg))
            .transpose()
    }
    pub(crate) fn permits_integrity_reason(&self) -> bool {
        !self.same_id_recovery
    }

    pub(crate) fn is_current(self: &Arc<Self>) -> bool {
        self.owner
            .upgrade()
            .is_some_and(|owner| owner.owns_admission(self))
    }

    pub(crate) fn attach_provider_error(&self, event: &mut Value) {
        if event["id"].as_str() != Some(self.turn_id.as_str())
            || !matches!(
                event["msg"]["type"].as_str(),
                Some("task_complete" | "turn_complete")
            )
        {
            return;
        }
        let Some(error) = event["msg"]["error"].as_object_mut() else {
            return;
        };
        let captured = self
            .provider_error
            .lock()
            .expect("Provider error receipt poisoned");
        if let Some((category, metadata)) = captured.as_ref()
            && error.get("codex_error_info") == Some(category)
        {
            error.insert("_sdk_provider_error".into(), metadata.clone());
        }
    }

    pub(crate) fn attach_policy_failure(&self, event: &mut Value) {
        if event["id"].as_str() == Some(self.turn_id.as_str())
            && matches!(
                event["msg"]["type"].as_str(),
                Some("task_complete" | "turn_complete")
            )
            && let Some(message) = self.policy_failure.get()
        {
            event["msg"]["_sdk_policy_failure"] = json!(message);
        }
    }

    pub(crate) fn reject_model(
        self: &Arc<Self>,
        thread: &str,
        reason: codex_protocol::error::ResponseIntegrityReason,
    ) {
        if let Some(owner) = self.owner.upgrade() {
            // Core drains tool futures before reporting the model error. Wake
            // the SDK's existing failure settlement so it can interrupt the
            // real turn and join callbacks, without claiming native completion.
            let _ = owner.family.requests.send(QueuedRequest::ModelFailure {
                owner: Arc::downgrade(&owner),
                receipt: Arc::downgrade(self),
                thread: thread.to_owned(),
                policy_message: None,
                response_integrity_reason: Some(reason),
            });
        }
    }

    pub(crate) fn reject_policy(self: &Arc<Self>, thread: &str, message: &'static str) {
        // The early notice can race terminal delivery. Retain its reason on
        // this admission so that the owning child can report it either way.
        let message = *self.policy_failure.get_or_init(|| message);
        if let Some(owner) = self.owner.upgrade() {
            let _ = owner.family.requests.send(QueuedRequest::ModelFailure {
                owner: Arc::downgrade(&owner),
                receipt: Arc::downgrade(self),
                thread: thread.to_owned(),
                policy_message: Some(message),
                response_integrity_reason: None,
            });
        }
    }

    pub(crate) fn steering_client_id(&self) -> String {
        format!(
            "{}{}",
            self.steering_namespace,
            codex_protocol::ThreadId::new()
        )
    }

    pub(crate) fn root_steering_client_id(&self, exact: bool) -> Result<String> {
        ensure!(
            *self.exact_steering.get_or_init(|| exact) == exact,
            "Cannot mix exact and continuation steering on a native turn"
        );
        Ok(if exact {
            format!("agents-exact-steer:{}", codex_protocol::ThreadId::new())
        } else {
            self.steering_client_id()
        })
    }

    fn record_steering(&self, client_id: Option<&str>) {
        if client_id.is_some_and(|id| id.starts_with(&self.steering_namespace)) {
            self.steering_tail.store(true, Ordering::Release);
        }
    }

    pub(crate) fn generation(&self) {
        self.tool_completion_proposed
            .store(false, Ordering::Release);
        self.steering_tail.store(false, Ordering::Release);
    }

    pub(crate) fn has_steering_tail(&self) -> bool {
        self.steering_tail.load(Ordering::Acquire)
    }

    pub(crate) async fn wait(&self) -> Result<()> {
        let mut state = self.state.subscribe();
        loop {
            match *state.borrow_and_update() {
                AdmissionState::Ready => return Ok(()),
                AdmissionState::Closed => anyhow::bail!("Native turn admission receipt closed"),
                AdmissionState::Pending => {}
            }
            state.changed().await?;
        }
    }

    pub(crate) fn check_local_command_cleanup(&self) -> Result<()> {
        ensure!(
            !self.local_command_cleanup_failed.load(Ordering::Acquire),
            LOCAL_COMMAND_CLEANUP_FAILED
        );
        Ok(())
    }

    async fn probe_idle(self: &Arc<Self>, thread: &CodexThread) -> Result<()> {
        self.check_local_command_cleanup()?;
        IDLE_PROBE_RECEIPT
            .scope(
                Arc::clone(self),
                thread.emit_thread_idle_lifecycle_if_idle(
                    codex_extension_api::ThreadIdleCause::Interrupted,
                ),
            )
            .await;
        Ok(())
    }
}

// This is selected by the trusted thread startup ceiling, not by a model item
// or a checkpoint field. The ceiling can never grant any Core builtin.
pub(crate) fn sdk_name() -> ToolName {
    ToolName::namespaced("_agents_sdk", "dispatch")
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Phase {
    Admitted,
    Entered,
    Parked,
}

/// Private trusted SDK reply envelope. Content JSON never supplies this disposition.
#[derive(Debug)]
pub(crate) struct ToolReply {
    pub(crate) response: DynamicToolResponse,
    pub(crate) start_response: bool,
    pub(crate) acceptance: Option<oneshot::Sender<()>>,
}

impl From<DynamicToolResponse> for ToolReply {
    fn from(response: DynamicToolResponse) -> Self {
        Self {
            response,
            start_response: true,
            acceptance: None,
        }
    }
}

/// Physical callback ownership attached to an existing native pending entry.
struct CallbackRelease {
    receipt: Value,
    model_attempt: Option<Arc<dyn codex_extension_api::ModelAttemptAdmission>>,
    entered: std::sync::atomic::AtomicBool,
    cancelled: std::sync::atomic::AtomicBool,
    released: std::sync::atomic::AtomicBool,
    cleanup_failed: std::sync::atomic::AtomicBool,
    changed: tokio::sync::Notify,
}
impl CallbackRelease {
    fn new(receipt: Value) -> Arc<Self> {
        Self::with_model_attempt(receipt, None)
    }
    fn with_model_attempt(
        receipt: Value,
        model_attempt: Option<Arc<dyn codex_extension_api::ModelAttemptAdmission>>,
    ) -> Arc<Self> {
        Arc::new(Self {
            receipt,
            model_attempt,
            entered: std::sync::atomic::AtomicBool::new(false),
            cancelled: std::sync::atomic::AtomicBool::new(false),
            released: std::sync::atomic::AtomicBool::new(false),
            cleanup_failed: std::sync::atomic::AtomicBool::new(false),
            changed: tokio::sync::Notify::new(),
        })
    }
    fn admits_reply(&self) -> bool {
        !self.cancelled.load(Ordering::Acquire)
            && !self.released.load(Ordering::Acquire)
            && self.model_attempt.as_ref().is_none_or(|attempt| {
                !attempt.cancellation().is_cancelled() && !attempt.sampling_stop().requested()
            })
    }
    fn release(&self) -> bool {
        let first = !self.released.swap(true, Ordering::AcqRel);
        self.changed.notify_waiters();
        first
    }
    async fn wait(&self) {
        loop {
            let changed = self.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            if self.released.load(Ordering::Acquire) {
                return;
            }
            changed.await;
        }
    }
}
fn closed_tool_sender() -> oneshot::Sender<ToolReply> {
    let (sender, receiver) = oneshot::channel();
    drop(receiver);
    sender
}

#[derive(Clone)]
pub(crate) struct NativeMediaContext {
    pub(crate) inline_data_url_low_detail_supported: bool,
    pub(crate) model_info: Arc<codex_protocol::openai_models::ModelInfo>,
    pub(crate) profile: codex_protocol::provider_reference_profile::ProviderImageReferencePolicy,
}

struct PendingCall {
    // Captured by native enqueue, never reconstructed from a host reply.
    computer_arguments: Option<String>,
    callback: Option<Arc<CallbackRelease>>,
    // Captured at enqueue; never borrow a successor turn during admission.
    deadline: Option<Arc<codex_extension_api::ModelInvocationDeadlineState>>,
    media: Option<NativeMediaContext>,
    _tracing: Option<Arc<codex_otel::sdk_trace::ContextLease>>,
    invocation: u64,
    retired: bool,
    manual_request: Option<Value>,
    approval_request: Option<Value>,
    sender: oneshot::Sender<ToolReply>,
    phase: Phase,
    // Imported approvals have no live Core turn. Only their current binding
    // can exempt them from response-control's live-turn capture check.
    reentry: bool,
    continuation: Option<Value>,
}

fn foreground_response_default() -> bool {
    true
}

#[derive(Deserialize)]
#[serde(untagged)]
pub(crate) enum Reply {
    Output {
        request: PendingKey,
        response: DynamicToolResponse,
        #[serde(default = "foreground_response_default")]
        start_response: bool,
    },
    Park {
        request: PendingKey,
        park: bool,
    },
    Continuation {
        request: PendingKey,
        continuation: Value,
    },
}

fn functions(
    specs: &[DynamicToolSpec],
) -> Vec<(
    Option<&DynamicToolNamespaceSpec>,
    Cow<'_, DynamicToolFunctionSpec>,
)> {
    let mut functions = Vec::new();
    for spec in specs {
        match spec {
            DynamicToolSpec::Function(tool) => functions.push((None, Cow::Borrowed(tool))),
            DynamicToolSpec::Computer(tool) => {
                functions.push((None, Cow::Owned(tool.callback_spec())))
            }
            DynamicToolSpec::ToolSearch(tool) => {
                functions.push((None, Cow::Owned(tool.callback_spec())))
            }
            DynamicToolSpec::Namespace(namespace) => {
                for DynamicToolNamespaceTool::Function(tool) in &namespace.tools {
                    functions.push((Some(namespace), Cow::Borrowed(tool)));
                }
            }
        }
    }
    functions
}

fn validate_slots(specs: &[DynamicToolSpec]) -> Result<()> {
    ensure!(
        specs.iter().all(|spec| match spec {
            DynamicToolSpec::Function(_)
            | DynamicToolSpec::Computer(_)
            | DynamicToolSpec::ToolSearch(_) => true,
            DynamicToolSpec::Namespace(ns) => ns.tools.len() == 1,
        }),
        "invalid Python tool slot projection"
    );
    Ok(())
}

// Declaration-only admission. Fixed history and ToolPolicy remain the live
// thread's authority, including on every replacement.
pub fn validate_declarations(specs: &[DynamicToolSpec]) -> Result<()> {
    validate_slots(specs)?;
    for declaration in specs {
        match declaration {
            DynamicToolSpec::Computer(computer) => {
                codex_tools::computer_tool_spec(computer).map_err(anyhow::Error::msg)?;
            }
            DynamicToolSpec::ToolSearch(search) => {
                codex_tools::client_tool_search_spec(search).map_err(anyhow::Error::msg)?;
            }
            _ => {}
        }
    }
    let mut seen = HashSet::new();
    for (declaration, (namespace, tool)) in specs.iter().zip(functions(specs)) {
        let key = ToolName::new(namespace.map(|ns| ns.name.clone()), tool.name.clone())
            .with_default_namespace();
        ensure!(seen.insert(key.clone()), "Duplicate Python tool: {key}");
        ensure!(
            !(key.is_default_namespace()
                && !matches!(declaration, DynamicToolSpec::ToolSearch(_))
                && matches!(
                    key.name.as_str(),
                    "exec_command"
                        | "shell_command"
                        | "exec"
                        | "wait"
                        | "tool_search"
                        | "apply_patch"
                        | "write_stdin"
                ))
                && key.namespace.as_deref() != Some("tool_search")
                && !matches!(
                    (key.namespace.as_deref(), key.name.as_str()),
                    (Some("web"), "run") | (Some("image_gen"), "imagegen")
                ),
            "Python tool uses a reserved name: {key}"
        );
    }
    Ok(())
}

// Return the actual model parameter schemas before the caller elects to publish
// a candidate. Names, policy, and generation remain the live thread's authority.
pub fn model_parameters(specs: Vec<DynamicToolSpec>) -> Result<Value> {
    validate_slots(&specs)?;
    let parameters = functions(&specs)
        .into_iter()
        .map(|(_, tool)| dynamic_tool_to_responses_api_tool(&tool).map(|tool| tool.parameters))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(serde_json::to_value(parameters)?)
}

fn tool_names(specs: &[DynamicToolSpec]) -> HashSet<ToolName> {
    functions(specs)
        .into_iter()
        .map(|(namespace, tool)| {
            ToolName::new(namespace.map(|ns| ns.name.clone()), tool.name.clone())
                .with_default_namespace()
        })
        .collect()
}

// Portable SDK v2 specs include deferLoading even when false. Core omits that
// default on serialization. Keep the exact existing SDK representation (also
// for namespace members), without preserving any unknown authority fields.
pub fn checkpoint_specs(specs: &[DynamicToolSpec]) -> Result<Value> {
    let mut value = serde_json::to_value(specs)?;
    for spec in value.as_array_mut().expect("serialized specs array") {
        if let Some(tools) = spec.get_mut("tools").and_then(Value::as_array_mut) {
            for tool in tools {
                tool.as_object_mut()
                    .expect("serialized namespace tool")
                    .entry("deferLoading")
                    .or_insert(Value::Bool(false));
            }
        } else if spec.get("type").and_then(Value::as_str) == Some("function") {
            spec.as_object_mut()
                .expect("serialized tool")
                .entry("deferLoading")
                .or_insert(Value::Bool(false));
        }
    }
    Ok(value)
}

impl PythonTools {
    pub(crate) fn callback_operation(
        self: &Arc<Self>,
        root: &str,
        receipt: &Value,
        operation: &str,
    ) -> bool {
        if receipt.get("kind").and_then(Value::as_str) != Some("tool") {
            return self.preparation_callback_operation(root, receipt, operation);
        }
        let Ok(key) = serde_json::from_value::<PendingKey>(
            receipt.get("request").cloned().unwrap_or(Value::Null),
        ) else {
            return false;
        };
        let Some(owner) = self.owner(root, &key) else {
            return false;
        };
        let mut pending = owner.pending.lock().expect("callback pending poisoned");
        let Some(call) = pending.get_mut(&key) else {
            return false;
        };
        let Some(callback) = call
            .callback
            .as_ref()
            .filter(|callback| callback.receipt == *receipt)
        else {
            return false;
        };
        match operation {
            "enter" => {
                if call.sender.is_closed()
                    || callback.cancelled.load(Ordering::Acquire)
                    || callback.released.load(Ordering::Acquire)
                {
                    return false;
                }
                callback.entered.store(true, Ordering::Release);
                true
            }
            "cancelled" => callback.cancelled.load(Ordering::Acquire),
            "release" | "release_failed" => {
                let released = callback.release();
                // Restored approval waiters have no live PendingReply drop guard.
                // The accepted receipt is recorded before physical callback release.
                if call.reentry && call.approval_request.is_some() && call.sender.is_closed() {
                    pending.remove(&key);
                }
                released
            }
            _ => false,
        }
    }
    fn cancel_callback(&self, key: &PendingKey, callback: &Arc<CallbackRelease>) {
        if !callback.cancelled.swap(true, Ordering::AcqRel) {
            let mut notice = json!({"id":key.turn,"msg":{"type":"_sdk_callback_cancel","receipt":callback.receipt}});
            if let Some(instance) = &key.reply.instance {
                notice["subagent_id"] = json!(key.reply.thread);
                notice["subagent_incarnation"] = json!(instance);
            }
            let _ = self.family.requests.send(QueuedRequest::Callback(notice));
        }
        if !callback.entered.load(Ordering::Acquire) {
            callback.release();
        }
    }
    fn retire_callback(&self, key: &PendingKey, invocation: u64, completed: bool) {
        let mut pending = self.pending.lock().expect("callback pending poisoned");
        let Some(call) = pending.get_mut(key) else {
            return;
        };
        if call.invocation != invocation {
            return;
        }
        call.retired = true;
        let Some(callback) = call.callback.clone() else {
            pending.remove(key);
            return;
        };
        if !completed {
            drop(std::mem::replace(&mut call.sender, closed_tool_sender()));
            self.cancel_callback(key, &callback);
        }
        if !callback.entered.load(Ordering::Acquire) {
            callback.release();
            pending.remove(key);
        }
    }
    async fn finish_callbacks(&self, turn: Option<&str>, call_id: Option<&str>, cancel: bool) {
        let callbacks: Vec<_> = {
            let mut pending = self.pending.lock().expect("callback pending poisoned");
            pending
                .iter_mut()
                .filter(|(key, call)| {
                    turn.is_none_or(|turn| key.turn == turn)
                        && call_id.is_none_or(|id| key.call == id)
                        && (cancel || call.retired)
                })
                .filter_map(|(key, call)| {
                    call.callback.clone().map(|callback| {
                        if cancel {
                            drop(std::mem::replace(&mut call.sender, closed_tool_sender()));
                            self.cancel_callback(key, &callback);
                        }
                        if !callback.entered.load(Ordering::Acquire) {
                            callback.release();
                        }
                        (key.clone(), callback)
                    })
                })
                .collect()
        };
        for (key, callback) in callbacks {
            callback.wait().await;
            let mut pending = self.pending.lock().expect("callback pending poisoned");
            if pending
                .get(&key)
                .and_then(|call| call.callback.as_ref())
                .is_some_and(|current| Arc::ptr_eq(current, &callback))
            {
                pending.remove(&key);
            }
        }
    }
    fn preparation_callback_owner(
        self: &Arc<Self>,
        root: &str,
        receipt: &Value,
    ) -> Option<(Arc<Self>, u64)> {
        if receipt.get("kind").and_then(Value::as_str) != Some("preparation") {
            return None;
        }
        let Some(thread) = receipt.get("thread").and_then(Value::as_str) else {
            return None;
        };
        let instance = receipt.get("instance").and_then(Value::as_str);
        let Some(id) = receipt
            .get("id")
            .and_then(Value::as_str)
            .and_then(|s| s.parse::<u64>().ok())
        else {
            return None;
        };
        let Ok(owner) = self.step_owner(Some(thread), instance) else {
            return None;
        };
        if thread != root && owner.instance.is_none() {
            return None;
        }
        Some((owner, id))
    }

    pub(crate) fn report_model_attempt_usage(
        self: &Arc<Self>,
        root: &str,
        attempt_id: &str,
        receipt: &Value,
        usage: codex_protocol::protocol::TokenUsage,
    ) -> Result<bool> {
        let Some((owner, id)) = self.preparation_callback_owner(root, receipt) else {
            return Ok(false);
        };
        let replies = owner
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        let Some(slot) = replies.as_ref().and_then(|pending| pending.get(&id)) else {
            return Ok(false);
        };
        let callback = &slot.callback;
        // Cancellation revokes execution, but an entered worker can still measure its own
        // cleanup before physical release. Never consult a later current-attempt slot.
        if callback.receipt != *receipt
            || !callback.entered.load(Ordering::Acquire)
            || callback.released.load(Ordering::Acquire)
        {
            return Ok(false);
        }
        let Some(attempt) = &callback.model_attempt else {
            return Ok(false);
        };
        if attempt.id() != attempt_id {
            return Ok(false);
        }
        attempt.report_usage(usage).map_err(anyhow::Error::msg)?;
        Ok(true)
    }

    fn preparation_callback_operation(
        self: &Arc<Self>,
        root: &str,
        receipt: &Value,
        operation: &str,
    ) -> bool {
        let Some((owner, id)) = self.preparation_callback_owner(root, receipt) else {
            return false;
        };
        let mut replies = owner
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        let Some(pending) = replies.as_mut() else {
            return false;
        };
        let Some(slot) = pending.get(&id) else {
            return false;
        };
        if slot.callback.receipt != *receipt {
            return false;
        }
        let callback = slot.callback.clone();
        match operation {
            "enter" => {
                if slot.is_closed()
                    || callback.cancelled.load(Ordering::Acquire)
                    || callback.released.load(Ordering::Acquire)
                {
                    return false;
                }
                !callback.entered.swap(true, Ordering::AcqRel)
            }
            "cancelled" => callback.cancelled.load(Ordering::Acquire),
            "release" | "release_failed" => {
                if operation == "release_failed" {
                    callback.cleanup_failed.store(true, Ordering::Release);
                }
                callback.release();
                if slot.is_closed()
                    && !(callback.model_attempt.is_some()
                        && callback.cleanup_failed.load(Ordering::Acquire))
                {
                    pending.remove(&id);
                }
                true
            }
            _ => false,
        }
    }

    fn preparation_callback(&self, id: u64, request: &mut Value) -> Arc<CallbackRelease> {
        self.model_preparation_callback(id, request, None)
    }

    fn model_preparation_callback(
        &self,
        id: u64,
        request: &mut Value,
        model_attempt: Option<Arc<dyn codex_extension_api::ModelAttemptAdmission>>,
    ) -> Arc<CallbackRelease> {
        let receipt = json!({"kind":"preparation", "thread":request["msg"]["thread"],
            "instance":self.instance, "id":id.to_string()});
        request["msg"]["_sdk_callback"] = receipt.clone();
        CallbackRelease::with_model_attempt(receipt, model_attempt)
    }

    fn cancel_preparation(&self, callback: &CallbackRelease, turn: &str) {
        if !callback.cancelled.swap(true, Ordering::AcqRel) {
            let mut notice =
                json!({"id":turn,"msg":{"type":"_sdk_callback_cancel","receipt":callback.receipt}});
            if let Some(instance) = &self.instance {
                notice["subagent_id"] = callback.receipt["thread"].clone();
                notice["subagent_incarnation"] = json!(instance);
            }
            let _ = self.family.requests.send(QueuedRequest::Callback(notice));
        }
        if !callback.entered.load(Ordering::Acquire) {
            callback.release();
        }
    }

    fn retire_preparation(&self, id: u64, physical_joined: bool) {
        let mut replies = self
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        let Some(pending) = replies.as_mut() else {
            return;
        };
        let Some(slot) = pending.get_mut(&id) else {
            return;
        };
        slot.reply.take();
        if !slot.callback.released.load(Ordering::Acquire) {
            self.cancel_preparation(&slot.callback, &slot.turn);
        }
        if slot.callback.released.load(Ordering::Acquire)
            && (physical_joined
                || slot.callback.model_attempt.is_none()
                || !slot.callback.cleanup_failed.load(Ordering::Acquire))
        {
            pending.remove(&id);
        }
    }

    async fn await_preparation<T>(
        &self,
        id: u64,
        callback: &Arc<CallbackRelease>,
        receive: oneshot::Receiver<T>,
        cancellation: Option<tokio_util::sync::CancellationToken>,
    ) -> Result<T> {
        let result = if let Some(cancellation) = cancellation {
            tokio::select! {
                biased;
                _ = cancellation.cancelled() => {
                    self.retire_preparation(id, false);
                    Err(anyhow::anyhow!("Preparation callback cancelled"))
                }
                result = receive => result.context("Preparation callback cancelled"),
            }
        } else {
            receive.await.context("Preparation callback cancelled")
        };
        if result.is_err() {
            self.retire_preparation(id, false);
        } else {
            // Reply authority is already consumed. An unclaimed native-only reply
            // cannot have started application code, and future enter is fenced.
            let replies = self
                .preparation_replies
                .lock()
                .expect("Python preparation replies poisoned");
            if replies
                .as_ref()
                .and_then(|pending| pending.get(&id))
                .is_some_and(|slot| slot.is_closed())
                && !callback.entered.load(Ordering::Acquire)
            {
                callback.release();
            }
        }
        callback.wait().await;
        result
    }

    pub(crate) async fn retire_model_attempt_preparations(
        &self,
        attempt: &Arc<dyn codex_extension_api::ModelAttemptAdmission>,
    ) -> Result<()> {
        let callbacks = {
            let mut replies = self
                .preparation_replies
                .lock()
                .expect("Python preparation replies poisoned");
            let Some(pending) = replies.as_mut() else {
                return Ok(());
            };
            pending
                .iter_mut()
                .filter_map(|(id, slot)| {
                    let captured = slot.callback.model_attempt.as_ref()?;
                    if !Arc::ptr_eq(captured, attempt) {
                        return None;
                    }
                    slot.reply.take();
                    self.cancel_preparation(&slot.callback, &slot.turn);
                    Some((*id, slot.callback.clone()))
                })
                .collect::<Vec<_>>()
        };
        let mut failed = false;
        for (id, callback) in callbacks {
            callback.wait().await;
            failed |= callback.cleanup_failed.load(Ordering::Acquire);
            self.retire_preparation(id, true);
        }
        ensure!(!failed, "Model callback cleanup failed");
        Ok(())
    }

    async fn finish_preparations(&self) {
        let callbacks = {
            let mut replies = self
                .preparation_replies
                .lock()
                .expect("Python preparation replies poisoned");
            let Some(pending) = replies.as_mut() else {
                return;
            };
            pending
                .iter_mut()
                .map(|(id, slot)| {
                    slot.reply.take();
                    self.cancel_preparation(&slot.callback, &slot.turn);
                    (*id, slot.callback.clone())
                })
                .collect::<Vec<_>>()
        };
        for (id, callback) in callbacks {
            callback.wait().await;
            self.retire_preparation(id, true);
        }
    }

    pub(crate) fn generation_high_water(&self) -> u64 {
        self.generation_high_water
            .load(Ordering::Acquire)
            .max(self.snapshot().id)
    }

    fn allocate_generation(&self) -> Result<u64> {
        let current = self.snapshot().id;
        let previous = self
            .generation_high_water
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |value| {
                value.max(current).checked_add(1)
            })
            .map_err(|_| anyhow::anyhow!("Python tool generation counter exhausted"))?;
        Ok(previous.max(current) + 1)
    }

    pub(crate) fn resolve_generation(&self, id: u64) -> Option<Arc<Generation>> {
        self.profile_catalogs
            .read()
            .expect("Profile catalogs poisoned")
            .get(&id)
            .and_then(Weak::upgrade)
            .or_else(|| {
                let current = self.snapshot();
                (current.id == id).then_some(current)
            })
    }

    fn admits_generation(&self, generation: &Arc<Generation>) -> bool {
        self.resolve_generation(generation.id)
            .is_some_and(|admitted| Arc::ptr_eq(&admitted, generation))
    }

    pub(crate) fn profile_catalog(
        &self,
        specs: Vec<DynamicToolSpec>,
        manual_slots: &[usize],
        terminal_slots: &[usize],
        input_error_slots: &[usize],
        defaults: Option<&[usize]>,
        flags: [bool; 5],
        _reuse_initial: bool,
    ) -> Result<ProfileCatalog> {
        self.allocate_profile_catalog(
            specs,
            manual_slots,
            terminal_slots,
            input_error_slots,
            defaults,
            flags,
            true,
        )
    }

    pub(crate) fn voice_profile_catalog(
        &self,
        specs: Vec<DynamicToolSpec>,
        manual_slots: &[usize],
        terminal_slots: &[usize],
        input_error_slots: &[usize],
        defaults: Option<&[usize]>,
        flags: [bool; 5],
    ) -> Result<ProfileCatalog> {
        self.allocate_profile_catalog(
            specs,
            manual_slots,
            terminal_slots,
            input_error_slots,
            defaults,
            flags,
            false,
        )
    }

    fn allocate_profile_catalog(
        &self,
        specs: Vec<DynamicToolSpec>,
        manual_slots: &[usize],
        terminal_slots: &[usize],
        input_error_slots: &[usize],
        defaults: Option<&[usize]>,
        flags: [bool; 5],
        managed: bool,
    ) -> Result<ProfileCatalog> {
        // Graph profiles always allocate a fresh identity, including the root.
        let id = self.allocate_generation()?;
        // An explicitly admitted voice Agent supplies its own callback catalog.
        // The opening policy still governs all durable profiles and turns.
        let policy = managed.then_some(self.policy.as_ref());
        let mut generation = self.prepare_catalog(id, specs, policy)?;
        generation.set_manual(manual_slots)?;
        generation.set_finish(terminal_slots)?;
        generation.set_input_errors(input_error_slots)?;
        let defaults = generation.default_names(defaults)?;
        let generation = Arc::new(generation);
        let mut catalogs = self
            .profile_catalogs
            .write()
            .expect("Profile catalogs poisoned");
        catalogs.retain(|_, generation| generation.strong_count() > 0);
        ensure!(
            !catalogs.contains_key(&id),
            "Duplicate native profile catalog generation"
        );
        catalogs.insert(id, Arc::downgrade(&generation));
        if managed {
            self.managed_profiles.store(true, Ordering::Release);
        }
        Ok(ProfileCatalog {
            generation,
            defaults,
            handoff_defaults: None,
            selecting_steps: flags[0],
            transform_context: flags[1],
            before_compact: flags[2],
            before_complete: flags[3],
            tool_batch_completion: flags[4],
        })
    }

    pub(crate) fn startup_profile_catalog(
        &self,
        flags: [bool; 5],
        defaults: Option<&[usize]>,
    ) -> Result<ProfileCatalog> {
        let generation = self.snapshot();
        let manual = generation
            .entries
            .iter()
            .enumerate()
            .filter_map(|(slot, entry)| entry.manual.then_some(slot))
            .collect::<Vec<_>>();
        let terminal = generation
            .entries
            .iter()
            .enumerate()
            .filter_map(|(slot, entry)| entry.finish_turn.then_some(slot))
            .collect::<Vec<_>>();
        let input_errors = generation
            .entries
            .iter()
            .enumerate()
            .filter_map(|(slot, entry)| entry.input_error_recovery.then_some(slot))
            .collect::<Vec<_>>();
        self.profile_catalog(
            generation.specs.clone(),
            &manual,
            &terminal,
            &input_errors,
            defaults,
            flags,
            false,
        )
    }

    /// Snapshot the exact native owner, including its captured MCP declarations.
    pub(crate) async fn realtime_selection_snapshot(&self, thread: &CodexThread) -> Result<Value> {
        let profile = thread.captured_agent_profile().await?;
        let generation = match &profile {
            Some(profile) => profile
                .profile
                .data
                .get::<ProfileCatalog>()
                .context("Captured Agent has no Python tool catalog")?
                .generation
                .clone(),
            None => {
                ensure!(
                    !self.managed_profiles.load(Ordering::Acquire),
                    "Missing captured Agent profile"
                );
                self.snapshot()
            }
        };
        ensure!(
            self.admits_generation(&generation),
            "Expired native selection catalog"
        );
        let receipt = profile.as_ref().map(|profile| &profile.receipt);
        let (selection_id, function_tools) =
            thread.realtime_agent_source_selection(None, None).await?;
        ensure!(
            selection_id
                == codex_extension_api::realtime_tool_selection_id(
                    thread.thread_extension_data().level_id(),
                    generation.id,
                    receipt,
                ),
            "Realtime source changed during selection snapshot"
        );
        Ok(json!({
            "selection_id": selection_id,
            "profile_receipt": receipt,
            "catalog_generation": generation.id,
            "function_tools": function_tools,
        }))
    }

    pub(crate) fn source_profile(
        &self,
        step: Option<&ExtensionData>,
    ) -> Result<Option<codex_extension_api::AgentProfileReceipt>> {
        self.generation_for_step(step)?;
        let profile = step.and_then(|step| {
            step.get::<codex_extension_api::CapturedAgentProfile>()
                .map(|profile| profile.receipt.clone())
                .or_else(|| {
                    step.get::<DescendantProfileSource>()
                        .map(|source| source.0.clone())
                })
        });
        if self.managed_profiles.load(Ordering::Acquire) {
            ensure!(profile.is_some(), "Missing captured Agent profile source");
        }
        Ok(profile)
    }

    pub(crate) fn generation_for_step(
        &self,
        step: Option<&ExtensionData>,
    ) -> Result<Arc<Generation>> {
        if let (Some(source), Some(step)) = (&self.descendant_source, step) {
            if source.profile.is_none() && source.mcp_policy.is_some() {
                step.get_or_init(|| crate::native_interception::ProfileMcpSource {
                    result_selection: source.mcp_selection.clone(),
                    receipt: None,
                    policy: source.mcp_policy.clone(),
                });
            }
            if let Some(catalog) = &source.catalog {
                step.get_or_init(|| catalog.as_ref().clone());
            }
            if let Some(profile) = &source.profile {
                step.get_or_init(|| DescendantProfileSource(profile.clone()));
            }
            if let Some(selection) = &source.mcp_selection {
                step.get_or_init(|| selection.as_ref().clone());
            }
            if let Some(policy) = &source.mcp_policy {
                step.get_or_init(|| policy.as_ref().clone());
            }
        }
        if let Some(step) = step {
            let profile = step
                .get::<codex_extension_api::CapturedAgentProfile>()
                .map(|profile| profile.receipt.clone())
                .or_else(|| {
                    step.get::<DescendantProfileSource>()
                        .map(|source| source.0.clone())
                });
            if let Some(receipt) = profile {
                let policy = step.get::<crate::native_interception::ProfileMcpPolicy>();
                step.get_or_init(|| crate::native_interception::ProfileMcpSource {
                    result_selection: None,
                    receipt: Some(receipt),
                    policy,
                });
            } else if let Some(control) = self.interception.get().and_then(Weak::upgrade) {
                // get_or_init holds the attachment-map lock while initializing.
                let binding = step.get::<Arc<codex_mcp::McpBinding>>();
                step.get_or_init(|| match binding {
                    Some(binding) => control.capture_mcp_source_for_binding(&binding, &self.policy),
                    None => control.capture_mcp_source(),
                });
            }
        }
        if let Some(catalog) = step.and_then(|step| step.get::<ProfileCatalog>()) {
            ensure!(
                self.admits_generation(&catalog.generation),
                "Expired captured profile catalog"
            );
            return Ok(catalog.generation.clone());
        }
        ensure!(
            !self.managed_profiles.load(Ordering::Acquire),
            "Managed Agent callback requires its captured native step profile"
        );
        Ok(self.snapshot())
    }

    pub(crate) fn request_sampling_stop(&self, turn_id: &str) {
        let admission = self
            .admission
            .lock()
            .expect("Python admission receipt poisoned");
        if let Some(receipt) = admission
            .as_ref()
            .filter(|receipt| receipt.turn_id == turn_id)
        {
            receipt.sampling_stop.request();
        }
    }

    /// Reserve the stop before returning an awaitable to Python. Native tools
    /// may finish while its settlement task is still waiting to be scheduled.
    pub(crate) fn request_graph_interrupt(&self, force: bool) -> Result<GraphStopRequest> {
        ensure!(
            self.instance.is_none(),
            "Only the root can interrupt its graph"
        );
        {
            let mut graph = self.family.graph.lock().expect("Python graph poisoned");
            graph.pending_stops += 1;
            graph.fenced = true;
            for (_, _, receipt) in self.graph_turns(false) {
                receipt.graceful_requested.store(true, Ordering::Release);
                if force {
                    receipt.sampling_stop.request();
                }
            }
        }
        self.family.graph_changed.send_replace(());
        Ok(GraphStopRequest {
            family: Arc::clone(&self.family),
        })
    }

    // The family membership comes from thread startup, before any child turn
    // starts. It must not depend on whether Python has observed a graph event.
    fn graph_turns(
        &self,
        fenced_only: bool,
    ) -> Vec<(String, Arc<PythonTools>, Arc<TurnAdmission>)> {
        self.family
            .members
            .read()
            .expect("Python family poisoned")
            .iter()
            .filter_map(|(id, member)| {
                let member = member.upgrade()?;
                let receipt = member
                    .admission
                    .lock()
                    .expect("Python admission receipt poisoned")
                    .clone()?;
                if (*receipt.state.borrow() != AdmissionState::Pending
                    && receipt.terminal.load(Ordering::Acquire))
                    || (fenced_only && !receipt.graph_fenced)
                {
                    return None;
                }
                Some((id.clone(), member, receipt))
            })
            .collect()
    }

    /// Establish an all-scope stop at native turn admission. The fence survives
    /// this waiter and is only released by a later explicit SDK submission.
    pub(crate) async fn interrupt_graph(
        &self,
        manager: &ThreadManager,
        mutations: &AsyncMutex<()>,
        force: bool,
    ) -> Result<()> {
        ensure!(
            self.instance.is_none(),
            "Only the root can interrupt its graph"
        );
        let turns = {
            let _mutation = mutations.lock().await;
            let mut graph = self.family.graph.lock().expect("Python graph poisoned");
            graph.fenced = true;
            let turns = self.graph_turns(false);
            if !force {
                for (_, _, receipt) in &turns {
                    receipt.graceful_requested.store(true, Ordering::Release);
                }
            }
            turns
        };
        if force {
            for (id, owner, receipt) in turns {
                let id = codex_core_api::ThreadId::from_string(&id)?;
                if let Ok(thread) = manager.get_thread(id).await {
                    // During a stop no explicit SDK turn may enter. A native
                    // successor racing this Op was born fenced and must stop too.
                    if thread
                        .thread_extension_data()
                        .get::<PythonTools>()
                        .is_some_and(|live| Arc::ptr_eq(&live, &owner))
                        && owner.owns_admission(&receipt)
                    {
                        thread.submit(Op::Interrupt).await?;
                    }
                }
            }
        }
        self.settle_graph(manager, false).await?;
        // Keep explicit starts and other stop callers excluded until the host
        // has acknowledged every selected cell. Do not block force escalation
        // while graceful waits for the current turn's safe boundary.
        let _mutation = mutations.lock().await;
        let members: Vec<_> = self
            .family
            .members
            .read()
            .expect("Python family poisoned")
            .values()
            .filter_map(Weak::upgrade)
            .collect();
        let mut failure = None;
        for member in members {
            if let Some(control) = member.interception.get().and_then(Weak::upgrade)
                && let Err(error) = control.interrupt_code_mode_cells(false, None).await
            {
                failure.get_or_insert(error);
            }
        }
        if let Some(error) = failure {
            return Err(error);
        }
        Ok(())
    }

    /// A new application request is the authority to resume. Drain births
    /// already fenced before releasing the family; never authorize them by
    /// changing a global flag alone.
    pub(crate) async fn resume_graph(&self, manager: &ThreadManager) -> Result<()> {
        self.settle_graph(manager, true).await
    }

    async fn settle_graph(&self, manager: &ThreadManager, resume: bool) -> Result<()> {
        let mut changed = self.family.graph_changed.subscribe();
        loop {
            let turns = {
                let mut graph = self.family.graph.lock().expect("Python graph poisoned");
                if let Some(failure) = graph.failure {
                    anyhow::bail!(failure);
                }
                if resume && graph.pending_stops != 0 {
                    // An admitted start may own mutations while the stop needs
                    // that lock. Leave its new receipt fenced instead of waiting.
                    return Ok(());
                }
                if !graph.fenced {
                    return Ok(());
                }
                let turns = self.graph_turns(resume);
                if turns.is_empty() {
                    if resume {
                        graph.fenced = false;
                    }
                    return Ok(());
                }
                turns
            };
            for (id, owner, receipt) in turns {
                // A raw Core interrupt emits TurnAborted before completing
                // its SQ work; it does not emit thread idle. Reuse the root
                // interruption barrier before releasing any fenced receipt.
                if receipt.terminal.load(Ordering::Acquire)
                    && *receipt.state.borrow() == AdmissionState::Pending
                {
                    let id = codex_core_api::ThreadId::from_string(&id)?;
                    if let Ok(thread) = manager.get_thread(id).await
                        && thread
                            .thread_extension_data()
                            .get::<PythonTools>()
                            .is_some_and(|live| Arc::ptr_eq(&live, &owner))
                        && owner.owns_admission(&receipt)
                    {
                        // Shutdown may already be queued ahead of this Op.
                        // Core ends its SQ loop after Shutdown. A retained
                        // sender can then keep the unprocessed reply alive.
                        let settled = tokio::select! {
                            biased;
                            result = owner.wait_for_graph_thread_stop() => result,
                            result = owner.finish_session_interrupt(&thread, &receipt) => result,
                        };
                        // Core may have already enrolled another fenced wake
                        // after the original abort. Only failures still owned
                        // by this exact receipt are errors.
                        if owner.owns_admission(&receipt) {
                            settled?;
                        }
                    }
                }
            }
            changed.changed().await?;
        }
    }

    // Observe the *current* receipt of this PythonTools owner. Replacement
    // closes the previous receipt only after removing it from this slot.
    // Thread stop is the sole producer of Closed on the current slot. Ready,
    // a former same-ID receipt, or a graceful request cannot bypass an SQ ack.
    async fn wait_for_graph_thread_stop(&self) -> Result<()> {
        let mut changed = self.family.graph_changed.subscribe();
        loop {
            if self
                .admission
                .lock()
                .expect("Python admission receipt poisoned")
                .as_ref()
                .is_some_and(|receipt| {
                    receipt.terminal.load(Ordering::Acquire)
                        && *receipt.state.borrow() == AdmissionState::Closed
                })
            {
                return Ok(());
            }
            changed.changed().await?;
        }
    }

    /// Only the public Session's sole submission owner may use this path.
    /// Caller keeps ThreadState.mutations across both queued operations.
    pub(crate) async fn interrupt_session(
        &self,
        thread: &CodexThread,
        expected_turn_id: &str,
    ) -> Result<()> {
        crate::ops::call(thread, "interrupt", json!({})).await?;
        let (reply, acknowledged) = oneshot::channel();
        thread
            .submit(Op::InterruptIfNoPendingInput {
                turn_id: expected_turn_id.to_owned(),
                reply,
            })
            .await?;
        ensure!(
            !acknowledged.await?,
            "Native managed turn restarted during interruption"
        );
        // The SQ has now processed both the queued Compact and its stop.
        // Capturing before acknowledgement can capture the preceding receipt.
        let receipt = self.admission_ready(expected_turn_id)?;
        receipt.probe_idle(thread).await
    }

    pub(crate) fn request_graceful_interrupt(&self, turn_id: &str) -> Result<bool> {
        ensure!(
            self.instance.is_none(),
            "Only a managed root can request interruption"
        );
        let admission = self
            .admission
            .lock()
            .expect("Python admission receipt poisoned");
        let Some(receipt) = admission.as_ref().filter(|receipt| {
            receipt.turn_id == turn_id && *receipt.state.borrow() == AdmissionState::Pending
        }) else {
            return Ok(false);
        };
        receipt.graceful_requested.store(true, Ordering::Release);
        Ok(true)
    }

    pub(crate) async fn finish_graceful_interrupt(
        &self,
        thread: &CodexThread,
        turn_id: &str,
    ) -> Result<()> {
        ensure!(
            self.instance.is_none(),
            "Only a managed root can finish interruption"
        );
        // A completed force/close may already have retired this request.
        // A same-ID successor starts unmarked and must never receive this Op.
        let receipt = self.admission_ready(turn_id)?;
        if receipt.graceful_requested.load(Ordering::Acquire) {
            self.finish_session_interrupt(thread, &receipt).await?;
        }
        Ok(())
    }

    pub(crate) async fn finish_child_before_submit(&self, thread: &CodexThread) -> Result<()> {
        let terminal = self
            .admission
            .lock()
            .expect("Python admission receipt poisoned")
            .as_ref()
            .filter(|receipt| receipt.terminal.load(Ordering::Acquire))
            .cloned();
        if let Some(receipt) = terminal {
            receipt.check_local_command_cleanup()?;
            if *receipt.state.borrow() != AdmissionState::Pending {
                return Ok(());
            }
            // Core has already detached or taken this task. Never infer this
            // from a reader's current turn, which may lag native lifecycle.
            self.finish_session_interrupt(thread, &receipt).await?;
            loop {
                ensure!(
                    self.owns_admission(&receipt),
                    "Native child turn changed while finishing its previous turn"
                );
                tokio::select! {
                    biased;
                    result = receipt.wait() => {
                        result?;
                        break;
                    },
                    _ = tokio::time::sleep(std::time::Duration::from_millis(50)) => {
                        // A detached finisher may still own the active slot.
                        // A later raw Core interrupt can take that slot without
                        // notifying idle at all. Only Core may prove it idle;
                        // do not repeat an interrupt or an input submission.
                        receipt.probe_idle(thread).await?;
                    }
                }
            }
            ensure!(
                self.owns_admission(&receipt),
                "Native child turn changed while finishing its previous turn"
            );
        }
        Ok(())
    }

    async fn finish_session_interrupt(
        &self,
        thread: &CodexThread,
        receipt: &Arc<TurnAdmission>,
    ) -> Result<()> {
        let (reply, acknowledged) = oneshot::channel();
        // Use the raw queued Op. The convenience method can return before
        // enqueueing when Core has already detached its finishing task.
        thread
            .submit(Op::InterruptIfNoPendingInput {
                turn_id: receipt.turn_id.clone(),
                reply,
            })
            .await?;
        self.acknowledge_session_interrupt(receipt, acknowledged)
            .await?;
        // Core owns the active-slot and trigger-mailbox check. An
        // acknowledged interrupt is not by itself a global idle claim.
        receipt.probe_idle(thread).await
    }

    fn owns_admission(&self, receipt: &Arc<TurnAdmission>) -> bool {
        self.admission
            .lock()
            .expect("Python admission receipt poisoned")
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, receipt))
    }

    fn admitted_turn(&self, turn_store: &ExtensionData) -> Option<Arc<TurnAdmission>> {
        let receipt = turn_store.get::<Arc<TurnAdmission>>()?;
        (receipt.turn_id == turn_store.level_id()
            && self.owns_admission(&receipt)
            && *receipt.state.borrow() == AdmissionState::Pending)
            .then(|| Arc::clone(receipt.as_ref()))
    }

    fn requested_graceful_admission(
        &self,
        turn_store: &ExtensionData,
    ) -> Option<Arc<TurnAdmission>> {
        self.admitted_turn(turn_store)
            .filter(|receipt| receipt.graceful_requested.load(Ordering::Acquire))
    }

    fn graceful_failure(self: &Arc<Self>, receipt: &Arc<TurnAdmission>) {
        if receipt.graceful_requested.swap(false, Ordering::AcqRel) {
            let mut graph = self.family.graph.lock().expect("Python graph poisoned");
            if graph.fenced {
                graph.failure = Some("Native graph graceful interruption failed");
                self.family.graph_changed.send_replace(());
                return;
            }
            let _ = self.family.requests.send(QueuedRequest::GracefulFailure(
                Arc::downgrade(self),
                Arc::downgrade(receipt),
            ));
        }
    }

    async fn acknowledge_session_interrupt(
        &self,
        receipt: &Arc<TurnAdmission>,
        acknowledged: oneshot::Receiver<bool>,
    ) -> Result<()> {
        ensure!(
            !acknowledged.await?,
            "Native managed turn restarted during interruption"
        );
        let admission = self
            .admission
            .lock()
            .expect("Python admission receipt poisoned");
        ensure!(
            admission
                .as_ref()
                .is_some_and(|current| Arc::ptr_eq(current, receipt)),
            "Native managed turn changed during interruption"
        );
        Ok(())
    }

    pub(crate) fn observed_admission(&self, turn_id: &str) -> Option<Arc<TurnAdmission>> {
        self.admission
            .lock()
            .expect("Python admission receipt poisoned")
            .as_ref()
            .filter(|receipt| receipt.turn_id == turn_id && receipt.permits_integrity_reason())
            .cloned()
    }

    pub(crate) fn admission_ready(&self, turn_id: &str) -> Result<Arc<TurnAdmission>> {
        self.admission
            .lock()
            .expect("Python admission receipt poisoned")
            .as_ref()
            .filter(|receipt| receipt.turn_id == turn_id)
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("Native completion has no matching admission receipt"))
    }

    pub(crate) fn child_steer_admission(
        self: &Arc<Self>,
        turn_id: &str,
    ) -> Result<Arc<TurnAdmission>> {
        let admission = self
            .admission
            .lock()
            .expect("Python admission receipt poisoned");
        let receipt = admission
            .as_ref()
            .filter(|receipt| Weak::ptr_eq(&receipt.owner, &Arc::downgrade(self)))
            .context("Native completion has no matching admission receipt")?;
        if receipt.turn_id != turn_id {
            // on_turn_start can advance the native owner before the event
            // reader observes TurnStarted. Only a live, owned successor proves
            // the requested child turn stale; a closed/fenced receipt does not.
            if *receipt.state.borrow() != AdmissionState::Closed
                && !receipt.graph_fenced
                && !receipt.terminal.load(Ordering::Acquire)
            {
                anyhow::bail!("Native child turn is no longer current");
            }
            anyhow::bail!("Native completion has no matching admission receipt");
        }
        Ok(Arc::clone(receipt))
    }

    pub(crate) fn can_continue_steering(&self, turn_id: &str) -> bool {
        let graph = self.family.graph.lock().expect("Python graph poisoned");
        !graph.fenced
            && self.admission_ready(turn_id).is_ok_and(|receipt| {
                !receipt.graph_fenced
                    && *receipt.state.borrow() == AdmissionState::Ready
                    && receipt.has_steering_tail()
            })
    }

    pub(crate) fn can_finish_child_steering(&self, receipt: &Arc<TurnAdmission>) -> bool {
        let graph = self.family.graph.lock().expect("Python graph poisoned");
        !graph.fenced
            && !receipt.graph_fenced
            && self.owns_admission(receipt)
            && receipt.has_steering_tail()
    }

    pub(crate) fn sdk_owned(&self) -> bool {
        self.policy.allowed_tools.as_deref() == Some(&[sdk_name()])
    }
    pub fn install(
        startup: &mut StartThreadOptions,
        specs: Vec<DynamicToolSpec>,
        transform_context: bool,
        before_compact: bool,
        before_complete: bool,
        tool_batch_completion: bool,
    ) -> Result<()> {
        Self::install_manual(
            startup,
            specs,
            &[],
            transform_context,
            before_compact,
            before_complete,
            tool_batch_completion,
        )
    }

    pub fn install_manual(
        startup: &mut StartThreadOptions,
        specs: Vec<DynamicToolSpec>,
        manual_slots: &[usize],
        transform_context: bool,
        before_compact: bool,
        before_complete: bool,
        tool_batch_completion: bool,
    ) -> Result<()> {
        // Core resolves absent source options from resumed metadata at startup.
        let resumed_sources = startup.initial_history.get_resumed_session_sources();
        let session_source = startup
            .session_source
            .as_ref()
            .or_else(|| resumed_sources.as_ref().map(|(source, _)| source));
        let thread_source = startup.thread_source.as_ref().or_else(|| {
            resumed_sources
                .as_ref()
                .and_then(|(_, source)| source.as_ref())
        });
        ensure!(
            matches!(thread_source, None | Some(ThreadSource::User))
                && !matches!(
                    session_source,
                    Some(SessionSource::Internal(_) | SessionSource::SubAgent(_))
                ),
            "replaceable tools require a root thread"
        );
        ensure!(
            !startup
                .config
                .features
                .enabled(Feature::CodeModeOnlyStrictThirdPartyTools),
            "replaceable tools do not support strict third-party Code Mode"
        );
        ensure!(
            !startup
                .config
                .features
                .enabled(Feature::MultiAgentV2DynamicTools),
            "replaceable tools do not support managed child-dynamic inheritance"
        );
        ensure!(
            !startup
                .config
                .features
                .persistent_execution_enabled(startup.config.model_reasoning_effort.as_ref()),
            "replaceable tools do not support persistent execution"
        );
        // Core can fall back to the header when raw startup tools are absent.
        // Preserve every fixed name as a boundary, even if explicit raw
        // options temporarily hide that history from the native advertisement.
        let history_tools = startup
            .initial_history
            .get_dynamic_tools()
            .unwrap_or_default();
        let mut fixed = tool_names(&history_tools);
        fixed.extend(tool_names(&startup.dynamic_tools));
        let policy = startup
            .thread_extension_init
            .get::<ToolPolicy>()
            .unwrap_or_default();
        let profile_capabilities = startup
            .thread_extension_init
            .get::<crate::agent_profiles::OpeningProfiles>()
            .map(|opening| opening.capabilities());
        let transform_context =
            transform_context || profile_capabilities.is_some_and(|flags| flags[1]);
        let before_compact = before_compact || profile_capabilities.is_some_and(|flags| flags[2]);
        let before_complete = before_complete || profile_capabilities.is_some_and(|flags| flags[3]);
        let tools = Self {
            current: RwLock::new(Arc::new(Generation {
                id: 0,
                entries: Vec::new(),
                specs: Vec::new(),
            })),
            generation_high_water: AtomicU64::new(0),
            profile_catalogs: RwLock::new(HashMap::new()),
            managed_profiles: AtomicBool::new(false),
            descendant_source: None,
            fixed,
            policy,
            pending: Mutex::new(HashMap::new()),
            failed_turns: Mutex::default(),
            family: Arc::new(PythonFamily::default()),
            instance: None,
            suspended: AtomicBool::new(false),
            manual_restore: Mutex::new(None),
            manual_progress: watch::channel(0).0,
            admission: Mutex::new(None),
            interception: OnceLock::new(),
            model_producer: Mutex::default(),
            prepared_requests: Mutex::default(),
            selecting_steps: AtomicBool::new(false),
            selecting_children: AtomicBool::new(false),
            context_transform_enabled: transform_context,
            before_compact_enabled: before_compact,
            before_complete_enabled: before_complete,
            tool_batch_completion_enabled: tool_batch_completion,
            step_defaults: RwLock::new(None),
            preparation_replies: Mutex::new(Some(HashMap::new())),
            preparations_stopped: AtomicBool::new(false),
            #[cfg(test)]
            before_result: Mutex::new(None),
        };
        let mut seed = tools.prepare(0, specs)?;
        seed.set_manual(manual_slots)?;
        if let Some(slots) = startup.thread_extension_init.get::<FinishToolSlots>() {
            seed.set_finish(&slots.0)?;
        }
        if let Some(slots) = startup.thread_extension_init.get::<InputErrorToolSlots>() {
            seed.set_input_errors(&slots.0)?;
        }
        *tools.current.write().expect("Python catalog poisoned") = Arc::new(seed);
        startup.thread_extension_init.insert(tools);
        Ok(())
    }

    pub(crate) fn owner(self: &Arc<Self>, root: &str, key: &PendingKey) -> Option<Arc<Self>> {
        if key.reply.thread == root && key.reply.instance == self.instance {
            return Some(Arc::clone(self));
        }
        self.family
            .members
            .read()
            .ok()?
            .get(&key.reply.thread)?
            .upgrade()
            .filter(|tools| {
                tools.instance == key.reply.instance && Arc::ptr_eq(&tools.family, &self.family)
            })
    }

    #[cfg(test)]
    fn descendant(parent: &Self, instance: String) -> Self {
        Self::descendant_from_step(parent, instance, None).expect("unmanaged child fixture")
    }

    fn descendant_from_step(
        parent: &Self,
        instance: String,
        step: Option<&ExtensionData>,
    ) -> Result<Self> {
        let (generation, source) = match step {
            Some(step) => {
                let generation = parent.generation_for_step(Some(step))?;
                if let Some(selected) = step.get::<StepCatalog>() {
                    ensure!(
                        Arc::ptr_eq(&selected.generation, &generation),
                        "Spawning tool catalog no longer matches captured source"
                    );
                }
                let profile = parent.source_profile(Some(step))?;
                let catalog = step.get::<ProfileCatalog>();
                let selection = step
                    .get::<crate::native_interception::ProfileMcpSource>()
                    .filter(|source| source.receipt.is_none())
                    .map(|source| source.result_selection.clone())
                    .unwrap_or_else(|| step.get::<crate::mcp_result_control::Selection>());
                let policy = match step.get::<crate::native_interception::ProfileMcpSource>() {
                    Some(source) => {
                        ensure!(
                            profile.as_ref() == source.receipt.as_ref(),
                            "Spawning MCP policy source does not match captured Agent"
                        );
                        source.policy.clone()
                    }
                    None => step.get::<crate::native_interception::ProfileMcpPolicy>(),
                };
                if profile.is_some() {
                    ensure!(
                        catalog.is_some(),
                        "Managed child source has no captured catalog"
                    );
                    ensure!(
                        selection
                            .as_ref()
                            .is_some_and(|selection| selection.generation == generation.id),
                        "Managed child source has no matching MCP result policy"
                    );
                }
                (
                    generation,
                    Some(DescendantSource {
                        catalog,
                        profile,
                        mcp_selection: selection,
                        mcp_policy: policy,
                    }),
                )
            }
            None => {
                ensure!(
                    parent
                        .descendant_source
                        .as_ref()
                        .is_none_or(|source| source.profile.is_none())
                        && !parent.managed_profiles.load(Ordering::Acquire),
                    "Managed child has no validated spawning source; restored child requires persisted source validation"
                );
                (parent.snapshot(), None)
            }
        };
        let catalogs = source
            .as_ref()
            .and_then(|source| source.catalog.as_ref())
            .map(|catalog| {
                HashMap::from([(catalog.generation.id, Arc::downgrade(&catalog.generation))])
            })
            .unwrap_or_default();
        Ok(Self {
            current: RwLock::new(generation),
            generation_high_water: AtomicU64::new(parent.generation_high_water()),
            profile_catalogs: RwLock::new(catalogs),
            managed_profiles: AtomicBool::new(
                source
                    .as_ref()
                    .is_some_and(|source| source.profile.is_some()),
            ),
            descendant_source: source,
            fixed: parent.fixed.clone(),
            policy: Arc::clone(&parent.policy),
            pending: Mutex::new(HashMap::new()),
            failed_turns: Mutex::default(),
            family: Arc::clone(&parent.family),
            instance: Some(instance),
            suspended: AtomicBool::new(false),
            manual_restore: Mutex::new(None),
            manual_progress: watch::channel(0).0,
            admission: Mutex::new(None),
            interception: OnceLock::new(),
            model_producer: Mutex::default(),
            prepared_requests: Mutex::default(),
            selecting_steps: AtomicBool::new(
                parent.selecting_steps.load(Ordering::Acquire)
                    || parent.selecting_children.load(Ordering::Acquire),
            ),
            selecting_children: AtomicBool::new(
                parent.selecting_steps.load(Ordering::Acquire)
                    || parent.selecting_children.load(Ordering::Acquire),
            ),
            context_transform_enabled: parent.context_transform_enabled,
            before_compact_enabled: parent.before_compact_enabled,
            before_complete_enabled: parent.before_complete_enabled,
            tool_batch_completion_enabled: parent.tool_batch_completion_enabled,
            step_defaults: RwLock::new(
                parent
                    .step_defaults
                    .read()
                    .expect("Step defaults poisoned")
                    .clone(),
            ),
            preparation_replies: Mutex::new(Some(HashMap::new())),
            preparations_stopped: AtomicBool::new(false),
            #[cfg(test)]
            before_result: Mutex::new(None),
        })
    }

    pub(crate) fn response_receipt(
        &self,
        key: &PendingKey,
        response: DynamicToolResponse,
    ) -> Result<Option<ResponseItem>> {
        let arguments = {
            let pending = self.pending.lock().expect("tool replies poisoned");
            let Some(call) = pending.get(key) else {
                return Ok(None);
            };
            call.computer_arguments.clone()
        };
        let Some(arguments) = arguments else {
            return Ok(Some(PythonOutput(response).receipt(key)));
        };
        let output = codex_tools::ComputerToolOutput::from_dynamic_response(&arguments, response)
            .map_err(|_| anyhow::anyhow!("Invalid native computer response"))?;
        let mut item: ResponseItem = output
            .to_response_item(&key.call, &ToolPayload::Computer { arguments })
            .into();
        if let ResponseItem::ComputerCallOutput {
            internal_chat_message_metadata_passthrough,
            ..
        } = &mut item
        {
            *internal_chat_message_metadata_passthrough = Some(
                codex_protocol::models::InternalChatMessageMetadataPassthrough {
                    turn_id: Some(key.turn.clone()),
                    ..Default::default()
                },
            );
        }
        Ok(Some(item))
    }

    fn prepare(&self, id: u64, specs: Vec<DynamicToolSpec>) -> Result<Generation> {
        self.prepare_catalog(id, specs, Some(self.policy.as_ref()))
    }

    fn prepare_catalog(
        &self,
        id: u64,
        specs: Vec<DynamicToolSpec>,
        policy: Option<&ToolPolicy>,
    ) -> Result<Generation> {
        validate_declarations(&specs)?;
        let mut entries = Vec::new();
        for (declaration, (namespace, tool)) in specs.iter().zip(functions(&specs)) {
            let name = ToolName::new(namespace.map(|ns| ns.name.clone()), tool.name.clone());
            let key = name.clone().with_default_namespace();
            ensure!(
                !self.fixed.contains(&key),
                "Python tool collides with fixed tool: {key}"
            );
            ensure!(
                policy.is_none_or(|policy| policy.allows(&key)),
                "Python tool is excluded by ToolPolicy: {key}"
            );
            let spec = match declaration {
                DynamicToolSpec::Computer(computer) => {
                    codex_tools::computer_tool_spec(computer).map_err(anyhow::Error::msg)?
                }
                DynamicToolSpec::ToolSearch(search) => {
                    codex_tools::client_tool_search_spec(search).map_err(anyhow::Error::msg)?
                }
                _ => {
                    let mut function = dynamic_tool_to_responses_api_tool(&tool)?;
                    function.defer_loading = None;
                    match namespace.filter(|_| !key.is_default_namespace()) {
                        None => ToolSpec::Function(function),
                        Some(ns) => ToolSpec::Namespace(ResponsesApiNamespace {
                            name: ns.name.clone(),
                            description: if ns.description.trim().is_empty() {
                                default_namespace_description(&ns.name)
                            } else {
                                ns.description.clone()
                            },
                            tools: vec![ResponsesApiNamespaceTool::Function(function)],
                        }),
                    }
                }
            };
            entries.push(Entry {
                manual: false,
                finish_turn: false,
                input_error_recovery: false,
                name,
                spec,
                exposure: if matches!(
                    declaration,
                    DynamicToolSpec::Computer(_) | DynamicToolSpec::ToolSearch(_)
                ) {
                    ToolExposure::DirectModelOnly
                } else if tool.defer_loading {
                    ToolExposure::Deferred
                } else {
                    ToolExposure::Direct
                },
            });
        }
        Ok(Generation { id, entries, specs })
    }

    pub fn current_specs(&self) -> Result<Value> {
        checkpoint_specs(&self.snapshot().specs)
    }

    pub(crate) fn select_steps(
        &self,
        enabled: bool,
        defaults: Option<Vec<ToolName>>,
        children: bool,
    ) {
        self.selecting_children.store(children, Ordering::Release);
        *self.step_defaults.write().expect("Step defaults poisoned") = defaults;
        self.selecting_steps.store(enabled, Ordering::Release);
    }

    pub(crate) fn transforms_context(&self) -> bool {
        self.context_transform_enabled
    }

    pub(crate) fn needs_model_retry_callback(&self, policy_enabled: bool) -> bool {
        policy_enabled
            || self
                .model_producer
                .lock()
                .expect("Model producer poisoned")
                .enabled_for_retry()
    }

    pub(crate) fn publish_model_attempt(
        &self,
        input: codex_extension_api::ModelAttemptInput,
    ) -> Result<()> {
        let owner = input
            .admission
            .turn_store()
            .get::<Arc<TurnAdmission>>()
            .context("Missing model attempt owner")?;
        ensure!(
            owner.thread_id == input.admission.thread_id(),
            "Mismatched model attempt owner"
        );
        // Retain the exact ledger view before the FIFO notice. Reads at manual sealing
        // also include measurements received during joined cleanup, before a final fact.
        *owner
            .model_attempt
            .lock()
            .expect("Model attempt mirror poisoned") = input.admission.model_attempt();
        let mut data = serde_json::to_value(input.snapshot)?;
        data["attempt_id"] = json!(input.attempt_id);
        data["phase"] = json!(input.phase);
        let mut event = json!({"id":owner.turn_id,"msg":{"type":"_sdk_model_attempt","thread":owner.thread_id,"turn_id":owner.turn_id,"data":data,
            "root_turn_id":input.admission.root_turn_id()}});
        if let Some(instance) = &self.instance {
            event["subagent_id"] = json!(owner.thread_id);
            event["subagent_incarnation"] = json!(instance);
        }
        self.family
            .requests
            .send(QueuedRequest::Step(event))
            .map_err(|_| anyhow::anyhow!("Model attempt reader closed"))
    }

    pub(crate) async fn decide_model_retry(
        self: &Arc<Self>,
        input: codex_extension_api::ModelRetryInput,
    ) -> Result<codex_protocol::model_retry::ModelRetryResolution> {
        let owner = input
            .admission
            .turn_store()
            .get::<Arc<TurnAdmission>>()
            .context("Missing model retry owner")?;
        let mut source = self.source_metadata(input.admission.step_store())?;
        if !source.contains_key("generation") {
            source.insert(
                "generation".into(),
                json!(self.generation_for_step(input.admission.step_store())?.id),
            );
        }
        let turn = owner.turn_id.clone();
        let root_turn_id = input.admission.root_turn_id().map(str::to_owned);
        self.exchange_preparation(
            Arc::clone(owner.as_ref()),
            None,
            Some(input.cancellation),
            [
                "Expired model retry owner",
                "Model retry reader closed",
                "Model retry callback cancelled",
            ],
            PreparationReply::ModelRetry,
            |id| {
                let mut msg = source;
                msg.insert("type".into(), json!("_sdk_model_retry"));
                msg.insert("request_id".into(), json!(id));
                msg.insert("thread".into(), json!(input.admission.thread_id()));
                msg.insert("instance".into(), json!(self.instance));
                msg.insert("turn_id".into(), json!(turn));
                msg.insert("data".into(), json!(input.context));
                msg.insert("root_turn_id".into(), json!(root_turn_id));
                let mut event = json!({"id":turn,"msg":msg});
                if let Some(instance) = &self.instance {
                    event["subagent_id"] = json!(input.admission.thread_id());
                    event["subagent_incarnation"] = json!(instance);
                }
                event
            },
            |reply| reply.map_err(|_| anyhow::anyhow!("Model retry callback failed")),
        )
        .await
    }

    pub(crate) fn reply_model_retry(
        self: &Arc<Self>,
        id: u64,
        data: Option<&str>,
        error: bool,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> Result<()> {
        let owner = self.step_owner(thread, instance)?;
        let mut replies = owner
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        let slot = replies
            .as_mut()
            .and_then(|pending| pending.get_mut(&id))
            .context("Expired model retry callback")?;
        ensure!(
            !slot.is_closed() && matches!(slot.reply, Some(PreparationReply::ModelRetry(_))),
            "Expired model retry callback"
        );
        let value = if error {
            Err(())
        } else {
            let data = data.context("Missing model retry reply")?;
            ensure!(data.len() <= 65_536, "Model retry reply exceeds capacity");
            Ok(serde_json::from_str(data)?)
        };
        let Some(PreparationReply::ModelRetry(reply)) = slot.reply.take() else {
            unreachable!()
        };
        reply
            .send(value)
            .map_err(|_| anyhow::anyhow!("Expired model retry callback"))
    }

    async fn exchange_preparation<T, U>(
        self: &Arc<Self>,
        owner: Arc<TurnAdmission>,
        model_attempt: Option<Arc<dyn codex_extension_api::ModelAttemptAdmission>>,
        cancellation: Option<tokio_util::sync::CancellationToken>,
        [expired, reader_closed, cancelled]: [&'static str; 3],
        prepare: impl FnOnce(oneshot::Sender<T>) -> PreparationReply,
        request: impl FnOnce(u64) -> Value,
        decode: impl FnOnce(T) -> Result<U>,
    ) -> Result<U> {
        let id = self.family.next_invocation.fetch_add(1, Ordering::Relaxed);
        let (reply, receive) = oneshot::channel();
        let mut request = request(id);
        let callback = self.model_preparation_callback(id, &mut request, model_attempt);
        {
            let mut replies = self
                .preparation_replies
                .lock()
                .expect("Python preparation replies poisoned");
            ensure!(
                !self.preparations_stopped.load(Ordering::Acquire),
                "{expired}"
            );
            let prepared = prepare(reply);
            ensure!(
                !prepared.is_closed() && callback.admits_reply(),
                "{expired}"
            );
            replies.as_mut().context(expired)?.insert(
                id,
                PreparationSlot {
                    turn: request["id"].as_str().unwrap_or_default().to_owned(),
                    reply: Some(prepared),
                    callback: callback.clone(),
                },
            );
        }
        let _pending = PendingPreparation {
            tools: self.clone(),
            id,
        };
        if let Some(tracing) = &owner.tracing {
            request["msg"]["tracing_context"] = json!(tracing.token());
        }
        ensure!(owner.is_current(), "{expired}");
        self.family
            .requests
            .send(QueuedRequest::Step(request))
            .map_err(|_| anyhow::anyhow!(reader_closed))?;
        let result = decode(
            self.await_preparation(id, &callback, receive, cancellation)
                .await
                .context(cancelled)?,
        )?;
        ensure!(owner.is_current(), "{expired}");
        Ok(result)
    }

    fn source_metadata(
        &self,
        step: Option<&ExtensionData>,
    ) -> Result<serde_json::Map<String, Value>> {
        let generation = self.generation_for_step(step)?;
        let mut message = serde_json::Map::new();
        if let Some(profile) = self.source_profile(step)? {
            message.insert("profile_id".into(), json!(profile.profile_id));
            message.insert("profile_epoch".into(), json!(profile.epoch));
            message.insert("generation".into(), json!(generation.id));
        }
        Ok(message)
    }

    pub(crate) async fn transform_context(
        self: &Arc<Self>,
        mut input: codex_extension_api::ModelContextInput<'_>,
    ) -> Result<()> {
        let source = self.source_metadata(input.step_store)?;
        if input
            .step_store
            .and_then(|step| step.get::<ProfileCatalog>())
            .is_some_and(|catalog| !catalog.transform_context)
        {
            return Ok(());
        }
        if let Some(prompt) = input.hosted_prompt.as_deref_mut() {
            if !prompt.omit_instructions {
                prompt.resolved_instructions = Some(
                    crate::application_instructions::captured_prompt_text(input.step_store)?,
                );
            }
            crate::application_instructions::remove_prompt_fragments(input.items)?;
        }
        let owner = self.admission_ready(input.turn_id)?;
        let update = self
            .exchange_preparation(
                owner,
                input
                    .step_store
                    .and_then(|step| step.get::<codex_extension_api::CapturedModelAttempt>())
                    .map(|captured| captured.0.clone()),
                input
                    .step_store
                    .and_then(|step| step.get::<codex_extension_api::ToolsOnlyWorkCancellation>())
                    .map(|scope| scope.0.clone()),
                [
                    "Expired model context owner",
                    "Model context reader closed",
                    "Model context transformation cancelled",
                ],
                PreparationReply::Context,
                |id| {
                    let mut request = json!({
                        "id": input.turn_id,
                        "msg": {
                            "type": "_sdk_transform_context", "turn_id": input.turn_id,
                            "thread": input.thread_id, "instance": self.instance,
                            "step_id": id, "model": input.model,
                            "purpose": if input.compaction { "compaction" } else { "turn" },
                            "hosted_prompt": input.hosted_prompt.is_some(),
                            "hosted_prompt_omissions": input.hosted_prompt.as_deref().map(|prompt| json!({
                                "omit_model":prompt.omit_model, "omit_instructions":prompt.omit_instructions,
                                "omit_tools":prompt.omit_tools,
                            })),
                            "items": input.items,
                        }
                    });
                    request["msg"]
                        .as_object_mut()
                        .expect("constructed callback object")
                        .extend(source.clone());
                    request
                },
                |update| update.context("Model context transformation failed"),
            )
            .await?;
        match update {
            ModelContextUpdate::Keep => {}
            ModelContextUpdate::Replace(items) => *input.items = items,
            ModelContextUpdate::Hosted(reply) => {
                let target = input
                    .hosted_prompt
                    .as_deref_mut()
                    .context("Unexpected hosted prompt reply")?;
                target.reference = Some(reply.hosted_prompt);
                if reply.explicit_tools {
                    target.omit_tools = false;
                }
                if let Some(items) = reply.items {
                    *input.items = items;
                }
            }
        }
        Ok(())
    }

    pub(crate) fn customizes_compaction(&self) -> bool {
        self.before_compact_enabled
    }

    pub(crate) async fn before_compact(
        self: &Arc<Self>,
        input: codex_extension_api::CompactionInput<'_>,
    ) -> Result<Option<String>> {
        let source = self.source_metadata(input.step_store)?;
        if input
            .step_store
            .and_then(|step| step.get::<ProfileCatalog>())
            .is_some_and(|catalog| !catalog.before_compact)
        {
            return Ok(None);
        }
        let owner = self.admission_ready(input.turn_id)?;
        self.exchange_preparation(
            owner,
            None,
            input
                .step_store
                .and_then(|step| step.get::<codex_extension_api::ToolsOnlyWorkCancellation>())
                .map(|scope| scope.0.clone()),
            [
                "Expired compaction owner",
                "Compaction callback reader closed",
                "Compaction callback cancelled",
            ],
            PreparationReply::Compaction,
            |id| {
                let mut request = json!({
                    "id": input.turn_id,
                    "msg": {
                        "type": "_sdk_before_compact", "turn_id": input.turn_id,
                        "thread": input.thread_id, "instance": self.instance,
                        "step_id": id, "model": input.model,
                        "trigger": if input.automatic { "auto" } else { "manual" },
                        "items": input.items,
                    }
                });
                request["msg"]
                    .as_object_mut()
                    .expect("constructed callback object")
                    .extend(source.clone());
                request
            },
            |summary| summary.map_err(|_| anyhow::anyhow!("Compaction callback failed")),
        )
        .await
    }

    pub(crate) fn reply_compaction(
        self: &Arc<Self>,
        id: u64,
        summary: std::result::Result<Option<String>, ()>,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> Result<()> {
        let PreparationReply::Compaction(reply) =
            self.step_owner(thread, instance)?.take_preparation(id)?
        else {
            anyhow::bail!("Invalid compaction reply");
        };
        reply
            .send(summary)
            .map_err(|_| anyhow::anyhow!("Expired compaction callback"))
    }

    pub(crate) async fn before_complete(
        self: &Arc<Self>,
        input: codex_extension_api::TurnCompletionInput<'_>,
    ) -> Result<Option<String>> {
        let source = self.source_metadata(input.step_store)?;
        if input
            .step_store
            .and_then(|step| step.get::<ProfileCatalog>())
            .is_some_and(|catalog| !catalog.before_complete)
        {
            return Ok(None);
        }
        if !self.before_complete_enabled {
            return Ok(None);
        }
        let owner = self.admission_ready(input.turn_id)?;
        let stop =
            crate::native_interception::capture_step_stop(input.thread_store, input.turn_store)
                .context("Completion owner unavailable")?;
        self.exchange_preparation(
            owner,
            input
                .step_store
                .and_then(|step| step.get::<codex_extension_api::CapturedModelAttempt>())
                .map(|captured| captured.0.clone()),
            input
                .step_store
                .and_then(|step| step.get::<codex_extension_api::ToolsOnlyWorkCancellation>())
                .map(|scope| scope.0.clone()),
            [
                "Expired completion owner",
                "Completion callback reader closed",
                "Completion callback cancelled",
            ],
            |reply| PreparationReply::Completion { reply, stop },
            |id| {
                let mut request = json!({
                    "id": input.turn_id,
                    "msg": {
                        "type": "_sdk_before_complete", "turn_id": input.turn_id,
                        "thread": input.thread_store.level_id(), "instance": self.instance,
                        "step_id": id, "model": input.model,
                        "text": input.last_assistant_message,
                        "continuation_count": input.continuation_count,
                    }
                });
                request["msg"]
                    .as_object_mut()
                    .expect("constructed callback object")
                    .extend(source.clone());
                request
            },
            |instructions| instructions.map_err(|_| anyhow::anyhow!("Completion callback failed")),
        )
        .await
    }

    pub(crate) fn reply_completion(
        self: &Arc<Self>,
        id: u64,
        instructions: std::result::Result<Option<String>, ()>,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> Result<()> {
        let PreparationReply::Completion { reply, .. } =
            self.step_owner(thread, instance)?.take_preparation(id)?
        else {
            anyhow::bail!("Invalid completion reply");
        };
        reply
            .send(instructions)
            .map_err(|_| anyhow::anyhow!("Expired completion callback"))
    }

    pub(crate) fn reply_tool_completion(
        self: &Arc<Self>,
        id: u64,
        decision: std::result::Result<ToolCompletionReply, ()>,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> Result<()> {
        let PreparationReply::ToolCompletion { reply, .. } =
            self.step_owner(thread, instance)?.take_preparation(id)?
        else {
            anyhow::bail!("Invalid terminal tool proposal reply");
        };
        reply
            .send(decision)
            .map_err(|_| anyhow::anyhow!("Expired terminal tool proposal"))
    }

    pub(crate) fn retire_steps(&self) {
        self.retire_model_producer();
        self.retire_prepared_requests();
        let mut replies = self
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        self.preparations_stopped.store(true, Ordering::Release);
        if let Some(pending) = replies.as_mut() {
            pending.retain(|_, slot| {
                slot.reply.take();
                self.cancel_preparation(&slot.callback, &slot.turn);
                !slot.callback.released.load(Ordering::Acquire)
            });
        }
    }

    pub(crate) fn reply_step(&self, id: u64, names: Option<Vec<ToolName>>) -> Result<()> {
        let PreparationReply::Step(reply) = self.take_preparation(id)? else {
            anyhow::bail!("Invalid step selection reply");
        };
        reply
            .reply
            .send(names)
            .map_err(|_| anyhow::anyhow!("Expired step selection"))
    }

    pub(crate) fn stop_owned_step(
        self: &Arc<Self>,
        id: u64,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> bool {
        let Ok(owner) = self.step_owner(thread, instance) else {
            return false;
        };
        let stop = match owner.take_preparation(id) {
            Ok(PreparationReply::Step(pending)) => {
                if pending.reply.send(None).is_err() {
                    return false;
                }
                pending.stop
            }
            Ok(PreparationReply::ToolCompletion { reply, stop }) => {
                if reply.send(Err(())).is_err() {
                    return false;
                }
                stop
            }
            Ok(PreparationReply::Completion { reply, stop }) => {
                if reply.send(Err(())).is_err() {
                    return false;
                }
                stop
            }
            _ => return false,
        };
        // Claim the live preparation before fencing its captured turn. Neither
        // a late reply nor a successor with the same display ID owns this stop.
        stop.stop();
        true
    }

    fn take_preparation(&self, id: u64) -> Result<PreparationReply> {
        self.preparation_replies
            .lock()
            .expect("Python preparation replies poisoned")
            .as_mut()
            .context("Expired preparation")?
            .get_mut(&id)
            .filter(|slot| !slot.is_closed())
            .and_then(|slot| slot.reply.take())
            .context("Expired preparation")
    }

    fn step_owner(
        self: &Arc<Self>,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> Result<Arc<Self>> {
        let owner = if let Some(thread) = thread {
            self.family
                .members
                .read()
                .expect("Python graph poisoned")
                .get(thread)
                .and_then(Weak::upgrade)
                .context("Expired step owner")?
        } else {
            Arc::clone(self)
        };
        ensure!(
            owner.instance.as_deref() == instance,
            "Expired step incarnation"
        );
        Ok(owner)
    }

    pub(crate) fn check_step(
        self: &Arc<Self>,
        id: u64,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> Result<()> {
        let owner = self.step_owner(thread, instance)?;
        let replies = owner
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        ensure!(
            replies
                .as_ref()
                .and_then(|pending| pending.get(&id))
                .is_some_and(|pending| !pending.is_closed()),
            "Expired step selection"
        );
        Ok(())
    }

    pub(crate) fn reply_owned_step(
        self: &Arc<Self>,
        id: u64,
        names: Option<Vec<ToolName>>,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> Result<()> {
        self.step_owner(thread, instance)?.reply_step(id, names)
    }

    pub(crate) fn reply_context(
        self: &Arc<Self>,
        id: u64,
        update: Option<ModelContextUpdate>,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> Result<()> {
        let PreparationReply::Context(reply) =
            self.step_owner(thread, instance)?.take_preparation(id)?
        else {
            anyhow::bail!("Invalid model context reply");
        };
        reply
            .send(update)
            .map_err(|_| anyhow::anyhow!("Expired model context transformation"))
    }

    pub(crate) fn retain_context_revision(&self, source: &Self) -> Result<()> {
        let original = source.snapshot();
        let mut current = self.current.write().expect("Python catalog poisoned");
        ensure!(
            current.id == 0,
            "compaction candidate tool revision is no longer fresh"
        );
        ensure!(
            checkpoint_specs(&current.specs)? == checkpoint_specs(&original.specs)?
                && current
                    .entries
                    .iter()
                    .map(|entry| entry.manual)
                    .eq(original.entries.iter().map(|entry| entry.manual)),
            "compaction candidate must retain current tool declarations and bindings"
        );
        // Revalidate against this candidate's policy. Only the revision number
        // survives: pending calls, graph admissions and reply owners stay local.
        let mut seed = self.prepare(original.id, current.specs.clone())?;
        seed.set_input_errors(
            &current
                .entries
                .iter()
                .enumerate()
                .filter_map(|(slot, entry)| entry.input_error_recovery.then_some(slot))
                .collect::<Vec<_>>(),
        )?;
        seed.set_manual(
            &current
                .entries
                .iter()
                .enumerate()
                .filter_map(|(slot, entry)| entry.manual.then_some(slot))
                .collect::<Vec<_>>(),
        )?;
        seed.set_finish(
            &current
                .entries
                .iter()
                .enumerate()
                .filter_map(|(slot, entry)| entry.finish_turn.then_some(slot))
                .collect::<Vec<_>>(),
        )?;
        *current = Arc::new(seed);
        Ok(())
    }

    pub async fn replace(
        &self,
        thread: &CodexThread,
        id: u64,
        specs: Vec<DynamicToolSpec>,
        manual_slots: &[usize],
    ) -> Result<u64> {
        self.replace_with_defaults(thread, id, specs, manual_slots, None, None, &[], &[])
            .await
    }

    pub async fn replace_with_defaults(
        &self,
        thread: &CodexThread,
        id: u64,
        specs: Vec<DynamicToolSpec>,
        manual_slots: &[usize],
        default_slots: Option<&[usize]>,
        profile_update: Option<&crate::agent_profiles::ProfileToolUpdate>,
        finish_slots: &[usize],
        input_error_slots: &[usize],
    ) -> Result<u64> {
        if let Some(id) = crate::agent_profiles::replace_tools(
            thread,
            id,
            specs.clone(),
            manual_slots,
            default_slots,
            profile_update,
            finish_slots,
            input_error_slots,
        )
        .await?
        {
            return Ok(id);
        }
        ensure!(
            Some(id) == self.generation_high_water().checked_add(1),
            "Python tool generation is out of sequence"
        );
        let mut candidate = self.prepare(id, specs)?;
        candidate.set_manual(manual_slots)?;
        candidate.set_finish(finish_slots)?;
        candidate.set_input_errors(input_error_slots)?;
        let defaults = candidate.default_names(default_slots)?;
        self.check_idle(thread).await?;
        let allocated = self.allocate_generation()?;
        ensure!(id == allocated, "Python tool generation is out of sequence");
        let generation = self.publish(candidate)?;
        *self.step_defaults.write().expect("Step defaults poisoned") = defaults;
        crate::hosted_prompt::explicit_root_tools(thread).await?;
        Ok(generation)
    }

    fn publish(&self, candidate: Generation) -> Result<u64> {
        let id = candidate.id;
        let mut current = self.current.write().expect("Python catalog poisoned");
        ensure!(
            id > current.id && id <= self.generation_high_water.load(Ordering::Acquire),
            "Python tool generation is out of sequence"
        );
        *current = Arc::new(candidate);
        Ok(id)
    }

    pub async fn check_idle(&self, thread: &CodexThread) -> Result<()> {
        ensure!(
            thread.active_turn_environment_selections().await.is_none()
                && !matches!(
                    thread.agent_status().await,
                    AgentStatus::Running | AgentStatus::Shutdown | AgentStatus::NotFound
                ),
            "replaceable tools require an idle, open native thread"
        );
        ensure!(
            self.pending
                .lock()
                .expect("Python replies poisoned")
                .is_empty(),
            "Python tool replies are pending"
        );
        let restored = self
            .manual_restore
            .lock()
            .expect("Manual restore poisoned")
            .as_ref()
            .and_then(Weak::upgrade);
        ensure!(
            restored.is_none_or(|owner| owner
                .lock()
                .expect("Manual continuation poisoned")
                .is_none()),
            "Manual continuation still owns the native batch"
        );
        Ok(())
    }

    pub(crate) fn fail_pending(&self, key: &PendingKey) -> bool {
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        if key.reply.instance != self.instance {
            return false;
        }
        let Some(call) = pending.get_mut(key) else {
            return false;
        };
        if let Some(callback) = call.callback.clone() {
            drop(std::mem::replace(&mut call.sender, closed_tool_sender()));
            self.cancel_callback(key, &callback);
            if !callback.entered.load(Ordering::Acquire) {
                pending.remove(key);
            }
        } else {
            pending.remove(key);
        }
        self.failed_turns
            .lock()
            .expect("Python failures poisoned")
            .insert(key.turn.clone());
        true
    }

    pub(crate) fn take_failed(&self, turn_id: &str) -> bool {
        self.failed_turns
            .lock()
            .expect("Python failures poisoned")
            .remove(turn_id)
    }

    // An automatic checkpoint is the one busy snapshot admitted by the SDK.
    // Do not accept a name, a past generation, or an expired reply as authority.
    pub fn check_pending(&self, owner: &str, key: &PendingKey) -> Result<()> {
        ensure!(
            key.reply.thread == owner
                && key.reply.instance == self.instance
                && self.resolve_generation(key.reply.generation).is_some()
                && self
                    .pending
                    .lock()
                    .expect("Python replies poisoned")
                    .get(key)
                    .is_some_and(|call| !call.sender.is_closed()),
            "Expired or invalid managed Python tool request"
        );
        Ok(())
    }

    pub(crate) fn restored_media_model(
        &self,
        key: &PendingKey,
        response: &DynamicToolResponse,
    ) -> Result<Option<String>> {
        if !response.content_items.iter().any(|part| {
            matches!(
                part,
                codex_protocol::dynamic_tools::DynamicToolCallOutputContentItem::InputFile { .. }
                    | codex_protocol::dynamic_tools::DynamicToolCallOutputContentItem::InputImage { .. }
            )
        }) {
            return Ok(None);
        }
        let pending = self.pending.lock().expect("Python replies poisoned");
        let Some(call) = pending.get(key) else {
            return Ok(None);
        };
        if !call.reentry {
            return Ok(None);
        }
        match call
            .manual_request
            .as_ref()
            .or(call.approval_request.as_ref())
        {
            Some(request) => Ok(Some(
                request["msg"]["_sdk_model"]
                    .as_str()
                    .context("restored media reply lacks its admitted original model")?
                    .to_owned(),
            )),
            // Fresh SDK approval reentry uses the current native thread configuration.
            None => Ok(None),
        }
    }

    // Private callback admission only. Checkpoint/mutation/manual entry checks stay strict.
    pub(crate) fn admit_callback(
        &self,
        owner: &str,
        key: &PendingKey,
        invocation: u64,
        entering: bool,
    ) -> Result<bool> {
        ensure!(
            key.reply.thread == owner
                && key.reply.instance == self.instance
                && self.resolve_generation(key.reply.generation).is_some(),
            "Expired or invalid managed Python tool request"
        );
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        let Some(call) = pending.get_mut(key) else {
            return Ok(false);
        };
        ensure!(
            call.invocation == invocation,
            "Invalid managed Python invocation nonce"
        );
        if call.retired || call.sender.is_closed() {
            return Ok(false);
        }
        let deadline = call.deadline.clone();
        let mut admit = || -> Result<bool> {
            if entering {
                ensure!(self.sdk_owned(), "effect entry requires the SDK catalog");
                ensure!(
                    call.phase == Phase::Admitted,
                    "Python tool effect was entered or parked"
                );
                call.phase = Phase::Entered;
            }
            Ok(true)
        };
        // This synchronous gate linearizes admission with this exact turn's expiry.
        match deadline {
            Some(state) => state.admit(admit).unwrap_or(Ok(false)),
            None => admit(),
        }
    }

    pub(crate) fn is_reentry(&self, key: &PendingKey) -> bool {
        self.pending
            .lock()
            .expect("Python replies poisoned")
            .get(key)
            .is_some_and(|call| call.reentry)
    }

    pub(crate) fn park(&self, owner: &str, key: &PendingKey) -> Result<bool> {
        ensure!(
            self.sdk_owned(),
            "approval parking requires the SDK catalog"
        );
        if key.reply.thread != owner {
            return Ok(false);
        }
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        let Some(call) = pending.get_mut(key) else {
            return Ok(false);
        };
        ensure!(
            call.phase == Phase::Admitted,
            "only a not-entered tool can park"
        );
        call.phase = Phase::Parked;
        Ok(true)
    }

    // Only the live SDK Agent.as_tool invocation can supply this after its
    // owned child returned unresolved approvals. It is not "not started".
    pub(crate) fn park_continuation(
        &self,
        owner: &str,
        key: &PendingKey,
        continuation: Value,
    ) -> Result<bool> {
        ensure!(
            self.sdk_owned(),
            "child continuation requires the SDK catalog"
        );
        ensure!(
            continuation
                .as_object()
                .is_some_and(|value| !value.is_empty()),
            "child continuation must contain its immutable history and receipts"
        );
        if key.reply.thread != owner {
            return Ok(false);
        }
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        let Some(call) = pending.get_mut(key) else {
            return Ok(false);
        };
        ensure!(
            call.phase == Phase::Entered,
            "child continuation requires a live entered call"
        );
        call.phase = Phase::Parked;
        call.continuation = Some(continuation);
        Ok(true)
    }

    pub(crate) fn continuations(&self, keys: &[PendingKey]) -> Result<HashMap<String, Value>> {
        let pending = self.pending.lock().expect("Python replies poisoned");
        let mut values = HashMap::new();
        for key in keys {
            let call = pending
                .get(key)
                .ok_or_else(|| anyhow::anyhow!("expired parked SDK call"))?;
            ensure!(call.phase == Phase::Parked, "SDK call is not parked");
            if let Some(value) = &call.continuation {
                values.insert(key.call.clone(), value.clone());
            }
        }
        Ok(values)
    }

    pub(crate) fn parked(&self, owner: &str) -> Result<Vec<PendingKey>> {
        ensure!(
            self.sdk_owned(),
            "approval suspension requires the SDK catalog"
        );
        let pending = self.pending.lock().expect("Python replies poisoned");
        ensure!(!pending.is_empty(), "no parked approval");
        ensure!(
            pending
                .iter()
                .all(|(key, call)| key.reply.thread == owner && call.phase == Phase::Parked),
            "non-parked tool callbacks remain"
        );
        Ok(pending.keys().cloned().collect())
    }

    pub(crate) fn suspended(&self) -> bool {
        self.suspended.load(Ordering::Acquire)
    }

    pub(crate) fn suspend_reentry(&self, keys: &[PendingKey]) -> Result<()> {
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        ensure!(
            keys.iter().all(|key| pending
                .get(key)
                .is_some_and(|call| call.reentry && call.phase == Phase::Parked)),
            "approval re-entry is not settled"
        );
        self.suspended.store(true, Ordering::Release);
        // Dropping these senders expires the current re-entry futures. This
        // does not return a model-visible result or revive an old sender.
        for key in keys {
            pending.remove(key);
        }
        Ok(())
    }

    pub(crate) fn cancel_reentry(&self, keys: &[PendingKey]) {
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        for key in keys {
            pending.remove(key);
        }
    }

    // Only this current native owner can prove that an admitted callback did
    // not enter its tool effect. Freeze those calls before writing the same
    // approval seal used by ordinary parking. An entered/uncertain sibling
    // prevents sealing the batch; it is never converted to "not started".
    pub(crate) fn park_cancelled_reentry(
        &self,
        owner: &str,
        keys: &[PendingKey],
    ) -> Vec<PendingKey> {
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        let live: Vec<_> = keys
            .iter()
            .filter(|key| pending.contains_key(*key))
            .cloned()
            .collect();
        if live.iter().any(|key| {
            let call = &pending[key];
            key.reply.thread != owner
                || self.resolve_generation(key.reply.generation).is_none()
                || !call.reentry
                || call.phase == Phase::Entered
        }) {
            return Vec::new();
        }
        for key in &live {
            pending.get_mut(key).expect("selected live re-entry").phase = Phase::Parked;
        }
        live
    }

    // EOF's IPC publishers have joined. Copy only unreturned bindings of this
    // live owner/generation before Core cancels and drops their reply senders.
    pub(crate) fn pending_on_loss(&self, owner: &str) -> Vec<PendingKey> {
        self.pending
            .lock()
            .expect("Python replies poisoned")
            .keys()
            .filter(|key| {
                key.reply.thread == owner
                    && self
                        .resolve_generation(key.reply.generation)
                        .is_some_and(|generation| key.reply.slot < generation.entries.len())
            })
            .cloned()
            .collect()
    }

    fn snapshot(&self) -> Arc<Generation> {
        self.current
            .read()
            .expect("Python catalog poisoned")
            .clone()
    }

    pub(crate) fn manual_snapshot(
        self: &Arc<Self>,
        owner: &str,
    ) -> Result<Vec<(u64, ManualToolGrant, String)>> {
        let pending = self.pending.lock().expect("Python replies poisoned");
        let mut result = Vec::new();
        for (key, call) in pending.iter() {
            if key.reply.thread != owner
                || self.resolve_generation(key.reply.generation).is_none()
                || call.sender.is_closed()
            {
                continue;
            }
            if let Some(request) = &call.manual_request {
                result.push((
                    call.invocation,
                    ManualToolGrant {
                        owner: ManualOwner::Core(Arc::downgrade(self)),
                        key: key.clone(),
                        invocation: call.invocation,
                    },
                    serde_json::to_string(request)?,
                ));
            }
        }
        result.sort_by_key(|(invocation, _, _)| *invocation);
        Ok(result)
    }

    pub(crate) fn family_manual_snapshot(
        self: &Arc<Self>,
        root: &str,
    ) -> Result<Vec<(u64, ManualToolGrant, String)>> {
        let mut result = self.manual_snapshot(root)?;
        let members = self
            .family
            .members
            .read()
            .expect("Python family poisoned")
            .iter()
            .filter_map(|(id, member)| member.upgrade().map(|member| (id.clone(), member)))
            .collect::<Vec<_>>();
        for (id, member) in members {
            if id != root && Arc::ptr_eq(&member.family, &self.family) {
                result.extend(member.manual_snapshot(&id)?);
            }
        }
        result.sort_by_key(|(invocation, _, _)| *invocation);
        Ok(result)
    }

    pub(crate) fn check_manual(self: &Arc<Self>, owner: &str, grant: &ManualToolGrant) -> bool {
        matches!(&grant.owner, ManualOwner::Core(owner) if owner.ptr_eq(&Arc::downgrade(self)))
            && grant.key.reply.thread == owner
            && self
                .resolve_generation(grant.key.reply.generation)
                .is_some()
            && self
                .pending
                .lock()
                .expect("Python replies poisoned")
                .get(&grant.key)
                .is_some_and(|call| {
                    call.invocation == grant.invocation
                        && call.manual_request.is_some()
                        && call.phase == Phase::Admitted
                        && !call.sender.is_closed()
                })
    }

    pub(crate) fn respond_manual(
        self: &Arc<Self>,
        owner: &str,
        grant: &ManualToolGrant,
        response: DynamicToolResponse,
        start_response: bool,
    ) -> bool {
        let Some(generation) = self.resolve_generation(grant.key.reply.generation) else {
            return false;
        };
        if !matches!(&grant.owner, ManualOwner::Core(owner) if owner.ptr_eq(&Arc::downgrade(self)))
            || grant.key.reply.thread != owner
        {
            return false;
        }
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        if !pending.get(&grant.key).is_some_and(|call| {
            call.invocation == grant.invocation
                && call.manual_request.is_some()
                && call.phase == Phase::Admitted
                && !call.sender.is_closed()
        }) {
            return false;
        }
        let receipt = response.clone();
        let accepted = pending
            .remove(&grant.key)
            .expect("checked live invocation")
            .sender
            .send(ToolReply {
                response,
                start_response,
                acceptance: None,
            })
            .is_ok();
        if accepted {
            if let Some(owner) = self
                .manual_restore
                .lock()
                .expect("Manual restore poisoned")
                .as_ref()
                .and_then(|owner| owner.upgrade())
            {
                if let Some(frame) = owner.lock().expect("Manual continuation poisoned").as_mut() {
                    frame.record(&grant.key, grant.invocation, generation.clone(), &receipt);
                }
            }
        }
        accepted
    }

    /// Capture the decision requirement before exposing an approval interaction.
    pub(crate) fn require_approval(
        self: &Arc<Self>,
        owner: &str,
        grant: &ManualToolGrant,
        arguments: Option<Value>,
    ) -> bool {
        if !self.check_manual(owner, grant) {
            return false;
        }
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        let Some(call) = pending.get_mut(&grant.key) else {
            return false;
        };
        let Some(request) = call.manual_request.as_mut() else {
            return false;
        };
        request["msg"]["_sdk_approval_required"] = Value::Bool(true);
        if let Some(arguments) = arguments {
            request["msg"]["_sdk_effective_arguments"] = arguments;
        }
        true
    }

    pub(crate) fn manual_approval_state(&self, key: &PendingKey) -> Result<(bool, Option<Value>)> {
        let pending = self.pending.lock().expect("Python replies poisoned");
        let request = pending
            .get(key)
            .and_then(|call| call.manual_request.as_ref())
            .context("Manual approval call is no longer pending")?;
        Ok((
            request["msg"]["_sdk_approval_required"] == true,
            request["msg"].get("_sdk_effective_arguments").cloned(),
        ))
    }

    pub(crate) fn restore_approval_requirement(
        &self,
        call_id: &str,
        required: bool,
        arguments: Option<Value>,
    ) {
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        for (key, call) in pending.iter_mut().filter(|(key, _)| key.call == call_id) {
            let _ = key;
            if let Some(request) = call.manual_request.as_mut() {
                request["msg"]["_sdk_restored_approval"] = Value::Bool(true);
                request["msg"]["_sdk_approval_required"] = Value::Bool(required);
                if let Some(arguments) = &arguments {
                    request["msg"]["_sdk_effective_arguments"] = arguments.clone();
                }
            }
        }
    }

    /// Claim the exact native waiter once, then use the ordinary callback inbox.
    pub(crate) fn approve_manual(
        self: &Arc<Self>,
        owner: &str,
        grant: &ManualToolGrant,
    ) -> Result<bool> {
        if !self.check_manual(owner, grant) {
            return Ok(false);
        }
        let request = {
            let mut pending = self.pending.lock().expect("Python replies poisoned");
            let Some(call) = pending.get_mut(&grant.key) else {
                return Ok(false);
            };
            let Some(original) = call.manual_request.as_ref() else {
                return Ok(false);
            };
            ensure!(
                original["msg"]["_sdk_approval_required"] == true,
                "Tool has no captured approval requirement"
            );
            ensure!(
                original["msg"]["_sdk_code_origin"].is_null(),
                "Managed approvals require a direct function call"
            );
            if call
                .deadline
                .as_ref()
                .is_some_and(|deadline| !deadline.admit(|| true).unwrap_or(false))
            {
                return Ok(false);
            }
            let mut request = original.clone();
            let callback = CallbackRelease::new(
                json!({"kind":"tool","request":grant.key,"invocation":grant.invocation.to_string()}),
            );
            request["msg"]["_sdk_manual"] = Value::Bool(false);
            request["msg"]["_sdk_callback"] = callback.receipt.clone();
            call.approval_request = call.manual_request.take();
            call.callback = Some(callback);
            request
        };
        self.family
            .requests
            .send(QueuedRequest::Call(
                Arc::downgrade(self),
                grant.key.clone(),
                grant.invocation,
                request,
            ))
            .map_err(|_| anyhow::anyhow!("Managed approval owner closed"))?;
        Ok(true)
    }

    pub(crate) fn record_approved_result(&self, key: &PendingKey, response: &DynamicToolResponse) {
        let Some(generation) = self.resolve_generation(key.reply.generation) else {
            return;
        };
        let invocation = self
            .pending
            .lock()
            .expect("Python replies poisoned")
            .get(key)
            .filter(|call| call.approval_request.is_some())
            .map(|call| call.invocation);
        let Some(invocation) = invocation else {
            return;
        };
        if let Some(owner) = self
            .manual_restore
            .lock()
            .expect("Manual restore poisoned")
            .as_ref()
            .and_then(|owner| owner.upgrade())
        {
            if let Some(frame) = owner.lock().expect("Manual continuation poisoned").as_mut() {
                frame.record(key, invocation, generation, response);
            }
        }
    }

    pub(crate) fn bind_manual_restore(&self, owner: crate::manual_checkpoint::WeakOwner) {
        *self.manual_restore.lock().expect("Manual restore poisoned") = Some(owner);
    }

    pub(crate) fn enqueue_manual_reentry(
        self: &Arc<Self>,
        owner: &str,
        turn: &str,
        item: &ResponseItem,
        model: Option<&str>,
    ) -> Result<oneshot::Receiver<ToolReply>> {
        ensure!(
            !self.managed_profiles.load(Ordering::Acquire),
            "Managed Agent continuation requires its captured profile"
        );
        self.enqueue_manual_reentry_source(owner, turn, item, model, self.snapshot(), None)
    }

    pub(crate) fn enqueue_manual_reentry_source(
        self: &Arc<Self>,
        owner: &str,
        turn: &str,
        item: &ResponseItem,
        model: Option<&str>,
        generation: Arc<Generation>,
        profile: Option<codex_extension_api::AgentProfileReceipt>,
    ) -> Result<oneshot::Receiver<ToolReply>> {
        ensure!(
            self.admits_generation(&generation),
            "Expired manual continuation catalog"
        );
        let ResponseItem::FunctionCall {
            name,
            namespace,
            arguments,
            call_id,
            ..
        } = item
        else {
            anyhow::bail!("Manual continuation requires a function call");
        };
        let slot = generation
            .entries
            .iter()
            .position(|entry| {
                entry.manual
                    && entry.name.clone().with_default_namespace()
                        == ToolName::new(namespace.clone(), name.clone()).with_default_namespace()
            })
            .context("Pending manual declaration is absent")?;
        self.enqueue_profile(
            owner,
            &generation,
            slot,
            turn,
            call_id,
            serde_json::from_str(arguments)?,
            None,
            &ToolCallSource::Direct,
            model,
            true,
            profile.as_ref(),
            None,
            None,
        )
        .map(|(_, _, receiver)| receiver)
    }

    fn record_completed_call(&self, turn_id: &str, call_id: &str) {
        if let Some(admission) = self
            .admission
            .lock()
            .expect("Python admission receipt poisoned")
            .as_ref()
            && admission.turn_id == turn_id
        {
            let recorded = admission
                .completed_calls
                .lock()
                .expect("Completed calls poisoned")
                .insert(call_id.to_owned());
            if recorded {
                self.manual_progress.send_modify(|version| {
                    *version = version.wrapping_add(1);
                });
            }
        }
    }

    pub(crate) fn manual_progress(&self) -> u64 {
        *self.manual_progress.borrow()
    }

    pub(crate) async fn wait_manual_progress(&self, version: u64) -> Result<()> {
        let mut progress = self.manual_progress.subscribe();
        progress.wait_for(|current| *current != version).await?;
        Ok(())
    }

    pub(crate) fn completed_calls(&self, turn: &str) -> HashSet<String> {
        self.admission
            .lock()
            .expect("Python admission receipt poisoned")
            .as_ref()
            .filter(|receipt| receipt.turn_id == turn)
            .map(|receipt| {
                receipt
                    .completed_calls
                    .lock()
                    .expect("Completed calls poisoned")
                    .clone()
            })
            .unwrap_or_default()
    }

    /// Publication classification from the currently admitted catalogue, never saved markers.
    pub(crate) fn private_terminal_calls(
        &self,
        source: Option<&codex_extension_api::CapturedAgentProfile>,
        history: &[codex_history::RolloutItem],
        private_calls: &HashSet<String>,
    ) -> Result<HashSet<String>> {
        let generation = if let Some(source) = source {
            source
                .profile
                .data
                .get::<ProfileCatalog>()
                .context("Captured source has no tool catalog")?
                .generation
                .clone()
        } else {
            ensure!(
                !self.managed_profiles.load(Ordering::Acquire),
                "Missing captured terminal source"
            );
            self.snapshot()
        };
        ensure!(
            self.admits_generation(&generation),
            "Expired terminal source catalog"
        );
        let published: HashSet<_> = history
            .iter()
            .filter_map(|row| match row {
                codex_history::RolloutItem::ResponseItem(envelope) => match &envelope.item {
                    ResponseItem::FunctionCallOutput {
                        call_id: Some(id), ..
                    } => Some(id),
                    _ => None,
                },
                _ => None,
            })
            .collect();
        Ok(history
            .iter()
            .filter_map(|row| {
                let codex_history::RolloutItem::ResponseItem(envelope) = row else {
                    return None;
                };
                let ResponseItem::FunctionCall {
                    call_id,
                    name,
                    namespace,
                    ..
                } = &envelope.item
                else {
                    return None;
                };
                (private_calls.contains(call_id)
                    && !published.contains(call_id)
                    && generation.entries.iter().any(|entry| {
                        entry.finish_turn
                            && entry.name == ToolName::new(namespace.clone(), name.clone())
                    }))
                .then(|| call_id.clone())
            })
            .collect())
    }

    pub(crate) fn held_tool_outputs(
        &self,
        turn: &str,
    ) -> Vec<(codex_history::ResponseItemEnvelope, bool)> {
        self.admission
            .lock()
            .expect("Python admission receipt poisoned")
            .as_ref()
            .filter(|receipt| receipt.turn_id == turn)
            .map(|receipt| {
                receipt
                    .held_tool_outputs
                    .outputs
                    .lock()
                    .expect("held terminal outputs poisoned")
                    .clone()
            })
            .unwrap_or_default()
    }

    pub(crate) fn pending_manual_keys(&self, owner: &str) -> Result<Vec<PendingKey>> {
        let pending = self.pending.lock().expect("Python replies poisoned");
        ensure!(!pending.is_empty(), "No pending manual calls");
        ensure!(
            pending.iter().all(|(key, value)| key.reply.thread == owner
                && value.manual_request.is_some()
                && !value.sender.is_closed()),
            "Automatic work has not settled"
        );
        Ok(pending.keys().cloned().collect())
    }

    pub async fn next_request(&self) -> Value {
        let mut receiver = self.family.receiver.lock().await;
        loop {
            match receiver.recv().await {
                Some(QueuedRequest::Callback(notice)) => return notice,
                Some(QueuedRequest::Step(request)) => return request,
                Some(QueuedRequest::Call(owner, key, invocation, request)) => {
                    if owner.upgrade().is_some_and(|tools| {
                        tools
                            .pending
                            .lock()
                            .expect("Python replies poisoned")
                            .get(&key)
                            .is_some_and(|pending| {
                                pending.invocation == invocation && !pending.sender.is_closed()
                            })
                    }) {
                        return request;
                    }
                }
                Some(QueuedRequest::Retired {
                    thread,
                    incarnation,
                }) => {
                    // This issuer may retire before the event reader ever
                    // discovers it. It therefore cannot supply a numeric handle.
                    return json!({
                        "id": "",
                        "subagent_id": thread,
                        "subagent_incarnation": incarnation,
                        "msg": {"type": "shutdown_complete"},
                    });
                }
                Some(QueuedRequest::ToolCompletionFailure(owner, receipt)) => {
                    if let (Some(owner), Some(receipt)) = (owner.upgrade(), receipt.upgrade())
                        && Arc::ptr_eq(&self.family, &owner.family)
                        && owner.owns_admission(&receipt)
                        && *receipt.state.borrow() == AdmissionState::Ready
                    {
                        return json!({"id":receipt.turn_id,"msg":{
                            "type":"_sdk_tool_completion_failed", "turn_id":receipt.turn_id,
                            "thread":receipt.thread_id,"instance":owner.instance,
                        }});
                    }
                }
                Some(QueuedRequest::GracefulFailure(owner, receipt)) => {
                    if let (Some(owner), Some(receipt)) = (owner.upgrade(), receipt.upgrade())
                        && std::ptr::eq(self, owner.as_ref())
                        && owner.instance.is_none()
                        && owner.owns_admission(&receipt)
                        && *receipt.state.borrow() == AdmissionState::Pending
                    {
                        return json!({
                            "id": receipt.turn_id,
                            "msg": {
                                "type": "_sdk_graceful_interrupt_failed",
                                "turn_id": receipt.turn_id,
                            }
                        });
                    }
                }
                Some(QueuedRequest::ModelFailure {
                    owner,
                    receipt,
                    thread,
                    policy_message,
                    response_integrity_reason,
                }) => {
                    if let (Some(owner), Some(receipt)) = (owner.upgrade(), receipt.upgrade())
                        && Arc::ptr_eq(&self.family, &owner.family)
                        && self
                            .family
                            .members
                            .read()
                            .expect("Python family poisoned")
                            .get(&thread)
                            .is_some_and(|member| Weak::ptr_eq(member, &Arc::downgrade(&owner)))
                        && owner.owns_admission(&receipt)
                        && *receipt.state.borrow() == AdmissionState::Pending
                    {
                        let mut event = json!({
                            "id": receipt.turn_id,
                            "msg": {
                                "type": "_sdk_model_generation_failed",
                                "turn_id": receipt.turn_id,
                                "message": policy_message.unwrap_or(crate::response_integrity::UNSUPPORTED_OUTPUT),
                                "policy_stop": policy_message.is_some(),
                                "response_integrity_reason": response_integrity_reason,
                            }
                        });
                        if let Some(instance) = &owner.instance {
                            event["subagent_id"] = json!(thread);
                            event["subagent_incarnation"] = json!(instance);
                        }
                        return event;
                    }
                }
                None => std::future::pending::<()>().await,
            }
        }
    }

    /// Data from the exact native pending receipt, never SDK-observed call metadata.
    pub(crate) fn native_media(&self, key: &PendingKey) -> Option<NativeMediaContext> {
        self.pending
            .lock()
            .expect("managed replies poisoned")
            .get(key)
            .and_then(|call| call.media.clone())
    }

    /// Check automatic reply eligibility without releasing Core's waiting sender.
    /// The outer option distinguishes a retired reply from a live no-media call.
    pub(crate) fn automatic_reply_media(
        &self,
        owner: &str,
        key: &PendingKey,
    ) -> Result<Option<Option<NativeMediaContext>>> {
        if key.reply.thread != owner || key.reply.instance != self.instance {
            return Ok(None);
        }
        let pending = self.pending.lock().expect("Python replies poisoned");
        let Some(call) = pending.get(key) else {
            return Ok(None);
        };
        ensure!(
            call.manual_request.is_none(),
            "Manual tool replies require their native grant"
        );
        ensure!(
            call.phase != Phase::Parked,
            "parked tool cannot reply from an old owner"
        );
        // Voice retirement retains closed callback senders as tombstones.
        // Preserve take_reply eligibility before asynchronous media validation.
        if call.sender.is_closed() {
            return Ok(None);
        }
        Ok(Some(call.media.clone()))
    }

    // Take the live sender only once. The binding retains it while its writer
    // commits the real result; a failed commit cannot be mistaken for success.
    pub(crate) fn take_reply(
        &self,
        owner: &str,
        key: &PendingKey,
    ) -> Result<Option<oneshot::Sender<ToolReply>>> {
        if key.reply.thread != owner || key.reply.instance != self.instance {
            return Ok(None);
        }
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        if let Some(call) = pending.get(key) {
            ensure!(
                call.manual_request.is_none(),
                "Manual tool replies require their native grant"
            );
            ensure!(
                call.phase != Phase::Parked,
                "parked tool cannot reply from an old owner"
            );
        }
        if let Some(call) = pending.get_mut(key) {
            if call.sender.is_closed() {
                return Ok(None);
            }
            if call.callback.is_some() {
                return Ok(Some(std::mem::replace(
                    &mut call.sender,
                    closed_tool_sender(),
                )));
            }
        }
        Ok(pending.remove(key).map(|call| call.sender))
    }

    fn enqueue(
        self: &Arc<Self>,
        thread: &str,
        generation: &Generation,
        slot: usize,
        turn: &str,
        call: &str,
        arguments: Value,
        source: &ToolCallSource,
        model: Option<&str>,
        reentry: bool,
        media: Option<NativeMediaContext>,
    ) -> Result<(PendingKey, u64, oneshot::Receiver<ToolReply>)> {
        self.enqueue_profile(
            thread, generation, slot, turn, call, arguments, None, source, model, reentry, None,
            media, None,
        )
    }

    fn enqueue_profile(
        self: &Arc<Self>,
        thread: &str,
        generation: &Generation,
        slot: usize,
        turn: &str,
        call: &str,
        arguments: Value,
        argument_error: Option<String>,
        source: &ToolCallSource,
        model: Option<&str>,
        reentry: bool,
        profile: Option<&codex_extension_api::AgentProfileReceipt>,
        media: Option<NativeMediaContext>,
        realtime_tool_call: Option<Value>,
    ) -> Result<(PendingKey, u64, oneshot::Receiver<ToolReply>)> {
        let entry = &generation.entries[slot];
        let key = PendingKey {
            reply: ReplyKey {
                thread: thread.to_owned(),
                generation: generation.id,
                slot,
                instance: self.instance.clone(),
            },
            turn: turn.to_owned(),
            call: call.to_owned(),
        };
        let invocation = self
            .family
            .next_invocation
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |n| n.checked_add(1))
            .map_err(|_| anyhow::anyhow!("Python tool invocation counter exhausted"))?;
        let code_origin = match source {
            ToolCallSource::Direct => Value::Null,
            ToolCallSource::CodeMode {
                cell_id,
                runtime_tool_call_id,
            } => json!({"cell_id": cell_id, "nested_call_id": runtime_tool_call_id}),
        };
        let tracing = if self.instance.is_some() {
            self.admission_ready(turn)?.tracing.clone()
        } else {
            codex_otel::sdk_trace::ContextLease::capture().map(Arc::new)
        };
        let callback = (!(entry.manual)).then(|| {
            CallbackRelease::new(
                json!({"kind":"tool","request":key,"invocation":(invocation).to_string()}),
            )
        });
        let mut request = json!({
            "id": call,
            "msg": {
                "type":"dynamic_tool_call_request", "turnId": turn, "callId": call,
                "namespace": entry.name.namespace, "tool": entry.name.name,
                "arguments": arguments, "_sdk_python_tool": key.reply,
                "_sdk_invocation": invocation, "_sdk_manual": entry.manual,
                "_sdk_code_origin": code_origin, "_sdk_model": model,
                "_sdk_realtime_tool_call": realtime_tool_call,
                "_sdk_realtime_tool_name": codex_extension_api::realtime_tool_wire_name(&entry.name),
                "_sdk_trace": codex_otel::current_span_w3c_trace_context()
            }
        });
        if let Some(error) = argument_error {
            request["msg"]["_sdk_argument_error"] = json!(error);
        }
        if let Some(callback) = &callback {
            request["msg"]["_sdk_callback"] = callback.receipt.clone();
        }
        if let Some(profile) = profile {
            request["msg"]["profile_id"] = json!(profile.profile_id);
            request["msg"]["profile_epoch"] = json!(profile.epoch);
        }
        if let Some(context) = &tracing {
            request["msg"]["tracing_context"] = json!(context.token());
        }
        let deadline = self
            .admission
            .lock()
            .expect("Python admission receipt poisoned")
            .as_ref()
            .filter(|receipt| receipt.turn_id == turn)
            .and_then(|receipt| receipt.sampling_stop.capture_model_deadline());
        let (sender, receiver) = oneshot::channel();
        {
            let mut pending = self.pending.lock().expect("Python replies poisoned");
            ensure!(!pending.contains_key(&key), "Duplicate Python tool call");
            pending.insert(
                key.clone(),
                PendingCall {
                    computer_arguments: matches!(
                        entry.spec,
                        ToolSpec::Computer { .. } | ToolSpec::ComputerUsePreview { .. }
                    )
                    .then(|| request["msg"]["arguments"].to_string()),
                    callback,
                    deadline,
                    media,
                    _tracing: tracing,
                    invocation,
                    retired: false,
                    approval_request: None,
                    manual_request: entry.manual.then(|| {
                        let mut manual = request.clone();
                        // Trace metadata belongs to automatic ToolContext callbacks,
                        // not the bounded manual action envelope.
                        manual["msg"].as_object_mut().unwrap().remove("_sdk_trace");
                        manual
                    }),
                    sender,
                    phase: Phase::Admitted,
                    reentry,
                    continuation: None,
                },
            );
        }
        if self
            .family
            .requests
            .send(QueuedRequest::Call(
                Arc::downgrade(self),
                key.clone(),
                invocation,
                request,
            ))
            .is_err()
        {
            self.pending
                .lock()
                .expect("Python replies poisoned")
                .remove(&key);
            anyhow::bail!("Python tool session closed");
        }
        Ok((key, invocation, receiver))
    }

    pub(crate) fn enqueue_reentry(
        self: &Arc<Self>,
        owner: &str,
        turn: &str,
        item: &ResponseItem,
    ) -> Result<(PendingKey, oneshot::Receiver<ToolReply>)> {
        ensure!(!self.suspended(), "SDK re-entry owner is suspended");
        ensure!(
            self.sdk_owned(),
            "approval re-entry requires the SDK catalog"
        );
        let generation = self.snapshot();
        ensure!(
            generation.entries.len() == 1 && generation.entries[0].name == sdk_name(),
            "SDK catalog is not installed"
        );
        let ResponseItem::FunctionCall {
            name,
            namespace,
            arguments,
            call_id,
            ..
        } = item
        else {
            anyhow::bail!("approval re-entry requires an admitted SDK call");
        };
        ensure!(
            ToolName::new(namespace.clone(), name.clone()) == sdk_name(),
            "approval re-entry cannot enable another tool"
        );
        self.enqueue(
            owner,
            &generation,
            0,
            turn,
            call_id,
            serde_json::from_str(arguments)?,
            &ToolCallSource::Direct,
            None,
            true,
            None,
        )
        .map(|(key, _, receiver)| (key, receiver))
    }

    pub(crate) fn validate_reentry(
        &self,
        calls: &[ResponseItem],
        continuations: &HashMap<String, Value>,
    ) -> Result<()> {
        ensure!(
            !self.suspended() && self.sdk_owned(),
            "SDK re-entry requires a current SDK owner"
        );
        let generation = self.snapshot();
        ensure!(
            generation.entries.len() == 1 && generation.entries[0].name == sdk_name(),
            "SDK catalog is not installed"
        );
        ensure!(!calls.is_empty(), "no calls to admit");
        let mut identities = HashSet::new();
        for call in calls {
            let Some((call_id, arguments)) = sdk_projection(call) else {
                anyhow::bail!("approval re-entry requires only admitted SDK projections");
            };
            ensure!(
                !call_id.is_empty() && identities.insert(call_id),
                "duplicate or empty SDK call ID"
            );
            let envelope: Value = serde_json::from_str(arguments)?;
            ensure!(
                envelope.get("original").is_some_and(Value::is_object),
                "missing original SDK call data"
            );
            // The JSON of original.arguments is deliberately not parsed.
        }
        ensure!(
            continuations
                .iter()
                .all(|(key, value)| identities.contains(key.as_str())
                    && value.as_object().is_some_and(|object| !object.is_empty())),
            "continuation is not bound to an admitted call"
        );
        Ok(())
    }
}

// Ignore Core-assigned item IDs and metadata, but match the exact admitted
// call ID and arguments. Neither a bare name nor "functions" grants this slot.
pub(crate) fn sdk_projection(item: &ResponseItem) -> Option<(&str, &str)> {
    match item {
        ResponseItem::FunctionCall {
            name,
            namespace,
            call_id,
            arguments,
            ..
        } if ToolName::new(namespace.clone(), name.clone()) == sdk_name() => {
            Some((call_id, arguments))
        }
        _ => None,
    }
}

pub(crate) struct ReentryGuard<'a> {
    pub tools: &'a PythonTools,
    pub keys: Vec<PendingKey>,
}

impl Drop for ReentryGuard<'_> {
    fn drop(&mut self) {
        self.tools.cancel_reentry(&self.keys);
    }
}

// Removing the sender on any future drop also expires already queued requests.
struct PendingReply<'a> {
    completed: bool,
    tools: &'a PythonTools,
    key: PendingKey,
    invocation: u64,
}

impl Drop for PendingReply<'_> {
    fn drop(&mut self) {
        self.tools
            .retire_callback(&self.key, self.invocation, self.completed);
    }
}

fn provider_error_metadata(
    details: &codex_protocol::error::CodexErrorDetails,
    registered_plan: bool,
) -> Option<Value> {
    use codex_protocol::error::CHATGPT_PLAN_UNSUPPORTED_CAPABILITY;
    use codex_protocol::error::CodexErrorDetails;

    let (status, body, request_id, provider_code) = match details {
        CodexErrorDetails::UnexpectedStatus(error) => (
            error.status.as_u16(),
            Some(error.body.as_str()),
            error.request_id.as_deref(),
            None,
        ),
        // Core also constructs RetryLimit with a synthetic 500 for a transport
        // retry limit. Only this pinned HTTP 429 path retains an observed status.
        CodexErrorDetails::RetryLimit(error)
            if error.status == http::StatusCode::TOO_MANY_REQUESTS =>
        {
            (
                error.status.as_u16(),
                None,
                error.request_id.as_deref(),
                if registered_plan {
                    error.provider_code
                } else {
                    None
                },
            )
        }
        CodexErrorDetails::ConnectionFailed(error) => {
            (error.source.status()?.as_u16(), None, None, None)
        }
        CodexErrorDetails::ResponseStreamFailed(error) => {
            (error.source.status()?.as_u16(), None, None, None)
        }
        _ => return None,
    };
    let body = body
        .filter(|body| body.len() <= 128 * 1024)
        .and_then(|body| serde_json::from_str::<Value>(body).ok());
    let error = body.as_ref().and_then(|body| body.get("error"));
    let code = error
        .and_then(|error| error.get("code"))
        .and_then(Value::as_str)
        .filter(|code| {
            !code.is_empty()
                && code.len() <= 128
                && code
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"_.-".contains(&byte))
        });
    let mut metadata = json!({
        "status_code": status,
        "code": code.or(provider_code),
        "request_id": request_id.filter(|id| {
            !id.is_empty() && id.len() <= 256 && id.bytes().all(|byte| byte.is_ascii_graphic())
        }),
    });
    if status == http::StatusCode::BAD_REQUEST.as_u16()
        && code == Some(CHATGPT_PLAN_UNSUPPORTED_CAPABILITY)
        && let Some(param) = error
            .and_then(|error| error.get("param"))
            .and_then(Value::as_str)
        && !param.is_empty()
        && param.len() <= 128
        && param
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_.-[]".contains(&byte))
    {
        metadata["param"] = Value::String(param.to_owned());
    }
    Some(metadata)
}

#[derive(Default)]
pub struct PythonContributor {
    pub(crate) managed_calls: Option<Arc<crate::storage::ManagedCalls>>,
    pub(crate) manager: Weak<ThreadManager>,
    pub(crate) models_config: codex_models_manager::ModelsManagerConfig,
    pub(crate) code_mode_enabled: bool,
    pub(crate) registered_plan: bool,
}
type AcceptedToolCalls = Arc<Mutex<HashMap<PendingKey, u64>>>;

/// Live fresh receipts are never serialized into a checkpoint.
pub(crate) struct FreshTerminalReceipts {
    generation: Arc<Generation>,
    calls: Mutex<Option<HashMap<PendingKey, u64>>>,
}

impl FreshTerminalReceipts {
    pub(crate) fn record(
        target: &mut Option<Self>,
        generation: Arc<Generation>,
        key: &PendingKey,
        invocation: u64,
        batch_completion: bool,
    ) {
        if !generation
            .entries
            .get(key.reply.slot)
            .is_some_and(|entry| entry.finish_turn || batch_completion)
        {
            return;
        }
        let fresh = target.get_or_insert_with(|| Self {
            generation: generation.clone(),
            calls: Mutex::new(Some(HashMap::new())),
        });
        fresh
            .calls
            .lock()
            .expect("fresh terminal receipts poisoned")
            .as_mut()
            .expect("fresh terminal receipts already adopted")
            .insert(key.clone(), invocation);
    }
    pub(crate) fn check(&self, tools: &PythonTools) -> Result<()> {
        ensure!(
            tools.admits_generation(&self.generation),
            "Expired recovered terminal generation"
        );
        Ok(())
    }
    fn adopt(&self, tools: &PythonTools, step: &StepCatalog) -> Result<()> {
        self.check(tools)?;
        ensure!(
            Arc::ptr_eq(&self.generation, &step.generation),
            "Recovered terminal step generation changed"
        );
        if let Some(calls) = self
            .calls
            .lock()
            .expect("fresh terminal receipts poisoned")
            .take()
        {
            step.accepted
                .lock()
                .expect("terminal receipts poisoned")
                .extend(calls);
        }
        Ok(())
    }
}

pub(crate) struct RecoveredToolCompletionSeed {
    turn: String,
    pending: Mutex<
        Option<(
            crate::manual_checkpoint::ToolOutcomes,
            Option<FreshTerminalReceipts>,
            Vec<(codex_history::ResponseItemEnvelope, bool)>,
        )>,
    >,
}

impl RecoveredToolCompletionSeed {
    pub(crate) fn new(
        turn: String,
        outcomes: crate::manual_checkpoint::ToolOutcomes,
        fresh: Option<FreshTerminalReceipts>,
        held_outputs: Vec<(codex_history::ResponseItemEnvelope, bool)>,
    ) -> Self {
        Self {
            turn,
            pending: Mutex::new(Some((outcomes, fresh, held_outputs))),
        }
    }
    pub(crate) fn attach(self, init: &mut codex_extension_api::ExtensionDataInit) {
        init.insert(self);
    }
    fn consume(&self, input: &TurnStartInput<'_>) {
        if self.turn != input.turn_id {
            return;
        }
        // Take on the first matching start, including starts cancelled before sampling.
        if let Some((outcomes, fresh, held_outputs)) = self
            .pending
            .lock()
            .expect("terminal recovery seed poisoned")
            .take()
        {
            input
                .turn_store
                .insert(outcomes.candidates(input.turn_id, held_outputs));
            if let Some(fresh) = fresh {
                input.turn_store.insert(fresh);
            }
        }
    }
}

struct StepCatalog {
    accepted: AcceptedToolCalls,
    generation: Arc<Generation>,
    use_deferred_specs: bool,
    slots: Vec<usize>,
}

impl ToolLifecycleContributor for PythonContributor {
    fn on_tool_finish<'a>(&'a self, input: ToolFinishInput<'a>) -> ToolLifecycleFuture<'a> {
        Box::pin(async move {
            if let Some(tools) = input.thread_store.get::<PythonTools>() {
                // A rejected duplicate shares a call ID, not the original invocation.
                // PendingReply already retires and cancels its exact owner on drop.
                tools
                    .finish_callbacks(Some(input.turn_id), Some(input.call_id), false)
                    .await;
            }

            let Some(tools) = input.thread_store.get::<PythonTools>() else {
                return;
            };
            // A model-selected tool proves this turn consumed its initial
            // input, including native mail (which has no UserMessage item).
            if let Some(receipt) = tools.admitted_turn(input.turn_store) {
                receipt.input_recorded.store(true, Ordering::Release);
            }
            if input.source == codex_tools::ToolCallSource::Direct
                && matches!(input.outcome, ToolCallOutcome::Completed { .. })
            {
                // Core sets terminal_outcome_reached after the handler and
                // result policies, before calling this observer. Suspension
                // joins the dispatch and drains its final Core-owned result,
                // including later native post-hook changes. A successful
                // oneshot send alone cannot prove that settlement.
                tools.record_completed_call(input.turn_id, input.call_id);
            }
        })
    }
}

impl TurnLifecycleContributor for PythonContributor {
    fn hold_tool_completion(&self, input: codex_extension_api::ToolCompletionInput<'_>) -> bool {
        let Some(tools) = input.thread_store.get::<PythonTools>() else {
            return false;
        };
        let Some(step) = input.step_store.get::<StepCatalog>() else {
            return false;
        };
        let ResponseItem::FunctionCall {
            call_id,
            name,
            namespace,
            ..
        } = input.call
        else {
            return false;
        };
        if !tools.admits_generation(&step.generation) {
            return false;
        }
        let qualified = ToolName::new(namespace.clone(), name.clone());
        step.accepted
            .lock()
            .expect("Terminal receipts poisoned")
            .keys()
            .any(|key| {
                key.call == *call_id
                    && key.turn == input.turn_id
                    && key.reply.thread == input.thread_store.level_id()
                    && key.reply.generation == step.generation.id
                    && key.reply.instance == tools.instance
                    && step
                        .generation
                        .entries
                        .get(key.reply.slot)
                        .is_some_and(|entry| entry.finish_turn && entry.name == qualified)
            })
    }

    fn propose_tool_completion<'a>(
        &'a self,
        input: codex_extension_api::ToolCompletionInput<'a>,
    ) -> ExtensionFuture<
        'a,
        std::result::Result<Option<codex_extension_api::ToolCompletionDecision>, String>,
    > {
        Box::pin(async move {
            let Some(tools) = input.thread_store.get::<PythonTools>() else {
                return Ok(None);
            };
            let Some(step) = input.step_store.get::<StepCatalog>() else {
                return Ok(None);
            };
            let ResponseItem::FunctionCall {
                call_id,
                name,
                namespace,
                ..
            } = input.call
            else {
                return Ok(None);
            };
            if let Some(fresh) = input.turn_store.get::<FreshTerminalReceipts>() {
                fresh
                    .adopt(&tools, &step)
                    .map_err(|error| error.to_string())?;
            }
            let qualified_name = ToolName::new(namespace.clone(), name.clone());
            let receipt = step
                .accepted
                .lock()
                .expect("Terminal receipts poisoned")
                .iter()
                .find(|(key, _)| {
                    key.call == *call_id
                        && step
                            .generation
                            .entries
                            .get(key.reply.slot)
                            .is_some_and(|entry| entry.name == qualified_name)
                })
                .map(|(key, invocation)| (key.clone(), *invocation));
            if receipt
                .as_ref()
                .is_some_and(|(key, _)| !step.generation.entries[key.reply.slot].finish_turn)
            {
                return Ok(None);
            }
            let held = input
                .turn_store
                .get::<codex_extension_api::HeldToolCompletionResults>();
            let is_held = || {
                held.as_ref().is_some_and(|held| {
                    held.outputs
                        .lock()
                        .expect("held terminal outputs poisoned")
                        .iter()
                        .any(|(item, _)| item.item == *input.output)
                })
            };
            let guard_only = input.guard_only || receipt.is_none();
            if guard_only && !is_held() {
                return Ok(None);
            }
            let slot = receipt
                .as_ref()
                .map(|(key, _)| key.reply.slot)
                .or_else(|| {
                    step.generation
                        .entries
                        .iter()
                        .position(|entry| entry.finish_turn && entry.name == qualified_name)
                })
                .ok_or("Held terminal source is not currently declared")?;
            let invocation = receipt.as_ref().map(|(_, invocation)| *invocation);
            let verify = || -> Result<()> {
                ensure!(
                    tools.admits_generation(&step.generation),
                    "Expired terminal tool generation"
                );
                let entry = step
                    .generation
                    .entries
                    .get(slot)
                    .context("Invalid terminal tool slot")?;
                ensure!(
                    entry.finish_turn && entry.name == qualified_name,
                    "Invalid terminal tool identity"
                );
                if let Some((key, invocation)) = &receipt {
                    ensure!(
                        key.reply.thread == input.thread_store.level_id()
                            && key.turn == input.turn_id
                            && key.call == *call_id
                            && key.reply.generation == step.generation.id
                            && key.reply.instance == tools.instance,
                        "Invalid terminal tool receipt identity"
                    );
                    ensure!(
                        step.accepted
                            .lock()
                            .expect("Terminal receipts poisoned")
                            .get(key)
                            .is_some_and(|current| current == invocation),
                        "Expired terminal tool receipt"
                    );
                } else {
                    ensure!(is_held(), "Expired held terminal candidate");
                }
                Ok(())
            };
            verify().map_err(|e| e.to_string())?;
            let ResponseItem::FunctionCallOutput { output, .. } = input.output else {
                return Err("Invalid terminal tool output".into());
            };
            let text = match &output.body {
                FunctionCallOutputBody::Text(text) => Some(text.clone()),
                FunctionCallOutputBody::ContentItems(items) => match items.as_slice() {
                    [FunctionCallOutputContentItem::InputText { text }] => Some(text.clone()),
                    _ => None,
                },
            };
            let source = tools
                .source_metadata(Some(input.step_store))
                .map_err(|e| e.to_string())?;
            let owner = tools
                .admission_ready(input.turn_id)
                .map_err(|e| e.to_string())?;
            let stop =
                crate::native_interception::capture_step_stop(input.thread_store, input.turn_store)
                    .ok_or("Terminal tool completion owner unavailable")?;
            let decision = tools
                .exchange_preparation(
                    owner.clone(),
                    input
                        .step_store
                        .get::<codex_extension_api::CapturedModelAttempt>()
                        .map(|captured| captured.0.clone()),
                    input
                        .step_store
                        .get::<codex_extension_api::ToolsOnlyWorkCancellation>()
                        .map(|scope| scope.0.clone()),
                    [
                        "Expired terminal tool owner",
                        "Terminal tool reader closed",
                        "Terminal tool proposal cancelled",
                    ],
                    |reply| PreparationReply::ToolCompletion { reply, stop },
                    |id| {
                        let mut request = json!({"id":input.turn_id,"msg":{
                            "type":"_sdk_tool_completion", "turn_id":input.turn_id,
                            "thread":input.thread_store.level_id(),"instance":tools.instance,
                            "step_id":id,"model":input.model,"text":text,"call_id":call_id,
                            "name":name,"namespace":namespace,"generation":step.generation.id,
                            "slot":slot, "invocation":invocation, "guard_only":guard_only,
                        }});
                        request["msg"]
                            .as_object_mut()
                            .expect("constructed callback object")
                            .extend(source.clone());
                        request
                    },
                    |decision| {
                        decision.map_err(|_| anyhow::anyhow!("Terminal tool validation failed"))
                    },
                )
                .await
                .map_err(|e| e.to_string())?;
            verify().map_err(|e| e.to_string())?;
            if guard_only && matches!(decision, ToolCompletionReply::Accept { .. }) {
                // Checked ordinary-history admission, never finish-turn authority.
                return Ok(None);
            }
            Ok(Some(match decision {
                ToolCompletionReply::Accept { final_output } => {
                    owner
                        .tool_completion_proposed
                        .store(true, Ordering::Release);
                    codex_extension_api::ToolCompletionDecision::Accept { final_output }
                }
                ToolCompletionReply::Continue { feedback } => {
                    codex_extension_api::ToolCompletionDecision::Continue { feedback }
                }
            }))
        })
    }

    fn tool_batch_completion_enabled(
        &self,
        thread_store: &ExtensionData,
        step_store: &ExtensionData,
    ) -> bool {
        if let Some(catalog) = step_store.get::<ProfileCatalog>() {
            return catalog.tool_batch_completion;
        }
        thread_store
            .get::<PythonTools>()
            .is_some_and(|tools| tools.tool_batch_completion_enabled)
    }

    fn propose_tool_batch_completion<'a>(
        &'a self,
        input: codex_extension_api::ToolBatchCompletionInput<'a>,
    ) -> ExtensionFuture<
        'a,
        std::result::Result<Option<codex_extension_api::ToolCompletionDecision>, String>,
    > {
        Box::pin(async move {
            let Some(tools) = input.thread_store.get::<PythonTools>() else {
                return Ok(None);
            };
            let source = tools
                .source_metadata(Some(input.step_store))
                .map_err(|error| error.to_string())?;
            let owner = tools
                .admission_ready(input.turn_id)
                .map_err(|error| error.to_string())?;
            let stop =
                crate::native_interception::capture_step_stop(input.thread_store, input.turn_store)
                    .ok_or("Tool batch completion owner unavailable")?;
            let mut results = Vec::with_capacity(input.results.len());
            for result in input.results {
                let (
                    ResponseItem::FunctionCall {
                        call_id,
                        name,
                        namespace,
                        arguments,
                        ..
                    },
                    ResponseItem::FunctionCallOutput { output, .. },
                ) = (result.call, result.output)
                else {
                    return Err("Invalid settled tool batch".into());
                };
                results.push(json!({
                    "call_id": call_id,
                    "name": name,
                    "namespace": namespace,
                    "arguments": arguments,
                    "output": output.body,
                    "succeeded": result.succeeded,
                }));
            }
            let decision = tools
                .exchange_preparation(
                    owner.clone(),
                    input
                        .step_store
                        .get::<codex_extension_api::CapturedModelAttempt>()
                        .map(|captured| captured.0.clone()),
                    input
                        .step_store
                        .get::<codex_extension_api::ToolsOnlyWorkCancellation>()
                        .map(|scope| scope.0.clone()),
                    [
                        "Expired tool batch completion owner",
                        "Tool batch completion reader closed",
                        "Tool batch completion cancelled",
                    ],
                    |reply| PreparationReply::ToolCompletion { reply, stop },
                    |id| {
                        let mut request = json!({"id": input.turn_id, "msg": {
                            "type": "_sdk_tool_batch_completion",
                            "turn_id": input.turn_id,
                            "thread": input.thread_store.level_id(),
                            "instance": tools.instance,
                            "step_id": id,
                            "model": input.model,
                            "results": results,
                        }});
                        request["msg"]
                            .as_object_mut()
                            .expect("constructed callback object")
                            .extend(source);
                        request
                    },
                    |decision| {
                        decision.map_err(|_| anyhow::anyhow!("Tool batch completion failed"))
                    },
                )
                .await
                .map_err(|error| error.to_string())?;
            let current = tools
                .admission_ready(input.turn_id)
                .map_err(|error| error.to_string())?;
            if !Arc::ptr_eq(&owner, &current) {
                return Err("Expired tool batch completion owner".into());
            }
            Ok(Some(match decision {
                ToolCompletionReply::Accept { final_output } => {
                    owner
                        .tool_completion_proposed
                        .store(true, Ordering::Release);
                    codex_extension_api::ToolCompletionDecision::Accept { final_output }
                }
                ToolCompletionReply::Continue { feedback } => {
                    codex_extension_api::ToolCompletionDecision::Continue { feedback }
                }
            }))
        })
    }

    // The default BeforeTaskRegistration phase includes compaction tasks.
    fn on_turn_start<'a>(&'a self, input: TurnStartInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(seed) = input.turn_store.get::<RecoveredToolCompletionSeed>() {
                seed.consume(&input);
            }

            let tracing = input.turn_store.get_or_init(|| {
                input
                    .thread_store
                    .get::<crate::native_interception::Control>()
                    .map_or_else(codex_otel::sdk_trace::CapturedContext::capture, |owner| {
                        owner.tracing_context()
                    })
            });
            #[cfg(test)]
            if let Some(hold) = input.thread_store.get::<admission_order_tests::StartHold>() {
                hold.entered.add_permits(1);
                hold.release.acquire().await.expect("start hold").forget();
            }
            if let Some(tools) = input.thread_store.get::<PythonTools>() {
                if tools.instance.is_some() && tools.selecting_steps.load(Ordering::Acquire) {
                    input
                        .turn_store
                        .get_or_init(codex_extension_api::InitialTurnInput::default);
                }

                // This is presentation, not a grant. Resolve the actual native
                // model; the SDK must never hide selected tools from a model
                // which cannot request Core's search surface.
                let use_deferred_specs = if let Some(manager) = self.manager.upgrade() {
                    let model = manager
                        .get_models_manager()
                        .get_model_info(input.collaboration_mode.model(), &self.models_config)
                        .await;
                    // Core adds tool_search after its direct-namespace
                    // overrides. In CodeModeOnly it is hidden unless the
                    // application granted exec. A later step can select
                    // tools even if this turn opened with an empty catalog.
                    model.supports_search_tool
                        && (self.code_mode_enabled
                            || model.tool_mode != Some(ToolMode::CodeModeOnly))
                } else {
                    false
                };
                let graph = tools.family.graph.lock().expect("Python graph poisoned");
                let mut admission = tools
                    .admission
                    .lock()
                    .expect("Python admission receipt poisoned");
                let same_id_recovery = admission
                    .as_ref()
                    .is_some_and(|prior| prior.turn_id == input.turn_id);
                let receipt = Arc::new(TurnAdmission {
                    model_attempt: Mutex::new(None),
                    thread_id: input.thread_store.level_id().to_owned(),
                    tracing: tracing.context.clone(),
                    turn_id: input.turn_id.to_owned(),
                    owner: Arc::downgrade(&tools),
                    use_deferred_specs,
                    prepared_operation: tools.prepared_operation(),
                    state: watch::channel(AdmissionState::Pending).0,
                    completed_calls: Mutex::default(),
                    held_tool_outputs: input
                        .turn_store
                        .get_or_init(codex_extension_api::HeldToolCompletionResults::default),
                    graceful_requested: AtomicBool::new(false),
                    sampling_stop: input.turn_store.get_or_init(TurnSamplingStop::default),
                    graph_fenced: graph.fenced,
                    terminal: AtomicBool::new(false),
                    tool_completion_proposed: AtomicBool::new(false),
                    tool_completion_failed: AtomicBool::new(false),
                    local_command_cleanup_failed: AtomicBool::new(false),
                    input_recorded: AtomicBool::new(false),
                    steering_namespace: format!(
                        "agents-steer:{}:",
                        codex_protocol::ThreadId::new()
                    ),
                    steering_tail: AtomicBool::new(false),
                    exact_steering: OnceLock::new(),
                    provider_error: Mutex::default(),
                    same_id_recovery,
                    policy_failure: OnceLock::new(),
                });
                crate::native_interception::attach_sdk_admission(input.turn_store, &receipt);
                input.turn_store.insert(receipt.clone());
                if let Some(previous) = admission.replace(receipt) {
                    previous.graceful_requested.store(false, Ordering::Release);
                    previous.state.send_if_modified(|state| {
                        if *state == AdmissionState::Pending {
                            *state = AdmissionState::Closed;
                            true
                        } else {
                            false
                        }
                    });
                }
                drop(admission);
                tools.family.graph_changed.send_replace(());
            }
        })
    }

    fn on_turn_error<'a>(&'a self, input: TurnErrorInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(tools) = input.thread_store.get::<PythonTools>()
                && let Some(receipt) = tools.admitted_turn(input.turn_store)
                && receipt.turn_id == input.turn_id
            {
                if receipt.tool_completion_proposed.load(Ordering::Acquire) {
                    receipt
                        .tool_completion_failed
                        .store(true, Ordering::Release);
                }
                // A later error, including a non-provider error, replaces the
                // earlier metadata. Capturing this never marks a turn failed.
                *receipt
                    .provider_error
                    .lock()
                    .expect("Provider error receipt poisoned") =
                    provider_error_metadata(input.error_details, self.registered_plan).and_then(
                        |metadata| {
                            serde_json::to_value(input.error)
                                .ok()
                                .map(|category| (category, metadata))
                        },
                    );
            }
        })
    }

    fn on_user_input_recorded<'a>(
        &'a self,
        thread_store: &'a ExtensionData,
        turn_store: &'a ExtensionData,
        client_id: Option<&'a str>,
    ) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(tools) = thread_store.get::<PythonTools>()
                && let Some(receipt) = tools.admitted_turn(turn_store)
            {
                receipt.input_recorded.store(true, Ordering::Release);
                receipt.record_steering(client_id);
            }
        })
    }

    fn on_item_completed<'a>(
        &'a self,
        thread_store: &'a ExtensionData,
        turn_store: &'a ExtensionData,
        item: &'a codex_protocol::items::TurnItem,
    ) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            // Core's initial world-state build precedes input recording.
            // Native mail is recorded without a UserMessage item; current
            // model output also proves that initial input survived. Compaction
            // and hook items do not, because Core can emit them before input.
            use codex_protocol::items::TurnItem;
            if matches!(
                item,
                TurnItem::UserMessage(_)
                    | TurnItem::AgentMessage(_)
                    | TurnItem::Plan(_)
                    | TurnItem::Reasoning(_)
                    | TurnItem::WebSearch(_)
                    | TurnItem::ImageGeneration(_)
            ) && let Some(tools) = thread_store.get::<PythonTools>()
                && let Some(receipt) = tools.admitted_turn(turn_store)
            {
                receipt.input_recorded.store(true, Ordering::Release);
                if let TurnItem::UserMessage(message) = item {
                    receipt.record_steering(message.client_id.as_deref());
                }
            }
        })
    }

    fn on_turn_stop<'a>(
        &'a self,
        input: codex_extension_api::TurnStopInput<'a>,
    ) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            crate::request_metadata::retire(input.turn_store);
            if let Some(tools) = input.thread_store.get::<PythonTools>()
                && let Some(receipt) = input.turn_store.get::<Arc<TurnAdmission>>()
            {
                receipt.terminal.store(true, Ordering::Release);
                tools.family.graph_changed.send_replace(());
            }
            #[cfg(test)]
            if let Some(hold) = input.thread_store.get::<admission_order_tests::Hold>() {
                hold.reached
                    .send(input.turn_store.level_id().to_owned())
                    .expect("owning admission test is listening");
                hold.release
                    .acquire()
                    .await
                    .expect("admission test hold")
                    .forget();
            }
        })
    }

    fn on_turn_abort<'a>(
        &'a self,
        input: codex_extension_api::TurnAbortInput<'a>,
    ) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            crate::request_metadata::retire(input.turn_store);
            if let Some(tools) = input.thread_store.get::<PythonTools>()
                && let Some(receipt) = input.turn_store.get::<Arc<TurnAdmission>>()
            {
                if input
                    .turn_store
                    .get::<ManagedLocalExecCancelFailure>()
                    .is_some()
                {
                    // Another turn can replace the thread's last failure while
                    // this turn's cancellation still owns its cleanup.
                    receipt
                        .local_command_cleanup_failed
                        .store(true, Ordering::Release);
                    let mut graph = tools.family.graph.lock().expect("Python graph poisoned");
                    if graph.fenced {
                        graph.failure.get_or_insert(LOCAL_COMMAND_CLEANUP_FAILED);
                    }
                }
                receipt.terminal.store(true, Ordering::Release);
                tools.family.graph_changed.send_replace(());
            }
        })
    }
}

// Core's global turn-admission hook intentionally bypasses mail and delegated
// child input. Gate these turns after native task registration, when an Op
// can cancel them without deadlocking the Core submission loop.
pub(crate) struct GraphFenceContributor {
    pub manager: Weak<ThreadManager>,
}

impl TurnLifecycleContributor for GraphFenceContributor {
    fn turn_start_phase(&self, _: &ExtensionData) -> TurnStartPhase {
        TurnStartPhase::RegularTaskStart
    }

    fn on_turn_start<'a>(&'a self, input: TurnStartInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            let Some(tools) = input.thread_store.get::<PythonTools>() else {
                return;
            };
            let Some(receipt) = input.turn_store.get::<Arc<TurnAdmission>>() else {
                return;
            };
            if !receipt.graph_fenced || !tools.owns_admission(&receipt) {
                return;
            }
            let stopped = async {
                let manager = self
                    .manager
                    .upgrade()
                    .context("Native graph owner closed")?;
                let id = codex_core_api::ThreadId::from_string(input.thread_store.level_id())?;
                let thread = manager.get_thread(id).await?;
                ensure!(
                    thread
                        .thread_extension_data()
                        .get::<PythonTools>()
                        .is_some_and(|live| Arc::ptr_eq(&live, &tools))
                        && tools.owns_admission(&receipt),
                    "Native graph owner changed"
                );
                thread.submit(Op::Interrupt).await?;
                anyhow::Ok(())
            }
            .await;
            if stopped.is_err() {
                let mut graph = tools.family.graph.lock().expect("Python graph poisoned");
                graph.failure = Some("Native graph could not stop a queued turn");
                tools.family.graph_changed.send_replace(());
            }
            // Op::Interrupt is queued before entering this pending future.
            // No external waiter, spawned task or SDK mutex owns this work.
            std::future::pending::<()>().await;
        })
    }
}

impl ContextContributor for PythonContributor {
    fn contribute_world_state<'a>(
        &'a self,
        input: WorldStateContributionInput<'a>,
    ) -> ExtensionFuture<'a, Vec<WorldStateSectionContribution>> {
        Box::pin(async move {
            // A child may intentionally inherit no Python tools. Source policy still belongs
            // to its actual parent step, with a fresh child admission and cancellation owner.
            if input
                .step_store
                .get::<crate::native_interception::ProfileMcpSource>()
                .is_none()
            {
                if let Some(source) = input
                    .thread_store
                    .get::<crate::native_interception::ProfileMcpSource>()
                {
                    input.step_store.get_or_init(|| source.as_ref().clone());
                    if let Some(selection) = input
                        .thread_store
                        .get::<crate::mcp_result_control::Selection>()
                    {
                        input.step_store.get_or_init(|| selection.as_ref().clone());
                    }
                }
            }
            let Some(tools) = input.thread_store.get::<PythonTools>() else {
                return Vec::new();
            };
            #[cfg(test)]
            let proof = input.thread_store.get::<managed_tests::GracefulHold>();
            #[cfg(test)]
            if let Some(proof) = &proof {
                proof.reached.add_permits(1);
                proof.release.acquire().await.unwrap().forget();
            }
            let Some(receipt) = tools.requested_graceful_admission(input.turn_store) else {
                return Vec::new();
            };
            if receipt.turn_id != input.turn_id {
                return Vec::new();
            }
            // A recovery starts from the saved input and validated outputs.
            // Consult its existing single-use enrollment only after all turn
            // start contributors have run; retained Seed presence is not proof.
            let recovered = input
                .thread_store
                .get::<crate::recovery_guard::Guard>()
                .is_some_and(|guard| guard.has_active_recovery());
            if !receipt.input_recorded.load(Ordering::Acquire) && !recovered {
                #[cfg(test)]
                if let Some(proof) = &proof {
                    let _ = proof.decisions.send(None);
                }
                return Vec::new();
            }
            let Some(manager) = self.manager.upgrade() else {
                tools.graceful_failure(&receipt);
                return std::future::pending().await;
            };
            let thread = match manager.get_thread(input.thread_id).await {
                Ok(thread) => thread,
                Err(_) => {
                    tools.graceful_failure(&receipt);
                    return std::future::pending().await;
                }
            };
            if thread
                .thread_extension_data()
                .get::<PythonTools>()
                .is_none_or(|live| !Arc::ptr_eq(&live, &tools))
                || tools
                    .requested_graceful_admission(input.turn_store)
                    .is_none_or(|current| !Arc::ptr_eq(&current, &receipt))
            {
                return Vec::new();
            }
            let stop_graph = tools
                .family
                .graph
                .lock()
                .expect("Python graph interruption poisoned")
                .fenced;
            if stop_graph {
                // This context is after the current graph member's safe
                // input/model/tool boundary. Do not let queued mail prolong
                // all-scope graceful interruption. Any successor created by
                // Core is itself fenced before it can work.
                if thread
                    .submit(codex_protocol::protocol::Op::Interrupt)
                    .await
                    .is_err()
                {
                    tools.graceful_failure(&receipt);
                }
                return std::future::pending().await;
            }
            let decision = thread.interrupt_if_no_pending_input(input.turn_id).await;
            #[cfg(test)]
            if let Some(proof) = &proof {
                let _ = proof.decisions.send(decision.as_ref().ok().copied());
            }
            match decision {
                // Core refuses if input or native mail is queued. Only this
                // exact receipt may request a stop at a later eligible step.
                Ok(false) => Vec::new(),
                Ok(true) => {
                    // The decision arrives before token cancellation. Do not
                    // let Core sample again in that gap. Its bounded task-abort
                    // path drops this future; no SDK locks are held here.
                    std::future::pending().await
                }
                Err(_) => {
                    tools.graceful_failure(&receipt);
                    std::future::pending().await
                }
            }
        })
    }
}

impl ThreadLifecycleContributor<Config> for PythonContributor {
    fn on_thread_start<'a>(
        &'a self,
        input: ThreadStartInput<'a, Config>,
    ) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(control) = input
                .thread_store
                .get::<crate::native_interception::Control>()
                && let Some(parent) = control.native_agents_parent(&self.manager).await
                && let Some(parent_tools) = parent.thread_extension_data().get::<PythonTools>()
            {
                let child = PythonTools::descendant_from_step(
                    &parent_tools,
                    control.incarnation.clone(),
                    input.spawning_step_store,
                );
                match child {
                    Ok(child) => {
                        input.thread_store.get_or_init(|| child);
                    }
                    Err(_) => {
                        // A restored runtime has no live spawning Arc. It cannot inherit the
                        // parent's later Agent/catalog as a substitute for validated provenance.
                        control.revoke();
                        return;
                    }
                }
                if let Some(hosted) = parent
                    .thread_extension_data()
                    .get::<codex_extension_api::HostedModelTools>()
                {
                    input.thread_store.insert((*hosted).clone());
                }
            }
            if let Some(tools) = input.thread_store.get::<PythonTools>() {
                if let Some(control) = input
                    .thread_store
                    .get::<crate::native_interception::Control>()
                {
                    let _ = tools.interception.set(Arc::downgrade(&control));
                    control.bind_context_tools(&tools);
                }
                let _graph = tools.family.graph.lock().expect("Python graph poisoned");
                tools
                    .family
                    .members
                    .write()
                    .expect("Python family poisoned")
                    .insert(
                        input.thread_store.level_id().to_owned(),
                        Arc::downgrade(&tools),
                    );
                tools.family.graph_changed.send_replace(());
            }
        })
    }
    fn on_thread_idle<'a>(&'a self, input: ThreadIdleInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            #[cfg(test)]
            if let Some(hold) = input.thread_store.get::<admission_order_tests::IdleHold>() {
                hold.entered.add_permits(1);
                hold.release
                    .acquire()
                    .await
                    .expect("idle test hold")
                    .forget();
            }
            if let Some(tools) = input.thread_store.get::<PythonTools>()
                && let Some(receipt) = tools
                    .admission
                    .lock()
                    .expect("Python admission receipt poisoned")
                    .as_ref()
                && IDLE_PROBE_RECEIPT
                    .try_with(|expected| Arc::ptr_eq(expected, receipt))
                    .unwrap_or(true)
                && receipt.check_local_command_cleanup().is_ok()
            {
                receipt.graceful_requested.store(false, Ordering::Release);
                receipt.state.send_if_modified(|state| {
                    if *state == AdmissionState::Pending {
                        *state = AdmissionState::Ready;
                        true
                    } else {
                        false
                    }
                });
                if input.cause == codex_extension_api::ThreadIdleCause::Failed
                    && receipt.tool_completion_failed.swap(false, Ordering::AcqRel)
                {
                    // Core deliberately emitted no replacement terminal fact.
                    // This exact idle receipt settles SDK waiters with failure only.
                    let _ = tools
                        .family
                        .requests
                        .send(QueuedRequest::ToolCompletionFailure(
                            Arc::downgrade(&tools),
                            Arc::downgrade(receipt),
                        ));
                }
                tools.family.graph_changed.send_replace(());
            }
        })
    }

    fn on_thread_stop<'a>(&'a self, input: ThreadStopInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(tools) = input.thread_store.get::<PythonTools>() {
                tools.finish_callbacks(None, None, true).await;
                tools.finish_preparations().await;
            }

            if let Some(tools) = input.thread_store.get::<PythonTools>() {
                if let Some(receipt) = tools
                    .admission
                    .lock()
                    .expect("Python admission receipt poisoned")
                    .as_ref()
                {
                    receipt.graceful_requested.store(false, Ordering::Release);
                    receipt.terminal.store(true, Ordering::Release);
                    receipt.state.send_replace(AdmissionState::Closed);
                }
                tools.retire_model_producer();
                tools.retire_prepared_requests();
                tools.family.graph_changed.send_replace(());
                // A callback may still retain its issuer after Core tears it
                // down. Stop must revoke its pending senders immediately.
                if let Some(incarnation) = &tools.instance {
                    tools
                        .pending
                        .lock()
                        .expect("Python replies poisoned")
                        .clear();
                    // Unlike a weak event reader, this family-scoped notice
                    // survives retirement even if the child was never discovered.
                    let _ = tools.family.requests.send(QueuedRequest::Retired {
                        thread: input.thread_store.level_id().to_owned(),
                        incarnation: incarnation.clone(),
                    });
                }
            }
        })
    }
}

impl PythonContributor {
    fn executors(
        &self,
        thread: &ExtensionData,
        tools: Arc<PythonTools>,
        generation: Arc<Generation>,
        use_deferred_specs: bool,
        slots: &[usize],
        profile: Option<codex_extension_api::AgentProfileReceipt>,
        accepted: Option<AcceptedToolCalls>,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        slots
            .iter()
            .copied()
            .map(|slot| {
                let executor = Arc::new(PythonExecutor {
                    managed_calls: self.managed_calls.clone(),
                    accepted: accepted.clone(),
                    thread: thread.level_id().to_string(),
                    tools: tools.clone(),
                    generation: generation.clone(),
                    profile: profile.clone(),
                    slot,
                    use_deferred_specs,
                    code_mode_enabled: self.code_mode_enabled,
                })
                    as Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>;
                crate::agent_profiles::wrap_handoff_callback(thread, profile.as_ref(), executor)
            })
            .collect()
    }
}

impl ToolContributor for PythonContributor {
    fn capture_realtime_tools(
        &self,
        input: codex_extension_api::RealtimeToolCaptureInput<'_>,
    ) -> std::result::Result<Option<codex_extension_api::RealtimeToolDeclarations>, String> {
        let Some(tools) = input.thread_store.get::<PythonTools>() else {
            return Ok(None);
        };
        let catalog = if let Some(source) = input.retained_execution_data {
            source
                .get::<ProfileCatalog>()
                .ok_or("Retained Realtime source has no captured Python catalog")?
                .as_ref()
                .clone()
        } else {
            match input.profile {
                Some(profile) => profile
                    .profile
                    .data
                    .get::<ProfileCatalog>()
                    .ok_or("Captured Agent has no admitted Python tool catalog")?
                    .as_ref()
                    .clone(),
                None => match tools
                    .descendant_source
                    .as_ref()
                    .and_then(|source| source.catalog.as_ref())
                {
                    Some(catalog) => catalog.as_ref().clone(),
                    None => {
                        if tools.managed_profiles.load(Ordering::Acquire) {
                            return Err(
                                "Managed Realtime source has no captured Agent profile".into()
                            );
                        }
                        ProfileCatalog {
                            generation: tools.snapshot(),
                            handoff_defaults: None,
                            defaults: tools
                                .step_defaults
                                .read()
                                .expect("Step defaults poisoned")
                                .clone(),
                            selecting_steps: tools.selecting_steps.load(Ordering::Acquire),
                            transform_context: tools.context_transform_enabled,
                            before_compact: tools.before_compact_enabled,
                            before_complete: tools.before_complete_enabled,
                            tool_batch_completion: tools.tool_batch_completion_enabled,
                        }
                    }
                },
            }
        };
        let generation = &catalog.generation;
        {
            let mut admitted = tools
                .profile_catalogs
                .write()
                .expect("Profile catalogs poisoned");
            admitted.retain(|_, generation| generation.strong_count() > 0);
            if let Some(existing) = admitted.get(&generation.id).and_then(Weak::upgrade)
                && !Arc::ptr_eq(&existing, generation)
            {
                return Err(
                    "Realtime source catalog identity conflicts with admitted generation".into(),
                );
            }
            admitted.insert(generation.id, Arc::downgrade(generation));
        }
        let voice = input.profile.is_some_and(|profile| {
            profile
                .profile
                .data
                .get::<codex_extension_api::RealtimeVoiceProfileScope>()
                .is_some()
        });
        let allows = |name: &ToolName| voice || tools.policy.allows(name);
        let mut declarations = Vec::new();
        for ((_, raw), entry) in functions(&generation.specs)
            .into_iter()
            .zip(&generation.entries)
        {
            let defaults = if crate::agent_profiles::is_handoff_callback(
                input.thread_store,
                input.profile.map(|profile| &profile.receipt),
                &entry.name,
            ) {
                &catalog.handoff_defaults
            } else {
                &catalog.defaults
            };
            if !allows(&entry.name)
                || entry.exposure == ToolExposure::Deferred
                || defaults.as_ref().is_some_and(|defaults| {
                    !defaults.iter().any(|name| {
                        name.clone().with_default_namespace()
                            == entry.name.clone().with_default_namespace()
                    })
                })
            {
                continue;
            }
            let native = match &entry.spec {
                ToolSpec::Function(spec) => spec,
                ToolSpec::Namespace(namespace) => match namespace.tools.as_slice() {
                    [ResponsesApiNamespaceTool::Function(spec)] => spec,
                    _ => return Err("Unsupported Python Realtime namespace declaration".into()),
                },
                _ => return Err("Unsupported Python Realtime tool declaration".into()),
            };
            declarations.push(codex_extension_api::RealtimeToolDeclaration {
                native_name: entry.name.clone(),
                spec: codex_extension_api::PreparedToolPresentation {
                    namespace: entry.name.namespace.clone(),
                    name: native.name.clone(),
                    kind: "function".into(),
                    strict: native.strict,
                    defer_loading: None,
                    description: native.description.clone(),
                    parameters: raw.input_schema.clone(),
                },
            });
        }
        if let Some(handoffs) = input.profile.and_then(|profile| {
            profile
                .profile
                .data
                .get::<codex_extension_api::AgentProfileHandoffTools>()
        }) {
            declarations.extend(
                crate::agent_profiles::realtime_handoff_declarations(
                    &handoffs,
                    input.thread_store,
                    input.profile.map(|profile| &profile.receipt),
                )
                .map_err(|error| error.to_string())?
                .into_iter()
                .filter(|declaration| {
                    allows(&declaration.native_name)
                        && catalog.handoff_defaults.as_ref().is_none_or(|defaults| {
                            defaults
                                .contains(&declaration.native_name.clone().with_default_namespace())
                        })
                }),
            );
        }
        if let Some(binding) = input.mcp {
            for info in binding.tools() {
                let native_name = info.canonical_tool_name();
                if !allows(&native_name) {
                    continue;
                }
                let server = binding
                    .config()
                    .mcp_server_catalog
                    .server(&info.server_name)
                    .ok_or("Captured MCP declaration has no admitted server")?;
                if info.server_name == codex_mcp::CODEX_APPS_MCP_SERVER_NAME
                    && server.config().is_local_environment()
                    && matches!(
                        server.source(),
                        codex_mcp::McpServerSource::Compatibility { .. }
                            | codex_mcp::McpServerSource::Extension {
                                host_owned_apps: true,
                                ..
                            }
                    )
                {
                    return Err(
                        "Hosted Apps MCP is unavailable for native Realtime tool dispatch".into(),
                    );
                }
                if server
                    .config()
                    .omit_tools_from
                    .as_deref()
                    .unwrap_or_default()
                    .contains(&codex_protocol::config_types::ToolExposureSurface::Direct)
                {
                    continue;
                }
                let native = codex_tools::mcp_tool_to_responses_api_tool(
                    &native_name,
                    &info.tool,
                    server
                        .config()
                        .tool_input_schema_max_bytes
                        .map(|limit| limit.get()),
                )
                .map_err(|error| error.to_string())?;
                declarations.push(codex_extension_api::RealtimeToolDeclaration {
                    native_name: native_name.clone(),
                    spec: codex_extension_api::PreparedToolPresentation {
                        namespace: native_name.namespace.clone(),
                        name: native.name,
                        kind: "function".into(),
                        strict: native.strict,
                        defer_loading: None,
                        description: native.description,
                        parameters: Value::Object(info.tool.input_schema.as_ref().clone()),
                    },
                });
            }
        }
        let mut execution_data = input.retained_execution_data.cloned().unwrap_or_default();
        let generation_id = generation.id;
        let selection_id = codex_extension_api::realtime_tool_selection_id(
            input.thread_store.level_id(),
            generation_id,
            input.profile.map(|profile| &profile.receipt),
        );
        if input.retained_execution_data.is_none() {
            execution_data = input
                .profile
                .map(|profile| profile.profile.data.clone())
                .unwrap_or_default();
            execution_data.insert(catalog);
            if let Some(profile) = input.profile {
                let policy = profile
                    .profile
                    .data
                    .get::<crate::native_interception::ProfileMcpPolicy>()
                    .ok_or("Managed Realtime source has no captured MCP policy")?;
                execution_data.insert(policy.as_ref().clone());
                execution_data.insert(crate::native_interception::ProfileMcpSource {
                    result_selection: None,
                    receipt: Some(profile.receipt.clone()),
                    policy: Some(policy),
                });
            }
            if let Some(source) = &tools.descendant_source {
                if let Some(profile) = &source.profile {
                    let policy = source
                        .mcp_policy
                        .clone()
                        .ok_or("Managed child Realtime source has no captured MCP policy")?;
                    execution_data.insert(DescendantProfileSource(profile.clone()));
                    execution_data.insert(policy.as_ref().clone());
                    execution_data.insert(crate::native_interception::ProfileMcpSource {
                        result_selection: None,
                        receipt: Some(profile.clone()),
                        policy: Some(policy),
                    });
                }
                if let Some(selection) = &source.mcp_selection {
                    execution_data.insert(selection.as_ref().clone());
                }
            }
            if input.profile.is_none()
                && tools
                    .descendant_source
                    .as_ref()
                    .is_none_or(|source| source.profile.is_none())
            {
                if let Some(source) = input
                    .thread_store
                    .get::<crate::native_interception::ProfileMcpSource>()
                {
                    execution_data.insert(source.as_ref().clone());
                    if let Some(selection) = input
                        .thread_store
                        .get::<crate::mcp_result_control::Selection>()
                    {
                        execution_data.insert(selection.as_ref().clone());
                    }
                } else if let Some(control) = input
                    .thread_store
                    .get::<crate::native_interception::Control>()
                {
                    execution_data.insert(control.capture_mcp_source());
                    if let Some(selection) = input
                        .thread_store
                        .get::<crate::mcp_result_control::McpResultControl>()
                        .and_then(|control| control.capture_source_selection())
                    {
                        execution_data.insert(selection);
                    }
                } else if input.mcp.is_some_and(|binding| !binding.tools().is_empty()) {
                    return Err("Realtime MCP source has no admitted SDK policy owner".into());
                }
            }
            let accepted = if let Some(profile) = input.profile {
                Some(
                    profile
                        .profile
                        .data
                        .get::<crate::application_instructions::Accepted>()
                        .ok_or("Captured Agent has no accepted application instructions")?
                        .as_ref()
                        .clone(),
                )
            } else {
                input
                    .thread_store
                    .get::<crate::application_instructions::Instructions>()
                    .and_then(|instructions| instructions.realtime_accepted_snapshot())
            };
            execution_data.insert(codex_extension_api::AgentProfileInstructions {
                text: accepted.as_ref().map(|accepted| accepted.text.clone()),
            });
            if let Some(accepted) = accepted {
                execution_data.insert(accepted);
            }
            if let (Some(control), Some(source)) = (
                input
                    .thread_store
                    .get::<crate::native_interception::Control>(),
                execution_data.get::<crate::native_interception::ProfileMcpSource>(),
            ) {
                execution_data.insert(
                    control.capture_realtime_mcp_source(source.as_ref().clone(), generation_id),
                );
            }
        }
        if input.mcp.is_some_and(|binding| !binding.tools().is_empty())
            && execution_data
                .get::<crate::native_interception::ProfileMcpSource>()
                .is_none_or(|source| source.policy.is_none())
        {
            return Err("Captured Realtime MCP source has no admitted SDK policy".into());
        }
        let application_instructions = execution_data
            .get::<codex_extension_api::AgentProfileInstructions>()
            .ok_or("Retained Realtime source has no captured application instructions")?
            .text
            .clone();
        Ok(Some(codex_extension_api::RealtimeToolDeclarations {
            selection_id,
            application_instructions,
            tools: declarations,
            execution_data,
        }))
    }

    fn tools(
        &self,
        _: &ExtensionData,
        thread: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        let Some(tools) = thread.get::<PythonTools>() else {
            return Vec::new();
        };
        let generation = tools.snapshot();
        let use_deferred_specs = tools
            .admission
            .lock()
            .expect("Python admission receipt poisoned")
            .as_ref()
            .is_some_and(|receipt| receipt.use_deferred_specs);
        let slots = (0..generation.entries.len()).collect::<Vec<_>>();
        if tools.managed_profiles.load(Ordering::Acquire) {
            return Vec::new();
        }
        self.executors(
            thread,
            tools,
            generation,
            use_deferred_specs,
            &slots,
            None,
            None,
        )
    }

    fn tools_for_step(
        &self,
        _: &ExtensionData,
        thread: &ExtensionData,
        step: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        let Some(tools) = thread.get::<PythonTools>() else {
            return Vec::new();
        };
        let Ok(generation) = tools.generation_for_step(Some(step)) else {
            return Vec::new();
        };
        let Ok(profile) = tools.source_profile(Some(step)) else {
            return Vec::new();
        };
        let profile_catalog = step.get::<ProfileCatalog>();
        let snapshot = step.get_or_init(|| StepCatalog {
            accepted: Default::default(),
            slots: {
                let defaults = match profile_catalog {
                    Some(catalog) => catalog.defaults.clone(),
                    None => tools
                        .step_defaults
                        .read()
                        .expect("Step defaults poisoned")
                        .clone(),
                };
                let mut slots = defaults
                    .as_ref()
                    .into_iter()
                    .flatten()
                    .filter_map(|name| {
                        generation.entries.iter().position(|entry| {
                            name.namespace.as_deref().is_none_or(|ns| ns == "functions")
                                && entry.name.name == name.name
                        })
                    })
                    .collect::<Vec<_>>();
                slots.extend(
                    (0..generation.entries.len())
                        .filter(|slot| !slots.contains(slot))
                        .collect::<Vec<_>>(),
                );
                slots
            },
            generation,
            use_deferred_specs: tools
                .admission
                .lock()
                .expect("Python admission receipt poisoned")
                .as_ref()
                .is_some_and(|receipt| receipt.use_deferred_specs),
        });
        self.executors(
            thread,
            tools,
            snapshot.generation.clone(),
            snapshot.use_deferred_specs,
            &snapshot.slots,
            profile,
            Some(snapshot.accepted.clone()),
        )
    }

    fn select_tools_for_step<'a>(
        &'a self,
        _: &'a ExtensionData,
        thread: &'a ExtensionData,
        turn: &'a ExtensionData,
        step: &'a ExtensionData,
        candidates: &'a [ToolName],
    ) -> ExtensionFuture<'a, std::result::Result<Option<Vec<ToolName>>, String>> {
        Box::pin(async move {
            let Some(tools) = thread.get::<PythonTools>() else {
                return Ok(None);
            };
            let generation = tools
                .generation_for_step(Some(step))
                .map_err(|error| error.to_string())?;
            let profile = tools
                .source_profile(Some(step))
                .map_err(|error| error.to_string())?;
            let catalog = step.get::<ProfileCatalog>();
            let selecting = tools.selecting_steps.load(Ordering::Acquire)
                || catalog
                    .as_ref()
                    .is_some_and(|catalog| catalog.selecting_steps);
            if !selecting {
                return Ok(None);
            }
            let stop = crate::native_interception::capture_step_stop(thread, turn)
                .ok_or("Step owner unavailable")?;
            let id = tools.family.next_invocation.fetch_add(1, Ordering::Relaxed);
            let (reply, receive) = oneshot::channel();
            let defaults = match &catalog {
                Some(catalog) => catalog.defaults.clone(),
                None => tools
                    .step_defaults
                    .read()
                    .expect("Step defaults poisoned")
                    .clone(),
            };
            let defaults = match step.get::<Arc<codex_mcp::McpBinding>>() {
                Some(binding) => defaults.map(|names| {
                    names
                        .iter()
                        .filter_map(|name| tools.policy.resolve_mcp_reference(name, &binding))
                        .collect::<Vec<_>>()
                }),
                None => defaults,
            };
            let defaults = match (
                defaults,
                step.get::<codex_extension_api::CapturedAgentProfile>(),
            ) {
                (Some(ordinary), Some(source)) => {
                    let handoffs = source
                        .profile
                        .data
                        .get::<codex_extension_api::AgentProfileHandoffTools>();
                    let native = handoffs
                        .as_ref()
                        .into_iter()
                        .flat_map(|handoffs| handoffs.targets.keys().cloned())
                        .chain(
                            source
                                .profile
                                .mcp
                                .tools()
                                .iter()
                                .map(|tool| tool.canonical_tool_name()),
                        );
                    Some(merge_profile_native_defaults(
                        self.code_mode_enabled,
                        ordinary,
                        native,
                        candidates,
                        step.get::<codex_extension_api::ToolPolicy>()
                            .as_deref()
                            .unwrap_or(&tools.policy),
                    ))
                }
                (Some(ordinary), None)
                    if step
                        .get::<codex_extension_api::ToolsOnlyWorkCancellation>()
                        .is_some() =>
                {
                    let native = step
                        .get::<Arc<codex_mcp::McpBinding>>()
                        .map(|binding| {
                            binding
                                .tools()
                                .iter()
                                .map(|tool| tool.canonical_tool_name())
                                .collect::<Vec<_>>()
                        })
                        .unwrap_or_default();
                    // Tools-only Realtime adds captured MCP defaults, not Code Mode helpers.
                    Some(merge_profile_native_defaults(
                        false,
                        ordinary,
                        native,
                        candidates,
                        step.get::<codex_extension_api::ToolPolicy>()
                            .as_deref()
                            .unwrap_or(&tools.policy),
                    ))
                }
                (defaults, _) => defaults,
            };
            let mcp_names = step
                .get::<Arc<codex_mcp::McpBinding>>()
                .map(|binding| {
                    binding
                        .tools()
                        .iter()
                        .filter_map(|tool| {
                            let name = tool.canonical_tool_name();
                            candidates.contains(&name).then(|| {
                                json!({
                                    "server": tool.server_name, "tool": tool.tool.name,
                                    "namespace": name.namespace, "name": name.name,
                                })
                            })
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            let tracing = if tools.instance.is_some() {
                turn.get::<codex_otel::sdk_trace::CapturedContext>()
                    .and_then(|captured| captured.context.clone())
            } else {
                codex_otel::sdk_trace::ContextLease::capture().map(Arc::new)
            };
            let mut request = json!({
                "id": turn.level_id(),
                "msg": {"type": "_sdk_select_step", "turn_id": turn.level_id(),
                    "thread": thread.level_id(), "instance": tools.instance,
                    "step_id": id, "generation": generation.id,
                    "native_step": step.level_id(), "candidates": candidates, "defaults": defaults, "mcp_names": mcp_names}
            });
            if let Some(initial) = turn.get::<codex_extension_api::InitialTurnInput>() {
                if let Some(records) = &initial.0 {
                    request["msg"]["initial_input"] = json!(records);
                }
            }
            if let Some(profile) = profile {
                request["msg"]["profile_id"] = json!(profile.profile_id);
                request["msg"]["profile_epoch"] = json!(profile.epoch);
            }
            if let Some(context) = &tracing {
                request["msg"]["tracing_context"] = json!(context.token());
            }
            let callback = tools.model_preparation_callback(
                id,
                &mut request,
                step.get::<codex_extension_api::CapturedModelAttempt>()
                    .map(|captured| captured.0.clone()),
            );
            {
                let mut replies = tools
                    .preparation_replies
                    .lock()
                    .expect("Python preparation replies poisoned");
                if tools.preparations_stopped.load(Ordering::Acquire) {
                    return Err("Expired step owner".into());
                }
                replies.as_mut().ok_or("Expired step owner")?.insert(
                    id,
                    PreparationSlot {
                        turn: turn.level_id().to_owned(),
                        reply: Some(PreparationReply::Step(StepReply { reply, stop })),
                        callback: callback.clone(),
                    },
                );
            }
            let _pending = PendingPreparation {
                tools: tools.clone(),
                id,
            };
            tools
                .family
                .requests
                .send(QueuedRequest::Step(request))
                .map_err(|_| "Step reader closed")?;
            let cancellation = step
                .get::<codex_extension_api::ToolsOnlyWorkCancellation>()
                .map(|scope| scope.0.clone());
            let names = tools
                .await_preparation(id, &callback, receive, cancellation)
                .await
                .map_err(|error| error.to_string())?
                .ok_or("Step selection failed")?;
            Ok(Some(names))
        })
    }
}

struct PythonExecutor {
    accepted: Option<AcceptedToolCalls>,
    managed_calls: Option<Arc<crate::storage::ManagedCalls>>,
    thread: String,
    tools: Arc<PythonTools>,
    generation: Arc<Generation>,
    profile: Option<codex_extension_api::AgentProfileReceipt>,
    slot: usize,
    use_deferred_specs: bool,
    code_mode_enabled: bool,
}

impl<'call> ToolExecutor<ToolCall<'call>> for PythonExecutor {
    fn on_result_accepted(
        &self,
        _: &str,
        _: &str,
        result: &dyn ToolOutput,
    ) -> codex_tools::AcceptedToolOutcome {
        codex_tools::AcceptedToolOutcome {
            start_response: result.realtime_response_requested(),
            ..Default::default()
        }
    }

    fn tool_name(&self) -> ToolName {
        self.generation.entries[self.slot].name.clone()
    }

    fn spec(&self) -> ToolSpec {
        self.generation.entries[self.slot].spec.clone()
    }

    fn exposure(&self) -> ToolExposure {
        let exposure = self.generation.entries[self.slot].exposure;
        match (exposure, self.use_deferred_specs, self.code_mode_enabled) {
            (ToolExposure::Deferred, true, false) => ToolExposure::DeferredModelOnly,
            (ToolExposure::Deferred, false, false) => ToolExposure::DirectModelOnly,
            (ToolExposure::Deferred, false, true) => ToolExposure::Direct,
            _ => exposure,
        }
    }

    fn supports_parallel_tool_calls(&self) -> bool {
        // Core admits each callback independently. A waiting callback must not
        // prevent an automatic or manual sibling from producing its result.
        true
    }

    fn search_info(&self) -> Option<ToolSearchInfo> {
        ToolSearchInfo::from_tool_spec(
            self.spec(),
            Some(ToolSearchSourceInfo {
                name: "Dynamic tools".to_string(),
                description: Some("Tools provided by the current Codex thread.".to_string()),
            }),
        )
    }

    fn handle<'a>(&'a self, call: ToolCall<'call>) -> ToolExecutorFuture<'a>
    where
        ToolCall<'call>: 'a,
    {
        Box::pin(async move {
            let (arguments, argument_error) = match &call.payload {
                ToolPayload::ClientToolSearch(search) => (search.arguments.clone(), None),
                _ => {
                    let raw = call.function_arguments()?;
                    match serde_json::from_str::<Value>(raw) {
                        Ok(value) => (value, None),
                        Err(error) if self.generation.entries[self.slot].input_error_recovery => {
                            (Value::String(raw.to_owned()), Some(error.to_string()))
                        }
                        Err(error) => {
                            return Err(FunctionCallError::RespondToModel(error.to_string()));
                        }
                    }
                }
            };
            let (key, invocation, receiver) = self
                .tools
                .enqueue_profile(
                    &self.thread,
                    &self.generation,
                    self.slot,
                    &call.turn_id,
                    &call.call_id,
                    arguments,
                    argument_error,
                    &call.source,
                    Some(&call.model),
                    false,
                    self.profile.as_ref(),
                    Some(NativeMediaContext {
                        inline_data_url_low_detail_supported: call
                            .inline_data_url_low_detail_supported,
                        model_info: Arc::clone(&call.model_info),
                        profile: call.provider_reference_profile,
                    }),
                    call.conversation_history.realtime_tool_call().cloned(),
                )
                .map_err(|error| FunctionCallError::RespondToModel(error.to_string()))?;
            // A rejected enqueue never reaches application code. Keep Core's
            // error output for it instead of recording an unknown effect.
            if let Some(calls) = &self.managed_calls {
                calls.register(&self.thread, &call.turn_id, &call.call_id);
            }
            let mut _reply = PendingReply {
                completed: false,
                tools: &self.tools,
                invocation,
                key: key.clone(),
            };
            let response = receiver.await.map_err(|_| {
                if matches!(call.payload, ToolPayload::Computer { .. }) {
                    FunctionCallError::Fatal("Computer call cancelled before reply".into())
                } else {
                    FunctionCallError::RespondToModel(
                        "Python tool call cancelled before reply".into(),
                    )
                }
            })?;
            _reply.completed = true;
            #[cfg(test)]
            {
                let hold = self.tools.before_result.lock().unwrap().clone();
                if let Some(hold) = hold
                    && hold.call_id == key.call
                {
                    hold.entered.add_permits(1);
                    hold.release.acquire().await.unwrap().forget();
                }
            }
            if call.source == ToolCallSource::Direct
                && response.response.success
                && self.generation.entries[self.slot].finish_turn
                && self.tools.admits_generation(&self.generation)
            {
                if let Some(accepted) = &self.accepted {
                    accepted
                        .lock()
                        .expect("Terminal receipts poisoned")
                        .insert(key, invocation);
                }
            }
            if let ToolPayload::Computer { arguments } = &call.payload {
                return Ok(Box::new(ReplyOutput {
                    output: codex_tools::ComputerToolOutput::from_dynamic_response(
                        arguments,
                        response.response,
                    )?,
                    start_response: response.start_response,
                    acceptance: response.acceptance,
                }) as Box<dyn ToolOutput>);
            }
            if let ToolPayload::ClientToolSearch(search) = &call.payload {
                let selection =
                    codex_tools::ClientToolSearchSelection::from_response(&response.response)?;
                if selection.catalog_generation.is_some() {
                    return Err(FunctionCallError::RespondToModel(
                        "Python tool search cannot select a replacement catalog".into(),
                    ));
                }
                return Ok(
                    Box::new(selection.resolve(&search.catalog, &[])?) as Box<dyn ToolOutput>
                );
            }
            Ok(Box::new(ReplyOutput {
                output: PythonOutput(response.response),
                start_response: response.start_response,
                acceptance: response.acceptance,
            }) as Box<dyn ToolOutput>)
        })
    }
}

struct ReplyOutput<T> {
    output: T,
    start_response: bool,
    acceptance: Option<oneshot::Sender<()>>,
}

impl<T: ToolOutput> ToolOutput for ReplyOutput<T> {
    fn log_output(&self) -> String {
        self.output.log_output()
    }
    fn success_for_logging(&self) -> bool {
        self.output.success_for_logging()
    }
    fn to_response_item(&self, call_id: &str, payload: &ToolPayload) -> ResponseInputItem {
        self.output.to_response_item(call_id, payload)
    }
    fn realtime_response_requested(&self) -> bool {
        self.start_response
    }
    fn contains_external_context(&self) -> bool {
        self.output.contains_external_context()
    }
    fn mark_accepted(&mut self) {
        self.output.mark_accepted();
        if let Some(acceptance) = self.acceptance.take() {
            let _ = acceptance.send(());
        }
    }
}

pub(crate) struct PythonOutput(pub(crate) DynamicToolResponse);

impl PythonOutput {
    pub(crate) fn receipt(&self, key: &PendingKey) -> ResponseItem {
        self.receipt_for(&key.turn, &key.call)
    }

    pub(crate) fn receipt_for(&self, turn: &str, call: &str) -> ResponseItem {
        let mut item: ResponseItem = self
            .to_response_item(
                call,
                &ToolPayload::Function {
                    arguments: String::new(),
                },
            )
            .into();
        if let ResponseItem::FunctionCallOutput {
            internal_chat_message_metadata_passthrough,
            ..
        } = &mut item
        {
            *internal_chat_message_metadata_passthrough = Some(
                codex_protocol::models::InternalChatMessageMetadataPassthrough {
                    turn_id: Some(turn.to_owned()),
                    ..Default::default()
                },
            );
        }
        item
    }
}

impl ToolOutput for PythonOutput {
    fn log_output(&self) -> String {
        FunctionCallOutputBody::ContentItems(
            self.0
                .content_items
                .iter()
                .cloned()
                .map(Into::into)
                .collect(),
        )
        .to_text()
        .unwrap_or_default()
    }

    fn success_for_logging(&self) -> bool {
        self.0.success
    }

    fn to_response_item(&self, call_id: &str, _: &ToolPayload) -> ResponseInputItem {
        let content: Vec<FunctionCallOutputContentItem> = self
            .0
            .content_items
            .iter()
            .cloned()
            .map(Into::into)
            .collect();
        let body = match content.as_slice() {
            [FunctionCallOutputContentItem::InputText { text }] => {
                FunctionCallOutputBody::Text(text.clone())
            }
            _ => FunctionCallOutputBody::ContentItems(content),
        };
        ResponseInputItem::FunctionCallOutput {
            call_id: call_id.to_string(),
            output: FunctionCallOutputPayload {
                body,
                success: Some(self.0.success),
            },
        }
    }
}

#[cfg(test)]
pub(crate) mod admission_order_tests {
    use super::*;

    // Thread-scoped only. Core has detached the real task before this SDK hook
    // runs, but it has not sent TurnComplete or cleared the active slot yet.
    pub(crate) struct Hold {
        pub reached: mpsc::UnboundedSender<String>,
        pub release: tokio::sync::Semaphore,
    }

    pub(crate) struct IdleHold {
        pub entered: tokio::sync::Semaphore,
        pub release: tokio::sync::Semaphore,
    }

    pub(crate) struct StartHold {
        pub entered: tokio::sync::Semaphore,
        pub release: tokio::sync::Semaphore,
    }
}

#[cfg(test)]
#[path = "python_manual_checkpoint_tests.rs"]
pub(crate) mod manual_checkpoint_tests;

#[cfg(test)]
pub(crate) mod managed_tests {
    use super::*;

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn owned_compaction_stop_joins_queued_start_and_leaves_later_root_untouched() {
        use crate::engine::settlement_tests::Provider;
        use codex_protocol::user_input::UserInput;
        use tokio::sync::Semaphore;

        tokio::time::timeout(std::time::Duration::from_secs(20), async {
            let provider = Provider::start().await;
            let directory = tempfile::tempdir().unwrap();
            let engine = real_engine(&directory, &provider.url).await;
            let native = engine
                .start_with_tools(Default::default(), None, Some(vec![]), false)
                .await
                .unwrap();
            let state = native.current().unwrap();
            let tools = state.thread.thread_extension_data().get::<PythonTools>().unwrap();
            let first = crate::ops::submit_replaceable(&state, json!({
                "input":[UserInput::Text {text:"first".into(), text_elements:vec![]}]
            })).await.unwrap();
            assert_eq!(first["status"], "started");
            loop {
                let event = state.next_event().await.unwrap();
                if matches!(event["msg"]["type"].as_str(), Some("task_complete" | "turn_complete")) {
                    assert_eq!(event["msg"]["turn_id"], first["turn_id"]);
                    break;
                }
            }
            let before = tools.admission_ready(first["turn_id"].as_str().unwrap()).unwrap();
            let hold = state.thread.thread_extension_data().get_or_init(|| admission_order_tests::StartHold {
                entered: Semaphore::new(0),
                release: Semaphore::new(0),
            });
            // Only the native test can suspend the genuine BeforeTaskRegistration
            // hook. This is the managed _compact ingress; no fabricated receipt
            // or earlier provider hold can represent the SQ registration boundary.
            let compact = {
                let _mutation = state.mutations.lock().await;
                tools.check_idle(&state.thread).await.unwrap();
                state.thread.submit(Op::Compact).await.unwrap()
            };
            hold.entered.acquire().await.unwrap().forget();
            assert!(tools.admission_ready(&compact).is_err());
            assert!(tools.owns_admission(&before));
            let mut stopping = Box::pin(crate::ops::interrupt_managed_root(&state, &compact, true));
            assert!(
                tokio::time::timeout(std::time::Duration::from_millis(100), &mut stopping)
                    .await.is_err(),
                "owned compaction stop returned before Core registered and joined its task"
            );
            hold.release.add_permits(2);
            stopping.await.unwrap();
            loop {
                let event = state.next_event().await.unwrap();
                if event["msg"]["type"] == "turn_aborted" {
                    assert_eq!(event["msg"]["turn_id"], compact);
                    break;
                }
                assert!(
                    !matches!(event["msg"]["type"].as_str(), Some("task_complete" | "turn_complete")),
                    "cancelled compaction completed naturally: {event:?}"
                );
            }
            let later = crate::ops::submit_replaceable(&state, json!({
                "input":[UserInput::Text {text:"after cancelled compaction".into(), text_elements:vec![]}]
            })).await.unwrap();
            assert_eq!(later["status"], "started");
            assert_ne!(later["turn_id"], compact);
            // The completed first Turn can never interrupt this later owner.
            crate::ops::interrupt_managed_root(&state, first["turn_id"].as_str().unwrap(), false)
                .await.unwrap();
            loop {
                let event = state.next_event().await.unwrap();
                assert_ne!(event["msg"]["type"], "turn_aborted", "late root interrupted: {event:?}");
                if matches!(event["msg"]["type"].as_str(), Some("task_complete" | "turn_complete")) {
                    assert_eq!(event["msg"]["turn_id"], later["turn_id"]);
                    break;
                }
            }
            state.close().await.unwrap();
            engine.close().await.unwrap();
        }).await.expect("owned compaction or later root did not settle");
    }

    #[test]
    fn provider_error_metadata_distinguishes_observed_and_synthetic_status() {
        use codex_protocol::error::{
            CodexErrorDetails, RetryLimitReachedError, UnexpectedResponseError,
        };

        let synthetic = CodexErrorDetails::RetryLimit(RetryLimitReachedError {
            status: http::StatusCode::INTERNAL_SERVER_ERROR,
            request_id: None,
            provider_code: None,
        });
        assert!(provider_error_metadata(&synthetic, true).is_none());

        let observed = CodexErrorDetails::UnexpectedStatus(UnexpectedResponseError {
            status: http::StatusCode::INTERNAL_SERVER_ERROR,
            body: r#"{"error":{"code":"server_error","message":"synthetic message"}}"#.into(),
            user_message: None,
            url: None,
            cf_ray: None,
            request_id: Some("synthetic-observed-request".into()),
            identity_authorization_error: None,
            identity_error_code: None,
        });
        assert_eq!(
            provider_error_metadata(&observed, false).unwrap(),
            json!({
                "status_code": 500,
                "code": "server_error",
                "request_id": "synthetic-observed-request",
            })
        );
    }

    // Only a native test can arrange steering after the Core input snapshot
    // and before its conditional stop. Real threads never install this value.
    pub(super) struct GracefulHold {
        pub(super) reached: tokio::sync::Semaphore,
        pub(super) release: tokio::sync::Semaphore,
        pub(super) decisions: mpsc::UnboundedSender<Option<bool>>,
    }

    async fn start_admission(thread: &ExtensionData, turn_id: &str) -> ExtensionData {
        start_admission_with_store(thread, turn_id, ExtensionData::new(turn_id)).await
    }

    async fn start_admission_with_store(
        thread: &ExtensionData,
        turn_id: &str,
        turn: ExtensionData,
    ) -> ExtensionData {
        let session = ExtensionData::new("session");
        let mode = serde_json::from_value(json!({
            "mode":"default", "settings":{
                "model":"test", "reasoning_effort":null, "developer_instructions":null
            }
        }))
        .unwrap();
        PythonContributor::default()
            .on_turn_start(TurnStartInput {
                turn_id,
                collaboration_mode: &mode,
                token_usage_at_turn_start: None,
                session_store: &session,
                thread_store: thread,
                turn_store: &turn,
            })
            .await;
        turn
    }

    #[tokio::test]
    async fn rejected_duplicate_finish_preserves_the_original_callback() {
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        let turn = start_admission(&thread, "turn").await;
        let session = ExtensionData::new("session");
        let generation = tools.snapshot();
        let (key, invocation, mut receive) = tools
            .enqueue(
                "thread",
                &generation,
                0,
                "turn",
                "call",
                json!({}),
                &ToolCallSource::Direct,
                None,
                false,
                None,
            )
            .unwrap();
        assert!(
            tools
                .enqueue(
                    "thread",
                    &generation,
                    0,
                    "turn",
                    "call",
                    json!({}),
                    &ToolCallSource::Direct,
                    None,
                    false,
                    None,
                )
                .is_err()
        );
        let receipt = tools.pending.lock().unwrap()[&key]
            .callback
            .as_ref()
            .unwrap()
            .receipt
            .clone();
        for outcome in [
            ToolCallOutcome::Completed { success: false },
            ToolCallOutcome::Failed {
                handler_executed: true,
            },
            ToolCallOutcome::Aborted,
        ] {
            PythonContributor::default()
                .on_tool_finish(ToolFinishInput {
                    session_store: &session,
                    thread_store: &thread,
                    turn_store: &turn,
                    turn_id: "turn",
                    call_id: "call",
                    tool_name: &generation.entries[0].name,
                    source: ToolCallSource::Direct,
                    outcome,
                })
                .await;
            assert!(matches!(
                receive.try_recv(),
                Err(oneshot::error::TryRecvError::Empty)
            ));
            assert!(!tools.callback_operation("thread", &receipt, "cancelled"));
        }
        assert!(tools.callback_operation("thread", &receipt, "enter"));
        tools.retire_callback(&key, invocation, false);
        assert!(receive.await.is_err());
        assert!(tools.callback_operation("thread", &receipt, "release"));
        tools
            .finish_callbacks(Some("turn"), Some("call"), false)
            .await;
        assert!(tools.pending.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn scoped_automatic_callback_abort_waits_physical_release_and_preserves_sibling() {
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        let _turn = start_admission(&thread, "turn").await;
        let generation = tools.snapshot();
        let (a, invocation, received_a) = tools
            .enqueue(
                "thread",
                &generation,
                0,
                "turn",
                "A",
                json!({}),
                &ToolCallSource::Direct,
                None,
                false,
                None,
            )
            .unwrap();
        let (b, b_invocation, _received_b) = tools
            .enqueue(
                "thread",
                &generation,
                0,
                "turn",
                "B",
                json!({}),
                &ToolCallSource::Direct,
                None,
                false,
                None,
            )
            .unwrap();
        let receipt = tools.pending.lock().unwrap()[&a]
            .callback
            .as_ref()
            .unwrap()
            .receipt
            .clone();
        assert!(tools.callback_operation("thread", &receipt, "enter"));
        let mut foreign = receipt.clone();
        foreign["invocation"] = json!("foreign");
        assert!(!tools.callback_operation("thread", &foreign, "release"));
        tools.retire_callback(&a, invocation, false);
        assert!(received_a.await.is_err());
        assert!(tools.callback_operation("thread", &receipt, "cancelled"));
        assert!(tools.take_reply("thread", &a).unwrap().is_none());
        tools.check_pending("thread", &b).unwrap();
        let mut joined = tokio::spawn({
            let tools = tools.clone();
            async move {
                tools.finish_callbacks(Some("turn"), Some("A"), true).await;
            }
        });
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(110), &mut joined)
                .await
                .is_err()
        );
        assert!(!joined.is_finished());
        assert!(tools.callback_operation("thread", &receipt, "release"));
        tokio::time::timeout(std::time::Duration::from_secs(1), joined)
            .await
            .unwrap()
            .unwrap();
        tools.check_pending("thread", &b).unwrap();
        tools.retire_callback(&b, b_invocation, false);
    }

    #[tokio::test]
    async fn automatic_callback_reply_ack_precedes_physical_finish_join() {
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        let _turn = start_admission(&thread, "turn").await;
        let (key, invocation, receive) = tools
            .enqueue(
                "thread",
                &tools.snapshot(),
                0,
                "turn",
                "call",
                json!({}),
                &ToolCallSource::Direct,
                None,
                false,
                None,
            )
            .unwrap();
        let receipt = tools.pending.lock().unwrap()[&key]
            .callback
            .as_ref()
            .unwrap()
            .receipt
            .clone();
        assert!(tools.callback_operation("thread", &receipt, "enter"));
        let sender = tools.take_reply("thread", &key).unwrap().unwrap();
        assert!(
            sender
                .send(
                    DynamicToolResponse {
                        content_items: Vec::new(),
                        success: true
                    }
                    .into()
                )
                .is_ok()
        );
        assert!(
            tokio::time::timeout(std::time::Duration::from_secs(1), receive)
                .await
                .unwrap()
                .is_ok()
        );
        tools.retire_callback(&key, invocation, true);
        let mut joined = tokio::spawn({
            let tools = tools.clone();
            async move {
                tools
                    .finish_callbacks(Some("turn"), Some("call"), false)
                    .await;
            }
        });
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(110), &mut joined)
                .await
                .is_err()
        );
        assert!(!tools.callback_operation("thread", &receipt, "cancelled"));
        assert!(tools.callback_operation("thread", &receipt, "release"));
        tokio::time::timeout(std::time::Duration::from_secs(1), joined)
            .await
            .unwrap()
            .unwrap();
        assert!(tools.pending.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn scoped_preparation_cancel_joins_claimed_callback_and_expires_reply() {
        let tools = sdk();
        tools
            .family
            .members
            .write()
            .unwrap()
            .insert("thread".into(), Arc::downgrade(&tools));
        let id = tools.family.next_invocation.fetch_add(1, Ordering::Relaxed);
        let mut request = json!({"id":"turn", "msg":{"thread":"thread"}});
        let callback = tools.preparation_callback(id, &mut request);
        let receipt = callback.receipt.clone();
        let (reply, receive) = oneshot::channel::<Option<ModelContextUpdate>>();
        tools
            .preparation_replies
            .lock()
            .unwrap()
            .as_mut()
            .unwrap()
            .insert(
                id,
                PreparationSlot {
                    turn: "turn".into(),
                    reply: Some(PreparationReply::Context(reply)),
                    callback: callback.clone(),
                },
            );
        assert!(tools.callback_operation("thread", &receipt, "enter"));
        let cancellation = tokio_util::sync::CancellationToken::new();
        let mut joined = tokio::spawn({
            let tools = tools.clone();
            let cancellation = cancellation.clone();
            async move {
                tools
                    .await_preparation(id, &callback, receive, Some(cancellation))
                    .await
            }
        });
        cancellation.cancel();
        let notice = tokio::time::timeout(std::time::Duration::from_secs(1), tools.next_request())
            .await
            .unwrap();
        assert_eq!(notice["id"], "turn");
        assert_eq!(notice["msg"]["type"], "_sdk_callback_cancel");
        assert_eq!(notice["msg"]["receipt"], receipt);
        assert!(tools.take_preparation(id).is_err());
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(110), &mut joined)
                .await
                .is_err()
        );
        assert!(tools.callback_operation("thread", &receipt, "cancelled"));
        assert!(tools.callback_operation("thread", &receipt, "release"));
        assert!(
            tokio::time::timeout(std::time::Duration::from_secs(1), joined)
                .await
                .unwrap()
                .unwrap()
                .is_err()
        );
    }

    #[tokio::test]
    async fn unclaimed_scoped_preparation_cannot_enter_after_retirement() {
        let tools = sdk();
        tools
            .family
            .members
            .write()
            .unwrap()
            .insert("thread".into(), Arc::downgrade(&tools));
        let id = tools.family.next_invocation.fetch_add(1, Ordering::Relaxed);
        let mut request = json!({"id":"turn", "msg":{"thread":"thread"}});
        let callback = tools.preparation_callback(id, &mut request);
        let receipt = callback.receipt.clone();
        let (reply, receive) = oneshot::channel::<Option<ModelContextUpdate>>();
        tools
            .preparation_replies
            .lock()
            .unwrap()
            .as_mut()
            .unwrap()
            .insert(
                id,
                PreparationSlot {
                    turn: "turn".into(),
                    reply: Some(PreparationReply::Context(reply)),
                    callback: callback.clone(),
                },
            );
        let cancellation = tokio_util::sync::CancellationToken::new();
        cancellation.cancel();
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_secs(1),
                tools.await_preparation(id, &callback, receive, Some(cancellation))
            )
            .await
            .unwrap()
            .is_err()
        );
        assert!(!tools.callback_operation("thread", &receipt, "enter"));
        assert!(tools.take_preparation(id).is_err());
    }

    #[test]
    fn profile_native_defaults_keep_empty_ordinary_slots_and_exact_native_ceiling() {
        let handoff = ToolName::new(None, "transfer_to_b").with_default_namespace();
        let denied = ToolName::new(None, "transfer_to_denied").with_default_namespace();
        let selected = ToolName::namespaced("mcp__records", "lookup");
        let other = ToolName::namespaced("mcp__records", "other");
        let ordinary = ToolName::new(None, "ordinary").with_default_namespace();
        let exec = ToolName::new(None, "exec").with_default_namespace();
        let policy = ToolPolicy {
            allowed_tools: Some(vec![
                handoff.clone(),
                selected.clone(),
                other.clone(),
                ordinary.clone(),
                exec.clone(),
            ]),
            ..ToolPolicy::default()
        };
        let actual = merge_profile_native_defaults(
            false,
            Vec::new(),
            vec![
                handoff.clone(),
                denied.clone(),
                selected.clone(),
                selected.clone(),
            ],
            &[
                ordinary.clone(),
                exec.clone(),
                handoff.clone(),
                denied,
                selected.clone(),
                other.clone(),
            ],
            &policy,
        );
        assert_eq!(actual.len(), 2);
        assert!(actual.contains(&handoff));
        assert!(actual.contains(&selected));
        assert!(!actual.contains(&ordinary));
        assert!(!actual.contains(&other));
        assert!(!actual.contains(&exec));
        assert!(
            merge_profile_native_defaults(false, Vec::new(), vec![handoff, selected], &[], &policy)
                .is_empty()
        );
    }

    #[test]
    fn profile_native_defaults_preserve_selected_ordinary_order_without_duplicates() {
        let a = ToolName::new(None, "a").with_default_namespace();
        let b = ToolName::new(None, "b").with_default_namespace();
        let handoff = ToolName::new(None, "transfer_to_b").with_default_namespace();
        let defaults = vec![b.clone(), a.clone()];
        let actual = merge_profile_native_defaults(
            false,
            defaults.clone(),
            vec![handoff.clone(), handoff.clone()],
            &[a, b, handoff.clone()],
            &ToolPolicy::default(),
        );
        assert_eq!(&actual[..2], defaults.as_slice());
        assert_eq!(actual.last(), Some(&handoff));
        assert_eq!(actual.len(), 3);
    }

    #[test]
    fn profile_code_mode_defaults_obey_mode_candidates_and_policy() {
        let exec = ToolName::plain("exec").with_default_namespace();
        let wait = ToolName::plain("wait").with_default_namespace();
        let candidates = vec![exec.clone(), wait.clone()];
        let policy = ToolPolicy {
            allowed_tools: Some(candidates.clone()),
            ..ToolPolicy::default()
        };
        assert_eq!(
            merge_profile_native_defaults(true, Vec::new(), Vec::new(), &candidates, &policy),
            candidates
        );
        assert!(
            merge_profile_native_defaults(false, Vec::new(), Vec::new(), &candidates, &policy)
                .is_empty()
        );
        assert!(
            merge_profile_native_defaults(true, Vec::new(), Vec::new(), &[], &policy).is_empty()
        );
        let policy = ToolPolicy {
            allowed_tools: Some(vec![exec.clone()]),
            ..ToolPolicy::default()
        };
        assert_eq!(
            merge_profile_native_defaults(true, Vec::new(), Vec::new(), &candidates, &policy),
            vec![exec]
        );
    }

    #[test]
    fn automatic_media_peek_keeps_live_sender_and_rejects_closed_retained_reply() {
        let tools = catalog(
            ToolPolicy::default(),
            DynamicToolSpec::Function(function("a")),
        );
        let (key, _, receiver) = tools
            .enqueue(
                "owner",
                &tools.snapshot(),
                0,
                "turn",
                "call",
                json!({}),
                &ToolCallSource::Direct,
                None,
                false,
                None,
            )
            .unwrap();
        assert!(matches!(
            tools.automatic_reply_media("owner", &key),
            Ok(Some(None))
        ));
        assert!(tools.pending.lock().unwrap().contains_key(&key));
        assert!(matches!(
            tools.automatic_reply_media("foreign", &key),
            Ok(None)
        ));
        drop(receiver);
        assert!(matches!(
            tools.automatic_reply_media("owner", &key),
            Ok(None)
        ));
        assert!(tools.pending.lock().unwrap().contains_key(&key));
        assert!(tools.take_reply("owner", &key).unwrap().is_none());
    }

    fn admission_thread() -> ExtensionData {
        let thread = ExtensionData::new("thread");
        let tools = match Arc::try_unwrap(sdk()) {
            Ok(tools) => tools,
            Err(_) => panic!("test catalog has unexpected owners"),
        };
        thread.insert(tools);
        thread
    }

    #[test]
    fn empty_step_catalog_and_profile_mcp_source_do_not_reenter_extension_data() {
        for with_profile in [false, true] {
            let (finished, received) = std::sync::mpsc::channel();
            let worker = std::thread::spawn(move || {
                let session = ExtensionData::new("session");
                let thread = admission_thread();
                let tools = thread.get::<PythonTools>().unwrap();
                *tools.current.write().unwrap() = Arc::new(Generation {
                    id: 0,
                    entries: Vec::new(),
                    specs: Vec::new(),
                });
                let step = ExtensionData::new("opening-step");
                let policy = if with_profile {
                    step.insert(DescendantProfileSource(
                        codex_extension_api::AgentProfileReceipt {
                            profile_id: "opening-agent".into(),
                            epoch: 0,
                        },
                    ));
                    step.insert(crate::native_interception::ProfileMcpPolicy::default());
                    step.get::<crate::native_interception::ProfileMcpPolicy>()
                } else {
                    None
                };
                let executors =
                    PythonContributor::default().tools_for_step(&session, &thread, &step);
                assert!(executors.is_empty());
                assert!(step.get::<StepCatalog>().unwrap().slots.is_empty());
                if let Some(policy) = policy {
                    let source = step
                        .get::<crate::native_interception::ProfileMcpSource>()
                        .unwrap();
                    assert_eq!(source.receipt.as_ref().unwrap().profile_id, "opening-agent");
                    assert!(Arc::ptr_eq(source.policy.as_ref().unwrap(), &policy));
                }
                finished.send(()).unwrap();
            });
            received
                .recv_timeout(std::time::Duration::from_secs(5))
                .expect("Opening tool capture must finish without a public turn");
            worker.join().unwrap();
        }
    }

    // Model-attempt observations share this FIFO but require no callback reply.
    pub(super) async fn next_callback_request(tools: &PythonTools) -> Value {
        loop {
            let event = tools.next_request().await;
            if event["msg"]["type"] != "_sdk_model_attempt" {
                return event;
            }
            assert_eq!(event["id"], event["msg"]["turn_id"]);
            assert!(event["msg"]["turn_id"].is_string());
            assert!(
                event["msg"]["data"]["attempt_id"]
                    .as_str()
                    .is_some_and(|id| !id.is_empty())
            );
            let _: codex_protocol::model_retry::ModelAttemptPhase =
                serde_json::from_value(event["msg"]["data"]["phase"].clone()).unwrap();
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn prepared_request_rechecks_admission_after_no_edit_reply() {
        use codex_protocol::user_input::UserInput;

        tokio::time::timeout(std::time::Duration::from_secs(20), async {
            let provider = crate::engine::settlement_tests::Provider::start().await;
            let directory = tempfile::tempdir().unwrap();
            let engine = real_engine(&directory, &provider.url).await;
            let native = engine
                .start_with_tools(Default::default(), None, Some(vec![]), false)
                .await
                .unwrap();
            let state = native.current().unwrap();
            let tools = state
                .thread
                .thread_extension_data()
                .get::<PythonTools>()
                .unwrap();
            tools.prepare_requests(true).unwrap();
            let started = crate::ops::submit_replaceable(
                &state,
                json!({
                    "input": [UserInput::Text { text: "Try".into(), text_elements: vec![] }],
                }),
            )
            .await
            .unwrap();
            assert_eq!(started["status"], "started");
            let attempt = tools.next_request().await;
            assert_eq!(attempt["msg"]["type"], "_sdk_model_attempt");
            assert_eq!(attempt["id"], started["turn_id"]);
            assert_eq!(attempt["msg"]["turn_id"], started["turn_id"]);
            assert_eq!(attempt["msg"]["data"]["phase"], "started");
            assert!(
                attempt["msg"]["data"]["attempt_id"]
                    .as_str()
                    .is_some_and(|id| !id.is_empty())
            );
            let request = tools.next_request().await;
            assert_eq!(request["msg"]["type"], "_sdk_prepare_request");
            assert_eq!(request["msg"]["turn_id"], started["turn_id"]);
            let request_id = request["msg"]["request_id"].as_u64().unwrap();
            // Keep these synchronous: Core cannot consume the accepted reply
            // until this task yields on the current-thread executor.
            assert!(tools.reply_prepared_request(request_id, None, false));
            tools.prepare_requests(false).unwrap();

            let failure = "Fatal error: Native request preparation failed or was cancelled";
            let mut errors = Vec::new();
            loop {
                let event = state.next_event().await.unwrap();
                if event["msg"]["type"] == "error" {
                    errors.push(event["msg"]["message"].as_str().unwrap().to_owned());
                }
                if matches!(
                    event["msg"]["type"].as_str(),
                    Some("task_complete" | "turn_complete")
                ) {
                    assert_eq!(event["id"], started["turn_id"]);
                    assert_eq!(event["msg"]["error"]["message"], failure);
                    break;
                }
            }
            assert_eq!(errors, vec![failure]);
            assert!(provider.requests.lock().unwrap().is_empty());
            state.close().await.unwrap();
            engine.close().await.unwrap();
        })
        .await
        .expect("revoked prepared request did not settle");
    }

    #[tokio::test]
    async fn prepared_request_marker_survives_operation_and_thread_retirement() {
        use codex_extension_api::ModelRequestContributor;
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        let contributor = PreparedRequestContributor;
        let original = start_admission(&thread, "original").await;
        assert!(!contributor.prepares_request(&original));
        tools.prepare_requests(true).unwrap();
        let edited = start_admission(&thread, "edited").await;
        let captured = edited
            .get::<Arc<TurnAdmission>>()
            .unwrap()
            .prepared_operation;
        assert!(contributor.prepares_request(&edited));
        tools.prepare_requests(false).unwrap();
        assert!(contributor.prepares_request(&edited));
        assert_ne!(tools.prepared_operation(), captured);
        let ordinary = start_admission(&thread, "ordinary").await;
        assert!(!contributor.prepares_request(&ordinary));
        tools.retire_steps();
        assert!(contributor.prepares_request(&edited));
        assert!(tools.prepare_requests(true).is_err());
    }

    #[tokio::test]
    async fn prepared_request_operations_and_selection_have_separate_owners() {
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        tools.prepare_requests(true).unwrap();
        let operation = tools.prepared_operation();
        tools.select_steps(true, None, false);
        tools.select_steps(false, None, false);
        assert_eq!(tools.prepared_operation(), operation);
        tools.prepare_requests(false).unwrap();
        tools.prepare_requests(true).unwrap();
        assert_ne!(tools.prepared_operation(), operation);
        let child = PythonTools::descendant(&tools, "synthetic-child".into());
        assert!(child.prepared_operation().is_none());
        assert!(child.prepare_requests(true).is_err());
    }

    #[tokio::test]
    async fn child_receipt_retains_original_tracing_after_origin_scope_ends() {
        const CHILD: &str = "AGENTS_CHILD_RECEIPT_TRACE_TEST";
        if std::env::var_os(CHILD).is_none() {
            let status = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "python_tools::managed_tests::child_receipt_retains_original_tracing_after_origin_scope_ends"]).env(CHILD, "1").status().unwrap();
            assert!(status.success());
            return;
        }
        codex_otel::sdk_trace::configure_pipeline(false);
        for (disabled, sensitive) in [(true, true), (false, false), (true, false)] {
            let original: serde_json::Value = serde_json::from_str(
                &codex_otel::sdk_trace::start_trace_span(
                    &json!({
                        "name": "origin", "kind": "workflow",
                        "trace_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "disabled": disabled, "include_sensitive_data": sensitive,
                    })
                    .to_string(),
                )
                .unwrap(),
            )
            .unwrap();
            let handle = original["handle"].as_str().unwrap();
            let attached = codex_otel::sdk_trace::context(handle).unwrap().attach();
            let creation = codex_otel::sdk_trace::CapturedContext::capture()
                .context
                .unwrap();
            let thread = admission_thread();
            crate::native_interception::tests::bind_child_tracing_owner(
                &thread,
                Some(creation.clone()),
            );
            drop(attached);
            codex_otel::sdk_trace::end_trace_span(handle, r#"{"status":"ok"}"#).unwrap();
            codex_otel::sdk_trace::release_trace_span(handle);
            let unrelated: serde_json::Value = serde_json::from_str(
                &codex_otel::sdk_trace::start_trace_span(
                    r#"{"name":"unrelated","kind":"workflow","trace_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","include_sensitive_data":true}"#,
                ).unwrap(),
            ).unwrap();
            let unrelated_handle = unrelated["handle"].as_str().unwrap();
            let unrelated_context = codex_otel::sdk_trace::context(unrelated_handle)
                .unwrap()
                .attach();
            for id in ["original", "successor"] {
                let turn = start_admission(&thread, id).await;
                let tracing = turn
                    .get::<codex_otel::sdk_trace::CapturedContext>()
                    .unwrap()
                    .context
                    .clone()
                    .unwrap();
                let receipt = turn.get::<Arc<TurnAdmission>>().unwrap();
                assert!(Arc::ptr_eq(&tracing, &creation));
                assert!(Arc::ptr_eq(receipt.tracing.as_ref().unwrap(), &creation));
                let snapshot: serde_json::Value = serde_json::from_str(
                    &codex_otel::sdk_trace::trace_span_context(tracing.token()).unwrap(),
                )
                .unwrap();
                assert_eq!(snapshot["trace_id"], "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
                assert_eq!(snapshot["disabled"], disabled);
                assert_eq!(snapshot["include_sensitive_data"], sensitive);
            }
            // An existing turn carrier belongs to its own admission and must survive.
            let turn = ExtensionData::new("existing-carrier");
            let explicit = codex_otel::sdk_trace::CapturedContext::capture()
                .context
                .unwrap();
            turn.insert(codex_otel::sdk_trace::CapturedContext {
                context: Some(explicit.clone()),
            });
            let turn = start_admission_with_store(&thread, "existing-carrier", turn).await;
            assert!(Arc::ptr_eq(
                turn.get::<Arc<TurnAdmission>>()
                    .unwrap()
                    .tracing
                    .as_ref()
                    .unwrap(),
                &explicit
            ));
            // Explicitly absent child ownership cannot borrow this unrelated workflow.
            let absent = admission_thread();
            crate::native_interception::tests::bind_child_tracing_owner(&absent, None);
            let turn = start_admission(&absent, "untraced-child").await;
            assert!(
                turn.get::<codex_otel::sdk_trace::CapturedContext>()
                    .unwrap()
                    .context
                    .is_none()
            );
            assert!(turn.get::<Arc<TurnAdmission>>().unwrap().tracing.is_none());
            drop(unrelated_context);
            codex_otel::sdk_trace::end_trace_span(unrelated_handle, r#"{"status":"ok"}"#).unwrap();
            codex_otel::sdk_trace::release_trace_span(unrelated_handle);
        }
    }

    // Seed the real registered SDK contributors in their Core registration
    // order while retaining the exact turn store for ordering tests.
    pub(crate) async fn sdk_turn(
        state: &crate::engine::ThreadState,
        turn_id: &str,
    ) -> ExtensionData {
        let session = ExtensionData::new("test-session");
        let turn = ExtensionData::new(turn_id);
        let mode = serde_json::from_value(json!({
            "mode":"default", "settings":{
                "model":"test", "reasoning_effort":null, "developer_instructions":null
            }
        }))
        .unwrap();
        let input = || TurnStartInput {
            turn_id,
            collaboration_mode: &mode,
            token_usage_at_turn_start: None,
            session_store: &session,
            thread_store: state.thread.thread_extension_data(),
            turn_store: &turn,
        };
        state.engine.interception.on_turn_start(input()).await;
        PythonContributor::default().on_turn_start(input()).await;
        turn
    }

    pub(crate) async fn steering_item(
        state: &crate::engine::ThreadState,
        turn: &ExtensionData,
        client_id: Option<String>,
    ) {
        let mut message = codex_protocol::items::UserMessageItem::new(&[
            codex_protocol::user_input::UserInput::Text {
                text: "additional instructions".into(),
                text_elements: vec![],
            },
        ]);
        message.client_id = client_id;
        PythonContributor::default()
            .on_item_completed(
                state.thread.thread_extension_data(),
                turn,
                &codex_protocol::items::TurnItem::UserMessage(message),
            )
            .await;
    }

    #[tokio::test]
    async fn steering_contributors_distinguish_warmup_current_generation_and_old_receipt() {
        use codex_extension_api::{ModelRequestContributor, ModelRequestInput, ModelRequestKind};
        let provider = crate::engine::settlement_tests::Provider::start().await;
        let directory = tempfile::tempdir().unwrap();
        let engine = real_engine(&directory, &provider.url).await;
        let native = engine
            .start_with_tools(Default::default(), None, Some(vec![]), false)
            .await
            .unwrap();
        let state = native.current().unwrap();
        let store = state.thread.thread_extension_data();
        let tools = store.get::<PythonTools>().unwrap();
        let old_turn = sdk_turn(&state, "reused-id").await;
        let old = tools.admission_ready("reused-id").unwrap();
        let request = |thread_id: &str, kind| {
            let mut metadata = None;
            // Request the genuine model contributor: only a Generation for
            // this owned thread can consume the recorded steering.
            drop(engine.interception.request(ModelRequestInput {
                kind,
                thread_id,
                client_metadata: &mut metadata,
                model: "fixture",
                model_omitted: false,
            }));
        };
        steering_item(&state, &old_turn, None).await;
        steering_item(&state, &old_turn, Some("ordinary-client-id".into())).await;
        assert!(!old.has_steering_tail());
        steering_item(&state, &old_turn, Some(old.steering_client_id())).await;
        assert!(old.has_steering_tail());
        request(&state.id.to_string(), ModelRequestKind::Warmup);
        request("unrelated-thread", ModelRequestKind::Generation);
        assert!(old.has_steering_tail());
        request(&state.id.to_string(), ModelRequestKind::Generation);
        assert!(!old.has_steering_tail());
        steering_item(&state, &old_turn, Some(old.steering_client_id())).await;
        assert!(old.has_steering_tail());
        let successor_turn = sdk_turn(&state, "reused-id").await;
        let successor = tools.admission_ready("reused-id").unwrap();
        assert!(!Arc::ptr_eq(&old, &successor));
        steering_item(&state, &successor_turn, Some(old.steering_client_id())).await;
        request(&state.id.to_string(), ModelRequestKind::Generation);
        assert!(
            old.has_steering_tail(),
            "a new request cannot clear an earlier result"
        );
        assert!(
            !successor.has_steering_tail(),
            "a new receipt cannot adopt the old input"
        );
        state.close().await.unwrap();
        engine.close().await.unwrap();
    }

    #[tokio::test]
    async fn admission_waits_for_idle_and_survives_late_consumers() {
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        start_admission(&thread, "turn-a").await;
        assert!(tools.admission_ready("wrong-turn").is_err());
        let receipt = tools.admission_ready("turn-a").unwrap();
        let mut pending = Box::pin(receipt.wait());
        assert!(futures_util::poll!(&mut pending).is_pending());
        // Neither cancellation of a reader nor late subscription loses readiness.
        drop(pending);
        PythonContributor::default()
            .on_thread_idle(ThreadIdleInput {
                cause: codex_extension_api::ThreadIdleCause::Completed,
                session_store: &ExtensionData::new("session"),
                thread_store: &thread,
            })
            .await;
        receipt.wait().await.unwrap();
        tools
            .admission_ready("turn-a")
            .unwrap()
            .wait()
            .await
            .unwrap();
        start_admission(&thread, "turn-b").await;
        assert!(tools.admission_ready("turn-a").is_err());
        // The previous exact receipt remains fulfilled after replacement.
        receipt.wait().await.unwrap();
        let next = tools.admission_ready("turn-b").unwrap();
        assert!(futures_util::poll!(Box::pin(next.wait())).is_pending());
    }

    #[tokio::test]
    async fn child_steer_missing_foreign_and_closed_receipts_are_not_stale_turns() {
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        let missing = "Native completion has no matching admission receipt";
        assert_eq!(
            tools
                .child_steer_admission("old")
                .err()
                .unwrap()
                .to_string(),
            missing
        );
        let foreign_thread = admission_thread();
        let foreign_tools = foreign_thread.get::<PythonTools>().unwrap();
        let _foreign_store = start_admission(&foreign_thread, "new").await;
        let foreign = foreign_tools.admission_ready("new").unwrap();
        *tools.admission.lock().unwrap() = Some(foreign);
        // Neither a mismatched ID nor a matching ID on someone else's receipt
        // establishes current ownership.
        for requested in ["old", "new"] {
            assert_eq!(
                tools
                    .child_steer_admission(requested)
                    .err()
                    .unwrap()
                    .to_string(),
                missing
            );
        }
        let _local_store = start_admission(&thread, "new").await;
        let local = tools.admission_ready("new").unwrap();
        assert!(Arc::ptr_eq(
            &local,
            &tools.child_steer_admission("new").unwrap()
        ));
        assert_eq!(
            tools.admission_ready("old").err().unwrap().to_string(),
            missing,
            "ordinary completion retains its own error contract"
        );
        local.state.send_replace(AdmissionState::Closed);
        assert_eq!(
            tools
                .child_steer_admission("old")
                .err()
                .unwrap()
                .to_string(),
            missing
        );
    }

    #[tokio::test]
    async fn failed_local_join_does_not_release_admission_or_poison_a_replacement() {
        let provider = crate::engine::settlement_tests::Provider::start().await;
        let directory = tempfile::tempdir().unwrap();
        let engine = real_engine(&directory, &provider.url).await;
        let native = engine
            .start_with_tools(Default::default(), None, Some(vec![]), false)
            .await
            .unwrap();
        let state = native.current().unwrap();
        let tools = state
            .thread
            .thread_extension_data()
            .get::<PythonTools>()
            .unwrap();
        let turn_id = "reused-id";
        let _first = sdk_turn(&state, turn_id).await;
        let failed = tools.admission_ready(turn_id).unwrap();
        // The native failure is independent of an idle slot and survives
        // every waiter on this exact receipt.
        failed
            .local_command_cleanup_failed
            .store(true, Ordering::Release);
        for _ in 0..2 {
            let error = tools
                .finish_session_interrupt(&state.thread, &failed)
                .await
                .unwrap_err();
            assert_eq!(error.to_string(), LOCAL_COMMAND_CLEANUP_FAILED);
        }
        state
            .thread
            .emit_thread_idle_lifecycle_if_idle(codex_extension_api::ThreadIdleCause::Interrupted)
            .await;
        assert!(*failed.state.borrow() == AdmissionState::Pending);

        let _next = sdk_turn(&state, turn_id).await;
        let successor = tools.admission_ready(turn_id).unwrap();
        tools
            .finish_session_interrupt(&state.thread, &successor)
            .await
            .unwrap();
        successor.wait().await.unwrap();
        assert!(failed.check_local_command_cleanup().is_err());
        state.close().await.unwrap();
        engine.close().await.unwrap();
    }

    #[tokio::test]
    async fn stale_child_idle_probe_cannot_ready_or_unmark_native_successor() {
        let provider = crate::engine::settlement_tests::Provider::start().await;
        let directory = tempfile::tempdir().unwrap();
        let engine = real_engine(&directory, &provider.url).await;
        let native = engine
            .start_with_tools(Default::default(), None, Some(vec![]), false)
            .await
            .unwrap();
        let state = native.current().unwrap();
        let tools = state
            .thread
            .thread_extension_data()
            .get::<PythonTools>()
            .unwrap();
        let _old_store = sdk_turn(&state, "same-turn-id").await;
        let old = tools.admission_ready("same-turn-id").unwrap();
        let hold =
            state
                .thread
                .thread_extension_data()
                .get_or_init(|| admission_order_tests::IdleHold {
                    entered: tokio::sync::Semaphore::new(0),
                    release: tokio::sync::Semaphore::new(0),
                });
        // Let the actual Core SQ barrier and idle checks finish, but pause
        // before the binding consumes this old turn's probe.
        let probing = tokio::spawn({
            let tools = tools.clone();
            let thread = state.thread.clone();
            let old = old.clone();
            async move { tools.finish_session_interrupt(&thread, &old).await }
        });
        hold.entered.acquire().await.unwrap().forget();
        let successor_store = sdk_turn(&state, "same-turn-id").await;
        let successor = tools.admission_ready("same-turn-id").unwrap();
        assert!(!Arc::ptr_eq(&old, &successor));
        successor.graceful_requested.store(true, Ordering::Release);
        hold.release.add_permits(1);
        // A later owner check may reject the old call. Neither that check
        // nor the passing of time can confer admission on its replacement.
        let _ = probing.await.unwrap();
        assert!(
            futures_util::poll!(Box::pin(successor.wait())).is_pending(),
            "the old Core idle probe made a replacement's receipt Ready"
        );
        assert!(successor.graceful_requested.load(Ordering::Acquire));
        assert!(tools.admitted_turn(&successor_store).is_some());
        // Ordinary Core idle remains valid for the still-current receipt.
        hold.release.add_permits(2);
        state
            .thread
            .emit_thread_idle_lifecycle_if_idle(codex_extension_api::ThreadIdleCause::Completed)
            .await;
        successor.wait().await.unwrap();
        assert!(!successor.graceful_requested.load(Ordering::Acquire));
        state.close().await.unwrap();
        engine.close().await.unwrap();
    }

    #[tokio::test]
    async fn graph_shutdown_proof_ignores_replaced_ready_and_other_owner_receipts() {
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        let session = ExtensionData::new("session");
        let _first = start_admission(&thread, "same-turn-id").await;
        let old = tools.admission_ready("same-turn-id").unwrap();
        // Model the real raw Op reply retained in Core's SQ after Shutdown.
        // It stays alive but never runs; the existing root barrier must still
        // wait for it, whereas a proved graph source stop can end that wait.
        let (_sent, reply) = oneshot::channel();
        let mut ack = Box::pin(tools.acknowledge_session_interrupt(&old, reply));
        let mut stopped = Box::pin(tools.wait_for_graph_thread_stop());
        assert!(futures_util::poll!(&mut ack).is_pending());
        assert!(futures_util::poll!(&mut stopped).is_pending());

        let _second = start_admission(&thread, "same-turn-id").await;
        assert!(*old.state.borrow() == AdmissionState::Closed);
        assert!(futures_util::poll!(&mut stopped).is_pending());
        let current = tools.admission_ready("same-turn-id").unwrap();
        assert!(!Arc::ptr_eq(&old, &current));
        PythonContributor::default()
            .on_thread_idle(ThreadIdleInput {
                cause: codex_extension_api::ThreadIdleCause::Interrupted,
                session_store: &session,
                thread_store: &thread,
            })
            .await;
        assert!(*current.state.borrow() == AdmissionState::Ready);
        assert!(futures_util::poll!(&mut stopped).is_pending());

        let sibling = ExtensionData::new("sibling");
        sibling.insert(PythonTools::descendant(&tools, "other-incarnation".into()));
        let _sibling_turn = start_admission(&sibling, "same-turn-id").await;
        PythonContributor::default()
            .on_thread_stop(ThreadStopInput {
                session_store: &session,
                thread_store: &sibling,
            })
            .await;
        assert!(futures_util::poll!(&mut stopped).is_pending());

        PythonContributor::default()
            .on_thread_stop(ThreadStopInput {
                session_store: &session,
                thread_store: &thread,
            })
            .await;
        stopped.await.unwrap();
        assert!(futures_util::poll!(&mut ack).is_pending());
    }

    #[tokio::test]
    async fn admission_close_wakes_waiters_and_cannot_be_revived_by_idle() {
        let thread = admission_thread();
        let session = ExtensionData::new("session");
        start_admission(&thread, "turn-a").await;
        let receipt = thread
            .get::<PythonTools>()
            .unwrap()
            .admission_ready("turn-a")
            .unwrap();
        let mut pending = Box::pin(receipt.wait());
        assert!(futures_util::poll!(&mut pending).is_pending());
        PythonContributor::default()
            .on_thread_stop(ThreadStopInput {
                session_store: &session,
                thread_store: &thread,
            })
            .await;
        assert!(pending.await.is_err());
        PythonContributor::default()
            .on_thread_idle(ThreadIdleInput {
                cause: codex_extension_api::ThreadIdleCause::Completed,
                session_store: &session,
                thread_store: &thread,
            })
            .await;
        assert!(receipt.wait().await.is_err());
    }

    #[tokio::test]
    async fn integrity_reason_projection_refuses_a_same_id_recovery_receipt() {
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        let _first = start_admission(&thread, "same-id").await;
        let receipt = tools.observed_admission("same-id").unwrap();
        assert!(receipt.permits_integrity_reason());
        let _second = start_admission(&thread, "same-id").await;
        assert!(tools.observed_admission("same-id").is_none());
        assert!(
            !tools
                .admission_ready("same-id")
                .unwrap()
                .permits_integrity_reason()
        );
    }

    #[tokio::test]
    async fn model_failure_reason_cannot_follow_same_id_receipt_replacement() {
        use codex_protocol::error::ResponseIntegrityReason;
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        tools
            .family
            .members
            .write()
            .unwrap()
            .insert("thread".into(), Arc::downgrade(&tools));
        let _first_store = start_admission(&thread, "same-id").await;
        let first = tools.admission_ready("same-id").unwrap();
        first.reject_model("thread", ResponseIntegrityReason::UndecodableOutputItem);
        let _second_store = start_admission(&thread, "same-id").await;
        let second = tools.admission_ready("same-id").unwrap();
        assert!(!Arc::ptr_eq(&first, &second));
        let mut read = Box::pin(tools.next_request());
        assert!(futures_util::poll!(&mut read).is_pending());
        second.reject_model("thread", ResponseIntegrityReason::UnfinishedAssistantItem);
        let notice = read.await;
        assert_eq!(
            notice["msg"]["response_integrity_reason"],
            "unfinished_assistant_item"
        );
        assert_eq!(
            notice["msg"]["message"],
            crate::response_integrity::UNSUPPORTED_OUTPUT
        );
    }

    #[tokio::test]
    async fn graceful_request_and_failure_cannot_follow_same_id_replacement() {
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        let original_store = start_admission(&thread, "same-id").await;
        assert!(!tools.request_graceful_interrupt("wrong-turn").unwrap());
        assert!(tools.request_graceful_interrupt("same-id").unwrap());
        let original = tools.requested_graceful_admission(&original_store).unwrap();
        tools.graceful_failure(&original);

        let successor_store = start_admission(&thread, "same-id").await;
        let successor = tools.admission_ready("same-id").unwrap();
        assert!(!Arc::ptr_eq(&successor, &original));
        let previous_item = codex_protocol::items::TurnItem::UserMessage(
            codex_protocol::items::UserMessageItem::new(&[
                codex_protocol::user_input::UserInput::Text {
                    text: "earlier turn".into(),
                    text_elements: vec![],
                },
            ]),
        );
        PythonContributor::default()
            .on_item_completed(&thread, &original_store, &previous_item)
            .await;
        assert!(!successor.input_recorded.load(Ordering::Acquire));
        assert!(
            tools
                .requested_graceful_admission(&original_store)
                .is_none()
        );
        assert!(
            tools
                .requested_graceful_admission(&successor_store)
                .is_none()
        );
        // The inbox must discard the failure of the retired receipt, even if
        // Core later reuses its ID and the owner starts a fresh graceful stop.
        assert!(tools.request_graceful_interrupt("same-id").unwrap());
        assert!(
            tools
                .requested_graceful_admission(&successor_store)
                .is_some()
        );
        let mut read = Box::pin(tools.next_request());
        assert!(futures_util::poll!(&mut read).is_pending());
        tools.graceful_failure(&successor);
        assert_eq!(
            read.await,
            json!({
                "id": "same-id",
                "msg": {"type": "_sdk_graceful_interrupt_failed", "turn_id": "same-id"}
            })
        );
    }

    #[tokio::test]
    async fn graceful_cannot_mark_an_idle_closed_or_descendant_receipt() {
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        let turn = start_admission(&thread, "turn-a").await;
        assert!(tools.request_graceful_interrupt("turn-a").unwrap());
        PythonContributor::default()
            .on_thread_idle(ThreadIdleInput {
                cause: codex_extension_api::ThreadIdleCause::Completed,
                session_store: &ExtensionData::new("session"),
                thread_store: &thread,
            })
            .await;
        assert!(tools.requested_graceful_admission(&turn).is_none());
        assert!(!tools.request_graceful_interrupt("turn-a").unwrap());

        let turn = start_admission(&thread, "turn-b").await;
        assert!(tools.request_graceful_interrupt("turn-b").unwrap());
        PythonContributor::default()
            .on_thread_stop(ThreadStopInput {
                session_store: &ExtensionData::new("session"),
                thread_store: &thread,
            })
            .await;
        assert!(tools.requested_graceful_admission(&turn).is_none());
        assert!(!tools.request_graceful_interrupt("turn-b").unwrap());
        let child = PythonTools::descendant(&tools, "child-incarnation".into());
        assert!(child.request_graceful_interrupt("turn-b").is_err());
    }

    pub(crate) async fn real_engine(
        directory: &tempfile::TempDir,
        url: &str,
    ) -> Arc<crate::engine::EngineState> {
        real_engine_store(directory, url, false).await
    }

    async fn real_engine_store(
        directory: &tempfile::TempDir,
        url: &str,
        memory: bool,
    ) -> Arc<crate::engine::EngineState> {
        real_engine_store_inner(directory, url, memory, false).await
    }

    async fn real_engine_store_with_stop_hook(
        directory: &tempfile::TempDir,
        url: &str,
        memory: bool,
    ) -> Arc<crate::engine::EngineState> {
        real_engine_store_inner(directory, url, memory, true).await
    }

    async fn real_engine_store_inner(
        directory: &tempfile::TempDir,
        url: &str,
        memory: bool,
        completion_hook: bool,
    ) -> Arc<crate::engine::EngineState> {
        let home = directory.path().join("home");
        let work = directory.path().join("work");
        let storage = directory.path().join("storage");
        for path in [&home, &work, &storage] {
            std::fs::create_dir(path).unwrap();
        }
        let mut config = json!({
            "model": "receipt-fixture",
            "model_provider": "fixture",
            "model_providers.fixture": {
                "name": "Receipt fixture", "base_url": url,
                "wire_api": "responses", "requires_openai_auth": false,
                "supports_websockets": false, "request_max_retries": 0,
                "stream_max_retries": 0
            },
            "approval_policy": "never", "sandbox_mode": "read-only",
            "web_search": "disabled", "agents.enabled": false,
            "features.code_mode": false, "features.instant_interrupt": false
        });
        let mut runtime = json!({
            "codex_home": home, "cwd": work, "native_thread_store": storage,
            "codex_self_exe": null, "ephemeral": false
        });
        if completion_hook {
            config["features.hooks"] = json!(true);
            config["hooks"] = json!({"Stop":[{"hooks":[{
                "type":"command", "command":"exit 0", "async":false
            }]}]});
            // Only this literal fixture hook is present; ambient config is isolated.
            runtime["bypass_hook_trust"] = json!(true);
            config["model_post_turn_compact_threshold_percent"] = json!(0);
        }
        Arc::new(
            crate::engine::EngineState::new(
                config.to_string(),
                runtime.to_string(),
                memory,
                Some("fixture-unused-key".into()),
            )
            .await
            .unwrap(),
        )
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn steering_real_last_request_gap_continues_once_and_rejects_abort_stop_or_supersession()
    {
        use crate::engine::settlement_tests::Provider;
        use crate::storage::{HookCompletionFlushHold, MemoryThreadStore};
        use codex_protocol::protocol::Op;
        use codex_protocol::user_input::UserInput;
        use codex_thread_store::LoadThreadHistoryParams;

        tokio::time::timeout(std::time::Duration::from_secs(55), async {
            for outcome in ["continue", "abort", "stop", "superseded"] {
                let abort = outcome == "abort";
                let provider = Provider::start().await;
                let directory = tempfile::tempdir().unwrap();
                let engine = real_engine_store_with_stop_hook(&directory, &provider.url, true).await;
                let native = engine
                    .start_with_tools(Default::default(), None, Some(vec![]), false)
                    .await.unwrap();
                let state = native.current().unwrap();
                let store = engine.store.as_any().downcast_ref::<MemoryThreadStore>().unwrap();
                // Let Stop finish, then hold the task-return durability flush.
                // No late input exists until after the outer task loop exits.
                let hold = Arc::new(HookCompletionFlushHold::new(
                    state.id, codex_protocol::protocol::HookEventName::Stop,
                ));
                *store.hook_completion_flush_hold.lock().unwrap() = Some(hold.clone());
                let started = crate::ops::submit_replaceable(&state, json!({
                    "input": [UserInput::Text { text: "initial request".into(), text_elements: vec![] }],
                })).await.unwrap();
                assert_eq!(started["status"], "started");
                hold.bind(started["turn_id"].as_str().unwrap());
                hold.flush.entered.acquire().await.unwrap().forget();
                assert_eq!(provider.requests.lock().unwrap().len(), 1,
                    "the barrier must be after the actual completed sample");
                let accepted = crate::ops::submit_replaceable(&state, json!({
                    "mode": "steer", "expected_turn_id": started["turn_id"],
                    "input": [UserInput::Text { text: "unique steering tail in the native gap".into(), text_elements: vec![] }],
                })).await.unwrap();
                assert_eq!(accepted, json!({"status": "steered", "turn_id": started["turn_id"]}));
                if abort {
                    state.thread.submit(Op::Interrupt).await.unwrap();
                    // The native force path cancels a task in the final flush;
                    // it must report its real abort rather than the tail.
                } else {
                    hold.flush.release.add_permits(1);
                }
                loop {
                    let event = state.next_session_event().await.unwrap();
                    let kind = event["msg"]["type"].as_str().unwrap_or("");
                    if ["turn_aborted", "task_complete", "turn_complete"].contains(&kind) {
                        assert_eq!(event["msg"]["turn_id"], started["turn_id"]);
                        assert_eq!(kind == "turn_aborted", abort);
                        assert_eq!(event["sdk_unsampled_steering"], if abort {
                            Value::Null
                        } else {
                            Value::Bool(true)
                        }, "terminal={event}; requests={}",
                            serde_json::to_string(&*provider.requests.lock().unwrap()).unwrap());
                        break;
                    }
                }
                assert_eq!(provider.requests.lock().unwrap().len(), 1);
                if !abort {
                    hold.assert_completed();
                }
                let continuation = json!({
                    "mode": "continue_if_idle", "input": [],
                    "expected_previous_turn_id": started["turn_id"],
                });
                if abort {
                    let rejected = crate::ops::submit_replaceable(&state, continuation.clone()).await.unwrap();
                    assert_eq!(rejected["status"], "not_submitted");
                }
                if !abort {
                    let context = engine.store.load_latest_model_context(LoadThreadHistoryParams {
                        thread_id: state.id, include_archived: true,
                    }).await.unwrap();
                    let response_items: Vec<_> = context.items.iter()
                        .filter(|item| matches!(item, codex_history::RolloutItem::ResponseItem(_)))
                        .collect();
                    let history = serde_json::to_string(&response_items).unwrap();
                    // Persisted context contains a single response item for
                    // the late user message, even though it was not sampled.
                    assert_eq!(history.matches("unique steering tail in the native gap").count(), 1);
                    if outcome == "stop" {
                        let tools = state.thread.thread_extension_data().get::<PythonTools>().unwrap();
                        let _stop = tools.request_graph_interrupt(false).unwrap();
                        let rejected = crate::ops::submit_replaceable(&state, continuation).await.unwrap();
                        assert_eq!(rejected["status"], "not_submitted");
                        assert_eq!(provider.requests.lock().unwrap().len(), 1);
                        state.close().await.unwrap();
                        engine.close().await.unwrap();
                        continue;
                    }
                    // Another explicit request may supersede the late-steer
                    // recovery, including if it finishes before recovery starts.
                    let request = if outcome == "superseded" {
                        json!({"input": [UserInput::Text {
                            text: "unrelated explicit work".into(), text_elements: vec![]
                        }]})
                    } else {
                        continuation.clone()
                    };
                    let successor = crate::ops::submit_replaceable(&state, request).await.unwrap();
                    assert_eq!(successor["status"], "started");
                    assert_ne!(successor["turn_id"], started["turn_id"]);
                    loop {
                        let event = state.next_session_event().await.unwrap();
                        if ["task_complete", "turn_complete"].contains(
                            &event["msg"]["type"].as_str().unwrap_or("")) {
                            assert_eq!(event["msg"]["turn_id"], successor["turn_id"]);
                            assert_eq!(event["sdk_unsampled_steering"], false);
                            break;
                        }
                    }
                    let requests = provider.requests.lock().unwrap();
                    assert_eq!(requests.len(), 2);
                    assert!(!requests[0].to_string().contains("unique steering tail"));
                    assert_eq!(requests[1]["input"].to_string()
                        .matches("unique steering tail in the native gap").count(), 1);
                    if outcome == "continue" {
                        assert!(requests[1]["input"].to_string().contains("agents_steering"));
                        assert!(!requests[1]["input"].to_string().contains("unrelated explicit work"));
                    }
                    drop(requests);
                    // Both explicit and automatic successors consume the right
                    // to continue; a stale callback must not issue a third sample.
                    let repeated = crate::ops::submit_replaceable(&state, continuation).await.unwrap();
                    assert_eq!(repeated["status"], "not_submitted");
                    assert_eq!(provider.requests.lock().unwrap().len(), 2);
                }
                state.close().await.unwrap();
                engine.close().await.unwrap();
            }
        }).await.expect("native steering at the last request boundary failed to settle");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn child_submit_waits_for_finisher_and_recovers_unnotified_raw_interrupt() {
        use crate::engine::settlement_tests::{Provider, request};
        use codex_protocol::turn_input::StartIfIdleSubmission;

        tokio::time::timeout(std::time::Duration::from_secs(15), async {
            for raw_interrupt in [false, true] {
                let provider = Provider::start().await;
                let directory = tempfile::tempdir().unwrap();
                let engine = real_engine(&directory, &provider.url).await;
                let native = engine
                    .start_with_tools(Default::default(), None, Some(vec![]), false)
                    .await
                    .unwrap();
                let state = native.current().unwrap();
                let tools = state
                    .thread
                    .thread_extension_data()
                    .get::<PythonTools>()
                    .unwrap();
                let (reached, mut observing) = mpsc::unbounded_channel();
                let hold = state.thread.thread_extension_data().get_or_init(|| {
                    admission_order_tests::Hold {
                        reached,
                        release: tokio::sync::Semaphore::new(0),
                    }
                });
                let StartIfIdleSubmission::Started { turn_id } = state
                    .thread
                    .start_turn_if_idle(request("first"))
                    .await
                    .unwrap()
                else {
                    panic!("initial native turn did not start");
                };
                assert_eq!(observing.recv().await.unwrap(), turn_id);
                let receipt = tools.admission_ready(&turn_id).unwrap();
                assert!(receipt.terminal.load(Ordering::Acquire));
                assert!(futures_util::poll!(Box::pin(receipt.wait())).is_pending());
                // Core detached the actual task but still owns its active
                // slot. A false SQ reply cannot admit the next turn yet.
                let mut finishing = Box::pin(tools.finish_child_before_submit(&state.thread));
                assert!(
                    tokio::time::timeout(std::time::Duration::from_millis(75), &mut finishing)
                        .await
                        .is_err(),
                    "child admission bypassed the held native finisher"
                );
                if raw_interrupt {
                    // This takes the placeholder without TurnAborted or idle.
                    // Do not release the held finisher: a wait on its natural
                    // idle notification can never complete on this path.
                    state.thread.submit(Op::Interrupt).await.unwrap();
                    finishing.await.unwrap();
                    receipt.wait().await.unwrap();
                    hold.release.add_permits(1);
                } else {
                    hold.release.add_permits(1);
                    finishing.await.unwrap();
                    receipt.wait().await.unwrap();
                }
                // The first and only actual new input request now gets a new
                // native turn; there is no rejection or retargeting retry.
                let StartIfIdleSubmission::Started { turn_id: next_id } = state
                    .thread
                    .start_turn_if_idle(request("second"))
                    .await
                    .unwrap()
                else {
                    panic!("completed child did not admit its next native turn");
                };
                assert_ne!(next_id, turn_id);
                hold.release.add_permits(2);
                state.close().await.unwrap();
                engine.close().await.unwrap();
            }
        })
        .await
        .expect("native child admission did not finish");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn recovered_manual_turn_steering_in_final_gap_continues_once() {
        use crate::engine::settlement_tests::Provider;
        use crate::storage::{HookCompletionFlushHold, MemoryThreadStore};
        use crate::{checkpoint, manual_checkpoint};
        use codex_protocol::dynamic_tools::DynamicToolResponse;
        use codex_protocol::user_input::UserInput;

        tokio::time::timeout(std::time::Duration::from_secs(35), async {
            let provider = Provider::with_first_item(Some(json!({
                "id":"fc-lookup", "type":"function_call", "name":"lookup",
                "arguments":"{}", "call_id":"call-lookup"
            }))).await;
            let directory = tempfile::tempdir().unwrap();
            let engine = real_engine_store_with_stop_hook(&directory, &provider.url, true).await;
            let options = json!({
                "_application_instructions":"Run lookup when requested.", "_manual_tool_slots":[0],
                "tool_policy":{"allowed_tools":[{"namespace":"functions","name":"lookup"}]}
            });
            let specs = json!([{
                "type":"function", "name":"lookup", "description":"Read an approved lookup.",
                "inputSchema":{"type":"object","properties":{},"additionalProperties":false}
            }]);
            let native = engine.start_with_tools(
                serde_json::from_value(options.clone()).unwrap(),
                None, Some(serde_json::from_value(specs.clone()).unwrap()), false
            ).await.unwrap();
            let state = native.current().unwrap();
            let tools = state.thread.thread_extension_data().get::<PythonTools>().unwrap();
            crate::application_instructions::publish(
                &state.thread, "Run lookup when requested.".into()
            ).await.unwrap();
            let original = crate::ops::submit_replaceable(&state, json!({
                "input":[UserInput::Text { text:"run the manual lookup".into(), text_elements:vec![] }]
            })).await.unwrap();
            assert_eq!(original["status"], "started");
            let notice = next_callback_request(&tools).await;
            assert_eq!(notice["msg"]["callId"], "call-lookup");
            assert!(manual_checkpoint::ready(&state, &native.manual).await.unwrap());
            let mut payload = manual_checkpoint::capture(&state, &native.manual, None)
                .await.unwrap().unwrap();
            assert_eq!(payload["format_version"], 3);
            assert_eq!(provider.requests.lock().unwrap().len(), 1);
            engine.close().await.unwrap();

            // Use the real validated v3 restore and current catalog, exactly as
            // the native entrypoint does. No saved frame grants a direct reply.
            let restored_directory = tempfile::tempdir().unwrap();
            let restored_engine = real_engine_store_with_stop_hook(&restored_directory, &provider.url, true).await;
            let restored_options: crate::options::ThreadOptions = serde_json::from_value(options).unwrap();
            let restored_specs: Vec<codex_core_api::DynamicToolSpec> =
                serde_json::from_value(specs).unwrap();
            let frame = manual_checkpoint::validate(
                &payload, restored_options.application_instructions.as_deref().unwrap(),
                &restored_specs, &restored_options.manual_tool_slots,
            ).unwrap();
            payload["format_version"] = json!(2);
            payload.as_object_mut().unwrap().remove("native_continuation");
            let restored = checkpoint::restore(
                &restored_engine, serde_json::from_value(payload).unwrap(),
                restored_options, Some(restored_specs),
            ).await.unwrap();
            *restored.manual.lock().unwrap() = Some(frame);
            let resumed = restored.current().unwrap();
            let tools = resumed.thread.thread_extension_data().get::<PythonTools>().unwrap();
            manual_checkpoint::prepare(&resumed, &restored.manual).await.unwrap();
            let notice = next_callback_request(&tools).await;
            assert_eq!(notice["msg"]["callId"], "call-lookup");
            let mut grants = tools.manual_snapshot(&resumed.id.to_string()).unwrap();
            assert_eq!(grants.len(), 1);
            let (_, grant, _) = grants.pop().unwrap();
            assert!(tools.check_manual(&resumed.id.to_string(), &grant));
            let response: DynamicToolResponse = serde_json::from_value(json!({
                "contentItems":[{"type":"inputText","text":"approved lookup value"}], "success":true
            })).unwrap();
            assert!(tools.respond_manual(&resumed.id.to_string(), &grant, response, true));
            assert!(!tools.check_manual(&resumed.id.to_string(), &grant));

            let store = restored_engine.store.as_any().downcast_ref::<MemoryThreadStore>().unwrap();
            let hold = Arc::new(HookCompletionFlushHold::new(
                resumed.id, codex_protocol::protocol::HookEventName::Stop,
            ));
            hold.bind(original["turn_id"].as_str().unwrap());
            *store.hook_completion_flush_hold.lock().unwrap() = Some(hold.clone());
            let recovery_state = resumed.clone();
            let manual = restored.manual.clone();
            let recovering = tokio::spawn(async move {
                manual_checkpoint::continue_if_ready(&recovery_state, &manual, None, None, None).await.unwrap()
            });
            hold.flush.entered.acquire().await.unwrap().forget();
            assert_eq!(provider.requests.lock().unwrap().len(), 2,
                "the restored native barrier must be after its completed model sample");
            let recovered = recovering.await.unwrap();
            assert_eq!(recovered, json!({"status":"started","turn_id":original["turn_id"]}));
            let steer = crate::ops::submit_replaceable(&resumed, json!({
                "mode":"steer", "expected_turn_id": recovered["turn_id"],
                "input":[UserInput::Text {
                    text:"manual recovery has exactly one late request".into(), text_elements:vec![]
                }]
            })).await.unwrap();
            assert_eq!(steer, json!({"status":"steered","turn_id":recovered["turn_id"]}));
            hold.flush.release.add_permits(1);
            loop {
                let event = resumed.next_session_event().await.unwrap();
                let kind = event["msg"]["type"].as_str().unwrap_or("");
                if ["turn_aborted", "task_complete", "turn_complete"].contains(&kind) {
                    assert_ne!(kind, "turn_aborted", "{event}");
                    assert_eq!(event["msg"]["turn_id"], recovered["turn_id"]);
                    assert_eq!(event["sdk_unsampled_steering"], true,
                        "terminal={event}; requests={}",
                        serde_json::to_string(&*provider.requests.lock().unwrap()).unwrap());
                    break;
                }
            }
            hold.assert_completed();
            let continuation = json!({
                "mode":"continue_if_idle", "input":[],
                "expected_previous_turn_id":recovered["turn_id"],
            });
            let next = crate::ops::submit_replaceable(&resumed, continuation.clone()).await.unwrap();
            assert_eq!(next["status"], "started");
            assert_ne!(next["turn_id"], recovered["turn_id"]);
            loop {
                let event = resumed.next_session_event().await.unwrap();
                let kind = event["msg"]["type"].as_str().unwrap_or("");
                if ["turn_aborted", "task_complete", "turn_complete"].contains(&kind) {
                    assert_ne!(kind, "turn_aborted", "{event}");
                    assert_eq!(event["msg"]["turn_id"], next["turn_id"]);
                    assert_eq!(event["sdk_unsampled_steering"], false);
                    break;
                }
            }
            let replay = crate::ops::submit_replaceable(&resumed, continuation).await.unwrap();
            assert_eq!(replay["status"], "not_submitted");
            let requests = provider.requests.lock().unwrap();
            assert_eq!(requests.len(), 3);
            assert_eq!(requests[1]["input"].to_string().matches("approved lookup value").count(), 1);
            assert!(!requests[1]["input"].to_string().contains("manual recovery has exactly one late request"));
            let input = requests[2]["input"].to_string();
            assert_eq!(input.matches("manual recovery has exactly one late request").count(), 1);
            assert_eq!(input.matches("approved lookup value").count(), 1);
            assert!(input.contains("agents_steering"));
            drop(requests);
            resumed.close().await.unwrap();
            restored_engine.close().await.unwrap();
        }).await.expect("recovered manual late steering failed to settle");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn stopped_graph_rejects_autonomous_native_starts_until_explicit_submit() {
        use crate::engine::settlement_tests::{Provider, request};
        use codex_protocol::AgentPath;
        use codex_protocol::protocol::{InterAgentCommunication, Op};
        use codex_protocol::turn_input::StartIfIdleSubmission;
        use codex_protocol::user_input::UserInput;

        tokio::time::timeout(std::time::Duration::from_secs(25), async {
            let provider = Provider::start().await;
            let directory = tempfile::tempdir().unwrap();
            let engine = real_engine(&directory, &provider.url).await;
            for force in [false, true] {
                let native = engine.start_with_tools(Default::default(), None, Some(vec![]), false)
                    .await.unwrap();
                let state = native.current().unwrap();
                let tools = state.thread.thread_extension_data().get::<PythonTools>().unwrap();
                let original_count = provider.requests.lock().unwrap().len();
                // No child is required for either all-scope mode. Both a
                // direct start and a real Core mailbox wake bypass explicit
                // SDK submit, so neither is allowed to release the fence.
                tools.interrupt_graph(&engine.manager, &state.mutations, force).await.unwrap();
                for text in ["queued before stop", "late autonomous wake"] {
                    let started = state.thread.start_turn_if_idle(request(text)).await.unwrap();
                    let StartIfIdleSubmission::Started { turn_id } = started else {
                        panic!("native start did not enter the cancellable regular turn");
                    };
                    loop {
                        let event = state.next_event().await.unwrap();
                        if event["msg"]["type"] == "turn_aborted" {
                            assert_eq!(event["msg"]["turn_id"], turn_id);
                            break;
                        }
                    }
                    assert_eq!(provider.requests.lock().unwrap().len(), original_count);
                }
                state.thread.submit(Op::InterAgentCommunication {
                    communication: InterAgentCommunication::new(
                        AgentPath::try_from("/root/worker").unwrap(),
                        AgentPath::root(), Vec::new(), "late mail wake".into(), true,
                    ),
                    start_options: Default::default(),
                }).await.unwrap();
                loop {
                    let event = state.next_event().await.unwrap();
                    assert_ne!(event["msg"]["type"], "task_complete");
                    assert_ne!(event["msg"]["type"], "turn_complete");
                    if event["msg"]["type"] == "turn_aborted" {
                        break;
                    }
                }
                assert_eq!(provider.requests.lock().unwrap().len(), original_count);
                let submit = crate::ops::submit_replaceable(&state, json!({
                    "input": [UserInput::Text { text: "application resumes".into(), text_elements: vec![] }],
                })).await.unwrap();
                assert_eq!(submit["status"], "started");
                loop {
                    let event = state.next_event().await.unwrap();
                    if event["msg"]["type"] == "task_complete"
                        || event["msg"]["type"] == "turn_complete"
                    {
                        assert_eq!(event["msg"]["turn_id"], submit["turn_id"]);
                        break;
                    }
                }
                assert_eq!(provider.requests.lock().unwrap().len(), original_count + 1);
                state.close().await.unwrap();
            }
            engine.close().await.unwrap();
        }).await.expect("a fenced native start or explicit restart did not settle");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn graceful_graph_stops_at_boundary_despite_pending_mail_and_input() {
        use crate::engine::settlement_tests::{Provider, request};
        use codex_protocol::AgentPath;
        use codex_protocol::protocol::{InterAgentCommunication, Op};
        use codex_protocol::turn_input::{StartIfIdleSubmission, SteerSubmission};

        tokio::time::timeout(std::time::Duration::from_secs(25), async {
            let provider = Provider::start().await;
            let directory = tempfile::tempdir().unwrap();
            let engine = real_engine(&directory, &provider.url).await;
            let native = engine
                .start_with_tools(Default::default(), None, Some(vec![]), false)
                .await
                .unwrap();
            let state = native.current().unwrap();
            let store = state.thread.thread_extension_data();
            let tools = store.get::<PythonTools>().unwrap();
            let (decisions, mut observed) = mpsc::unbounded_channel();
            let hold = store.get_or_init(|| GracefulHold {
                reached: tokio::sync::Semaphore::new(0),
                release: tokio::sync::Semaphore::new(0),
                decisions,
            });
            let mut mutation = Some(state.mutations.lock().await);
            let init = state.thread.config_snapshot().await.turn_extension_init;
            let StartIfIdleSubmission::Started { turn_id } = crate::turn_settlement::start(
                &state,
                &mut mutation,
                request("accepted input"),
                init,
                None,
            )
            .await
            .unwrap() else {
                panic!("expected original graph turn");
            };
            drop(mutation);
            hold.reached.acquire().await.unwrap().forget();
            let mut stopping =
                Box::pin(tools.interrupt_graph(&engine.manager, &state.mutations, false));
            assert!(futures_util::poll!(&mut stopping).is_pending());
            hold.release.add_permits(1);
            // Do not stop before Core records the original accepted input.
            assert_eq!(observed.recv().await, Some(None));
            hold.reached.acquire().await.unwrap().forget();
            state
                .thread
                .submit(Op::InterAgentCommunication {
                    communication: InterAgentCommunication::new(
                        AgentPath::try_from("/root/worker").unwrap(),
                        AgentPath::root(),
                        Vec::new(),
                        "queued graph mail".into(),
                        true,
                    ),
                    start_options: Default::default(),
                })
                .await
                .unwrap();
            // This receipt from the same SQ proves the earlier mail has been
            // queued. Root-only graceful would defer while both are pending.
            let steer = state
                .thread
                .steer_turn(request("also queued"), turn_id)
                .await
                .unwrap();
            assert!(matches!(steer, SteerSubmission::Steered { .. }));
            hold.release.add_permits(1);
            stopping.await.unwrap();
            assert!(provider.requests.lock().unwrap().is_empty());
            state.close().await.unwrap();
            engine.close().await.unwrap();
        })
        .await
        .expect("all-scope graceful must settle at a safe boundary despite queued work");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn graceful_finish_never_settles_an_unmarked_same_id_replacement() {
        let directory = tempfile::tempdir().unwrap();
        // The real Core owner remains idle; no inference request is needed.
        // If finish sends even the conditional barrier for the unmarked later
        // receipt, its checked idle notification would incorrectly settle it.
        let engine = real_engine(&directory, "http://127.0.0.1:9/v1").await;
        let native = engine
            .start_with_tools(Default::default(), None, Some(vec![]), false)
            .await
            .unwrap();
        let state = native.current().unwrap();
        let thread_store = state.thread.thread_extension_data();
        let tools = thread_store.get::<PythonTools>().unwrap();
        start_admission(&thread_store, "same-id").await;
        let old = tools.admission_ready("same-id").unwrap();
        assert!(tools.request_graceful_interrupt("same-id").unwrap());
        start_admission(&thread_store, "same-id").await;
        let later = tools.admission_ready("same-id").unwrap();
        assert!(!Arc::ptr_eq(&old, &later));
        tools
            .finish_graceful_interrupt(&state.thread, "same-id")
            .await
            .unwrap();
        assert!(futures_util::poll!(Box::pin(later.wait())).is_pending());
        state.close().await.unwrap();
        engine.close().await.unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn queued_input_defers_graceful_to_next_boundary_on_the_same_receipt() {
        use crate::engine::settlement_tests::{Provider, request};
        use codex_protocol::turn_input::{StartIfIdleSubmission, SteerSubmission};

        let provider = Provider::start().await;
        let directory = tempfile::tempdir().unwrap();
        let engine = real_engine(&directory, &provider.url).await;
        let native = engine
            .start_with_tools(Default::default(), None, Some(vec![]), false)
            .await
            .unwrap();
        let state = native.current().unwrap();
        let store = state.thread.thread_extension_data();
        let tools = store.get::<PythonTools>().unwrap();
        let (decisions, mut observed) = mpsc::unbounded_channel();
        let hold = store.get_or_init(|| GracefulHold {
            reached: tokio::sync::Semaphore::new(0),
            release: tokio::sync::Semaphore::new(0),
            decisions,
        });
        let mut mutation = Some(state.mutations.lock().await);
        let init = state.thread.config_snapshot().await.turn_extension_init;
        let submission = crate::turn_settlement::start(
            &state,
            &mut mutation,
            request("first input"),
            init,
            None,
        )
        .await
        .unwrap();
        drop(mutation);
        let StartIfIdleSubmission::Started { turn_id } = submission else {
            panic!("managed turn was not started");
        };
        hold.reached.acquire().await.unwrap().forget();
        assert!(tools.request_graceful_interrupt(&turn_id).unwrap());
        let receipt = tools.admission_ready(&turn_id).unwrap();
        let steer = state
            .thread
            .steer_turn(request("preserve queued input"), turn_id.clone())
            .await
            .unwrap();
        assert!(matches!(steer, SteerSubmission::Steered { .. }));
        hold.release.add_permits(1);
        // Initial context precedes Core recording the accepted first input:
        // the SDK must not call the conditional interrupt yet.
        assert_eq!(observed.recv().await, Some(None));
        assert!(receipt.graceful_requested.load(Ordering::Acquire));
        assert!(tools.owns_admission(&receipt));
        hold.reached.acquire().await.unwrap().forget();
        // Core builds both initial context and sampling-step world state.
        // It keeps steering queued through both while first input is pending.
        assert!(provider.requests.lock().unwrap().is_empty());
        assert!(receipt.input_recorded.load(Ordering::Acquire));
        hold.release.add_permits(1);
        assert_eq!(observed.recv().await, Some(Some(false)));
        assert!(receipt.graceful_requested.load(Ordering::Acquire));
        assert!(tools.owns_admission(&receipt));
        hold.reached.acquire().await.unwrap().forget();
        assert_eq!(provider.requests.lock().unwrap().len(), 1);
        hold.release.add_permits(1);
        assert_eq!(observed.recv().await, Some(Some(true)));
        loop {
            let event = state.next_event().await.unwrap();
            if event["msg"]["type"] == "turn_aborted" {
                break;
            }
        }
        tools
            .finish_graceful_interrupt(&state.thread, &turn_id)
            .await
            .unwrap();
        receipt.wait().await.unwrap();
        assert_eq!(provider.requests.lock().unwrap().len(), 1);
        assert!(!receipt.graceful_requested.load(Ordering::Acquire));
        let canonical = state.thread.conversation_history_snapshot().await;
        let encoded = serde_json::to_string(&canonical.items().collect::<Vec<_>>()).unwrap();
        assert!(encoded.contains("preserve queued input"));
        state.close().await.unwrap();
        engine.close().await.unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn graceful_at_initial_context_preserves_accepted_input() {
        use crate::engine::settlement_tests::{Provider, request};
        use codex_protocol::turn_input::StartIfIdleSubmission;

        let provider = Provider::start().await;
        let directory = tempfile::tempdir().unwrap();
        let engine = real_engine(&directory, &provider.url).await;
        let native = engine
            .start_with_tools(Default::default(), None, Some(vec![]), false)
            .await
            .unwrap();
        let state = native.current().unwrap();
        let store = state.thread.thread_extension_data();
        let tools = store.get::<PythonTools>().unwrap();
        let (decisions, mut observed) = mpsc::unbounded_channel();
        let hold = store.get_or_init(|| GracefulHold {
            reached: tokio::sync::Semaphore::new(0),
            release: tokio::sync::Semaphore::new(0),
            decisions,
        });
        let mut mutation = Some(state.mutations.lock().await);
        let init = state.thread.config_snapshot().await.turn_extension_init;
        let submission = crate::turn_settlement::start(
            &state,
            &mut mutation,
            request("retained accepted original input"),
            init,
            None,
        )
        .await
        .unwrap();
        drop(mutation);
        let StartIfIdleSubmission::Started { turn_id } = submission else {
            panic!("managed turn was not started");
        };
        hold.reached.acquire().await.unwrap().forget();
        assert!(tools.request_graceful_interrupt(&turn_id).unwrap());
        hold.release.add_permits(1);
        assert_eq!(observed.recv().await, Some(None));
        hold.reached.acquire().await.unwrap().forget();
        assert!(provider.requests.lock().unwrap().is_empty());
        hold.release.add_permits(1);
        assert_eq!(observed.recv().await, Some(Some(true)));
        loop {
            let event = state.next_event().await.unwrap();
            if event["msg"]["type"] == "turn_aborted" {
                break;
            }
        }
        tools
            .finish_graceful_interrupt(&state.thread, &turn_id)
            .await
            .unwrap();
        let canonical = state.thread.conversation_history_snapshot().await;
        let encoded = serde_json::to_string(&canonical.items().collect::<Vec<_>>()).unwrap();
        state.close().await.unwrap();
        engine.close().await.unwrap();
        assert!(
            encoded.contains("retained accepted original input"),
            "accepted turn input lost from canonical Core history: {encoded}"
        );
    }

    #[tokio::test]
    async fn interrupt_acknowledgement_does_not_authorize_replaced_or_closed_receipts() {
        let thread = admission_thread();
        let tools = thread.get::<PythonTools>().unwrap();
        start_admission(&thread, "same-id").await;
        let original = tools.admission_ready("same-id").unwrap();
        let (sent, reply) = oneshot::channel();
        let mut checked = Box::pin(tools.acknowledge_session_interrupt(&original, reply));
        assert!(futures_util::poll!(&mut checked).is_pending());
        // A later Core task can have the same turn ID. It is not the same
        // enrolled receipt and an old queue acknowledgement cannot bless it.
        start_admission(&thread, "same-id").await;
        let next = tools.admission_ready("same-id").unwrap();
        sent.send(false).unwrap();
        assert!(checked.await.is_err());
        assert!(original.wait().await.is_err());
        assert!(futures_util::poll!(Box::pin(next.wait())).is_pending());

        let (sent, reply) = oneshot::channel();
        sent.send(true).unwrap();
        assert!(
            tools
                .acknowledge_session_interrupt(&next, reply)
                .await
                .is_err()
        );
        assert!(futures_util::poll!(Box::pin(next.wait())).is_pending());

        PythonContributor::default()
            .on_thread_stop(ThreadStopInput {
                session_store: &ExtensionData::new("session"),
                thread_store: &thread,
            })
            .await;
        let (sent, reply) = oneshot::channel();
        sent.send(false).unwrap();
        tools
            .acknowledge_session_interrupt(&next, reply)
            .await
            .unwrap();
        PythonContributor::default()
            .on_thread_idle(ThreadIdleInput {
                cause: codex_extension_api::ThreadIdleCause::Interrupted,
                session_store: &ExtensionData::new("session"),
                thread_store: &thread,
            })
            .await;
        assert!(next.wait().await.is_err());
    }

    #[tokio::test]
    async fn native_receipt_metadata_does_not_reopen_a_consumed_sender() {
        let tools = catalog(
            ToolPolicy::default(),
            DynamicToolSpec::Function(function("lookup")),
        );
        let model = Arc::new(codex_models_manager::model_info::model_info_from_slug(
            "gpt-5.5",
        ));
        let context = NativeMediaContext {
            inline_data_url_low_detail_supported: false,
            model_info: Arc::clone(&model),
            profile: codex_protocol::provider_reference_profile::ProviderImageReferencePolicy::QualifiedStandardResponses,
        };
        let source = ToolCallSource::CodeMode {
            cell_id: "1".into(),
            runtime_tool_call_id: "nested".into(),
        };
        let (key, _, _receive) = tools
            .enqueue(
                "root",
                &tools.snapshot(),
                0,
                "turn",
                "call",
                json!({}),
                &source,
                Some("gpt-5.5"),
                false,
                Some(context),
            )
            .unwrap();
        // The existing pending owner retains data without a live root-turn query.
        let captured = tools.native_media(&key).unwrap();
        assert!(Arc::ptr_eq(&captured.model_info, &model));
        let _sender = tools.take_reply("root", &key).unwrap().unwrap();
        assert!(tools.native_media(&key).is_none());
        assert!(tools.take_reply("root", &key).unwrap().is_none());
        // A detached metadata clone still carries no publication capability.
        assert_eq!(captured.model_info.slug, "gpt-5.5");
    }

    fn catalog(policy: ToolPolicy, spec: DynamicToolSpec) -> Arc<PythonTools> {
        let tools = Arc::new(PythonTools {
            current: RwLock::new(Arc::new(Generation {
                id: 0,
                entries: vec![],
                specs: vec![],
            })),
            generation_high_water: AtomicU64::new(0),
            profile_catalogs: RwLock::new(HashMap::new()),
            managed_profiles: AtomicBool::new(false),
            descendant_source: None,
            fixed: HashSet::new(),
            policy: Arc::new(policy),
            pending: Mutex::new(HashMap::new()),
            failed_turns: Mutex::default(),
            family: Arc::new(PythonFamily::default()),
            instance: None,
            suspended: AtomicBool::new(false),
            admission: Mutex::new(None),
            interception: OnceLock::new(),
            model_producer: Mutex::default(),
            prepared_requests: Mutex::default(),
            manual_restore: Mutex::new(None),
            manual_progress: watch::channel(0).0,
            selecting_steps: AtomicBool::new(false),
            selecting_children: AtomicBool::new(false),
            context_transform_enabled: false,
            before_compact_enabled: false,
            before_complete_enabled: false,
            tool_batch_completion_enabled: false,
            step_defaults: RwLock::new(None),
            preparation_replies: Mutex::new(Some(HashMap::new())),
            preparations_stopped: AtomicBool::new(false),
            before_result: Mutex::new(None),
        });
        *tools.current.write().unwrap() = Arc::new(tools.prepare(1, vec![spec]).unwrap());
        tools
    }

    fn function(name: &str) -> DynamicToolFunctionSpec {
        DynamicToolFunctionSpec {
            strict: false,
            output_schema: None,
            name: name.into(),
            description: "test tool".into(),
            input_schema: json!({"type":"object","properties":{}}),
            defer_loading: false,
        }
    }

    #[test]
    fn checkpoint_catalog_validation_preserves_qualified_function_identity() {
        let qualified = |namespace: &str| {
            DynamicToolSpec::Namespace(DynamicToolNamespaceSpec {
                name: namespace.into(),
                description: "Qualified callback".into(),
                tools: vec![DynamicToolNamespaceTool::Function(function("lookup"))],
            })
        };
        let specs = vec![
            DynamicToolSpec::Function(function("lookup")),
            qualified("billing"),
            qualified("audit"),
        ];
        validate_declarations(&specs).unwrap();
        assert_eq!(tool_names(&specs).len(), 3);
        let saved = checkpoint_specs(&specs).unwrap();
        let restored: Vec<DynamicToolSpec> = serde_json::from_value(saved.clone()).unwrap();
        validate_declarations(&restored).unwrap();
        assert_eq!(checkpoint_specs(&restored).unwrap(), saved);
        for duplicate in [
            vec![qualified("billing"), qualified("billing")],
            vec![
                DynamicToolSpec::Function(function("lookup")),
                qualified("functions"),
            ],
        ] {
            assert!(
                validate_declarations(&duplicate)
                    .unwrap_err()
                    .to_string()
                    .contains("Duplicate Python tool")
            );
        }
    }

    #[test]
    fn checkpoint_catalog_validation_rejects_non_slot_namespace_shapes() {
        for tools in [
            vec![],
            vec![
                DynamicToolNamespaceTool::Function(function("lookup")),
                DynamicToolNamespaceTool::Function(function("record")),
            ],
        ] {
            let malformed = DynamicToolSpec::Namespace(DynamicToolNamespaceSpec {
                name: "billing".into(),
                description: "Malformed slot".into(),
                tools,
            });
            assert!(
                validate_declarations(&[malformed])
                    .unwrap_err()
                    .to_string()
                    .contains("invalid Python tool slot projection")
            );
        }
    }

    fn sdk() -> Arc<PythonTools> {
        catalog(
            ToolPolicy {
                allowed_tools: Some(vec![sdk_name()]),
                ..ToolPolicy::default()
            },
            DynamicToolSpec::Namespace(DynamicToolNamespaceSpec {
                name: "_agents_sdk".into(),
                description: "SDK managed call".into(),
                tools: vec![DynamicToolNamespaceTool::Function(function("dispatch"))],
            }),
        )
    }

    pub(super) fn call(call_id: &str, original_name: &str, original_args: &str) -> ResponseItem {
        ResponseItem::FunctionCall {
            id: None,
            namespace: Some("_agents_sdk".into()),
            name: "dispatch".into(),
            call_id: call_id.into(),
            arguments: json!({"original": {
                "type": "function_call", "name": original_name,
                "arguments": original_args, "call_id": call_id,
            }})
            .to_string(),
            encrypted_function_args: None,
            internal_chat_message_metadata_passthrough: None,
            asynchronous: None,
            status: None,
        }
    }

    fn executor(tools: Arc<PythonTools>) -> PythonExecutor {
        PythonExecutor {
            managed_calls: None,
            profile: None,
            accepted: None,
            thread: "owner".into(),
            generation: tools.snapshot(),
            tools,
            slot: 0,
            use_deferred_specs: false,
            code_mode_enabled: false,
        }
    }

    #[tokio::test]
    async fn only_enqueued_calls_filter_core_outputs() {
        for rejected in [true, false] {
            let tools = catalog(
                ToolPolicy::default(),
                DynamicToolSpec::Function(function("lookup")),
            );
            if rejected {
                tools.family.receiver.lock().await.close();
            }
            let thread = codex_protocol::ThreadId::new();
            let managed = Arc::new(crate::storage::ManagedCalls::default());
            let mut executor = executor(Arc::clone(&tools));
            executor.thread = thread.to_string();
            executor.managed_calls = Some(Arc::clone(&managed));
            let key = PendingKey {
                reply: ReplyKey {
                    thread: thread.to_string(),
                    generation: executor.generation.id,
                    slot: 0,
                    instance: None,
                },
                turn: "turn".into(),
                call: "call".into(),
            };
            let mut handled = executor.handle(ToolCall {
                turn_id: key.turn.clone(),
                call_id: key.call.clone(),
                tool_name: ToolName::new(None, "lookup"),
                model: "test-model".into(),
                model_info: Arc::new(codex_models_manager::model_info::model_info_from_slug("test-model")),
                inline_data_url_low_detail_supported: false,
                provider_reference_profile: codex_protocol::provider_reference_profile::ProviderImageReferencePolicy::PrepareInline,
                codex_turn_metadata: None,
                truncation_policy: codex_protocol::protocol::TruncationPolicy::Bytes(1000),
                source: ToolCallSource::Direct,
                conversation_history: Default::default(),
                turn_item_emitter: Arc::new(codex_tools::NoopTurnItemEmitter),
                environments: vec![],
                payload: ToolPayload::Function {
                    arguments: "{}".into(),
                },
            });
            if rejected {
                assert!(matches!(
                    handled.await,
                    Err(FunctionCallError::RespondToModel(message))
                    if message == "Python tool session closed"
                ));
                assert!(tools.pending.lock().unwrap().is_empty());
            } else {
                assert!(futures_util::poll!(&mut handled).is_pending());
                assert_eq!(tools.next_request().await["msg"]["callId"], "call");
            }
            let output = PythonOutput(DynamicToolResponse {
                content_items: vec![],
                success: false,
            })
            .receipt(&key);
            assert_eq!(
                managed.owns_output(
                    thread,
                    &codex_history::RolloutItem::ResponseItem(output.into())
                ),
                !rejected,
                "only application-admitted calls may suppress a Core error receipt"
            );
        }
    }

    #[tokio::test]
    async fn family_inbox_routes_identical_calls_before_children_are_observed() {
        let root = catalog(
            ToolPolicy::default(),
            DynamicToolSpec::Function(function("lookup")),
        );
        let first_thread = ExtensionData::new("first");
        let first = first_thread.get_or_init(|| PythonTools::descendant(&root, "runtime-a".into()));
        let second_thread = ExtensionData::new("second");
        let second =
            second_thread.get_or_init(|| PythonTools::descendant(&root, "runtime-b".into()));
        let _first_turn = start_admission(&first_thread, "turn").await;
        let _second_turn = start_admission(&second_thread, "turn").await;
        root.family.members.write().unwrap().extend([
            ("first".into(), Arc::downgrade(&first)),
            ("second".into(), Arc::downgrade(&second)),
        ]);
        let mut replies = Vec::new();
        for (id, tools) in [("root", &root), ("first", &first), ("second", &second)] {
            let (key, _, receiver) = tools
                .enqueue(
                    id,
                    &tools.snapshot(),
                    0,
                    "turn",
                    "same",
                    json!({"issuer": id}),
                    &ToolCallSource::Direct,
                    None,
                    false,
                    None,
                )
                .unwrap();
            replies.push((id, key, receiver));
        }
        for (id, key, receiver) in replies {
            let request = root.next_request().await;
            assert_eq!(request["msg"]["arguments"]["issuer"], id);
            assert!(request["msg"]["_sdk_model"].is_null());
            let issuer = root.owner("root", &key).unwrap();
            issuer.check_pending(id, &key).unwrap();
            assert!(!Arc::ptr_eq(&issuer, &second) || id == "second");
            let sender = issuer.take_reply(id, &key).unwrap().unwrap();
            drop(sender);
            assert!(receiver.await.is_err());
        }
    }

    #[tokio::test]
    async fn stopped_or_reopened_child_cannot_accept_an_old_family_reply() {
        let root = catalog(
            ToolPolicy::default(),
            DynamicToolSpec::Function(function("lookup")),
        );
        let thread = ExtensionData::new("child");
        let old = thread.get_or_init(|| PythonTools::descendant(&root, "old-runtime".into()));
        let _old_turn = start_admission(&thread, "turn").await;
        root.family
            .members
            .write()
            .unwrap()
            .insert("child".into(), Arc::downgrade(&old));
        let (key, _, reply) = old
            .enqueue(
                "child",
                &old.snapshot(),
                0,
                "turn",
                "same",
                json!({}),
                &ToolCallSource::Direct,
                None,
                false,
                None,
            )
            .unwrap();
        assert!(root.owner("root", &key).is_some());
        let session = ExtensionData::new("session");
        PythonContributor::default()
            .on_thread_stop(ThreadStopInput {
                session_store: &session,
                thread_store: &thread,
            })
            .await;
        assert!(reply.await.is_err());
        let replacement_thread = ExtensionData::new("child");
        let replacement =
            replacement_thread.get_or_init(|| PythonTools::descendant(&root, "new-runtime".into()));
        let _replacement_turn = start_admission(&replacement_thread, "turn").await;
        root.family
            .members
            .write()
            .unwrap()
            .insert(thread.level_id().into(), Arc::downgrade(&replacement));
        let (current, _, _receiver) = replacement
            .enqueue(
                "child",
                &replacement.snapshot(),
                0,
                "turn",
                "same",
                json!({}),
                &ToolCallSource::Direct,
                None,
                false,
                None,
            )
            .unwrap();
        assert!(root.owner("root", &key).is_none());
        assert!(replacement.take_reply("child", &key).unwrap().is_none());
        assert!(root.owner("root", &current).is_some());
        // A late reader cannot upgrade the original and the family lookup now
        // resolves its replacement. Retirement must still identify the issuer.
        drop(old);
        drop(thread);
        assert_eq!(
            root.next_request().await,
            json!({
                "id": "", "subagent_id": "child",
                "subagent_incarnation": "old-runtime",
                "msg": {"type": "shutdown_complete"}
            }),
        );
        let request = root.next_request().await;
        assert_eq!(
            request["msg"]["_sdk_python_tool"]["instance"],
            "new-runtime"
        );
        let unrelated = catalog(
            ToolPolicy::default(),
            DynamicToolSpec::Function(function("lookup")),
        );
        assert!(futures_util::poll!(Box::pin(unrelated.next_request())).is_pending());
    }

    #[tokio::test]
    async fn manual_grant_is_exact_owner_single_use_and_old_drop_cannot_remove_successor() {
        let tools = catalog(
            ToolPolicy::default(),
            DynamicToolSpec::Function(function("lookup")),
        );
        let mut generation = tools
            .prepare(2, vec![DynamicToolSpec::Function(function("lookup"))])
            .unwrap();
        generation.set_manual(&[0]).unwrap();
        assert_eq!(tools.allocate_generation().unwrap(), generation.id);
        tools.publish(generation).unwrap();
        assert!(executor(tools.clone()).supports_parallel_tool_calls());
        let generation = tools.snapshot();
        let (key, invocation, mut result) = tools
            .enqueue(
                "owner",
                &generation,
                0,
                "turn",
                "same",
                json!({}),
                &ToolCallSource::Direct,
                None,
                false,
                None,
            )
            .unwrap();
        let old = PendingReply {
            completed: false,
            tools: &tools,
            key: key.clone(),
            invocation,
        };
        let (_, grant, raw) = tools.manual_snapshot("owner").unwrap().pop().unwrap();
        let manual: Value = serde_json::from_str(&raw).unwrap();
        assert!(
            !manual["msg"]
                .as_object()
                .unwrap()
                .contains_key("_sdk_trace")
        );
        let foreign = catalog(
            ToolPolicy::default(),
            DynamicToolSpec::Function(function("lookup")),
        );
        assert!(!foreign.check_manual("owner", &grant));
        assert!(!tools.check_manual("other", &grant));
        assert!(tools.check_manual("owner", &grant));
        assert!(tools.take_reply("owner", &key).is_err());
        let response = DynamicToolResponse {
            content_items: vec![],
            success: true,
        };
        assert!(tools.respond_manual("owner", &grant, response.clone(), true));
        assert!(result.try_recv().is_ok());
        assert!(!tools.respond_manual("owner", &grant, response, true));
        let (_, _, next) = tools
            .enqueue(
                "owner",
                &generation,
                0,
                "turn",
                "same",
                json!({}),
                &ToolCallSource::Direct,
                None,
                false,
                None,
            )
            .unwrap();
        drop(old);
        assert_eq!(tools.manual_snapshot("owner").unwrap().len(), 1);
        assert!(!tools.check_manual("owner", &grant));
        drop(next);
        assert!(tools.manual_snapshot("owner").unwrap().is_empty());
        // The queued predecessor cannot impersonate the live invocation.
        let (_, _, _live) = tools
            .enqueue(
                "owner",
                &generation,
                0,
                "turn",
                "other",
                json!({}),
                &ToolCallSource::Direct,
                None,
                false,
                None,
            )
            .unwrap();
        assert_eq!(tools.next_request().await["msg"]["callId"], "other");
    }

    #[test]
    fn python_callbacks_run_parallel_without_enabling_native_builtins() {
        let sdk = sdk();
        assert!(executor(sdk.clone()).supports_parallel_tool_calls());
        for name in [
            "shell_command",
            "exec_command",
            "dispatch",
            "apply_patch",
            "write_stdin",
        ] {
            assert!(!sdk.policy.allows(&ToolName::plain(name)));
            assert!(!sdk.policy.allows(&ToolName::namespaced("functions", name)));
        }
        assert!(
            sdk.prepare(
                2,
                vec![DynamicToolSpec::Function(function("shell_command"))]
            )
            .is_err()
        );
        let lower = catalog(
            ToolPolicy::default(),
            DynamicToolSpec::Function(function("lookup")),
        );
        assert!(executor(lower.clone()).supports_parallel_tool_calls());
        assert!(
            lower
                .prepare(
                    2,
                    vec![DynamicToolSpec::Function(function("shell_command"))]
                )
                .is_err()
        );
    }

    #[tokio::test]
    async fn sdk_payload_keeps_bare_builtin_name_and_invalid_json_as_data() {
        let tools = sdk();
        let item = call("c1", "shell_command", "{invalid");
        tools
            .validate_reentry(&[item.clone()], &HashMap::new())
            .unwrap();
        let (key, mut result) = tools.enqueue_reentry("owner", "turn", &item).unwrap();
        let msg = tools.next_request().await;
        assert_eq!(msg["msg"]["namespace"], "_agents_sdk");
        assert_eq!(msg["msg"]["tool"], "dispatch");
        assert_eq!(msg["msg"]["arguments"]["original"]["arguments"], "{invalid");
        assert_eq!(msg["msg"]["arguments"]["original"]["name"], "shell_command");
        tools.park("owner", &key).unwrap();
        assert!(matches!(
            result.try_recv(),
            Err(oneshot::error::TryRecvError::Empty)
        ));
        assert!(tools.take_reply("owner", &key).is_err());
        tools.suspend_reentry(&[key]).unwrap();
        assert!(
            result.await.is_err(),
            "suspension must not manufacture a result"
        );
    }

    #[tokio::test]
    async fn entering_expired_or_currently_parked_calls_cannot_claim_not_started() {
        let tools = sdk();
        let (key, _receiver) = tools
            .enqueue_reentry("owner", "turn", &call("one", "f", "{}"))
            .unwrap();
        let request = tools.next_request().await;
        let invocation = request["msg"]["_sdk_invocation"].as_u64().unwrap();
        assert!(
            tools
                .admit_callback("other-owner", &key, invocation, false)
                .is_err()
        );
        assert!(
            tools
                .admit_callback("owner", &key, invocation + 1, false)
                .is_err()
        );
        assert!(
            tools
                .admit_callback("owner", &key, invocation, true)
                .unwrap()
        );
        assert!(tools.park("owner", &key).is_err());
        assert!(tools.park_continuation("owner", &key, json!(true)).is_err());
        assert!(
            tools
                .park_continuation("owner", &key, json!({"calls":["child1"],"receipts":[]}))
                .unwrap()
        );
        assert!(
            tools
                .admit_callback("owner", &key, invocation, true)
                .is_err()
        );
        let keys = tools.parked("owner").unwrap();
        assert_eq!(
            tools.continuations(&keys).unwrap()["one"]["calls"],
            json!(["child1"])
        );
        tools.suspend_reentry(&keys).unwrap();
        assert!(!tools.park("owner", &key).unwrap());
        assert!(
            !tools
                .admit_callback("owner", &key, invocation, false)
                .unwrap()
        );
        assert!(tools.check_pending("owner", &key).is_err());
    }

    #[tokio::test]
    async fn completed_sibling_settles_once_before_approval_and_keeps_its_actual_output() {
        let tools = sdk();
        let (parked, mut a) = tools
            .enqueue_reentry("owner", "turn", &call("first", "f", "{}"))
            .unwrap();
        let (finished, b) = tools
            .enqueue_reentry("owner", "turn", &call("second", "g", "{}"))
            .unwrap();
        let parked_request = tools.next_request().await;
        let finished_request = tools.next_request().await;
        assert_eq!(parked_request["msg"]["callId"], parked.call);
        assert_eq!(finished_request["msg"]["callId"], finished.call);
        let finished_invocation = finished_request["msg"]["_sdk_invocation"].as_u64().unwrap();
        tools.park("owner", &parked).unwrap();
        assert!(
            tools.parked("owner").is_err(),
            "do not seal a still admitted sibling"
        );
        assert!(
            tools
                .admit_callback("owner", &finished, finished_invocation, true)
                .unwrap()
        );
        assert!(
            tools.parked("owner").is_err(),
            "do not seal an entered sibling"
        );
        let actual = DynamicToolResponse {
            content_items: vec![
                codex_protocol::dynamic_tools::DynamicToolCallOutputContentItem::InputText {
                    text: "committed receipt".into(),
                },
            ],
            success: true,
        };
        tools
            .take_reply("owner", &finished)
            .unwrap()
            .unwrap()
            .send(actual.into())
            .unwrap();
        assert!(tools.take_reply("owner", &finished).unwrap().is_none());
        assert_eq!(
            PythonOutput(b.await.unwrap().response).log_output(),
            "committed receipt"
        );
        assert!(matches!(
            a.try_recv(),
            Err(oneshot::error::TryRecvError::Empty)
        ));
        assert_eq!(tools.parked("owner").unwrap(), vec![parked]);
    }

    #[tokio::test]
    async fn reentry_cancel_fences_only_current_not_entered_and_receipted_siblings() {
        let tools = sdk();
        let (admitted, mut a) = tools
            .enqueue_reentry("owner", "turn", &call("first", "f", "{}"))
            .unwrap();
        let (entered, b) = tools
            .enqueue_reentry("owner", "turn", &call("second", "g", "{}"))
            .unwrap();
        let admitted_request = tools.next_request().await;
        let entered_request = tools.next_request().await;
        assert_eq!(admitted_request["msg"]["callId"], admitted.call);
        assert_eq!(entered_request["msg"]["callId"], entered.call);
        let admitted_invocation = admitted_request["msg"]["_sdk_invocation"].as_u64().unwrap();
        let entered_invocation = entered_request["msg"]["_sdk_invocation"].as_u64().unwrap();
        let keys = vec![admitted.clone(), entered.clone()];
        assert!(
            tools
                .admit_callback("owner", &entered, entered_invocation, true)
                .unwrap()
        );
        // There is no receipt for the entered effect. Cancellation may not
        // make either that work or this whole unresolved batch resumable.
        assert!(tools.park_cancelled_reentry("owner", &keys).is_empty());
        assert!(tools.parked("owner").is_err());
        assert!(tools.park("owner", &entered).is_err());
        let actual = DynamicToolResponse {
            content_items: vec![
                codex_protocol::dynamic_tools::DynamicToolCallOutputContentItem::InputText {
                    text: "actual receipt".into(),
                },
            ],
            success: true,
        };
        // The owning binding flushes this actual reply under the mutation
        // lock before cancellation takes its snapshot.
        tools
            .take_reply("owner", &entered)
            .unwrap()
            .unwrap()
            .send(actual.into())
            .unwrap();
        assert_eq!(
            PythonOutput(b.await.unwrap().response).log_output(),
            "actual receipt"
        );
        assert!(
            tools
                .park_cancelled_reentry("another-owner", &keys)
                .is_empty()
        );
        assert_eq!(
            tools.park_cancelled_reentry("owner", &keys),
            vec![admitted.clone()]
        );
        assert!(
            tools
                .admit_callback("owner", &admitted, admitted_invocation, true)
                .is_err()
        );
        assert!(matches!(
            a.try_recv(),
            Err(oneshot::error::TryRecvError::Empty)
        ));
        tools.cancel_reentry(&keys);
        assert!(a.await.is_err(), "cancellation must not invent an output");
    }

    #[test]
    fn validate_all_reentry_data_before_any_callback_or_seal_consumption() {
        let tools = sdk();
        let good = call("c", "exec_command", "{");
        let mut native = good.clone();
        if let ResponseItem::FunctionCall {
            name, namespace, ..
        } = &mut native
        {
            *name = "exec_command".into();
            *namespace = None;
        }
        assert!(
            tools
                .validate_reentry(&[good.clone(), good.clone()], &HashMap::new())
                .is_err()
        );
        assert!(tools.validate_reentry(&[native], &HashMap::new()).is_err());
        assert!(
            tools
                .validate_reentry(&[good], &HashMap::from([("other".into(), json!({"c":1}))]))
                .is_err()
        );
        assert!(tools.pending.lock().unwrap().is_empty());
    }
    #[test]
    fn restored_defaults_narrow_advertisement_without_discarding_registered_declarations() {
        let specs = vec![
            DynamicToolSpec::Function(function("a")),
            DynamicToolSpec::Function(function("b")),
        ];
        let tools = catalog(ToolPolicy::default(), specs[0].clone());
        *tools.current.write().unwrap() = Arc::new(tools.prepare(1, specs.clone()).unwrap());
        tools.generation_high_water.store(1, Ordering::Release);
        let narrowed = tools
            .startup_profile_catalog([false; 5], Some(&[1]))
            .unwrap();
        assert_eq!(
            narrowed.checkpoint_specs().unwrap(),
            checkpoint_specs(&specs).unwrap()
        );
        assert!(narrowed.default_names(Some(&[2])).is_err());
        assert!(narrowed.default_names(Some(&[0, 0])).is_err());
        let declared = tools
            .profile_catalog(specs, &[], &[], &[], None, [false; 5], false)
            .unwrap();
        let thread = ExtensionData::new("source");
        thread.insert(Arc::try_unwrap(tools).ok().expect("exclusive fixture"));
        let tools = thread.get::<PythonTools>().unwrap();
        let session = ExtensionData::new("session");
        let advertise = |catalog: ProfileCatalog| {
            let mut retained = codex_extension_api::ExtensionDataInit::new();
            retained.insert(catalog);
            let captured = PythonContributor::default()
                .capture_realtime_tools(codex_extension_api::RealtimeToolCaptureInput {
                    session_store: &session,
                    thread_store: &thread,
                    profile: None,
                    mcp: None,
                    retained_execution_data: Some(&retained),
                })
                .unwrap()
                .unwrap();
            captured
                .tools
                .into_iter()
                .map(|tool| tool.native_name.name)
                .collect::<Vec<_>>()
        };
        assert_eq!(advertise(narrowed), vec!["b"]);
        let cleared = tools
            .startup_profile_catalog([false; 5], Some(&[]))
            .unwrap();
        assert!(advertise(cleared).is_empty());
        // A later declaration activation retains its own complete defaults.
        assert_eq!(advertise(declared), vec!["a", "b"]);
    }

    #[test]
    fn mono_descendants_freeze_result_selection_before_first_tool_catalog() {
        for selected in [false, true] {
            let parent = catalog(
                ToolPolicy::default(),
                DynamicToolSpec::Function(function("a")),
            );
            let source = ExtensionData::new("parent-step");
            source.insert(crate::native_interception::ProfileMcpSource {
                result_selection: selected.then(|| {
                    Arc::new(
                        serde_json::from_value::<crate::mcp_result_control::Selection>(
                            json!({"generation":7,"servers":{"vault":{"record":0.25}}}),
                        )
                        .unwrap(),
                    )
                }),
                receipt: None,
                policy: Some(Arc::new(
                    crate::native_interception::ProfileMcpPolicy::default(),
                )),
            });
            let child =
                PythonTools::descendant_from_step(&parent, "child".into(), Some(&source)).unwrap();
            let child_step = ExtensionData::new("child-first-step");
            // source_profile calls generation_for_step before tools_for_step/world state.
            assert!(child.source_profile(Some(&child_step)).unwrap().is_none());
            let nested =
                PythonTools::descendant_from_step(&child, "nested".into(), Some(&child_step))
                    .unwrap();
            let nested_step = ExtensionData::new("nested-first-step");
            assert!(nested.source_profile(Some(&nested_step)).unwrap().is_none());
            for step in [&child_step, &nested_step] {
                let captured = step
                    .get::<crate::native_interception::ProfileMcpSource>()
                    .unwrap();
                assert_eq!(
                    captured
                        .result_selection
                        .as_ref()
                        .map(|selection| selection.generation),
                    selected.then_some(7)
                );
            }
        }
    }

    #[test]
    fn managed_descendants_keep_source_catalog_profile_and_mcp_policy_after_root_replacement() {
        let parent = catalog(
            ToolPolicy::default(),
            DynamicToolSpec::Function(function("a")),
        );
        let a = parent
            .profile_catalog(
                vec![DynamicToolSpec::Function(function("a"))],
                &[],
                &[],
                &[],
                None,
                [false; 5],
                true,
            )
            .unwrap();
        let b = parent
            .profile_catalog(
                vec![DynamicToolSpec::Function(function("b"))],
                &[],
                &[],
                &[],
                None,
                [false; 5],
                false,
            )
            .unwrap();
        let source = ExtensionData::new("a-source");
        source.insert(a.clone());
        let receipt = codex_extension_api::AgentProfileReceipt {
            profile_id: "a".into(),
            epoch: 0,
        };
        source.insert(DescendantProfileSource(receipt.clone()));
        source.insert(
            serde_json::from_value::<crate::mcp_result_control::Selection>(
                json!({"generation":a.generation.id,"servers":{}}),
            )
            .unwrap(),
        );
        source.insert(StepCatalog {
            accepted: Default::default(),
            generation: a.generation.clone(),
            use_deferred_specs: false,
            slots: vec![0],
        });
        *parent.current.write().unwrap() = b.generation.clone();
        let child =
            PythonTools::descendant_from_step(&parent, "child-a".into(), Some(&source)).unwrap();
        assert!(child.pending.lock().unwrap().is_empty());
        assert!(child.admission.lock().unwrap().is_none());
        assert_eq!(child.instance.as_deref(), Some("child-a"));
        let child_step = ExtensionData::new("child-step");
        assert!(Arc::ptr_eq(
            &child.generation_for_step(Some(&child_step)).unwrap(),
            &a.generation
        ));
        assert_eq!(
            child.source_profile(Some(&child_step)).unwrap(),
            Some(receipt.clone())
        );
        assert_eq!(
            child_step
                .get::<crate::mcp_result_control::Selection>()
                .unwrap()
                .generation,
            a.generation.id
        );
        let nested =
            PythonTools::descendant_from_step(&parent, "nested-a".into(), Some(&child_step))
                .unwrap();
        let nested_step = ExtensionData::new("nested-step");
        assert!(Arc::ptr_eq(
            &nested.generation_for_step(Some(&nested_step)).unwrap(),
            &a.generation
        ));
        assert_eq!(
            nested.source_profile(Some(&nested_step)).unwrap(),
            Some(receipt)
        );
        assert!(Arc::ptr_eq(&parent.snapshot(), &b.generation));
        assert!(nested.pending.lock().unwrap().is_empty());
        assert!(nested.admission.lock().unwrap().is_none());
        assert!(
            PythonTools::descendant_from_step(&parent, "restored-without-source".into(), None)
                .is_err()
        );
    }

    #[test]
    fn managed_descendant_rejects_missing_captured_mcp_result_policy() {
        let parent = catalog(
            ToolPolicy::default(),
            DynamicToolSpec::Function(function("a")),
        );
        let a = parent
            .profile_catalog(
                vec![DynamicToolSpec::Function(function("a"))],
                &[],
                &[],
                &[],
                None,
                [false; 5],
                true,
            )
            .unwrap();
        let source = ExtensionData::new("missing-policy");
        source.insert(a);
        source.insert(DescendantProfileSource(
            codex_extension_api::AgentProfileReceipt {
                profile_id: "a".into(),
                epoch: 0,
            },
        ));
        assert!(PythonTools::descendant_from_step(&parent, "child".into(), Some(&source)).is_err());
    }

    #[test]
    fn realtime_mono_capture_selects_original_catalog_after_root_replacement_without_managed_mode()
    {
        let thread = ExtensionData::new("realtime-root");
        let owned = Arc::try_unwrap(catalog(
            ToolPolicy::default(),
            DynamicToolSpec::Function(function("a")),
        ))
        .ok()
        .expect("exclusive root fixture");
        thread.insert(owned);
        let tools = thread.get::<PythonTools>().unwrap();
        let session = ExtensionData::new("session");
        let contributor = PythonContributor::default();
        let mut captured = contributor
            .capture_realtime_tools(codex_extension_api::RealtimeToolCaptureInput {
                session_store: &session,
                thread_store: &thread,
                profile: None,
                mcp: None,
                retained_execution_data: None,
            })
            .unwrap()
            .unwrap();
        assert_eq!(
            captured
                .tools
                .iter()
                .map(|declaration| declaration.native_name.name.as_str())
                .collect::<Vec<_>>(),
            vec!["a"]
        );
        assert!(tools.admission.lock().unwrap().is_none());
        assert!(tools.pending.lock().unwrap().is_empty());
        // A retained accepted snapshot must survive the second binding projection,
        // even when the current thread cache has no such publication.
        let accepted = crate::application_instructions::Accepted {
            id: codex_protocol::ResponseItemId::with_suffix("msg_sdk_application", "source-a"),
            text: "Source A instructions".into(),
            files: Vec::new(),
        };
        captured.execution_data.insert(accepted.clone());
        captured
            .execution_data
            .insert(codex_extension_api::AgentProfileInstructions {
                text: Some(accepted.text.clone()),
            });
        let a = tools.snapshot();
        tools
            .publish(
                tools
                    .prepare(a.id + 1, vec![DynamicToolSpec::Function(function("b"))])
                    .unwrap(),
            )
            .unwrap();
        let projected = contributor
            .capture_realtime_tools(codex_extension_api::RealtimeToolCaptureInput {
                session_store: &session,
                thread_store: &thread,
                profile: None,
                mcp: None,
                retained_execution_data: Some(&captured.execution_data),
            })
            .unwrap()
            .unwrap();
        assert_eq!(projected.selection_id, captured.selection_id);
        assert_eq!(
            projected.application_instructions.as_deref(),
            Some("Source A instructions")
        );
        assert_eq!(projected.tools[0].native_name.name, "a");
        assert_eq!(
            projected
                .execution_data
                .get::<crate::application_instructions::Accepted>()
                .unwrap()
                .id,
            accepted.id
        );
        let step = ExtensionData::new_with_init("new-actual-a-step", projected.execution_data);
        let executors = contributor.tools_for_step(&session, &thread, &step);
        assert_eq!(
            executors
                .iter()
                .map(|executor| executor.tool_name().name)
                .collect::<Vec<_>>(),
            vec!["a"]
        );
        assert!(Arc::ptr_eq(
            &tools.generation_for_step(Some(&step)).unwrap(),
            &a
        ));
        assert_eq!(tools.source_profile(Some(&step)).unwrap(), None);
        assert!(!tools.managed_profiles.load(Ordering::Acquire));
        assert_eq!(tools.snapshot().entries[0].name.name, "b");
    }

    #[test]
    fn realtime_capture_prunes_expired_generation_entries() {
        let thread = ExtensionData::new("bounded-realtime");
        thread.insert(
            Arc::try_unwrap(catalog(
                ToolPolicy::default(),
                DynamicToolSpec::Function(function("a")),
            ))
            .ok()
            .expect("exclusive fixture"),
        );
        let tools = thread.get::<PythonTools>().unwrap();
        let session = ExtensionData::new("session");
        for _ in 0..20 {
            let generation = tools.snapshot().id;
            let captured = PythonContributor::default()
                .capture_realtime_tools(codex_extension_api::RealtimeToolCaptureInput {
                    session_store: &session,
                    thread_store: &thread,
                    profile: None,
                    mcp: None,
                    retained_execution_data: None,
                })
                .unwrap()
                .unwrap();
            assert_eq!(tools.profile_catalogs.read().unwrap().len(), 1);
            drop(captured);
            tools
                .publish(
                    tools
                        .prepare(
                            generation + 1,
                            vec![DynamicToolSpec::Function(function("next"))],
                        )
                        .unwrap(),
                )
                .unwrap();
            assert!(tools.resolve_generation(generation).is_none());
        }
        assert!(!tools.managed_profiles.load(Ordering::Acquire));
    }

    #[test]
    fn background_disposition_comes_only_from_trusted_reply_envelope() {
        let response = DynamicToolResponse {
            content_items: vec![
                codex_protocol::dynamic_tools::DynamicToolCallOutputContentItem::InputText {
                    text: "{\"start_response\":false}".into(),
                },
            ],
            success: true,
        };
        let ordinary = ToolReply::from(response.clone());
        assert!(ordinary.start_response);
        let plain = PythonOutput(response.clone());
        assert!(plain.realtime_response_requested());
        let background = ReplyOutput {
            output: PythonOutput(response),
            start_response: false,
            acceptance: None,
        };
        assert!(!background.realtime_response_requested());
        assert_eq!(
            plain.to_response_item(
                "call",
                &ToolPayload::Function {
                    arguments: "{}".into()
                }
            ),
            background.to_response_item(
                "call",
                &ToolPayload::Function {
                    arguments: "{}".into()
                }
            )
        );
    }
}

// Keep SDK-owned producers, native observations and replies available; never
// permit an untracked producer to race the session's idle gate.
pub fn check_raw_operation(operation: &str) -> Result<()> {
    ensure!(
        matches!(
            operation,
            "exec_approval"
                | "patch_approval"
                | "resolve_elicitation"
                | "cancel_url_completion"
                | "user_input_answer"
                | "request_permissions_response"
                | "dynamic_tool_response"
                | "token_usage_info"
                | "load_history"
                | "read_thread"
                | "list_subagents"
                | "agent_status"
                | "thread_settings_snapshot"
                | "list_background_terminals"
                | "read_mcp_resource"
                | "list_mcp_resources"
                | "list_mcp_prompts"
                | "get_mcp_prompt"
                | "list_mcp_resource_templates"
                | "read_mcp_resource_for_call"
                | "environment_ready"
                | "environment_failed"
                | "connect_environment"
                | "environment_attached"
                | "current_turn_model"
                | "active_turn_root"
                | "approvals_reviewer_for_turn"
                | "rollout_path"
                | "instruction_sources"
                | "queued_event_count"
                | "inject_if_running"
                | "interrupt"
                | "shutdown"
                | "wait_until_terminated"
                | "set_instructions"
                | "realtime_tool_selection_sdk"
                | "realtime_conversation_config_sdk"
                | "realtime_conversation_update_history_sdk"
                | "realtime_conversation_send_event_sdk"
                | "realtime_conversation_retrieve_item_sdk"
                | "realtime_conversation_retrieve_history_sdk"
                | "realtime_conversation_prepare_sdk"
                | "realtime_conversation_activate_sdk"
                | "realtime_conversation_hosted_mcp_approval_sdk"
                | "realtime_conversation_start_sdk"
                | "realtime_conversation_update_sdk"
                | "realtime_conversation_audio_sdk"
                | "realtime_conversation_audio_commit_sdk"
                | "realtime_conversation_input_sdk"
                | "realtime_conversation_response_sdk"
                | "realtime_conversation_delete_item_sdk"
                | "realtime_conversation_text_sdk"
                | "realtime_conversation_complete_sdk"
                | "realtime_conversation_fail_sdk"
                | "realtime_conversation_interrupt_sdk"
                | "realtime_conversation_close_sdk"
        ),
        "{operation} is unsupported for replaceable tool sessions; use Session.submit"
    );
    Ok(())
}
