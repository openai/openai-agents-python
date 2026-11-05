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
}

struct Generation {
    id: u64,
    entries: Vec<Entry>,
    specs: Vec<DynamicToolSpec>,
}

impl Generation {
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
#[pyclass(frozen, module = "agents._native")]
pub(crate) struct ManualToolGrant {
    pub(crate) owner: ManualOwner,
    pub(crate) key: PendingKey,
    pub(crate) invocation: u64,
}

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

pub struct PythonTools {
    current: RwLock<Arc<Generation>>,
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
    selecting_steps: AtomicBool,
    selecting_children: AtomicBool,
    step_defaults: RwLock<Option<Vec<ToolName>>>,
    // None retires this native thread's selectors. Registration and retirement
    // share the lock so a late step cannot wait on an already closed reader.
    step_replies: Mutex<Option<HashMap<u64, oneshot::Sender<Option<Vec<ToolName>>>>>>,
    #[cfg(test)]
    pub(crate) before_result: Mutex<Option<Arc<manual_checkpoint_tests::BeforeResult>>>,
}

enum QueuedRequest {
    Step(Value),
    Call(Weak<PythonTools>, PendingKey, u64, Value),
    Retired {
        thread: String,
        incarnation: String,
    },
    GracefulFailure(Weak<PythonTools>, Weak<TurnAdmission>),
    ModelFailure {
        owner: Weak<PythonTools>,
        receipt: Weak<TurnAdmission>,
        thread: String,
        policy_message: Option<&'static str>,
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
    turn_id: String,
    owner: Weak<PythonTools>,
    use_deferred_specs: bool,
    state: watch::Sender<AdmissionState>,
    completed_calls: Mutex<HashSet<String>>,
    graceful_requested: AtomicBool,
    sampling_stop: Arc<TurnSamplingStop>,
    // A turn born while the graph is stopped must stay fenced even if a
    // later explicit submission starts a new graph generation.
    graph_fenced: bool,
    // Idle notification and terminal delivery are different native boundaries.
    // Reopening must never trust an idle hint alone for a fenced new turn.
    terminal: AtomicBool,
    local_command_cleanup_failed: AtomicBool,
    input_recorded: AtomicBool,
    // Private namespace for SDK steers of this exact runtime, including same-ID recovery.
    steering_namespace: String,
    steering_tail: AtomicBool,
    exact_steering: OnceLock<bool>,
    provider_error: Mutex<Option<(Value, Value)>>,
    policy_failure: OnceLock<&'static str>,
}

tokio::task_local! {
    // Core checks idle and then awaits contributors on this same task.
    // A new receipt can arrive in between; explicit probes never own it.
    static IDLE_PROBE_RECEIPT: Arc<TurnAdmission>;
}

impl TurnAdmission {
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

    pub(crate) fn reject_model(self: &Arc<Self>, thread: &str) {
        if let Some(owner) = self.owner.upgrade() {
            // Core drains tool futures before reporting the model error. Wake
            // the SDK's existing failure settlement so it can interrupt the
            // real turn and join callbacks, without claiming native completion.
            let _ = owner.family.requests.send(QueuedRequest::ModelFailure {
                owner: Arc::downgrade(&owner),
                receipt: Arc::downgrade(self),
                thread: thread.to_owned(),
                policy_message: None,
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

struct PendingCall {
    invocation: u64,
    manual_request: Option<Value>,
    sender: oneshot::Sender<DynamicToolResponse>,
    phase: Phase,
    // Imported approvals have no live Core turn. Only their current binding
    // can exempt them from response-control's live-turn capture check.
    reentry: bool,
    continuation: Option<Value>,
}

#[derive(Deserialize)]
#[serde(untagged)]
pub(crate) enum Reply {
    Output {
        request: PendingKey,
        response: DynamicToolResponse,
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
) -> Vec<(Option<&DynamicToolNamespaceSpec>, &DynamicToolFunctionSpec)> {
    let mut functions = Vec::new();
    for spec in specs {
        match spec {
            DynamicToolSpec::Function(tool) => functions.push((None, tool)),
            DynamicToolSpec::Namespace(namespace) => {
                for DynamicToolNamespaceTool::Function(tool) in &namespace.tools {
                    functions.push((Some(namespace), tool));
                }
            }
        }
    }
    functions
}

fn validate_slots(specs: &[DynamicToolSpec]) -> Result<()> {
    ensure!(
        specs.iter().all(|spec| match spec {
            DynamicToolSpec::Function(_) => true,
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
    let mut seen = HashSet::new();
    for (namespace, tool) in functions(specs) {
        let key = ToolName::new(namespace.map(|ns| ns.name.clone()), tool.name.clone())
            .with_default_namespace();
        ensure!(seen.insert(key.clone()), "Duplicate Python tool: {key}");
        ensure!(
            !(key.is_default_namespace()
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
        .map(|(_, tool)| dynamic_tool_to_responses_api_tool(tool).map(|tool| tool.parameters))
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
        } else {
            spec.as_object_mut()
                .expect("serialized tool")
                .entry("deferLoading")
                .or_insert(Value::Bool(false));
        }
    }
    Ok(value)
}

impl PythonTools {
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
    pub fn install(startup: &mut StartThreadOptions, specs: Vec<DynamicToolSpec>) -> Result<()> {
        Self::install_manual(startup, specs, &[])
    }

    pub fn install_manual(
        startup: &mut StartThreadOptions,
        specs: Vec<DynamicToolSpec>,
        manual_slots: &[usize],
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
        let tools = Self {
            current: RwLock::new(Arc::new(Generation {
                id: 0,
                entries: Vec::new(),
                specs: Vec::new(),
            })),
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
            selecting_steps: AtomicBool::new(false),
            selecting_children: AtomicBool::new(false),
            step_defaults: RwLock::new(None),
            step_replies: Mutex::new(Some(HashMap::new())),
            #[cfg(test)]
            before_result: Mutex::new(None),
        };
        let mut seed = tools.prepare(0, specs)?;
        seed.set_manual(manual_slots)?;
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

    fn descendant(parent: &Self, instance: String) -> Self {
        Self {
            current: RwLock::new(parent.snapshot()),
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
            selecting_steps: AtomicBool::new(
                parent.selecting_steps.load(Ordering::Acquire)
                    || parent.selecting_children.load(Ordering::Acquire),
            ),
            selecting_children: AtomicBool::new(
                parent.selecting_steps.load(Ordering::Acquire)
                    || parent.selecting_children.load(Ordering::Acquire),
            ),
            step_defaults: RwLock::new(
                parent
                    .step_defaults
                    .read()
                    .expect("Step defaults poisoned")
                    .clone(),
            ),
            step_replies: Mutex::new(Some(HashMap::new())),
            #[cfg(test)]
            before_result: Mutex::new(None),
        }
    }

    fn prepare(&self, id: u64, specs: Vec<DynamicToolSpec>) -> Result<Generation> {
        validate_declarations(&specs)?;
        let mut entries = Vec::new();
        for (namespace, tool) in functions(&specs) {
            let name = ToolName::new(namespace.map(|ns| ns.name.clone()), tool.name.clone());
            let key = name.clone().with_default_namespace();
            ensure!(
                !self.fixed.contains(&key),
                "Python tool collides with fixed tool: {key}"
            );
            ensure!(
                self.policy.allows(&key),
                "Python tool is excluded by ToolPolicy: {key}"
            );
            let mut function = dynamic_tool_to_responses_api_tool(tool)?;
            function.defer_loading = None;
            let spec = match namespace.filter(|_| !key.is_default_namespace()) {
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
            };
            entries.push(Entry {
                manual: false,
                name,
                spec,
                exposure: if tool.defer_loading {
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

    pub(crate) fn retire_steps(&self) {
        // Dropping the owned senders wakes blocked selectors with no authority.
        let pending = self
            .step_replies
            .lock()
            .expect("Python step replies poisoned")
            .take();
        drop(pending);
    }

    pub(crate) fn reply_step(&self, id: u64, names: Option<Vec<ToolName>>) -> Result<()> {
        let reply = self
            .step_replies
            .lock()
            .expect("Python step replies poisoned")
            .as_mut()
            .context("Expired step selection")?
            .remove(&id)
            .context("Expired step selection")?;
        reply
            .send(names)
            .map_err(|_| anyhow::anyhow!("Expired step selection"))
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
            .step_replies
            .lock()
            .expect("Python step replies poisoned");
        ensure!(
            replies
                .as_ref()
                .and_then(|pending| pending.get(&id))
                .is_some_and(|reply| !reply.is_closed()),
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
        seed.set_manual(
            &current
                .entries
                .iter()
                .enumerate()
                .filter_map(|(slot, entry)| entry.manual.then_some(slot))
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
        let mut candidate = self.prepare(id, specs)?;
        candidate.set_manual(manual_slots)?;
        self.check_idle(thread).await?;
        self.publish(candidate)
    }

    fn publish(&self, candidate: Generation) -> Result<u64> {
        let id = candidate.id;
        let mut current = self.current.write().expect("Python catalog poisoned");
        ensure!(
            id == current.id + 1,
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
        Ok(())
    }

    pub(crate) fn fail_pending(&self, key: &PendingKey) -> bool {
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        if key.reply.instance != self.instance || pending.remove(key).is_none() {
            return false;
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
                && key.reply.generation == self.snapshot().id
                && self
                    .pending
                    .lock()
                    .expect("Python replies poisoned")
                    .contains_key(key),
            "Expired or invalid managed Python tool request"
        );
        Ok(())
    }

    pub(crate) fn is_reentry(&self, key: &PendingKey) -> bool {
        self.pending
            .lock()
            .expect("Python replies poisoned")
            .get(key)
            .is_some_and(|call| call.reentry)
    }

    pub(crate) fn enter(&self, owner: &str, key: &PendingKey) -> Result<()> {
        self.check_pending(owner, key)?;
        ensure!(self.sdk_owned(), "effect entry requires the SDK catalog");
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        let call = pending
            .get_mut(key)
            .ok_or_else(|| anyhow::anyhow!("expired Python call"))?;
        ensure!(
            call.phase == Phase::Admitted,
            "Python tool effect was entered or parked"
        );
        call.phase = Phase::Entered;
        Ok(())
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
        let generation = self.snapshot();
        let mut pending = self.pending.lock().expect("Python replies poisoned");
        let live: Vec<_> = keys
            .iter()
            .filter(|key| pending.contains_key(*key))
            .cloned()
            .collect();
        if live.iter().any(|key| {
            let call = &pending[key];
            key.reply.thread != owner
                || key.reply.generation != generation.id
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
        let current = self.snapshot();
        self.pending
            .lock()
            .expect("Python replies poisoned")
            .keys()
            .filter(|key| {
                key.reply.thread == owner
                    && key.reply.generation == current.id
                    && key.reply.slot < current.entries.len()
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
        let generation = self.snapshot().id;
        let pending = self.pending.lock().expect("Python replies poisoned");
        let mut result = Vec::new();
        for (key, call) in pending.iter() {
            if key.reply.thread != owner
                || key.reply.generation != generation
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
            && grant.key.reply.generation == self.snapshot().id
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
    ) -> bool {
        if !matches!(&grant.owner, ManualOwner::Core(owner) if owner.ptr_eq(&Arc::downgrade(self)))
            || grant.key.reply.thread != owner
            || grant.key.reply.generation != self.snapshot().id
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
            .send(response)
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
                    frame.record(&grant.key, &receipt);
                }
            }
        }
        accepted
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
    ) -> Result<oneshot::Receiver<DynamicToolResponse>> {
        let generation = self.snapshot();
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
        self.enqueue(
            owner,
            &generation,
            slot,
            turn,
            call_id,
            serde_json::from_str(arguments)?,
            &ToolCallSource::Direct,
            model,
            true,
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

    // Take the live sender only once. The binding retains it while its writer
    // commits the real result; a failed commit cannot be mistaken for success.
    pub(crate) fn take_reply(
        &self,
        owner: &str,
        key: &PendingKey,
    ) -> Result<Option<oneshot::Sender<DynamicToolResponse>>> {
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
    ) -> Result<(PendingKey, u64, oneshot::Receiver<DynamicToolResponse>)> {
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
        let request = json!({
            "id": call,
            "msg": {
                "type":"dynamic_tool_call_request", "turnId": turn, "callId": call,
                "namespace": entry.name.namespace, "tool": entry.name.name,
                "arguments": arguments, "_sdk_python_tool": key.reply,
                "_sdk_invocation": invocation, "_sdk_manual": entry.manual,
                "_sdk_code_origin": code_origin, "_sdk_model": model
            }
        });
        let (sender, receiver) = oneshot::channel();
        {
            let mut pending = self.pending.lock().expect("Python replies poisoned");
            ensure!(!pending.contains_key(&key), "Duplicate Python tool call");
            pending.insert(
                key.clone(),
                PendingCall {
                    invocation,
                    manual_request: entry.manual.then(|| request.clone()),
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
    ) -> Result<(PendingKey, oneshot::Receiver<DynamicToolResponse>)> {
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
    tools: &'a PythonTools,
    key: PendingKey,
    invocation: u64,
}

impl Drop for PendingReply<'_> {
    fn drop(&mut self) {
        let mut pending = self.tools.pending.lock().expect("Python replies poisoned");
        if pending
            .get(&self.key)
            .is_some_and(|call| call.invocation == self.invocation)
        {
            pending.remove(&self.key);
        }
    }
}

fn provider_error_metadata(details: &codex_protocol::error::CodexErrorDetails) -> Option<Value> {
    use codex_protocol::error::CodexErrorDetails;

    let (status, code, request_id) = match details {
        CodexErrorDetails::UnexpectedStatus(error) => (
            error.status.as_u16(),
            provider_error_code(&error.body),
            error.request_id.as_deref(),
        ),
        // Core also constructs RetryLimit with a synthetic 500 for a transport
        // retry limit. Only this pinned HTTP 429 path retains an observed status.
        CodexErrorDetails::RetryLimit(error)
            if error.status == http::StatusCode::TOO_MANY_REQUESTS =>
        {
            (error.status.as_u16(), None, error.request_id.as_deref())
        }
        CodexErrorDetails::ConnectionFailed(error) => (error.source.status()?.as_u16(), None, None),
        CodexErrorDetails::ResponseStreamFailed(error) => {
            (error.source.status()?.as_u16(), None, None)
        }
        _ => return None,
    };
    Some(json!({
        "status_code": status,
        "code": code,
        "request_id": request_id.filter(|id| {
            !id.is_empty() && id.len() <= 256 && id.bytes().all(|byte| byte.is_ascii_graphic())
        }),
    }))
}

fn provider_error_code(body: &str) -> Option<String> {
    if body.len() > 128 * 1024 {
        return None;
    }
    let body: Value = serde_json::from_str(body).ok()?;
    let code = body.get("error")?.get("code")?.as_str()?;
    (!code.is_empty()
        && code.len() <= 128
        && code
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_.-".contains(&byte)))
    .then(|| code.to_owned())
}

#[derive(Default)]
pub struct PythonContributor {
    pub(crate) manager: Weak<ThreadManager>,
    pub(crate) models_config: codex_models_manager::ModelsManagerConfig,
    pub(crate) code_mode_enabled: bool,
}
struct StepCatalog {
    generation: Arc<Generation>,
    use_deferred_specs: bool,
    slots: Vec<usize>,
}

impl ToolLifecycleContributor for PythonContributor {
    fn on_tool_finish<'a>(&'a self, input: ToolFinishInput<'a>) -> ToolLifecycleFuture<'a> {
        Box::pin(async move {
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
    // The default BeforeTaskRegistration phase includes compaction tasks.
    fn on_turn_start<'a>(&'a self, input: TurnStartInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            #[cfg(test)]
            if let Some(hold) = input.thread_store.get::<admission_order_tests::StartHold>() {
                hold.entered.add_permits(1);
                hold.release.acquire().await.expect("start hold").forget();
            }
            if let Some(tools) = input.thread_store.get::<PythonTools>() {
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
                let receipt = Arc::new(TurnAdmission {
                    turn_id: input.turn_id.to_owned(),
                    owner: Arc::downgrade(&tools),
                    use_deferred_specs,
                    state: watch::channel(AdmissionState::Pending).0,
                    completed_calls: Mutex::default(),
                    graceful_requested: AtomicBool::new(false),
                    sampling_stop: input.turn_store.get_or_init(TurnSamplingStop::default),
                    graph_fenced: graph.fenced,
                    terminal: AtomicBool::new(false),
                    local_command_cleanup_failed: AtomicBool::new(false),
                    input_recorded: AtomicBool::new(false),
                    steering_namespace: format!(
                        "agents-steer:{}:",
                        codex_protocol::ThreadId::new()
                    ),
                    steering_tail: AtomicBool::new(false),
                    exact_steering: OnceLock::new(),
                    provider_error: Mutex::default(),
                    policy_failure: OnceLock::new(),
                });
                crate::native_interception::attach_sdk_admission(input.turn_store, &receipt);
                input.turn_store.insert(receipt.clone());
                if let Some(previous) = tools
                    .admission
                    .lock()
                    .expect("Python admission receipt poisoned")
                    .replace(receipt)
                {
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
                // A later error, including a non-provider error, replaces the
                // earlier metadata. Capturing this never marks a turn failed.
                *receipt
                    .provider_error
                    .lock()
                    .expect("Provider error receipt poisoned") =
                    provider_error_metadata(input.error_details).and_then(|metadata| {
                        serde_json::to_value(input.error)
                            .ok()
                            .map(|category| (category, metadata))
                    });
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
                input.thread_store.get_or_init(|| {
                    PythonTools::descendant(&parent_tools, control.incarnation.clone())
                });
            }
            if let Some(tools) = input.thread_store.get::<PythonTools>() {
                if let Some(control) = input
                    .thread_store
                    .get::<crate::native_interception::Control>()
                {
                    let _ = tools.interception.set(Arc::downgrade(&control));
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
                tools.family.graph_changed.send_replace(());
            }
        })
    }

    fn on_thread_stop<'a>(&'a self, input: ThreadStopInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
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
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        slots
            .iter()
            .copied()
            .map(|slot| {
                Arc::new(PythonExecutor {
                    thread: thread.level_id().to_string(),
                    tools: tools.clone(),
                    generation: generation.clone(),
                    slot,
                    use_deferred_specs,
                    code_mode_enabled: self.code_mode_enabled,
                }) as Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>
            })
            .collect()
    }
}

impl ToolContributor for PythonContributor {
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
        self.executors(thread, tools, generation, use_deferred_specs, &slots)
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
        let snapshot = step.get_or_init(|| {
            let generation = tools.snapshot();
            StepCatalog {
                slots: {
                    let defaults = tools.step_defaults.read().expect("Step defaults poisoned");
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
            }
        });
        self.executors(
            thread,
            tools,
            snapshot.generation.clone(),
            snapshot.use_deferred_specs,
            &snapshot.slots,
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
            if !tools.selecting_steps.load(Ordering::Acquire) {
                return Ok(None);
            }
            let id = tools.family.next_invocation.fetch_add(1, Ordering::Relaxed);
            let (reply, receive) = oneshot::channel();
            {
                let mut replies = tools
                    .step_replies
                    .lock()
                    .expect("Python step replies poisoned");
                let pending = replies.as_mut().ok_or("Expired step owner")?;
                pending.insert(id, reply);
            }
            // Dropping the native step retires this exact nonce even if Python
            // returns later, after another step or incarnation has started.
            struct PendingStep {
                tools: Arc<PythonTools>,
                id: u64,
            }
            impl Drop for PendingStep {
                fn drop(&mut self) {
                    if let Some(pending) = self
                        .tools
                        .step_replies
                        .lock()
                        .expect("Python step replies poisoned")
                        .as_mut()
                    {
                        pending.remove(&self.id);
                    }
                }
            }
            let _pending = PendingStep {
                tools: tools.clone(),
                id,
            };
            let defaults = tools
                .step_defaults
                .read()
                .expect("Step defaults poisoned")
                .clone();
            tools.family.requests.send(QueuedRequest::Step(json!({
                "id": turn.level_id(),
                "msg": {"type": "_sdk_select_step", "turn_id": turn.level_id(),
                    "thread": thread.level_id(), "instance": tools.instance,
                    "step_id": id, "generation": tools.snapshot().id,
                    "native_step": step.level_id(), "candidates": candidates, "defaults": defaults}
            }))).map_err(|_| "Step reader closed")?;
            let names = receive
                .await
                .map_err(|_| "Step selection cancelled")?
                .ok_or("Step selection failed")?;
            Ok(Some(names))
        })
    }
}

struct PythonExecutor {
    thread: String,
    tools: Arc<PythonTools>,
    generation: Arc<Generation>,
    slot: usize,
    use_deferred_specs: bool,
    code_mode_enabled: bool,
}

impl<'call> ToolExecutor<ToolCall<'call>> for PythonExecutor {
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
            let arguments: Value = serde_json::from_str(call.function_arguments()?)
                .map_err(|error| FunctionCallError::RespondToModel(error.to_string()))?;
            let (key, invocation, receiver) = self
                .tools
                .enqueue(
                    &self.thread,
                    &self.generation,
                    self.slot,
                    &call.turn_id,
                    &call.call_id,
                    arguments,
                    &call.source,
                    Some(&call.model),
                    false,
                )
                .map_err(|error| FunctionCallError::RespondToModel(error.to_string()))?;
            let _reply = PendingReply {
                tools: &self.tools,
                invocation,
                key: key.clone(),
            };
            let response = receiver.await.map_err(|_| {
                FunctionCallError::RespondToModel("Python tool call cancelled before reply".into())
            })?;
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
            Ok(Box::new(PythonOutput(response)) as Box<dyn ToolOutput>)
        })
    }
}

pub(crate) struct PythonOutput(pub(crate) DynamicToolResponse);

impl PythonOutput {
    pub(crate) fn receipt(&self, key: &PendingKey) -> ResponseItem {
        let mut item: ResponseItem = self
            .to_response_item(
                &key.call,
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
                    turn_id: Some(key.turn.clone()),
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
        });
        assert!(provider_error_metadata(&synthetic).is_none());

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
            provider_error_metadata(&observed).unwrap(),
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
        let session = ExtensionData::new("session");
        let turn = ExtensionData::new(turn_id);
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

    fn admission_thread() -> ExtensionData {
        let thread = ExtensionData::new("thread");
        let tools = match Arc::try_unwrap(sdk()) {
            Ok(tools) => tools,
            Err(_) => panic!("test catalog has unexpected owners"),
        };
        thread.insert(tools);
        thread
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
        let home = directory.path().join("home");
        let work = directory.path().join("work");
        let storage = directory.path().join("storage");
        for path in [&home, &work, &storage] {
            std::fs::create_dir(path).unwrap();
        }
        let config = json!({
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
        let runtime = json!({
            "codex_home": home, "cwd": work, "native_thread_store": storage,
            "codex_self_exe": null, "ephemeral": false
        });
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
        use crate::storage::{CompletionHold, MemoryThreadStore};
        use codex_protocol::protocol::Op;
        use codex_protocol::user_input::UserInput;
        use codex_thread_store::LoadThreadHistoryParams;
        use tokio::sync::Semaphore;

        tokio::time::timeout(std::time::Duration::from_secs(55), async {
            for outcome in ["continue", "abort", "stop", "superseded"] {
                let abort = outcome == "abort";
                let provider = Provider::start().await;
                let directory = tempfile::tempdir().unwrap();
                let engine = real_engine_store(&directory, &provider.url, true).await;
                let native = engine
                    .start_with_tools(Default::default(), None, Some(vec![]), false)
                    .await.unwrap();
                let state = native.current().unwrap();
                let store = engine.store.as_any().downcast_ref::<MemoryThreadStore>().unwrap();
                // With no guardian, hooks, or subagents in this fixture, the
                // next native flush after task startup is tasks/mod.rs' flush
                // after RegularTask::run and before on_task_finished takes the
                // active slot. The post-terminal flush must proceed freely.
                let hold = Arc::new(CompletionHold {
                    parent: state.id,
                    entered: Semaphore::new(0),
                    release: Semaphore::new(0),
                });
                *store.next_flush_hold.lock().unwrap() = Some(hold.clone());
                let started = crate::ops::submit_replaceable(&state, json!({
                    "input": [UserInput::Text { text: "initial request".into(), text_elements: vec![] }],
                })).await.unwrap();
                assert_eq!(started["status"], "started");
                hold.entered.acquire().await.unwrap().forget();
                assert_eq!(provider.requests.lock().unwrap().len(), 1,
                    "the barrier must be after the actual completed sample");
                let accepted = crate::ops::submit_replaceable(&state, json!({
                    "mode": "steer", "expected_turn_id": started["turn_id"],
                    "input": [UserInput::Text { text: "unique steering tail in the native gap".into(), text_elements: vec![] }],
                })).await.unwrap();
                assert_eq!(accepted, json!({"status": "steered", "turn_id": started["turn_id"]}));
                if abort {
                    state.thread.submit(Op::Interrupt).await.unwrap();
                    // The native force path cancels a task waiting in flush;
                    // it must report its real abort rather than the tail.
                } else {
                    hold.release.add_permits(1);
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
                        });
                        break;
                    }
                }
                assert_eq!(provider.requests.lock().unwrap().len(), 1);
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
        use crate::storage::{CompletionHold, MemoryThreadStore};
        use crate::{checkpoint, manual_checkpoint};
        use codex_protocol::dynamic_tools::DynamicToolResponse;
        use codex_protocol::user_input::UserInput;
        use tokio::sync::Semaphore;

        tokio::time::timeout(std::time::Duration::from_secs(35), async {
            let provider = Provider::with_first_item(Some(json!({
                "id":"fc-lookup", "type":"function_call", "name":"lookup",
                "arguments":"{}", "call_id":"call-lookup"
            }))).await;
            let directory = tempfile::tempdir().unwrap();
            let engine = real_engine_store(&directory, &provider.url, true).await;
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
            let notice = tools.next_request().await;
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
            let restored_engine = real_engine_store(&restored_directory, &provider.url, true).await;
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
            manual_checkpoint::prepare(&resumed, &restored.manual).unwrap();
            let notice = tools.next_request().await;
            assert_eq!(notice["msg"]["callId"], "call-lookup");
            let mut grants = tools.manual_snapshot(&resumed.id.to_string()).unwrap();
            assert_eq!(grants.len(), 1);
            let (_, grant, _) = grants.pop().unwrap();
            assert!(tools.check_manual(&resumed.id.to_string(), &grant));
            let response: DynamicToolResponse = serde_json::from_value(json!({
                "contentItems":[{"type":"inputText","text":"approved lookup value"}], "success":true
            })).unwrap();
            assert!(tools.respond_manual(&resumed.id.to_string(), &grant, response));
            assert!(!tools.check_manual(&resumed.id.to_string(), &grant));

            let store = restored_engine.store.as_any().downcast_ref::<MemoryThreadStore>().unwrap();
            let hold = Arc::new(CompletionHold {
                parent: resumed.id, entered: Semaphore::new(0), release: Semaphore::new(0)
            });
            *store.next_flush_hold.lock().unwrap() = Some(hold.clone());
            let recovery_state = resumed.clone();
            let manual = restored.manual.clone();
            let recovering = tokio::spawn(async move {
                manual_checkpoint::continue_if_ready(&recovery_state, &manual, None).await.unwrap()
            });
            hold.entered.acquire().await.unwrap().forget();
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
            hold.release.add_permits(1);
            loop {
                let event = resumed.next_session_event().await.unwrap();
                let kind = event["msg"]["type"].as_str().unwrap_or("");
                if ["turn_aborted", "task_complete", "turn_complete"].contains(&kind) {
                    assert_ne!(kind, "turn_aborted", "{event}");
                    assert_eq!(event["msg"]["turn_id"], recovered["turn_id"]);
                    assert_eq!(event["sdk_unsampled_steering"], true);
                    break;
                }
            }
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

    fn catalog(policy: ToolPolicy, spec: DynamicToolSpec) -> Arc<PythonTools> {
        let tools = Arc::new(PythonTools {
            current: RwLock::new(Arc::new(Generation {
                id: 0,
                entries: vec![],
                specs: vec![],
            })),
            fixed: HashSet::new(),
            policy: Arc::new(policy),
            pending: Mutex::new(HashMap::new()),
            failed_turns: Mutex::default(),
            family: Arc::new(PythonFamily::default()),
            instance: None,
            suspended: AtomicBool::new(false),
            admission: Mutex::new(None),
            interception: OnceLock::new(),
            manual_restore: Mutex::new(None),
            manual_progress: watch::channel(0).0,
            selecting_steps: AtomicBool::new(false),
            selecting_children: AtomicBool::new(false),
            step_defaults: RwLock::new(None),
            step_replies: Mutex::new(Some(HashMap::new())),
            before_result: Mutex::new(None),
        });
        *tools.current.write().unwrap() = Arc::new(tools.prepare(1, vec![spec]).unwrap());
        tools
    }

    fn function(name: &str) -> DynamicToolFunctionSpec {
        DynamicToolFunctionSpec {
            name: name.into(),
            description: "test tool".into(),
            input_schema: json!({"type":"object","properties":{}}),
            defer_loading: false,
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
        }
    }

    fn executor(tools: Arc<PythonTools>) -> PythonExecutor {
        PythonExecutor {
            thread: "owner".into(),
            generation: tools.snapshot(),
            tools,
            slot: 0,
            use_deferred_specs: false,
            code_mode_enabled: false,
        }
    }

    #[tokio::test]
    async fn family_inbox_routes_identical_calls_before_children_are_observed() {
        let root = catalog(
            ToolPolicy::default(),
            DynamicToolSpec::Function(function("lookup")),
        );
        let first = Arc::new(PythonTools::descendant(&root, "runtime-a".into()));
        let second = Arc::new(PythonTools::descendant(&root, "runtime-b".into()));
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
        let replacement = Arc::new(PythonTools::descendant(&root, "new-runtime".into()));
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
            )
            .unwrap();
        let old = PendingReply {
            tools: &tools,
            key: key.clone(),
            invocation,
        };
        let (_, grant, _) = tools.manual_snapshot("owner").unwrap().pop().unwrap();
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
        assert!(tools.respond_manual("owner", &grant, response.clone()));
        assert!(result.try_recv().is_ok());
        assert!(!tools.respond_manual("owner", &grant, response));
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
        tools.enter("owner", &key).unwrap();
        assert!(tools.park("owner", &key).is_err());
        assert!(tools.park_continuation("owner", &key, json!(true)).is_err());
        assert!(
            tools
                .park_continuation("owner", &key, json!({"calls":["child1"],"receipts":[]}))
                .unwrap()
        );
        assert!(tools.enter("owner", &key).is_err());
        let keys = tools.parked("owner").unwrap();
        assert_eq!(
            tools.continuations(&keys).unwrap()["one"]["calls"],
            json!(["child1"])
        );
        tools.suspend_reentry(&keys).unwrap();
        assert!(!tools.park("owner", &key).unwrap());
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
        tools.park("owner", &parked).unwrap();
        assert!(
            tools.parked("owner").is_err(),
            "do not seal a still admitted sibling"
        );
        tools.enter("owner", &finished).unwrap();
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
            .send(actual)
            .unwrap();
        assert!(tools.take_reply("owner", &finished).unwrap().is_none());
        assert_eq!(
            PythonOutput(b.await.unwrap()).log_output(),
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
        let keys = vec![admitted.clone(), entered.clone()];
        tools.enter("owner", &entered).unwrap();
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
            .send(actual)
            .unwrap();
        assert_eq!(
            PythonOutput(b.await.unwrap()).log_output(),
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
        assert!(tools.enter("owner", &admitted).is_err());
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
}

// This mode admits SDK submit/steer only. Keep native observations and replies
// available; never permit an untracked producer to race the session's idle gate.
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
        ),
        "{operation} is unsupported for replaceable tool sessions; use Session.submit"
    );
    Ok(())
}
