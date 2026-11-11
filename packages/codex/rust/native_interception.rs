// Narrow native boundaries. No handler registry, interpreter, or policy is
// reproduced here. Core owns routing; a live cell owns its retained delegate.
use std::collections::{HashMap, HashSet, VecDeque};
use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{Arc, Mutex, OnceLock, Weak};
use std::time::Duration;

use anyhow::{Result, ensure};
use codex_code_mode::{
    CellId, CodeModeNestedToolCall, CodeModeSession, CodeModeSessionCellExecutionLimits,
    CodeModeSessionDelegate, CodeModeSessionProvider, CodeModeSessionProviderFuture,
    CodeModeSessionResultFuture, ExecuteRequest, NotificationFuture, RuntimeResponse, StartedCell,
    ToolInvocationFuture, WaitOutcome, WaitRequest,
};
use codex_core_api::{CodexThread, Config, ThreadManager};
use codex_extension_api::{
    BuiltinToolResultInput, ExtensionData, ExtensionFuture, McpToolResultInput,
    ModelRequestContributor, ModelRequestInput, ModelRequestKind, ModelResponseError,
    ModelResponseInterceptor, ModelResponseStream, ModelToolAdmission, ResponseEvent,
    ThreadLifecycleContributor, ThreadStartInput, ThreadStopInput, ToolContributor,
    ToolFinishInput, ToolLifecycleContributor, ToolLifecycleFuture, ToolPolicy, ToolStartInput,
    TurnAbortInput, TurnLifecycleContributor, TurnStartInput, TurnStopInput,
};
use codex_protocol::models::{
    FunctionCallOutputBody, ResponseInputItem, ResponseItem, SearchToolCallParams,
};
use codex_protocol::protocol::SessionSource;
use codex_tools::{
    FunctionCallError, ToolCall, ToolCallSource, ToolExecutor, ToolExecutorFuture, ToolExposure,
    ToolName, ToolOutput, ToolPayload, ToolSearchInfo, ToolSpec,
};
use futures_util::{StreamExt, stream};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::sync::{Mutex as AsyncMutex, OwnedSemaphorePermit, Semaphore, mpsc, oneshot};
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;
use tokio_util::task::TaskTracker;

use crate::environment::StrictExecutorWrites;

const FAILURE: &str = "Native tool interception failed or was denied";
const MAX_PENDING: usize = 128;
const MAX_PAYLOAD_BYTES: usize = 128 * 1024 * 1024;

// Only the serialized bridge representation is counted, not native tool
// execution or the first projection of an opaque pinned ToolOutput.
struct Counter {
    bytes: usize,
    limit: usize,
}
impl Write for Counter {
    fn write(&mut self, data: &[u8]) -> io::Result<usize> {
        let bytes = self
            .bytes
            .checked_add(data.len())
            .filter(|n| *n <= self.limit)
            .ok_or_else(|| io::Error::other(FAILURE))?;
        self.bytes = bytes;
        Ok(data.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[derive(Serialize)]
struct Name<'a> {
    namespace: Option<&'a str>,
    name: &'a str,
}
#[derive(Serialize)]
#[serde(untagged)]
enum Input<'a> {
    Text(&'a str),
    Json(&'a Value),
    JsonText(#[serde(serialize_with = "json_text")] &'a Value),
}

// MCP's public bridge input is JSON *inside a string*. serde_json's writer
// streams collect_str through its escaping formatter, so account() can reject
// the complete encoded notice without first allocating arguments.to_string().
fn json_text<S: serde::Serializer>(value: &Value, serializer: S) -> Result<S::Ok, S::Error> {
    serializer.collect_str(value)
}

// Borrow every potentially large field until its measured lease exists.
#[derive(Serialize)]
struct Request<'a, I, R, K> {
    #[serde(skip_serializing_if = "Option::is_none")]
    tracing_context: Option<&'a str>,
    // Only a native capture confers turn authority; never serialize it to Python.
    #[serde(skip)]
    owner: Option<&'a Arc<DirectTurn>>,
    phase: &'static str,
    route: &'static str,
    boundary: &'static str,
    turn_id: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    model: Option<&'a str>,
    call_id: Option<&'a str>,
    outer_call_id: Option<&'a str>,
    cell_id: Option<&'a CellId>,
    runtime_tool_call_id: Option<&'a str>,
    tool_name: Name<'a>,
    tool_kind: K,
    input: I,
    result: R,
}

#[derive(Clone)]
enum PolicyAdmission {
    Current,
    CellBefore(Arc<CellPolicyOwner>),
    CellAfter(Arc<CellPolicyOwner>),
    BuiltinAfter,
}
impl PolicyAdmission {
    fn admits(&self, owner: Option<&Arc<DirectTurn>>) -> bool {
        match self {
            Self::Current => owner.is_none_or(|turn| turn.admits_policy()),
            Self::CellBefore(cell) | Self::CellAfter(cell) => owner.is_some_and(|turn| {
                cell.control.root
                    && Arc::ptr_eq(turn, &cell.turn)
                    && cell.cell_id.get().is_some()
                    && !turn.policy_stopped.load(Ordering::Acquire)
            }),
            Self::BuiltinAfter => {
                owner.is_some_and(|turn| !turn.policy_stopped.load(Ordering::Acquire))
            }
        }
    }
    fn permits_stop(&self) -> bool {
        matches!(self, Self::Current | Self::CellBefore(_))
    }
}

struct CellPolicyOwner {
    id: u64,
    control: Arc<Control>,
    turn: Arc<DirectTurn>,
    outer_call_id: String,
    cell_id: OnceLock<CellId>,
}
impl CellPolicyOwner {
    fn identify(&self, cell_id: &CellId) -> Result<(), String> {
        let actual = self.cell_id.get_or_init(|| cell_id.clone());
        if actual != cell_id {
            return Err(FAILURE.into());
        }
        Ok(())
    }
    async fn bind(self: &Arc<Self>, cell_id: &CellId) -> Result<(), String> {
        self.identify(cell_id)?;
        let token = CancellationToken::new();
        match self
            .control
            .decide(CellOwnerBinding { lease: self }, &token)
            .await?
        {
            Decision::Continue => Ok(()),
            _ => Err(FAILURE.into()),
        }
    }
}
impl Drop for CellPolicyOwner {
    fn drop(&mut self) {
        let _ = self
            .control
            .channel
            .sender
            .send(Message::CellOwnerRetired(self.id));
    }
}
#[derive(Serialize)]
struct CellOwnerMetadata<'a> {
    owner_id: u64,
    thread_id: &'a str,
    turn_id: &'a str,
    outer_call_id: &'a str,
    cell_id: &'a CellId,
}
#[derive(Serialize)]
struct CellOwnerNotice<'a> {
    nonce: u64,
    cell_owner: CellOwnerMetadata<'a>,
}
#[derive(Serialize)]
struct CellOwnerBinding<'a> {
    #[serde(skip)]
    lease: &'a Arc<CellPolicyOwner>,
}
impl DecisionRequest for CellOwnerBinding<'_> {
    fn tool_identity(&self) -> Option<(Option<&str>, &str)> {
        None
    }
    fn owner(&self) -> Option<&Arc<DirectTurn>> {
        Some(&self.lease.turn)
    }
    fn cell_owner_binding(&self) -> Option<&CellPolicyOwner> {
        Some(self.lease)
    }
}
#[derive(Serialize)]
struct CellRequest<'a, T> {
    #[serde(flatten)]
    request: T,
    cell_owner_id: u64,
    #[serde(skip)]
    lease: &'a Arc<CellPolicyOwner>,
    #[serde(skip)]
    before: bool,
}
impl<T: DecisionRequest> DecisionRequest for CellRequest<'_, T> {
    fn profile_source(&self) -> Option<&Arc<ProfileMcpSource>> {
        self.request.profile_source()
    }
    fn result_policy(&self) -> bool {
        self.request.result_policy()
    }
    fn tool_identity(&self) -> Option<(Option<&str>, &str)> {
        self.request.tool_identity()
    }
    fn owner(&self) -> Option<&Arc<DirectTurn>> {
        self.request.owner()
    }
    fn admission(&self) -> PolicyAdmission {
        if self.before {
            PolicyAdmission::CellBefore(self.lease.clone())
        } else {
            PolicyAdmission::CellAfter(self.lease.clone())
        }
    }
}
#[derive(Serialize)]
struct BuiltinResult<T> {
    #[serde(flatten)]
    request: T,
}
impl<T: DecisionRequest> DecisionRequest for BuiltinResult<T> {
    fn profile_source(&self) -> Option<&Arc<ProfileMcpSource>> {
        self.request.profile_source()
    }
    fn result_policy(&self) -> bool {
        true
    }
    fn tool_identity(&self) -> Option<(Option<&str>, &str)> {
        self.request.tool_identity()
    }
    fn owner(&self) -> Option<&Arc<DirectTurn>> {
        self.request.owner()
    }
    fn admission(&self) -> PolicyAdmission {
        PolicyAdmission::BuiltinAfter
    }
}

trait DecisionRequest: Serialize {
    fn profile_source(&self) -> Option<&Arc<ProfileMcpSource>> {
        None
    }
    fn result_policy(&self) -> bool {
        false
    }
    fn tool_identity(&self) -> Option<(Option<&str>, &str)>;
    fn admission(&self) -> PolicyAdmission {
        PolicyAdmission::Current
    }
    fn cell_owner_binding(&self) -> Option<&CellPolicyOwner> {
        None
    }
    fn owner(&self) -> Option<&Arc<DirectTurn>> {
        None
    }
}

impl<I: Serialize, R: Serialize, K: Serialize> DecisionRequest for Request<'_, I, R, K> {
    fn result_policy(&self) -> bool {
        self.phase == "after"
    }
    fn tool_identity(&self) -> Option<(Option<&str>, &str)> {
        Some((self.tool_name.namespace, self.tool_name.name))
    }

    fn owner(&self) -> Option<&Arc<DirectTurn>> {
        self.owner
    }
}

impl<T: DecisionRequest> DecisionRequest for &T {
    fn profile_source(&self) -> Option<&Arc<ProfileMcpSource>> {
        (*self).profile_source()
    }
    fn result_policy(&self) -> bool {
        (*self).result_policy()
    }
    fn admission(&self) -> PolicyAdmission {
        (*self).admission()
    }
    fn cell_owner_binding(&self) -> Option<&CellPolicyOwner> {
        (*self).cell_owner_binding()
    }
    fn tool_identity(&self) -> Option<(Option<&str>, &str)> {
        (*self).tool_identity()
    }

    fn owner(&self) -> Option<&Arc<DirectTurn>> {
        (*self).owner()
    }
}

#[cfg(test)]
impl DecisionRequest for Value {
    fn tool_identity(&self) -> Option<(Option<&str>, &str)> {
        Some((
            self["tool_name"]["namespace"].as_str(),
            self["tool_name"]["name"].as_str()?,
        ))
    }
}

#[derive(Serialize)]
struct Identified<'a, T> {
    thread_id: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    agent_role: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    instance: Option<&'a str>,
    #[serde(flatten)]
    request: T,
}
#[derive(Serialize)]
struct Notice<'a, T> {
    #[serde(skip_serializing_if = "Option::is_none")]
    mcp_identity: Option<&'a (String, String)>,
    #[serde(skip_serializing_if = "Option::is_none")]
    profile_id: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    profile_epoch: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    generation: Option<u64>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    realtime_mcp: bool,
    nonce: u64,
    request: Identified<'a, T>,
}

struct Pending {
    profile_source: Option<Arc<ProfileMcpSource>>,
    generation: Option<u64>,
    issuer: Weak<Control>,
    stopped: CancellationToken,
    request_cancelled: CancellationToken,
    owner: Option<Arc<DirectTurn>>,
    admission: PolicyAdmission,
    owner_binding: bool,
    held_for_stop: bool,
    result_policy: bool,
    deadline: Option<Instant>,
    reply: Option<oneshot::Sender<Reply>>,
    payload: Payload,
    _bytes: OwnedSemaphorePermit,
    // Fields drop in declaration order. Wake a consumer waiting for physical
    // release only AFTER the charged bytes are returned, including abandon
    // and reader shutdown. Resolving the decision alone never wakes it.
    _released: Option<oneshot::Sender<()>>,
}
type NoticeRelease = oneshot::Receiver<()>;
enum Payload {
    Queued(String),
    InFlight(u64),
    Callback,
    // Physical callback returned first; dispatch still owns reply/request.
    Returned,
}
enum Message {
    CellOwnerRetired(u64),
    Payload(u64),
    Cancel(u64),
}

#[derive(Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Decision {
    Continue,
    Replace { value: Value },
    Deny,
}

// Only the SDK can reserve a granted decision until owned native retirement.
// Unlike Decision, this channel message cannot be supplied as serialized input.
enum Reply {
    Decision(Decision),
    HoldForStop,
}

#[derive(Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Scope {
    Root,
    ThreadTree,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ToolRule {
    #[serde(default)]
    generation: Option<String>,
    #[serde(default)]
    result: bool,
    name: String,
    namespace: Option<String>,
    timeout: Option<f64>,
    scope: Scope,
}

type Rules = HashMap<ToolName, (Option<Duration>, Scope, bool, Option<u64>)>;

/// Immutable MCP callback policy from the same native Agent profile as its tool router.
#[derive(Clone, Default)]
pub(crate) struct ProfileMcpPolicy {
    identities: HashMap<ToolName, (String, String)>,
    names: HashSet<ToolName>,
    rules: Rules,
    realtime: bool,
}
impl ProfileMcpPolicy {
    fn identities(binding: &codex_mcp::McpBinding) -> HashMap<ToolName, (String, String)> {
        binding
            .tools()
            .iter()
            .map(|tool| {
                (
                    tool.canonical_tool_name().with_default_namespace(),
                    (tool.server_name.clone(), tool.tool.name.to_string()),
                )
            })
            .collect()
    }

    pub(crate) fn prepare(
        binding: &codex_mcp::McpBinding,
        declarations: &std::collections::BTreeMap<
            String,
            crate::agent_profiles::McpToolDeclaration,
        >,
        generation: u64,
    ) -> Result<Self> {
        let mut names = HashSet::new();
        let mut rules = Vec::new();
        for tool in binding.tools() {
            let name = ToolName::new(
                Some(tool.callable_namespace.clone()),
                tool.callable_name.clone(),
            )
            .with_default_namespace();
            names.insert(name.clone());
            let Some(declaration) = declarations.get(&tool.server_name) else {
                continue;
            };
            if !(declaration.before || declaration.approval)
                || !declaration
                    .tools
                    .iter()
                    .any(|name| name.as_str() == tool.tool.name.as_ref())
            {
                continue;
            }
            rules.push(ToolRule {
                name: name.name,
                namespace: name.namespace,
                timeout: (!declaration.approval).then_some(30.0),
                scope: Scope::ThreadTree,
                generation: Some(generation.to_string()),
                result: false,
            });
        }
        Ok(Self {
            identities: Self::identities(binding),
            names,
            rules: Control::compile_rules(rules)?,
            realtime: false,
        })
    }
}

/// Callback attribution only; it carries no Session, environment, or callable executor.
#[derive(Clone)]
pub(crate) struct ProfileMcpSource {
    pub(crate) result_selection: Option<Arc<crate::mcp_result_control::Selection>>,
    pub(crate) receipt: Option<codex_extension_api::AgentProfileReceipt>,
    pub(crate) policy: Option<Arc<ProfileMcpPolicy>>,
}
fn capture_mcp_source(
    step: Option<&ExtensionData>,
) -> Result<Option<Arc<ProfileMcpSource>>, String> {
    let source = step.and_then(|step| step.get::<ProfileMcpSource>());
    if source
        .as_ref()
        .is_some_and(|source| source.policy.is_none())
        || (source.is_none()
            && step.is_some_and(|step| {
                step.get::<codex_extension_api::CapturedAgentProfile>()
                    .is_some()
            }))
    {
        return Err(FAILURE.into());
    }
    Ok(source)
}

#[derive(Serialize)]
struct ProfiledRequest<T> {
    #[serde(flatten)]
    request: T,
    #[serde(skip)]
    source: Option<Arc<ProfileMcpSource>>,
}
impl<T: DecisionRequest> DecisionRequest for ProfiledRequest<T> {
    fn result_policy(&self) -> bool {
        self.request.result_policy()
    }
    fn admission(&self) -> PolicyAdmission {
        self.request.admission()
    }
    fn cell_owner_binding(&self) -> Option<&CellPolicyOwner> {
        self.request.cell_owner_binding()
    }
    fn owner(&self) -> Option<&Arc<DirectTurn>> {
        self.request.owner()
    }
    fn tool_identity(&self) -> Option<(Option<&str>, &str)> {
        self.request.tool_identity()
    }
    fn profile_source(&self) -> Option<&Arc<ProfileMcpSource>> {
        self.source.as_ref()
    }
}

struct State {
    timeout: Option<Duration>,
    scope: Scope,
    revoked: bool,
    nonce: u64,
    pending: HashMap<u64, Pending>,
    tools: Option<Rules>,
    realtime_mcp_tools: Option<Vec<ToolName>>,
}

// The public turn store owns this identity. A model request retains this exact
// scope, never a mutable "current turn" lookup when the provider returns.
struct DirectTurn {
    id: String,
    model: Mutex<Option<Arc<str>>>,
    sdk_admission: OnceLock<Weak<crate::python_tools::TurnAdmission>>,
    cancelled: CancellationToken,
    // Normal terminal also cancels direct work, but yielded cells can still be
    // owned by Python then. Only an accepted whole-turn stop fences their effects.
    policy_stopped: AtomicBool,
    // Core starts, processes the result and finishes inside one spawned
    // dispatch task. The model's call_id can repeat across live invocations.
    mcp_calls: Mutex<HashSet<tokio::task::Id>>,
}

impl DirectTurn {
    fn admits_policy(&self) -> bool {
        !self.policy_stopped.load(Ordering::Acquire)
            && self.sdk_admission.get().is_none_or(|receipt| {
                receipt
                    .upgrade()
                    .is_some_and(|receipt| receipt.is_current())
            })
    }
}

// Boundary is registered before PythonContributor, so the native turn scope
// already exists here. Keep the existing owner graph; do not index turns by ID.
pub(crate) fn attach_sdk_admission(
    turn_store: &ExtensionData,
    receipt: &Arc<crate::python_tools::TurnAdmission>,
) {
    if let Some(turn) = turn_store.get::<DirectTurn>() {
        let _ = turn.sdk_admission.set(Arc::downgrade(receipt));
    }
}

pub(crate) struct CapturedTurnStop {
    control: Arc<Control>,
    owner: Arc<DirectTurn>,
}

pub(crate) fn capture_step_stop(
    thread_store: &ExtensionData,
    turn_store: &ExtensionData,
) -> Option<CapturedTurnStop> {
    Some(CapturedTurnStop {
        control: thread_store.get::<Control>()?,
        owner: turn_store.get::<DirectTurn>()?,
    })
}

impl CapturedTurnStop {
    pub(crate) fn stop(&self) {
        let _state = self
            .control
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        self.owner.policy_stopped.store(true, Ordering::Release);
    }
    pub(crate) async fn finish(self) -> Result<()> {
        self.control
            .interrupt_exact_cells(true, Some(&self.owner.id), Some(&self.owner))
            .await
    }
}

// The MCP hook also serves retained code-mode calls after ordinary turn end.
// Only a policy stop or a revoked thread fences those results, not that end.
pub(crate) fn publish_mcp_result(
    thread_store: &ExtensionData,
    turn_store: &ExtensionData,
    publish: impl FnOnce(),
) {
    if let (Some(control), Some(owner)) = (
        thread_store.get::<Control>(),
        turn_store.get::<DirectTurn>(),
    ) {
        let _ = control.publish_result(&owner, None, publish);
    }
}

// Claim an unanswered deadline or a rejected live reservation. The caller
// validates the exact unresolved obligation before stopping its captured owner.
pub(crate) fn expire_mcp_result(
    thread_store: &ExtensionData,
    turn_store: &ExtensionData,
    claim: impl FnOnce() -> bool,
) {
    if let (Some(control), Some(owner)) = (
        thread_store.get::<Control>(),
        turn_store.get::<DirectTurn>(),
    ) {
        let _state = control
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        if claim() {
            control.stop_expired_owner(&owner, "Native MCP result policy failed");
        }
    } else {
        claim();
    }
}

pub struct Control {
    mcp_results: OnceLock<Weak<crate::mcp_result_control::McpResultControl>>,
    profile_policies: AtomicU8,
    id: String,
    pub(crate) incarnation: String,
    root: bool,
    agent_role: OnceLock<Option<String>>,
    native_depth: usize,
    native_ancestor: Option<(String, String)>,
    stopped: CancellationToken,
    creation_tracing: OnceLock<Option<Arc<codex_otel::sdk_trace::ContextLease>>>,
    direct_turn: Mutex<Option<Arc<DirectTurn>>>,
    context_tools: OnceLock<Weak<crate::python_tools::PythonTools>>,
    // Only links to wrapper sessions this exact thread has used; never a host-wide
    // cell index. The sessions own cells, so this direction must stay weak.
    code_mode_sessions: Mutex<Vec<Weak<Session>>>,
    pending_code_import: Mutex<Option<Arc<PendingCodeImport>>>,
    expiry_cleanup: TaskTracker,
    expiry_cleanup_failed: AtomicBool,
    native_agents: OnceLock<Arc<crate::native_agents::Policy>>,
    executor_admission: OnceLock<PinnedExecutorAdmission>,
    strict_executor_writes: OnceLock<()>,
    collaboration_namespace: std::sync::OnceLock<Option<String>>,
    channel: Arc<Channel>,
}

// One bounded callback owner per explicit root. Native ThreadSpawn ancestry
// may share it, but never the parent's turn or thread-stop lifetime.
struct Channel {
    raw_usage: Mutex<Option<codex_core::SdkRawUsageLink>>,
    observe_raw_provider_events: AtomicBool,
    state: Mutex<State>,
    // At most MAX_PENDING live requests. Cancellation messages must not be
    // dropped when that bound is full, hence the unbounded control channel.
    sender: mpsc::UnboundedSender<Message>,
    receiver: AsyncMutex<mpsc::UnboundedReceiver<Message>>,
    bytes: Arc<Semaphore>,
}

struct PendingCodeImport {
    session: Arc<dyn CodeModeSession>,
    turn: Option<Weak<DirectTurn>>,
}

impl Control {
    pub(crate) fn bind_mcp_results(
        &self,
        results: &Arc<crate::mcp_result_control::McpResultControl>,
    ) {
        let _ = self.mcp_results.set(Arc::downgrade(results));
    }

    pub(crate) fn bind_context_tools(&self, tools: &Arc<crate::python_tools::PythonTools>) {
        let _ = self.context_tools.set(Arc::downgrade(tools));
    }

    pub(crate) fn creation_tracing(&self) -> Option<Arc<codex_otel::sdk_trace::ContextLease>> {
        self.creation_tracing.get().cloned().flatten()
    }

    pub(crate) fn tracing_context(&self) -> codex_otel::sdk_trace::CapturedContext {
        if self.native_ancestor.is_some() {
            // A child keeps its creation owner, including an absent tracing scope.
            codex_otel::sdk_trace::CapturedContext {
                context: self.creation_tracing(),
            }
        } else {
            codex_otel::sdk_trace::CapturedContext::capture()
        }
    }

    // Lifecycle callbacks retain the unpublished receiver if its opening future
    // is cancelled. Cleanup always joins this exact native session.
    async fn close_pending_code_import(&self, turn: Option<&Arc<DirectTurn>>) -> Result<()> {
        let pending = self
            .pending_code_import
            .lock()
            .expect("pending code import poisoned")
            .clone();
        let Some(pending) = pending else {
            return Ok(());
        };
        if let Some(turn) = turn {
            if !pending
                .turn
                .as_ref()
                .and_then(Weak::upgrade)
                .is_some_and(|owner| Arc::ptr_eq(&owner, turn))
            {
                return Ok(());
            }
        }
        pending
            .session
            .shutdown()
            .await
            .map_err(anyhow::Error::msg)?;
        let mut current = self
            .pending_code_import
            .lock()
            .expect("pending code import poisoned");
        if current
            .as_ref()
            .is_some_and(|owner| Arc::ptr_eq(owner, &pending))
        {
            current.take();
        }
        Ok(())
    }

    pub(crate) fn new(id: String, root: bool) -> Self {
        Self::with_budget(id, root, MAX_PAYLOAD_BYTES)
    }

    fn with_budget(id: String, root: bool, bytes: usize) -> Self {
        let (sender, receiver) = mpsc::unbounded_channel();
        Self {
            mcp_results: OnceLock::new(),
            profile_policies: AtomicU8::new(0),
            id,
            incarnation: codex_protocol::ThreadId::new().to_string(),
            root,
            agent_role: OnceLock::new(),
            native_depth: if root { 0 } else { usize::MAX },
            native_ancestor: None,
            stopped: CancellationToken::new(),
            creation_tracing: OnceLock::new(),
            direct_turn: Mutex::new(None),
            context_tools: OnceLock::new(),
            code_mode_sessions: Mutex::default(),
            pending_code_import: Mutex::default(),
            expiry_cleanup: TaskTracker::new(),
            expiry_cleanup_failed: AtomicBool::new(false),
            native_agents: OnceLock::new(),
            executor_admission: OnceLock::new(),
            strict_executor_writes: OnceLock::new(),
            collaboration_namespace: std::sync::OnceLock::new(),
            channel: Arc::new(Channel {
                raw_usage: Mutex::default(),
                observe_raw_provider_events: AtomicBool::new(false),
                state: Mutex::new(State {
                    timeout: None,
                    scope: Scope::Root,
                    revoked: false,
                    nonce: 0,
                    pending: HashMap::new(),
                    tools: None,
                    realtime_mcp_tools: None,
                }),
                sender,
                receiver: AsyncMutex::new(receiver),
                bytes: Arc::new(Semaphore::new(bytes)),
            }),
        }
    }

    pub(crate) fn raw_usage_active(&self) -> bool {
        self.channel
            .raw_usage
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .as_ref()
            .is_some_and(|link| link.is_active())
    }

    pub(crate) fn set_raw_usage_link(&self, link: codex_core::SdkRawUsageLink) {
        *self
            .channel
            .raw_usage
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = Some(link);
    }

    pub(crate) fn enable_raw_provider_events(&self) {
        self.channel
            .observe_raw_provider_events
            .store(true, Ordering::Release);
    }

    fn descendant(id: String, parent: &Control) -> Self {
        let incarnation = codex_protocol::ThreadId::new().to_string();
        let stopped = CancellationToken::new();
        Self {
            mcp_results: OnceLock::new(),
            profile_policies: AtomicU8::new(parent.profile_policies.load(Ordering::Acquire)),
            id,
            incarnation,
            root: false,
            agent_role: OnceLock::new(),
            native_depth: parent.native_depth.saturating_add(1),
            native_ancestor: Some((parent.id.clone(), parent.incarnation.clone())),
            stopped,
            creation_tracing: OnceLock::new(),
            direct_turn: Mutex::new(None),
            context_tools: OnceLock::new(),
            code_mode_sessions: Mutex::default(),
            pending_code_import: Mutex::default(),
            expiry_cleanup: TaskTracker::new(),
            expiry_cleanup_failed: AtomicBool::new(false),
            native_agents: parent.native_agents.clone(),
            executor_admission: if parent.native_agents.get().is_some() {
                parent.executor_admission.clone()
            } else {
                OnceLock::new()
            },
            strict_executor_writes: OnceLock::new(),
            collaboration_namespace: std::sync::OnceLock::new(),
            channel: parent.channel.clone(),
        }
    }

    pub(crate) fn has_unsettled_code_cells(self: &Arc<Self>) -> bool {
        // This includes an admitted start, a yielded execution, and an unread
        // native result. Even after CellClosed, a saved result still belongs to
        // this exact owner until Core consumes it. Completed/consumed entries
        // are removed from the same index by execute, wait or joined shutdown.
        let sessions: Vec<_> = self
            .code_mode_sessions
            .lock()
            .expect("code mode session links poisoned")
            .iter()
            .filter_map(Weak::upgrade)
            .collect();
        sessions.iter().any(|session| {
            session
                .cells
                .lock()
                .expect("interception cells poisoned")
                .values()
                .any(|cell| Arc::ptr_eq(&cell.control, self))
        })
    }

    /// Call after the selected turn(s) have settled, with SDK submissions still
    /// excluded. No model/tool callback is run by this administrative operation.
    pub(crate) async fn interrupt_code_mode_cells(
        self: &Arc<Self>,
        stopped_only: bool,
        expected_turn: Option<&str>,
    ) -> Result<()> {
        self.interrupt_exact_cells(stopped_only, expected_turn, None)
            .await?;
        self.join_expiry_cleanup().await
    }
    async fn interrupt_exact_cells(
        self: &Arc<Self>,
        stopped_only: bool,
        expected_turn: Option<&str>,
        exact_owner: Option<&Arc<DirectTurn>>,
    ) -> Result<()> {
        let sessions: Vec<_> = {
            let mut links = self
                .code_mode_sessions
                .lock()
                .expect("code mode session links poisoned");
            links.retain(|session| session.strong_count() != 0);
            links.iter().filter_map(Weak::upgrade).collect()
        };
        let mut failure = None;
        for session in sessions {
            if let Err(error) = session
                .interrupt_owned_cells(self, stopped_only, expected_turn, exact_owner)
                .await
            {
                // Best effort for other exact sessions, but never report a full
                // force stop if any native termination was not acknowledged.
                failure.get_or_insert(error);
            }
        }
        if let Some(error) = failure {
            anyhow::bail!(error);
        }
        Ok(())
    }

    pub(crate) fn enable(&self, timeout: f64, scope: Scope) -> Result<()> {
        let mut state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        ensure!(
            self.root,
            "Native interception supports explicit root sessions only"
        );
        ensure!(
            state.timeout.is_none() && !state.revoked,
            "Native interception already enabled or revoked"
        );
        ensure!(
            timeout.is_finite() && timeout > 0.0 && timeout <= 3600.0,
            "Native interception timeout must be in (0, 3600]"
        );
        state.timeout = Some(Duration::from_secs_f64(timeout));
        state.scope = scope;
        Ok(())
    }

    fn controls_environment_builtin(&self, name: &ToolName) -> bool {
        self.root
            && name.is_default_namespace()
            && matches!(
                name.name.as_str(),
                "exec_command" | "write_stdin" | "apply_patch" | "project_glob" | "project_grep"
            )
            && self
                .channel
                .state
                .lock()
                .expect("interception control poisoned")
                .tools
                .as_ref()
                .is_some_and(|tools| tools.contains_key(&name.clone().with_default_namespace()))
    }
    fn cell_policy_owner(
        self: &Arc<Self>,
        turn: &Arc<DirectTurn>,
        outer: &str,
    ) -> Result<Option<Arc<CellPolicyOwner>>, String> {
        let mut state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        let eligible = self.root
            && state.tools.as_ref().is_some_and(|tools| {
                tools.keys().any(|name| {
                    name.is_default_namespace()
                        && matches!(
                            name.name.as_str(),
                            "exec_command"
                                | "write_stdin"
                                | "apply_patch"
                                | "project_glob"
                                | "project_grep"
                        )
                })
            });
        if !eligible {
            return Ok(None);
        }
        if state.revoked || self.stopped.is_cancelled() || !turn.admits_policy() {
            return Err(FAILURE.into());
        }
        state.nonce = state.nonce.checked_add(1).ok_or(FAILURE)?;
        Ok(Some(Arc::new(CellPolicyOwner {
            id: state.nonce,
            control: self.clone(),
            turn: turn.clone(),
            outer_call_id: outer.into(),
            cell_id: OnceLock::new(),
        })))
    }
    fn controls_builtin_result(&self, name: &ToolName) -> bool {
        if !name.is_default_namespace()
            || !matches!(
                name.name.as_str(),
                "exec_command" | "write_stdin" | "apply_patch" | "project_glob" | "project_grep"
            )
        {
            return false;
        }
        self.channel
            .state
            .lock()
            .expect("interception control poisoned")
            .tools
            .as_ref()
            .and_then(|tools| tools.get(&name.clone().with_default_namespace()))
            .is_some_and(|(_, scope, result, _)| {
                *result && (self.root || *scope == Scope::ThreadTree)
            })
    }

    fn compile_rules(rules: Vec<ToolRule>) -> Result<Rules> {
        let mut tools = HashMap::new();
        for rule in rules {
            ensure!(
                !rule.name.is_empty()
                    && rule.timeout.is_none_or(|timeout| timeout.is_finite()
                        && timeout > 0.0
                        && timeout <= 3600.0),
                "Invalid native tool interception rule"
            );
            ensure!(
                tools
                    .insert(
                        ToolName::new(rule.namespace, rule.name).with_default_namespace(),
                        (
                            rule.timeout.map(Duration::from_secs_f64),
                            rule.scope,
                            rule.result,
                            rule.generation
                                .map(|value| value.parse::<u64>())
                                .transpose()?
                        ),
                    )
                    .is_none(),
                "Duplicate native tool interception selection"
            );
        }
        Ok(tools)
    }

    #[cfg(test)]
    fn configure_tools(&self, rules: Vec<ToolRule>) -> Result<()> {
        self.configure_tools_with_realtime(rules, None)
    }

    pub(crate) fn configure_tools_with_realtime(
        &self,
        rules: Vec<ToolRule>,
        realtime_mcp_tools: Option<Vec<ToolName>>,
    ) -> Result<()> {
        let tools = Self::compile_rules(rules)?;
        let mut state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        ensure!(
            self.root && state.timeout.is_some() && !state.revoked,
            "Native interception is not active"
        );
        state.tools = Some(tools);
        state.realtime_mcp_tools = realtime_mcp_tools;
        Ok(())
    }

    pub(crate) fn capture_mcp_source_for_binding(
        &self,
        binding: &codex_mcp::McpBinding,
        policy: &codex_extension_api::ToolPolicy,
    ) -> ProfileMcpSource {
        let mut source = self.capture_mcp_source();
        let Some(selectors) = &policy.allowed_mcp_tools else {
            return source;
        };
        let captured = source.policy.as_ref().expect("captured MCP policy");
        let mut rules = captured.rules.clone();
        let mut names = captured.names.clone();
        for (_, _, declared) in selectors {
            rules.remove(&declared.clone().with_default_namespace());
            names.remove(&declared.clone().with_default_namespace());
        }
        for tool in binding.tools() {
            let actual = tool.canonical_tool_name().with_default_namespace();
            names.insert(actual.clone());
            if let Some((_, _, declared)) = selectors.iter().find(|(server, raw, _)| {
                tool.server_name == *server && tool.tool.name.as_ref() == raw
            }) {
                if let Some(rule) = captured
                    .rules
                    .get(&declared.clone().with_default_namespace())
                {
                    rules.insert(actual, *rule);
                }
            }
        }
        source.policy = Some(Arc::new(ProfileMcpPolicy {
            realtime: captured.realtime,
            identities: ProfileMcpPolicy::identities(binding),
            names,
            rules,
        }));
        source
    }

    fn configured_in(&self, state: &State) -> bool {
        state.timeout.is_some() && (self.root || state.scope == Scope::ThreadTree)
    }

    pub(crate) fn enable_profile_policies(&self) {
        self.profile_policies.store(2, Ordering::Release);
    }

    /// Freeze only MCP policy; shared environment/child policies keep their own owner.
    pub(crate) fn capture_mcp_source(&self) -> ProfileMcpSource {
        self.profile_policies.fetch_max(1, Ordering::AcqRel);
        let state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        let rules: Rules = state
            .tools
            .as_ref()
            .into_iter()
            .flat_map(|rules| rules.iter())
            .filter(|(_, rule)| rule.3.is_some())
            .map(|(name, rule)| (name.clone(), *rule))
            .collect();
        drop(state);
        ProfileMcpSource {
            result_selection: self
                .mcp_results
                .get()
                .and_then(Weak::upgrade)
                .and_then(|results| results.capture_source_selection())
                .map(Arc::new),
            receipt: None,
            policy: Some(Arc::new(ProfileMcpPolicy {
                identities: HashMap::new(),
                names: rules.keys().cloned().collect(),
                rules,
                realtime: false,
            })),
        }
    }

    /// Add a predicate-only callback to each captured Realtime MCP tool.
    /// Existing before/approval rules retain their scope, generation, and deadline.
    pub(crate) fn capture_realtime_mcp_source(
        &self,
        mut source: ProfileMcpSource,
        generation: u64,
    ) -> ProfileMcpSource {
        let state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        if let (Some(names), Some(policy)) = (&state.realtime_mcp_tools, &mut source.policy) {
            let policy = Arc::make_mut(policy);
            if source.receipt.is_none() {
                policy.names.extend(names.iter().cloned());
            }
            policy.realtime = true;
            for name in &policy.names {
                policy.rules.entry(name.clone()).or_insert((
                    Some(Duration::from_secs(30)),
                    Scope::Root,
                    false,
                    Some(generation),
                ));
            }
        }
        source
    }

    fn configured(&self) -> bool {
        if self.profile_policies.load(Ordering::Acquire) != 0 {
            return true;
        }
        // Revocation must fail closed for an opted-in owner, never convert it
        // to a new opted-out owner while native work is being cancelled.
        self.configured_in(
            &self
                .channel
                .state
                .lock()
                .expect("interception control poisoned"),
        )
    }

    pub(crate) fn has_native_agents(&self) -> bool {
        self.native_agents.get().is_some()
    }

    // Boundary already selected the nearest live native ancestor, including
    // cold resumes. Never start another search here: an opted-out or revoked
    // live intermediate owner must not be skipped for a more permissive root.
    pub(crate) async fn native_agents_parent(
        &self,
        manager: &Weak<ThreadManager>,
    ) -> Option<Arc<CodexThread>> {
        if self.root || !self.has_native_agents() {
            return None;
        }
        let parent = async {
            let (id, incarnation) = self.native_ancestor.as_ref()?;
            let thread = manager
                .upgrade()?
                .get_thread(codex_core_api::ThreadId::from_string(id).ok()?)
                .await
                .ok()?;
            let source = thread.thread_extension_data().get::<Control>()?;
            let expected_policy = self.native_agents.get()?;
            let actual_policy = source.native_agents.get()?;
            let revoked = source
                .channel
                .state
                .lock()
                .expect("interception control poisoned")
                .revoked;
            (source.incarnation == *incarnation
                && !source.stopped.is_cancelled()
                && !self.stopped.is_cancelled()
                && !revoked
                && Arc::ptr_eq(expected_policy, actual_policy))
            .then_some(thread)
        }
        .await;
        if parent.is_none() {
            // No policy inheritance from a missing or replaced authority.
            // Deny dispatch, including MCP, before any unconfigured fallback.
            self.revoke();
        }
        parent
    }

    pub fn revoke(&self) {
        {
            let mut state = self
                .channel
                .state
                .lock()
                .expect("interception control poisoned");
            // Serialize stop against allocation and resolve. A child's stale
            // reply must be rejected even before its decide future is polled.
            self.stopped.cancel();
            if self.root {
                state.revoked = true;
                state.pending.retain(|_, pending| {
                    pending.reply.take();
                    // In-flight Python storage is still owned after revocation.
                    matches!(pending.payload, Payload::InFlight(_) | Payload::Callback)
                });
            }
        }
        if let Some(owner) = self
            .direct_turn
            .lock()
            .expect("interception turn poisoned")
            .take()
        {
            owner.cancelled.cancel();
        }
    }

    fn end_direct_turn(&self, store: &ExtensionData) {
        let Some(owner) = store.get::<DirectTurn>() else {
            return;
        };
        owner.cancelled.cancel();
        let mut current = self.direct_turn.lock().expect("interception turn poisoned");
        if current
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, &owner))
        {
            *current = None;
        }
    }

    pub async fn next_wire(&self, delivery: u64) -> Option<String> {
        let mut receiver = self.channel.receiver.lock().await;
        loop {
            match receiver.recv().await? {
                Message::CellOwnerRetired(id) => {
                    return Some(format!("{{\"cell_owner_retired\":{id}}}"));
                }
                Message::Cancel(nonce) => return Some(format!("{{\"cancel\":{nonce}}}")),
                Message::Payload(nonce) => {
                    let mut state = self
                        .channel
                        .state
                        .lock()
                        .expect("interception control poisoned");
                    if let Some(pending) = state.pending.get_mut(&nonce)
                        && matches!(pending.payload, Payload::Queued(_))
                    {
                        if let Payload::Queued(wire) =
                            std::mem::replace(&mut pending.payload, Payload::InFlight(delivery))
                        {
                            return Some(wire);
                        }
                    }
                    // Retired before transfer: the native queue no longer
                    // retains any request and cannot confer reply authority.
                }
            }
        }
    }

    #[cfg(test)]
    async fn next(&self) -> Option<Value> {
        self.next_wire(0)
            .await
            .map(|wire| serde_json::from_str(&wire).unwrap())
    }

    pub fn release(&self, nonce: u64) -> bool {
        let mut state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        let Some(pending) = state.pending.get_mut(&nonce) else {
            return false;
        };
        if !matches!(pending.payload, Payload::InFlight(_) | Payload::Callback) {
            return false;
        }
        pending.payload = Payload::Returned;
        if pending.reply.is_none() {
            state.pending.remove(&nonce);
        }
        true
    }

    // Unlike storage handoff, admission must still belong to a live caller.
    pub fn check(&self, nonce: u64) -> bool {
        let state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        !state.revoked
            && state.pending.get(&nonce).is_some_and(|pending| {
                pending.reply.is_some()
                    && pending.generation.is_none_or(|generation| {
                        if let Some(source) = &pending.profile_source {
                            source.policy.as_ref().is_some_and(|policy| {
                                policy.rules.values().any(|rule| rule.3 == Some(generation))
                            })
                        } else {
                            state.tools.as_ref().is_some_and(|rules| {
                                rules.values().any(|rule| rule.3 == Some(generation))
                            })
                        }
                    })
                    && !pending.stopped.is_cancelled()
                    && !pending.request_cancelled.is_cancelled()
                    && pending.admission.admits(pending.owner.as_ref())
                    && pending
                        .deadline
                        .is_none_or(|deadline| Instant::now() < deadline)
            })
    }

    // Storage handoff only. This never grants reply authority: a retired
    // sender remains retired even when Python must finish cancelling its task.
    pub fn handoff(&self, nonce: u64) -> bool {
        let mut state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        let Some(pending) = state.pending.get_mut(&nonce) else {
            return false;
        };
        if !matches!(pending.payload, Payload::InFlight(_)) {
            return false;
        }
        pending.payload = Payload::Callback;
        true
    }

    // The joined reader cannot still deliver these frames. A worker running a
    // sync callback has a separate physical owner and must keep its permit.
    pub fn reader_finished(&self) {
        let mut state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        state
            .pending
            .retain(|_, pending| !matches!(pending.payload, Payload::InFlight(_)));
    }

    // IPC joins the actual cancelled native read before abandoning its
    // delivery. Even cancellation between Rust return and Python conversion
    // cannot strand a transferred payload. Never apply to a different read.
    pub fn abandon(&self, delivery: u64) {
        let mut state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        state.pending.retain(
            |_, pending| !matches!(pending.payload, Payload::InFlight(id) if id == delivery),
        );
    }

    pub fn resolve(&self, nonce: u64, reply: &str) -> bool {
        let decision = serde_json::from_str::<Decision>(reply).unwrap_or(Decision::Deny);
        self.reply(nonce, Reply::Decision(decision))
    }

    pub(crate) fn capture_turn_stop(
        self: &Arc<Self>,
        expected_turn: &str,
    ) -> Option<CapturedTurnStop> {
        let current = self
            .direct_turn
            .lock()
            .expect("interception turn poisoned")
            .clone();
        let owner = current
            .filter(|owner| owner.id == expected_turn)
            .or_else(|| {
                // Core removes its turn store at terminal. Retained cells keep the
                // exact original owner alive; never substitute a successor's store.
                let sessions = self
                    .code_mode_sessions
                    .lock()
                    .expect("code mode session links poisoned");
                sessions
                    .iter()
                    .filter_map(Weak::upgrade)
                    .find_map(|session| {
                        session
                            .cells
                            .lock()
                            .expect("interception cells poisoned")
                            .values()
                            .find(|cell| {
                                Arc::ptr_eq(&cell.control, self) && cell.turn.id == expected_turn
                            })
                            .map(|cell| cell.turn.clone())
                    })
            })?;
        Some(CapturedTurnStop {
            control: self.clone(),
            owner,
        })
    }

    pub(crate) fn capture_held_stop(self: &Arc<Self>, nonce: u64) -> Result<CapturedTurnStop> {
        let state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        let pending = state
            .pending
            .get(&nonce)
            .ok_or_else(|| anyhow::anyhow!(FAILURE))?;
        ensure!(
            self.root && pending.held_for_stop && matches!(pending.payload, Payload::Callback),
            FAILURE
        );
        let owner = pending
            .owner
            .clone()
            .ok_or_else(|| anyhow::anyhow!(FAILURE))?;
        ensure!(owner.policy_stopped.load(Ordering::Acquire), FAILURE);
        Ok(CapturedTurnStop {
            control: self.clone(),
            owner,
        })
    }
    pub fn hold_for_stop(&self, nonce: u64) -> bool {
        self.reply(nonce, Reply::HoldForStop)
    }

    fn reply(&self, nonce: u64, reply: Reply) -> bool {
        let mut state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        let Some(pending) = state.pending.get_mut(&nonce) else {
            return false;
        };
        let deadline = pending.deadline;
        if pending.stopped.is_cancelled()
            || pending.request_cancelled.is_cancelled()
            || !pending.admission.admits(pending.owner.as_ref())
            || deadline.is_some_and(|deadline| Instant::now() >= deadline)
            || matches!(reply, Reply::HoldForStop)
                && (!matches!(pending.payload, Payload::Callback)
                    || pending.owner.is_none()
                    || pending.owner_binding
                    || !pending.admission.permits_stop())
        {
            return false;
        }
        let Some(tx) = pending.reply.take() else {
            return false;
        };
        let stopping = matches!(reply, Reply::HoldForStop);
        let accepted = tx.send(reply).is_ok();
        if accepted && stopping {
            pending.held_for_stop = true;
            // Effect admission and timeout resolution use this same channel
            // lock. Claiming this sender fences its entire captured turn before
            // another selected or unselected call may enter.
            pending
                .owner
                .as_ref()
                .expect("stop requires captured turn")
                .policy_stopped
                .store(true, Ordering::Release);
        }
        if matches!(pending.payload, Payload::Returned) {
            state.pending.remove(&nonce);
        }
        accepted
    }

    // The timer is eligible to expire only an unclaimed sender. If an on-time
    // reply already consumed it, the receiver must use that reply even if the
    // timer was selected before the receiver could be polled again.
    pub(crate) fn expire(self: &Arc<Self>, nonce: u64) {
        let mut state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        if let Some(pending) = state.pending.get_mut(&nonce)
            && pending.reply.take().is_some()
            && pending.result_policy
            && !pending.stopped.is_cancelled()
            && !pending.request_cancelled.is_cancelled()
            && let Some(owner) = pending.owner.clone()
            && let Some(issuer) = pending.issuer.upgrade()
        {
            issuer.stop_expired_owner(&owner, "Executor policy failed");
        }
    }

    // The caller holds the channel lock: publication, revoke and expiry-task
    // registration cannot pass this fence in a different order.
    fn stop_expired_owner(self: &Arc<Self>, owner: &Arc<DirectTurn>, message: &'static str) {
        if self.stopped.is_cancelled() || owner.policy_stopped.swap(true, Ordering::AcqRel) {
            return;
        }
        if let Some(receipt) = owner.sdk_admission.get().and_then(Weak::upgrade) {
            receipt.reject_policy(&self.id, message);
        }
        let control = self.clone();
        let owner = owner.clone();
        self.expiry_cleanup.spawn(async move {
            if control
                .interrupt_exact_cells(true, Some(&owner.id), Some(&owner))
                .await
                .is_err()
            {
                control.expiry_cleanup_failed.store(true, Ordering::Release);
            }
        });
    }

    async fn join_expiry_cleanup(&self) -> Result<()> {
        {
            let _state = self
                .channel
                .state
                .lock()
                .expect("interception control poisoned");
            self.expiry_cleanup.close();
        }
        self.expiry_cleanup.wait().await;
        ensure!(
            !self.expiry_cleanup_failed.load(Ordering::Acquire),
            "Native result policy cleanup failed"
        );
        Ok(())
    }

    pub(crate) fn admits_prepared_turn(
        &self,
        store: &ExtensionData,
        receipt: &Arc<crate::python_tools::TurnAdmission>,
    ) -> bool {
        let Some(owner) = store.get::<DirectTurn>() else {
            return false;
        };
        let current = self
            .direct_turn
            .lock()
            .expect("interception turn poisoned")
            .clone();
        current
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, &owner))
            && !owner.cancelled.is_cancelled()
            && owner
                .sdk_admission
                .get()
                .and_then(Weak::upgrade)
                .is_some_and(|captured| Arc::ptr_eq(&captured, receipt))
            && self.admit_model_request(&owner).is_ok()
    }

    fn admit_model_request(&self, owner: &DirectTurn) -> Result<(), String> {
        let state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        if state.revoked
            || self.stopped.is_cancelled()
            || owner.policy_stopped.load(Ordering::Acquire)
        {
            return Err(FAILURE.into());
        }
        Ok(())
    }

    fn publish_result<T>(
        &self,
        owner: &DirectTurn,
        token: Option<&CancellationToken>,
        publish: impl FnOnce() -> T,
    ) -> Result<T, String> {
        let state = self
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        // Stop claims and result publication share this mutex. Direct MCP passes
        // the turn token; yielded calls pass their independent request token.
        // Normal native terminal does not cancel a still-owned yielded request.
        if state.revoked
            || self.stopped.is_cancelled()
            || token.is_some_and(CancellationToken::is_cancelled)
            || owner.policy_stopped.load(Ordering::Acquire)
        {
            return Err(FAILURE.into());
        }
        Ok(publish())
    }

    // A retained input whose native consumer takes ownership also uses this
    // budget. Merge it into the complete notice rather than charging it twice.
    fn account<T: Serialize>(
        &self,
        value: &T,
        previous: Option<OwnedSemaphorePermit>,
    ) -> Result<OwnedSemaphorePermit, String> {
        let mut permit = previous.unwrap_or(
            self.channel
                .bytes
                .clone()
                .try_acquire_many_owned(0)
                .map_err(|_| FAILURE)?,
        );
        let have = permit.num_permits();
        let mut counter = Counter {
            bytes: 0,
            limit: self.channel.bytes.available_permits() + have,
        };
        serde_json::to_writer(&mut counter, value).map_err(|_| FAILURE)?;
        if counter.bytes > have {
            let extra = self
                .channel
                .bytes
                .clone()
                .try_acquire_many_owned((counter.bytes - have).try_into().map_err(|_| FAILURE)?)
                .map_err(|_| FAILURE)?;
            permit.merge(extra);
        } else if have > counter.bytes {
            drop(
                permit
                    .split(have - counter.bytes)
                    .expect("measured surplus"),
            );
        }
        Ok(permit)
    }

    async fn decide<T: DecisionRequest + Send>(
        self: &Arc<Self>,
        request: T,
        token: &CancellationToken,
    ) -> Result<Decision, String> {
        self.decide_retained(request, token, None, None).await
    }

    async fn decide_retained<T: DecisionRequest + Send>(
        self: &Arc<Self>,
        request: T,
        token: &CancellationToken,
        previous: Option<OwnedSemaphorePermit>,
        physical_release: Option<&mut Option<NoticeRelease>>,
    ) -> Result<Decision, String> {
        let profile_source = request.profile_source().cloned();
        let source_policy = profile_source
            .as_ref()
            .map(|source| source.policy.as_ref().ok_or(FAILURE))
            .transpose()?;
        let identity = request.tool_identity().map(|(namespace, name)| {
            ToolName::new(namespace.map(str::to_owned), name).with_default_namespace()
        });
        let source_rule = source_policy
            .and_then(|policy| identity.as_ref().and_then(|key| policy.rules.get(key)))
            .copied();
        let owner = request.owner().cloned();
        let admission = request.admission();
        let owner_binding = request.cell_owner_binding().is_some();
        let result_policy = request.result_policy();
        let realtime_mcp = !result_policy
            && source_rule.is_some()
            && source_policy.is_some_and(|policy| policy.realtime);
        let (nonce, deadline, mut rx) = {
            let mut state = self
                .channel
                .state
                .lock()
                .expect("interception control poisoned");
            if owner
                .as_ref()
                .is_some_and(|turn| turn.policy_stopped.load(Ordering::Acquire))
            {
                return Err(FAILURE.into());
            }
            if !self.configured_in(&state) && source_rule.is_none() {
                return Ok(Decision::Continue);
            }
            let (timeout, generation) = if owner_binding {
                (state.timeout, None)
            } else if state.tools.is_some() || source_policy.is_some() {
                let Some(key) = identity.as_ref() else {
                    return Ok(Decision::Continue);
                };
                let global = state
                    .tools
                    .as_ref()
                    .and_then(|rules| rules.get(key))
                    .copied();
                let rule = if source_policy.is_some_and(|policy| policy.names.contains(key))
                    || (source_policy.is_some() && global.is_some_and(|rule| rule.3.is_some()))
                {
                    source_rule
                } else {
                    global
                };
                let Some((timeout, scope, _, generation)) = rule else {
                    return Ok(Decision::Continue);
                };
                if (!self.root && scope != Scope::ThreadTree)
                    || (generation.is_some() && result_policy)
                {
                    return Ok(Decision::Continue);
                }
                (timeout, generation)
            } else {
                (state.timeout, None)
            };
            if state.revoked || self.stopped.is_cancelled() || state.pending.len() >= MAX_PENDING {
                return Err(FAILURE.into());
            }
            state.nonce = state.nonce.checked_add(1).ok_or(FAILURE)?;
            let nonce = state.nonce;
            let deadline = timeout.map(|timeout| Instant::now() + timeout);
            let (bytes, wire) = if let Some(cell) = request.cell_owner_binding() {
                let notice = CellOwnerNotice {
                    nonce,
                    cell_owner: CellOwnerMetadata {
                        owner_id: cell.id,
                        thread_id: &self.id,
                        turn_id: &cell.turn.id,
                        outer_call_id: &cell.outer_call_id,
                        cell_id: cell.cell_id.get().ok_or(FAILURE)?,
                    },
                };
                let bytes = self.account(&notice, previous)?;
                (bytes, serde_json::to_string(&notice).map_err(|_| FAILURE)?)
            } else {
                let notice = Notice {
                    mcp_identity: source_policy.and_then(|policy| {
                        identity
                            .as_ref()
                            .and_then(|name| policy.identities.get(name))
                    }),
                    profile_id: profile_source
                        .as_ref()
                        .and_then(|source| source.receipt.as_ref())
                        .map(|receipt| receipt.profile_id.as_str()),
                    profile_epoch: profile_source
                        .as_ref()
                        .and_then(|source| source.receipt.as_ref())
                        .map(|receipt| receipt.epoch),
                    generation,
                    realtime_mcp,
                    nonce,
                    request: Identified {
                        thread_id: &self.id,
                        agent_role: self.agent_role.get().and_then(|role| role.as_deref()),
                        instance: (!self.root).then_some(&self.incarnation),
                        request,
                    },
                };
                let bytes = self.account(&notice, previous)?;
                (bytes, serde_json::to_string(&notice).map_err(|_| FAILURE)?)
            };
            let (tx, rx) = oneshot::channel();
            let released = physical_release.map(|receipt| {
                let (released, returned) = oneshot::channel();
                *receipt = Some(returned);
                released
            });
            state.pending.insert(
                nonce,
                Pending {
                    profile_source: profile_source.clone(),
                    generation,
                    issuer: Arc::downgrade(self),
                    stopped: self.stopped.clone(),
                    request_cancelled: token.clone(),
                    owner: owner.clone(),
                    admission: admission.clone(),
                    owner_binding,
                    held_for_stop: false,
                    result_policy,
                    deadline,
                    reply: Some(tx),
                    payload: Payload::Queued(wire),
                    _bytes: bytes,
                    _released: released,
                },
            );
            if self.channel.sender.send(Message::Payload(nonce)).is_err() {
                state.pending.remove(&nonce);
                return Err(FAILURE.into());
            }
            (nonce, deadline, rx)
        };
        let _reservation = Reservation {
            control: self.clone(),
            nonce,
        };
        let reply = tokio::select! {
            biased;
            _ = self.stopped.cancelled() => return Err(FAILURE.into()),
            _ = token.cancelled() => return Err(FAILURE.into()),
            reply = &mut rx => reply,
            () = async {
                tokio::time::sleep_until(deadline.expect("finite deadline guard")).await;
            }, if deadline.is_some() => {
                self.expire(nonce);
                // expire and reply send or drop under the same lock. This
                // receive cannot wait on an unresolved application callback.
                rx.await
            },
        };
        match reply {
            Ok(Reply::HoldForStop) => {
                // An accepted stop waits for its native owner even when its
                // original deadline passed before this consumer resumed.
                tokio::select! {
                    _ = self.stopped.cancelled() => {},
                    _ = token.cancelled() => {},
                }
                Err(FAILURE.into())
            }
            reply => {
                // Resolve may have delivered and removed this reservation
                // before root revoke. Accept under the same lock as revoke,
                // including when this Control belongs to a still-live child.
                let state = self
                    .channel
                    .state
                    .lock()
                    .expect("interception control poisoned");
                match reply {
                    Ok(Reply::Decision(
                        decision @ (Decision::Continue | Decision::Replace { .. }),
                    )) if !state.revoked
                        && !owner
                            .as_ref()
                            .is_some_and(|turn| turn.policy_stopped.load(Ordering::Acquire)) =>
                    {
                        Ok(decision)
                    }
                    _ => Err(FAILURE.into()),
                }
            }
        }
    }
}

// Drop covers native cancellation even when the future is dropped from outside
// select!, plus timeouts and successful completion. Nonces are never reused.
struct Reservation {
    control: Arc<Control>,
    nonce: u64,
}
impl Drop for Reservation {
    fn drop(&mut self) {
        let mut state = self
            .control
            .channel
            .state
            .lock()
            .expect("interception control poisoned");
        if let Some(pending) = state.pending.get_mut(&self.nonce) {
            pending.reply.take();
            if !matches!(pending.payload, Payload::InFlight(_) | Payload::Callback) {
                state.pending.remove(&self.nonce);
            }
        }
        let _ = self
            .control
            .channel
            .sender
            .send(Message::Cancel(self.nonce));
    }
}

// Native dispatch runs lifecycle start and execute_owned construction on the
// issuing tool task. The captured future is polled by tracked startup; abort
// notification may run in the joining task.
// Only the short start-to-session handoff lives here. Cells retain their owner
// separately, never in this index. Register opt-out starts too so even
// reuse of a completed native task id cannot adopt a stale opted-in owner.
struct StartedExec {
    profile_source: Result<Option<Arc<ProfileMcpSource>>, String>,
    control: Arc<Control>,
    turn: Arc<DirectTurn>,
    model: Option<Arc<str>>,
    call_id: String,
    name: &'static str,
    rules: Option<crate::canonical_rules::CellImageOrigin>,
}

impl StartedExec {
    async fn edit_response(
        &self,
        mut response: RuntimeResponse,
        input: Value,
        outer_call_id: Option<&str>,
        bytes: OwnedSemaphorePermit,
    ) -> Result<RuntimeResponse, String> {
        let (cell_id, content_items) = match &mut response {
            // Native failures (including their partial output) are not successes.
            RuntimeResponse::Result {
                error_text: Some(_),
                ..
            } => return Ok(response),
            RuntimeResponse::Result {
                cell_id,
                content_items,
                ..
            }
            | RuntimeResponse::Yielded {
                cell_id,
                content_items,
                ..
            }
            | RuntimeResponse::Terminated {
                cell_id,
                content_items,
                ..
            } => (cell_id, content_items),
        };
        let tracing = codex_otel::sdk_trace::ContextLease::capture();
        let request = Request {
            tracing_context: tracing.as_ref().map(|context| context.token()),
            owner: Some(&self.turn),
            phase: "after",
            route: "code_mode",
            boundary: "cell_return",
            turn_id: &self.turn.id,
            model: self.model.as_deref(),
            call_id: Some(&self.call_id),
            outer_call_id,
            cell_id: Some(cell_id),
            runtime_tool_call_id: None,
            tool_name: Name {
                namespace: None,
                name: self.name,
            },
            tool_kind: if self.name == codex_code_mode::PUBLIC_TOOL_NAME {
                "freeform"
            } else {
                "function_json"
            },
            input: &input,
            result: &*content_items,
        };
        // A dropped observation revokes only its callback reservation. Native
        // preemption is a yield signal, never a cell-cancellation token.
        let token = CancellationToken::new();
        if let Decision::Replace { value } = self
            .control
            .decide_retained(request, &token, Some(bytes), None)
            .await?
        {
            *content_items = serde_json::from_value(value).map_err(|_| FAILURE.to_owned())?;
        }
        Ok(response)
    }
}

pub struct Boundary {
    expiry_cleanup_failed: AtomicBool,
    import_cleanup_failed: AtomicBool,
    restored_code: Mutex<HashMap<codex_protocol::ThreadId, Arc<codex_code_mode::CodeModeSnapshot>>>,
    starts: Mutex<HashMap<tokio::task::Id, StartedExec>>,
    numeric_forks: Mutex<HashMap<tokio::task::Id, NumericFork>>,
    threads: Mutex<HashMap<String, Weak<Control>>>,
    graph: Arc<crate::storage::MemoryAgentGraph>,
}

// One source read in the actual native spawn dispatch, never indexed by the
// model's path. Weak owners cannot keep an aborted turn or retired root alive.
struct NumericFork {
    control: Weak<Control>,
    turn: Weak<DirectTurn>,
    call_id: String,
}

impl Boundary {
    pub fn new(graph: Arc<crate::storage::MemoryAgentGraph>) -> Self {
        Self {
            import_cleanup_failed: AtomicBool::new(false),
            expiry_cleanup_failed: AtomicBool::new(false),
            restored_code: Mutex::default(),
            starts: Mutex::default(),
            numeric_forks: Mutex::default(),
            threads: Mutex::default(),
            graph,
        }
    }

    pub(crate) fn check_code_import_cleanup(&self) -> Result<()> {
        ensure!(
            !self.expiry_cleanup_failed.load(Ordering::Acquire),
            "Native result policy cleanup failed"
        );
        ensure!(
            !self.import_cleanup_failed.load(Ordering::Acquire),
            "Native CodeMode import cleanup failed"
        );
        Ok(())
    }

    pub(crate) fn stage_code_state(
        &self,
        id: codex_protocol::ThreadId,
        state: codex_code_mode::CodeModeSnapshot,
    ) {
        self.restored_code
            .lock()
            .expect("restored code state poisoned")
            .insert(id, Arc::new(state));
    }

    pub(crate) fn pending_code_state(
        &self,
        id: codex_protocol::ThreadId,
    ) -> Option<Arc<codex_code_mode::CodeModeSnapshot>> {
        self.restored_code
            .lock()
            .expect("restored code state poisoned")
            .get(&id)
            .cloned()
    }

    pub(crate) fn clear_code_state(&self, id: codex_protocol::ThreadId) {
        self.restored_code
            .lock()
            .expect("restored code state poisoned")
            .remove(&id);
    }

    async fn parent_control(&self, source: &SessionSource) -> Option<(Arc<Control>, usize)> {
        // Only ThreadSpawn supplies this identity. Fork history and other
        // internal SubAgent sources never authorize graph traversal.
        let mut id = source.parent_thread_id()?;
        let mut visited = HashSet::new();
        let mut distance = 1usize;
        while visited.insert(id) {
            let live = self
                .threads
                .lock()
                .expect("interception index poisoned")
                .get(&id.to_string())
                .and_then(Weak::upgrade);
            if let Some(live) = live {
                // Never skip an opted-out or revoked live owner to find a
                // different root. Its current choice governs descendants.
                let depth = live.native_depth.saturating_add(distance);
                return Some((live, depth));
            }
            // On cold V2 resume only the target may be loaded. The engine's
            // native graph retains intermediate ThreadSpawn edges after eviction.
            // No index lock is held while awaiting the graph's own lock.
            id = self.graph.edge(id).await?.parent;
            distance = distance.saturating_add(1);
        }
        None
    }

    fn start_exec(
        &self,
        control: Arc<Control>,
        turn: Arc<DirectTurn>,
        call_id: &str,
        name: &'static str,
        rules: Option<crate::canonical_rules::CellImageOrigin>,
        profile_source: Result<Option<Arc<ProfileMcpSource>>, String>,
    ) {
        // Snapshot before dispatch. A surviving cell keeps its issuing model
        // even if another generation or turn starts while the cell runs.
        let model = turn
            .model
            .lock()
            .expect("interception model poisoned")
            .clone();
        self.starts
            .lock()
            .expect("interception boundary poisoned")
            .insert(
                tokio::task::id(),
                StartedExec {
                    profile_source,
                    control,
                    turn,
                    model,
                    call_id: call_id.to_owned(),
                    name,
                    rules,
                },
            );
    }

    fn take_exec(&self, call_id: &str) -> Result<Option<StartedExec>, String> {
        let owner = self.take_start(codex_code_mode::PUBLIC_TOOL_NAME);
        match owner {
            Some(owner) if owner.call_id != call_id => Err(FAILURE.into()),
            // Opting out of result interception does not opt out of Session
            // force interruption. Preserve the exact native issuing Control.
            other => Ok(other),
        }
    }

    fn take_start(&self, name: &str) -> Option<StartedExec> {
        tokio::task::try_id().and_then(|id| {
            let mut starts = self.starts.lock().expect("interception boundary poisoned");
            // Core also calls terminate on interrupt without dispatching a wait
            // tool. It must never consume another task's unrelated handoff.
            if starts.get(&id).is_some_and(|owner| owner.name == name) {
                starts.remove(&id)
            } else {
                None
            }
        })
    }

    fn finish_exec(&self, thread_id: &str, turn_id: &str, call_id: &str) {
        // Normal completion has already consumed the handoff. Parse/availability
        // failure or abort can finish elsewhere; never use the finishing task id.
        self.starts
            .lock()
            .expect("interception boundary poisoned")
            .retain(|_, owner| {
                owner.control.id != thread_id
                    || owner.turn.id != turn_id
                    || owner.call_id != call_id
            });
    }

    pub(crate) fn take_numeric_fork_source(&self, thread_id: codex_protocol::ThreadId) -> bool {
        let Some(task) = tokio::task::try_id() else {
            return false;
        };
        let mut forks = self.numeric_forks.lock().expect("numeric forks poisoned");
        let Some(fork) = forks.get(&task) else {
            return false;
        };
        let (Some(control), Some(turn)) = (fork.control.upgrade(), fork.turn.upgrade()) else {
            forks.remove(&task);
            return false;
        };
        if control.id != thread_id.to_string() {
            return false;
        }
        let current = control
            .direct_turn
            .lock()
            .expect("interception turn poisoned");
        let active = !control.stopped.is_cancelled()
            && !turn.cancelled.is_cancelled()
            && current
                .as_ref()
                .is_some_and(|live| Arc::ptr_eq(live, &turn));
        forks.remove(&task);
        active
    }

    fn finish_numeric_forks(
        &self,
        control: &Arc<Control>,
        turn_id: Option<&str>,
        call_id: Option<&str>,
    ) {
        // Core may finish an aborted dispatch from a different task.
        self.numeric_forks
            .lock()
            .expect("numeric forks poisoned")
            .retain(|_, fork| {
                let (Some(owner), Some(turn)) = (fork.control.upgrade(), fork.turn.upgrade())
                else {
                    return false;
                };
                !Arc::ptr_eq(&owner, control)
                    || turn_id.is_some_and(|id| turn.id != id)
                    || call_id.is_some_and(|id| fork.call_id != id)
            });
    }
}

impl std::fmt::Debug for Boundary {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("NativeInterceptionBoundary")
            .finish_non_exhaustive()
    }
}

impl ThreadLifecycleContributor<Config> for Boundary {
    fn on_thread_execution_revoked(&self, thread_store: &ExtensionData) {
        if let Some(control) = thread_store.get::<Control>() {
            control.revoke();
        }
    }

    fn on_thread_start<'a>(
        &'a self,
        input: ThreadStartInput<'a, Config>,
    ) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            let creation_tracing = codex_otel::sdk_trace::CapturedContext::capture().context;
            let root = !matches!(
                input.session_source,
                SessionSource::Internal(_) | SessionSource::SubAgent(_)
            );
            // parent_thread_id is native and returns Some only for ThreadSpawn.
            // Start is awaited before the first turn, unlike UI creation notices.
            let parent = self.parent_control(input.session_source).await;
            // The child gets a fresh store. Carry the validated marker only
            // from live ancestry and for an executor Core selected for it.
            if !input.environments.is_empty()
                && parent.as_ref().is_some_and(|(owner, _)| {
                    !owner.stopped.is_cancelled() && owner.strict_executor_writes.get().is_some()
                })
            {
                input.thread_store.insert(StrictExecutorWrites::default());
            }
            let control = input.thread_store.get_or_init(|| {
                let id = input.thread_store.level_id().to_owned();
                match parent {
                    Some((parent, depth)) => {
                        let mut control = Control::descendant(id, &parent);
                        control.native_depth = depth;
                        control
                    }
                    None => Control::new(id, root),
                }
            });
            if input
                .thread_store
                .get::<crate::agent_profiles::OpeningProfiles>()
                .is_some()
            {
                control.enable_profile_policies();
            }
            if let Some(step) = input.spawning_step_store {
                match capture_mcp_source(Some(step)) {
                    Ok(Some(source)) => {
                        input.thread_store.insert((*source).clone());
                        if let Some(selection) = step.get::<crate::mcp_result_control::Selection>()
                        {
                            input.thread_store.insert((*selection).clone());
                        }
                    }
                    Ok(None) if control.profile_policies.load(Ordering::Acquire) == 2 => {
                        control.revoke()
                    }
                    Err(_) => control.revoke(),
                    _ => {}
                }
            } else if !root && control.profile_policies.load(Ordering::Acquire) == 2 {
                // Runtime-only spawn authority is absent on an unvalidated managed restart.
                control.revoke();
            }
            let _ = control.creation_tracing.set(creation_tracing);
            if input.thread_store.get::<StrictExecutorWrites>().is_some() {
                let _ = control.strict_executor_writes.set(());
            }
            let _ = control
                .agent_role
                .set(input.session_source.get_agent_role());
            if let Some(policy) = input.thread_store.get::<crate::native_agents::Policy>() {
                if let Some(allowed) = input.thread_store.get::<codex_extension_api::ToolPolicy>() {
                    let _ = policy.allowed.set(allowed);
                }
                let _ = control.native_agents.set(policy);
            }
            // Custom roles own their developer instructions even when their text matches.
            if !root
                && let Some(policy) = control.native_agents.get()
                && !control
                    .agent_role
                    .get()
                    .and_then(Option::as_ref)
                    .is_some_and(|role| policy.roles.contains(role))
            {
                crate::application_instructions::retain_child_files(
                    input.thread_store,
                    input.config,
                    &policy.instruction_files,
                );
            }
            if root
                && let Some(policy) = input.thread_store.get::<ToolPolicy>()
                && let Some(admission) = PinnedExecutorAdmission::from_policy(&policy, input.config)
            {
                let _ = control.executor_admission.set(admission);
            }
            let _ = control
                .collaboration_namespace
                .set(input.config.multi_agent_v2.tool_namespace.clone());
            self.threads
                .lock()
                .expect("interception index poisoned")
                .insert(control.id.clone(), Arc::downgrade(&control));
        })
    }

    fn on_thread_stop<'a>(&'a self, input: ThreadStopInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(control) = input.thread_store.get::<Control>() {
                {
                    let mut threads = self.threads.lock().expect("interception index poisoned");
                    if threads
                        .get(&control.id)
                        .is_some_and(|current| Weak::ptr_eq(current, &Arc::downgrade(&control)))
                    {
                        threads.remove(&control.id);
                    }
                }
                control.revoke();
                if control.join_expiry_cleanup().await.is_err() {
                    self.expiry_cleanup_failed.store(true, Ordering::Release);
                }
                if control.close_pending_code_import(None).await.is_err() {
                    self.import_cleanup_failed.store(true, Ordering::Release);
                }
                self.finish_numeric_forks(&control, None, None);
                self.starts
                    .lock()
                    .expect("interception boundary poisoned")
                    .retain(|_, owner| !Arc::ptr_eq(&owner.control, &control));
            }
        })
    }
}

impl TurnLifecycleContributor for Boundary {
    fn before_turn_complete<'a>(
        &'a self,
        input: codex_extension_api::TurnCompletionInput<'a>,
    ) -> ExtensionFuture<'a, std::result::Result<Option<String>, String>> {
        Box::pin(async move {
            let Some(tools) = input.thread_store.get::<crate::python_tools::PythonTools>() else {
                return Ok(None);
            };
            tools
                .before_complete(input)
                .await
                .map_err(|_| "Completion callback failed".to_owned())
        })
    }

    fn on_turn_start<'a>(&'a self, input: TurnStartInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(control) = input.thread_store.get::<Control>() {
                if control.native_ancestor.is_some()
                    && !control.stopped.is_cancelled()
                    && let Some(capture) = control
                        .channel
                        .raw_usage
                        .lock()
                        .unwrap_or_else(|error| error.into_inner())
                        .as_ref()
                {
                    capture.attach(input.thread_store);
                }
                if !control.stopped.is_cancelled()
                    && control
                        .channel
                        .observe_raw_provider_events
                        .load(Ordering::Acquire)
                {
                    input
                        .thread_store
                        .insert(codex_core::ObserveRawProviderEvents);
                }
                let owner = input.turn_store.get_or_init(|| DirectTurn {
                    id: input.turn_id.to_owned(),
                    model: Mutex::new(None),
                    sdk_admission: OnceLock::new(),
                    cancelled: CancellationToken::new(),
                    policy_stopped: AtomicBool::new(false),
                    mcp_calls: Mutex::default(),
                });
                let mut current = control
                    .direct_turn
                    .lock()
                    .expect("interception turn poisoned");
                if let Some(previous) = current.replace(owner.clone())
                    && !Arc::ptr_eq(&previous, &owner)
                {
                    previous.cancelled.cancel();
                }
            }
        })
    }
    fn on_turn_stop<'a>(&'a self, input: TurnStopInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(control) = input.thread_store.get::<Control>() {
                control.end_direct_turn(input.turn_store);
                if let Some(owner) = input.turn_store.get::<DirectTurn>() {
                    if control
                        .close_pending_code_import(Some(&owner))
                        .await
                        .is_err()
                    {
                        self.import_cleanup_failed.store(true, Ordering::Release);
                    }
                    self.finish_numeric_forks(&control, Some(&owner.id), None);
                }
            }
        })
    }
    fn on_turn_abort<'a>(&'a self, input: TurnAbortInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(control) = input.thread_store.get::<Control>() {
                control.end_direct_turn(input.turn_store);
                if let Some(owner) = input.turn_store.get::<DirectTurn>() {
                    if control
                        .close_pending_code_import(Some(&owner))
                        .await
                        .is_err()
                    {
                        self.import_cleanup_failed.store(true, Ordering::Release);
                    }
                    self.finish_numeric_forks(&control, Some(&owner.id), None);
                }
            }
        })
    }
}

impl ModelRequestContributor for Boundary {
    fn admit_tools_only_call(
        &self,
        input: codex_extension_api::ToolsOnlyCallInput,
    ) -> futures_util::future::BoxFuture<'_, Result<ResponseItem, String>> {
        Box::pin(async move {
            let control = self
                .threads
                .lock()
                .expect("interception index poisoned")
                .get(input.admission.thread_id())
                .and_then(Weak::upgrade)
                .ok_or_else(|| FAILURE.to_owned())?;
            let owner = input
                .admission
                .turn_store()
                .get::<DirectTurn>()
                .ok_or_else(|| FAILURE.to_owned())?;
            if !owner.admits_policy()
                || owner.cancelled.is_cancelled()
                || input.cancellation.is_cancelled()
            {
                return Err(FAILURE.to_owned());
            }
            control.admit_model_request(&owner)?;
            let model: Arc<str> = input.model.into();
            *owner.model.lock().expect("interception model poisoned") = Some(model.clone());
            let request = DirectRequest {
                tool_admission: Some(input.admission),
                control,
                owner,
                model,
            };
            request
                .admit_tools_only(input.item, &input.cancellation)
                .await
        })
    }

    fn customizes_compaction(&self, thread_id: &str) -> bool {
        self.context_tools(thread_id)
            .is_some_and(|tools| tools.customizes_compaction())
    }

    fn before_compact<'a>(
        &'a self,
        input: codex_extension_api::CompactionInput<'a>,
    ) -> ExtensionFuture<'a, std::result::Result<Option<String>, String>> {
        Box::pin(async move {
            let tools = self
                .context_tools(input.thread_id)
                .ok_or_else(|| "Expired compaction owner".to_owned())?;
            tools
                .before_compact(input)
                .await
                .map_err(|_| "Compaction callback failed".to_owned())
        })
    }

    fn transforms_context(&self, thread_id: &str) -> bool {
        self.context_tools(thread_id)
            .is_some_and(|tools| tools.transforms_context())
    }

    fn transform_context<'a>(
        &'a self,
        input: codex_extension_api::ModelContextInput<'a>,
    ) -> ExtensionFuture<'a, std::result::Result<(), String>> {
        Box::pin(async move {
            let tools = self
                .context_tools(input.thread_id)
                .ok_or_else(|| "Expired model context owner".to_owned())?;
            tools
                .transform_context(input)
                .await
                .map_err(|_| "Model context transformation failed".to_owned())
        })
    }

    fn request(&self, input: ModelRequestInput<'_>) -> Option<Box<dyn ModelResponseInterceptor>> {
        let control = self
            .threads
            .lock()
            .expect("interception index poisoned")
            .get(input.thread_id)?
            .upgrade()?;
        let owner = control
            .direct_turn
            .lock()
            .expect("interception turn poisoned")
            .clone();
        // Observe every managed generation, even with no executor/MCP/child
        // tools. A completed model item alone is never an admission boundary.
        if input.kind == ModelRequestKind::Generation
            && let Some(receipt) = owner
                .as_ref()
                .and_then(|turn| turn.sdk_admission.get())
                .and_then(Weak::upgrade)
        {
            receipt.generation();
        }
        Self::response_interceptor(control, owner, input)
    }
}

impl Boundary {
    fn context_tools(&self, thread_id: &str) -> Option<Arc<crate::python_tools::PythonTools>> {
        self.threads
            .lock()
            .expect("interception index poisoned")
            .get(thread_id)?
            .upgrade()?
            .context_tools
            .get()?
            .upgrade()
    }

    pub(crate) fn response_admission(
        &self,
        thread: &str,
    ) -> Option<Arc<crate::python_tools::TurnAdmission>> {
        let control = self
            .threads
            .lock()
            .expect("interception index poisoned")
            .get(thread)?
            .upgrade()?;
        let turn = control
            .direct_turn
            .lock()
            .expect("interception turn poisoned")
            .clone()?;
        turn.sdk_admission.get()?.upgrade()
    }

    fn response_interceptor(
        control: Arc<Control>,
        owner: Option<Arc<DirectTurn>>,
        input: ModelRequestInput<'_>,
    ) -> Option<Box<dyn ModelResponseInterceptor>> {
        if input.kind != ModelRequestKind::Generation {
            return owner.map(|owner| {
                Box::new(AdmissionOnly { control, owner }) as Box<dyn ModelResponseInterceptor>
            });
        }
        let owner = {
            let state = control
                .channel
                .state
                .lock()
                .expect("interception control poisoned");
            if !control.configured_in(&state)
                && control.profile_policies.load(Ordering::Acquire) == 0
                && control.executor_admission.get().is_none()
                && control.native_agents.get().is_none()
            {
                return owner.map(|owner| {
                    Box::new(AdmissionOnly {
                        control: control.clone(),
                        owner,
                    }) as Box<dyn ModelResponseInterceptor>
                });
            }
            if state.revoked || control.stopped.is_cancelled() {
                return Some(Box::new(RevokedRequest));
            }
            match owner {
                Some(owner) if !owner.policy_stopped.load(Ordering::Acquire) => owner,
                Some(_) => return Some(Box::new(RevokedRequest)),
                None => return Some(Box::new(RevokedRequest)),
            }
        };
        let model: Arc<str> = input.model.into();
        *owner.model.lock().expect("interception model poisoned") = Some(model.clone());
        Some(Box::new(DirectRequest {
            tool_admission: None,
            control,
            owner,
            model,
        }))
    }
}

// Unconfigured managed requests need only retain exact expiry admission.
struct AdmissionOnly {
    control: Arc<Control>,
    owner: Arc<DirectTurn>,
}
impl ModelResponseInterceptor for AdmissionOnly {
    fn admit_request(&self) -> Result<(), String> {
        self.control.admit_model_request(&self.owner)
    }
    fn intercept(self: Box<Self>, upstream: ModelResponseStream) -> ModelResponseStream {
        upstream
    }
}

// Revocation is permanent for a configured owner. A later generation must
// not reinterpret a failed reader as an unconfigured tool policy.
struct RevokedRequest;

impl ModelResponseInterceptor for RevokedRequest {
    fn admit_request(&self) -> Result<(), String> {
        Err(FAILURE.into())
    }
    fn intercept(self: Box<Self>, upstream: ModelResponseStream) -> ModelResponseStream {
        drop(upstream);
        Box::pin(futures_util::stream::once(async { Err(rejected()) }))
    }
}

struct DirectRequest {
    tool_admission: Option<Arc<dyn ModelToolAdmission>>,
    control: Arc<Control>,
    owner: Arc<DirectTurn>,
    model: Arc<str>,
}

// Pinned validation for the public, managed ExecutorTools catalog. Core's
// parsers accept some values excluded by its advertised schemas (including
// null optional strings). The extension API exposes no current spec or
// per-call rejection hook. Tests compare these complete rules with the actual
// wire schemas. Core still owns execution and its stricter numeric parsing.
#[derive(Clone)]
struct PinnedExecutorAdmission {
    exec_command: bool,
    write_stdin: bool,
    login: bool,
}

impl PinnedExecutorAdmission {
    fn from_policy(policy: &ToolPolicy, config: &Config) -> Option<Self> {
        if !policy.require_managed_sandbox
            || !policy.require_unified_exec
            || policy.expose_additional_permissions
            || policy.allowed_tools.is_none()
        {
            return None;
        }
        let admission = Self {
            exec_command: policy.allows(&ToolName::plain("exec_command")),
            write_stdin: policy.allows(&ToolName::plain("write_stdin")),
            // The SDK's single remote environment uses this same permission
            // snapshot. StableEnvironmentTools advertises login regardless.
            login: config.permissions.allow_login_shell
                || config
                    .features
                    .enabled(codex_core_api::Feature::StableEnvironmentTools),
        };
        (admission.exec_command || admission.write_stdin).then_some(admission)
    }

    fn valid_arguments(&self, name: &str, arguments: &str) -> bool {
        let Ok(Value::Object(object)) = serde_json::from_str::<Value>(arguments) else {
            return false;
        };
        let required = if name == "exec_command" {
            "cmd"
        } else {
            "session_id"
        };
        object.contains_key(required)
            && object
                .iter()
                .all(|(key, value)| match (name, key.as_str()) {
                    ("exec_command", "cmd" | "workdir" | "shell" | "justification")
                    | ("write_stdin", "chars") => value.is_string(),
                    ("exec_command", "tty") => value.is_boolean(),
                    ("exec_command", "login") => self.login && value.is_boolean(),
                    ("exec_command" | "write_stdin", "yield_time_ms" | "max_output_tokens")
                    | ("write_stdin", "session_id") => value.is_number(),
                    ("exec_command", "prefix_rule") => value
                        .as_array()
                        .is_some_and(|values| values.iter().all(Value::is_string)),
                    ("exec_command", "sandbox_permissions") => {
                        matches!(value.as_str(), Some("use_default" | "require_escalated"))
                    }
                    _ => false,
                })
    }

    fn check(&self, item: &ResponseItem) -> Result<(), ModelResponseError> {
        let ResponseItem::FunctionCall {
            name,
            namespace,
            arguments,
            call_id,
            ..
        } = item
        else {
            return Ok(());
        };
        if !matches!(namespace.as_deref(), None | Some("") | Some("functions")) {
            return Ok(());
        }
        if !(name == "exec_command" && self.exec_command
            || name == "write_stdin" && self.write_stdin)
        {
            return Ok(());
        }
        if !self.valid_arguments(name, arguments) {
            return Err(ModelResponseError::InvalidRequest {
                // Never reflect model-controlled names, keys, arguments or IDs.
                // A caller can correlate its own raw provider record by this ID
                // digest. The rejected item is not an accepted history record.
                message: format!(
                    "Native executor rejected invalid arguments before dispatch: tool={name}; call_id_sha256={:x}",
                    Sha256::digest(call_id.as_bytes()),
                ),
            });
        }
        Ok(())
    }
}

fn rejected() -> ModelResponseError {
    // Nonretryable, no callback/model payload reflected in errors. Core's
    // mapper can continue polling after Err, so only OUR denial ends the stream.
    ModelResponseError::InvalidRequest {
        message: FAILURE.into(),
    }
}

impl DirectRequest {
    async fn admit_tools_only(
        &self,
        item: ResponseItem,
        cancellation: &CancellationToken,
    ) -> Result<ResponseItem, String> {
        if cancellation.is_cancelled() || self.owner.cancelled.is_cancelled() {
            return Err(FAILURE.to_owned());
        }
        let mut release = None;
        let result = {
            let before = self.before_scoped(item, None, cancellation, &mut release);
            tokio::pin!(before);
            tokio::select! {biased;
                _=cancellation.cancelled()=>Err(rejected()),
                _=self.owner.cancelled.cancelled()=>Err(rejected()),
                result=&mut before=>result,
            }
        };
        // Fencing the nonce only prevents publication. The charged notice owns
        // the callback until release(), including cancellation and timeout.
        if let Some(released) = release {
            let _ = released.await;
        }
        let (item, _) = result.map_err(|_| FAILURE.to_owned())?;
        if self.owner.cancelled.is_cancelled() {
            return Err(FAILURE.to_owned());
        }
        self.control
            .publish_result(&self.owner, Some(cancellation), || item)
    }

    async fn before(
        &self,
        item: ResponseItem,
        previous: Option<OwnedSemaphorePermit>,
    ) -> Result<(ResponseItem, bool, Option<NoticeRelease>), ModelResponseError> {
        let mut release = None;
        let (item, changed) = self
            .before_scoped(item, previous, &self.owner.cancelled, &mut release)
            .await?;
        Ok((item, changed, release))
    }

    async fn before_scoped(
        &self,
        mut item: ResponseItem,
        previous: Option<OwnedSemaphorePermit>,
        cancellation: &CancellationToken,
        physical_release: &mut Option<NoticeRelease>,
    ) -> Result<(ResponseItem, bool), ModelResponseError> {
        if matches!(
            &item,
            ResponseItem::FunctionCall { .. }
                | ResponseItem::CustomToolCall { .. }
                | ResponseItem::ToolSearchCall { .. }
        ) {
            let admission = self.tool_admission.as_ref().ok_or_else(rejected)?;
            if admission.thread_id() != self.control.id
                || !admission
                    .turn_store()
                    .get::<DirectTurn>()
                    .is_some_and(|owner| Arc::ptr_eq(&owner, &self.owner))
            {
                return Err(rejected());
            }
            if !admission.admits(&item) {
                // Keep Core's ordinary unsupported/kind-mismatch handling, without
                // delivering an unadmitted call to any public policy callback.
                return Ok((item, false));
            }
        }
        if let Some(policy) = self.control.native_agents.get() {
            if matches!(
                &item,
                ResponseItem::FunctionCall { .. }
                    | ResponseItem::CustomToolCall { .. }
                    | ResponseItem::ToolSearchCall { .. }
            ) {
                let state = self
                    .control
                    .channel
                    .state
                    .lock()
                    .expect("interception control poisoned");
                if state.revoked
                    || self.control.stopped.is_cancelled()
                    || self.owner.cancelled.is_cancelled()
                    || !policy.admits(self.control.native_depth, &item)
                {
                    return Err(rejected());
                }
            }
        }
        if !self.control.configured() {
            if let Some(admission) = self.control.executor_admission.get() {
                admission.check(&item)?;
            }
            return Ok((item, false));
        }
        let (call_id, name, kind, input) = match &item {
            ResponseItem::FunctionCall {
                call_id,
                name,
                namespace,
                arguments,
                ..
            } => (
                call_id,
                Name {
                    namespace: namespace.as_deref(),
                    name,
                },
                "function_json",
                Input::Text(arguments),
            ),
            ResponseItem::CustomToolCall {
                call_id,
                name,
                namespace,
                input,
                ..
            } => (
                call_id,
                Name {
                    namespace: namespace.as_deref(),
                    name,
                },
                "freeform",
                Input::Text(input),
            ),
            ResponseItem::ToolSearchCall {
                call_id: Some(call_id),
                execution,
                arguments,
                ..
            } if execution == "client" && SearchToolCallParams::deserialize(arguments).is_ok() => (
                call_id,
                Name {
                    namespace: None,
                    name: "tool_search",
                },
                "tool_search",
                Input::Json(arguments),
            ),
            // Matches only actual router input shapes. Server search, hosted
            // web/image, missing ids and malformed search are not client calls.
            _ => return Ok((item, false)),
        };
        let tracing = codex_otel::sdk_trace::ContextLease::capture();
        let request = Request {
            tracing_context: tracing.as_ref().map(|context| context.token()),
            owner: Some(&self.owner),
            phase: "before",
            route: "direct",
            boundary: "pre_dispatch",
            turn_id: &self.owner.id,
            model: Some(&self.model),
            call_id: Some(call_id),
            outer_call_id: None,
            cell_id: None,
            runtime_tool_call_id: None,
            tool_name: name,
            tool_kind: kind,
            input,
            result: (),
        };
        let source = capture_mcp_source(
            self.tool_admission
                .as_ref()
                .and_then(|admission| admission.step_store()),
        )
        .map_err(|_| rejected())?;
        let decision = self
            .control
            .decide_retained(
                ProfiledRequest { request, source },
                cancellation,
                previous,
                Some(physical_release),
            )
            .await
            .map_err(|_| rejected())?;
        if cancellation.is_cancelled()
            || self.owner.cancelled.is_cancelled()
            || self.owner.policy_stopped.load(Ordering::Acquire)
        {
            return Err(rejected());
        }
        let mut changed = false;
        if let Decision::Replace { value } = decision {
            changed = match &mut item {
                ResponseItem::FunctionCall {
                    arguments,
                    encrypted_function_args,
                    namespace,
                    name,
                    ..
                } if value.is_object() => {
                    // Core's router additionally treats collaboration messages as
                    // encrypted unless the provider explicitly marked ALL args
                    // plaintext (Some([])). None is not equivalent to Some([]).
                    let encrypted_message = namespace.as_deref() == Some("collaboration")
                        && matches!(
                            name.as_str(),
                            "spawn_agent" | "send_message" | "followup_task"
                        )
                        && !encrypted_function_args.as_ref().is_some_and(Vec::is_empty);
                    let fields = encrypted_function_args.as_deref().unwrap_or_default();
                    if encrypted_message || !fields.is_empty() {
                        let original: Value =
                            serde_json::from_str(arguments).map_err(|_| rejected())?;
                        if !original.is_object()
                            || fields
                                .iter()
                                .map(String::as_str)
                                .chain(encrypted_message.then_some("message"))
                                .any(|field| original.get(field) != value.get(field))
                        {
                            return Err(rejected());
                        }
                    }
                    let changed =
                        serde_json::from_str::<Value>(arguments).ok().as_ref() != Some(&value);
                    if changed {
                        *arguments = value.to_string();
                    }
                    changed
                }
                ResponseItem::CustomToolCall { input, .. } => {
                    let replacement = value.as_str().ok_or_else(rejected)?;
                    let changed = input != replacement;
                    if changed {
                        *input = replacement.to_owned();
                    }
                    changed
                }
                ResponseItem::ToolSearchCall { arguments, .. } => {
                    serde_json::from_value::<SearchToolCallParams>(value.clone())
                        .map_err(|_| rejected())?;
                    let changed = *arguments != value;
                    *arguments = value;
                    changed
                }
                _ => return Err(rejected()),
            };
            if changed {
                // The rewritten item is local content, not the provider item
                // identified by its original ID. Keep call_id for execution
                // and results; let Core assign its normal local history ID.
                // Core caches this intercepted event before assigning that ID,
                // so its next request cannot reuse the provider's stale history.
                item.set_id(None);
            }
        }
        if let Some(admission) = self.control.executor_admission.get() {
            admission.check(&item)?;
        }
        if let Some(policy) = self.control.native_agents.get()
            && !policy.admits(self.control.native_depth, &item)
        {
            return Err(rejected());
        }
        Ok((item, changed))
    }
}

impl ModelResponseInterceptor for DirectRequest {
    fn admit_request(&self) -> Result<(), String> {
        self.control.admit_model_request(&self.owner)
    }
    fn intercept_with_tool_admission(
        mut self: Box<Self>,
        stream: ModelResponseStream,
        admission: Option<Arc<dyn ModelToolAdmission>>,
    ) -> ModelResponseStream {
        self.tool_admission = admission;
        self.intercept(stream)
    }

    fn intercept(self: Box<Self>, stream: ModelResponseStream) -> ModelResponseStream {
        Box::pin(stream::unfold(
            (
                stream,
                self,
                false,
                false,
                VecDeque::<(ResponseItem, bool, OwnedSemaphorePermit)>::new(),
            ),
            |(mut upstream, request, failed, mut active, mut pending)| async move {
                if failed {
                    return None;
                }
                loop {
                    // Core has one active display item, cleared on any emitted
                    // Done. Do not let its ID be borrowed by a rewritten call.
                    // Drain before polling again and keep ALL following calls in
                    // original order, including calls outside the policy scope.
                    if !active && let Some((item, decided, permit)) = pending.pop_front() {
                        let result = if decided {
                            let result = request
                                .control
                                .publish_result(
                                    &request.owner,
                                    Some(&request.owner.cancelled),
                                    || ResponseEvent::OutputItemDone(item),
                                )
                                .map_err(|_| rejected());
                            drop(permit);
                            result
                        } else {
                            request
                                .before(item, Some(permit))
                                .await
                                .map(|(item, _, _)| ResponseEvent::OutputItemDone(item))
                        };
                        let failed = result.is_err();
                        return Some((result, (upstream, request, failed, active, pending)));
                    }
                    let next = match upstream.next().await {
                        Some(next) => next,
                        None if pending.is_empty() => return None,
                        None => {
                            return Some((
                                Err(rejected()),
                                (upstream, request, true, active, pending),
                            ));
                        }
                    };
                    let (event, failed) = match next {
                        Ok(ResponseEvent::OutputItemAdded(item)) => {
                            // The same shapes for which Core can start a
                            // non-tool active item (parsing can still decline).
                            if matches!(
                                &item,
                                ResponseItem::Message { .. }
                                    | ResponseItem::Reasoning { .. }
                                    | ResponseItem::WebSearchCall { .. }
                            ) {
                                active = true;
                            }
                            (Ok(ResponseEvent::OutputItemAdded(item)), false)
                        }
                        Ok(ResponseEvent::OutputItemDone(item)) => {
                            let following_call = !pending.is_empty()
                                && matches!(
                                    &item,
                                    ResponseItem::FunctionCall { .. }
                                        | ResponseItem::CustomToolCall { .. }
                                        | ResponseItem::ToolSearchCall { .. }
                                        | ResponseItem::LocalShellCall { .. }
                                );
                            let result = if following_call {
                                // Admission is deferred too, never performed
                                // twice or out of order.
                                Ok((item, false, None))
                            } else {
                                request.before(item, None).await
                            };
                            match result {
                                Ok((item, changed, physical_release))
                                    if following_call || (changed && active) =>
                                {
                                    // Only a first already-edited overlapping
                                    // call can still have its notice held by
                                    // Python. Never charge the two sequential
                                    // owners at once, or free Python's bytes at
                                    // logical resolve. Genuine overflow after
                                    // physical release still rejects normally.
                                    if let Some(returned) = physical_release {
                                        // The decision met its deadline in
                                        // reply(). This wait only joins the
                                        // physical owner; stop still bounds it.
                                        let released = tokio::select! {
                                            biased;
                                            _ = request.control.stopped.cancelled() => false,
                                            _ = request.owner.cancelled.cancelled() => false,
                                            _ = returned => true,
                                        };
                                        if !released {
                                            return Some((
                                                Err(rejected()),
                                                (upstream, request, true, active, pending),
                                            ));
                                        }
                                    }
                                    let permit = (pending.len() < MAX_PENDING)
                                        .then(|| request.control.account(&item, None))
                                        .and_then(Result::ok);
                                    match permit {
                                        Some(permit) => {
                                            pending.push_back((item, !following_call, permit));
                                            continue;
                                        }
                                        None => (Err(rejected()), true),
                                    }
                                }
                                Ok((item, _, _)) => {
                                    active = false;
                                    (Ok(ResponseEvent::OutputItemDone(item)), false)
                                }
                                Err(err) => (Err(err), true),
                            }
                        }
                        // Never fabricate a terminal or publish queued calls
                        // from an unclosed overlap. Ordinary provider errors
                        // retain their original stream semantics.
                        Ok(ResponseEvent::Completed { .. }) if !pending.is_empty() => {
                            (Err(rejected()), true)
                        }
                        Err(err) => (Err(err), !pending.is_empty()),
                        other => (other, false),
                    };
                    return Some((event, (upstream, request, failed, active, pending)));
                }
            },
        ))
    }
}

impl ToolLifecycleContributor for Boundary {
    fn controls_builtin_result(&self, thread: &ExtensionData, name: &ToolName) -> bool {
        thread
            .get::<Control>()
            .is_some_and(|control| control.controls_builtin_result(name))
    }

    fn on_builtin_tool_result<'a>(
        &'a self,
        input: BuiltinToolResultInput<'a>,
    ) -> ExtensionFuture<'a, Result<Value, ()>> {
        Box::pin(async move {
            let control = input.thread_store.get::<Control>().ok_or(())?;
            let owner = input.turn_store.get::<DirectTurn>().ok_or(())?;
            if owner.id != input.turn_id || !control.controls_builtin_result(input.tool_name) {
                return Err(());
            }
            let (route, cell_id, runtime_id) = match &input.source {
                codex_tools::ToolCallSource::CodeMode {
                    cell_id,
                    runtime_tool_call_id,
                } => (
                    "code_mode",
                    Some(CellId::new(cell_id.clone())),
                    Some(runtime_tool_call_id.as_str()),
                ),
                _ => ("direct", None, None),
            };
            let (kind, arguments) = match input.payload {
                ToolPayload::Function { arguments } => ("function_json", arguments.as_str()),
                ToolPayload::Custom { input } => ("freeform", input.as_str()),
                _ => return Err(()),
            };
            if route == "code_mode" && input.outer_call_id.is_none() {
                return Err(());
            }
            let tracing = codex_otel::sdk_trace::ContextLease::capture();
            let request = Request {
                tracing_context: tracing.as_ref().map(|context| context.token()),
                owner: Some(&owner),
                phase: "after",
                route,
                boundary: "executor_return",
                turn_id: &owner.id,
                model: Some(input.model),
                call_id: (route == "direct").then_some(input.call_id),
                outer_call_id: input.outer_call_id,
                cell_id: cell_id.as_ref(),
                runtime_tool_call_id: runtime_id,
                tool_name: Name {
                    namespace: input.tool_name.namespace.as_deref(),
                    name: &input.tool_name.name,
                },
                tool_kind: kind,
                input: arguments,
                result: input.result,
            };
            // A normal turn end cancels its direct token, while a yielded
            // nested call retains its own native invocation cancellation.
            let token = CancellationToken::new();
            let decision = tokio::select! {
                biased;
                _ = input.cancelled => return Err(()),
                result = async {
                    if control.root && route == "code_mode" {
                        control.decide(BuiltinResult { request }, &token).await
                    } else { control.decide(request, &token).await }
                } => result.map_err(|_| ())?,
            };
            let value = match decision {
                Decision::Replace { value } => value,
                _ => return Err(()),
            };
            control
                .publish_result(&owner, Some(&token), || value)
                .map_err(|_| ())
        })
    }

    fn on_tool_start<'a>(&'a self, input: ToolStartInput<'a>) -> ToolLifecycleFuture<'a> {
        Box::pin(async move {
            if let (Some(control), Some(turn)) = (
                input.thread_store.get::<Control>(),
                input.turn_store.get::<DirectTurn>(),
            ) {
                let stopped = {
                    let _state = control
                        .channel
                        .state
                        .lock()
                        .expect("interception control poisoned");
                    turn.policy_stopped.load(Ordering::Acquire)
                };
                if stopped {
                    // Returning from this awaited hook admits Core's handler.
                    // Only native dispatch cancellation may retire a stopped
                    // call; a terminal notification must never release it.
                    std::future::pending::<()>().await;
                }
            }
            // Finalized by Core after pre-tool hooks, on the native dispatch
            // task. A V1 fork_context or MCP tool never opts into this scope.
            if input.mcp_tool.is_none()
                && input.tool_name.name == "spawn_agent"
                && let ToolPayload::Function { arguments } = input.payload
                && let Ok(value) = serde_json::from_str::<Value>(arguments)
                && value["fork_context"].is_null()
                && value["fork_turns"]
                    .as_str()
                    .and_then(|mode| mode.trim().parse::<usize>().ok())
                    .is_some_and(|n| n > 0)
                && let Some(control) = input.thread_store.get::<Control>()
                && control
                    .collaboration_namespace
                    .get()
                    .is_some_and(|namespace| {
                        namespace.as_deref() == input.tool_name.namespace.as_deref()
                    })
                && let Some(turn) = input.turn_store.get::<DirectTurn>()
                && let Some(task) = tokio::task::try_id()
            {
                self.numeric_forks
                    .lock()
                    .expect("numeric forks poisoned")
                    .insert(
                        task,
                        NumericFork {
                            control: Arc::downgrade(&control),
                            turn: Arc::downgrade(&turn),
                            call_id: input.call_id.to_owned(),
                        },
                    );
            }
            if input.source == ToolCallSource::Direct
                && input.mcp_tool.is_some()
                && input
                    .thread_store
                    .get::<Control>()
                    .is_some_and(|control| control.configured())
                && let Some(owner) = input.turn_store.get::<DirectTurn>()
                && let Some(dispatch) = tokio::task::try_id()
            {
                owner
                    .mcp_calls
                    .lock()
                    .expect("MCP dispatch sources poisoned")
                    .insert(dispatch);
            }
            if input.source == ToolCallSource::Direct
                && input.tool_name.is_default_namespace()
                && matches!(
                    input.tool_name.name.as_str(),
                    codex_code_mode::PUBLIC_TOOL_NAME | codex_code_mode::WAIT_TOOL_NAME
                )
            {
                if let Some(control) = input.thread_store.get::<Control>()
                    && let Some(turn) = input.turn_store.get::<DirectTurn>()
                {
                    let (name, rules) = if input.tool_name.name == codex_code_mode::PUBLIC_TOOL_NAME
                    {
                        (
                            codex_code_mode::PUBLIC_TOOL_NAME,
                            input
                                .thread_store
                                .get::<crate::canonical_rules::Rules>()
                                .and_then(|rules| rules.cell_image_origin(input.turn_id)),
                        )
                    } else {
                        (codex_code_mode::WAIT_TOOL_NAME, None)
                    };
                    self.start_exec(
                        control,
                        turn,
                        input.call_id,
                        name,
                        rules,
                        capture_mcp_source(input.step_store),
                    );
                }
            }
        })
    }
    fn on_tool_finish<'a>(&'a self, input: ToolFinishInput<'a>) -> ToolLifecycleFuture<'a> {
        Box::pin(async move {
            if let Some(control) = input.thread_store.get::<Control>() {
                self.finish_numeric_forks(&control, Some(input.turn_id), Some(input.call_id));
            }
            if input.source == ToolCallSource::Direct
                && let Some(owner) = input.turn_store.get::<DirectTurn>()
                && let Some(dispatch) = tokio::task::try_id()
            {
                owner
                    .mcp_calls
                    .lock()
                    .expect("MCP dispatch sources poisoned")
                    .remove(&dispatch);
            }
            if input.source == ToolCallSource::Direct
                && input.tool_name.is_default_namespace()
                && matches!(
                    input.tool_name.name.as_str(),
                    codex_code_mode::PUBLIC_TOOL_NAME | codex_code_mode::WAIT_TOOL_NAME
                )
            {
                self.finish_exec(input.thread_store.level_id(), input.turn_id, input.call_id);
            }
        })
    }

    fn on_mcp_tool_result<'a>(&'a self, input: McpToolResultInput<'a>) -> ToolLifecycleFuture<'a> {
        Box::pin(async move {
            let Some(owner) = input.turn_store.get::<DirectTurn>() else {
                return;
            };
            let Some(dispatch) = tokio::task::try_id() else {
                return;
            };
            if !owner
                .mcp_calls
                .lock()
                .expect("MCP dispatch sources poisoned")
                .remove(&dispatch)
            {
                return;
            }
            // Preserve the dedicated editor's existing ownership and failure
            // semantics when callers have already selected it for this thread.
            if input
                .thread_store
                .get::<crate::mcp_result_control::McpResultControl>()
                .is_some_and(|control| {
                    let tool = input.mcp_tool.tool_info();
                    control.owns_result(
                        input.thread_store,
                        input.turn_store,
                        input.step_store,
                        &tool.server_name,
                        &tool.tool.name,
                        matches!(
                            input.mcp_tool.source(),
                            codex_extension_api::McpToolSource::Config
                        ),
                    )
                })
            {
                return;
            }
            let Some(control) = input.thread_store.get::<Control>() else {
                return;
            };
            let original = std::mem::replace(input.result, crate::mcp_result_control::withheld());
            let tool = input.mcp_tool.tool_info();
            let tracing = codex_otel::sdk_trace::ContextLease::capture();
            let request = Request {
                tracing_context: tracing.as_ref().map(|context| context.token()),
                owner: Some(&owner),
                phase: "after",
                route: "mcp",
                boundary: "executor_return",
                turn_id: input.turn_id,
                model: None,
                call_id: Some(input.call_id),
                outer_call_id: None,
                cell_id: None,
                runtime_tool_call_id: None,
                tool_name: Name {
                    namespace: Some(&tool.callable_namespace),
                    name: &tool.callable_name,
                },
                tool_kind: "function_json",
                input: Input::JsonText(input.arguments),
                result: &original,
            };
            let Ok(source) = capture_mcp_source(input.step_store) else {
                return;
            };
            let edited = match control
                .decide(ProfiledRequest { request, source }, &owner.cancelled)
                .await
            {
                Ok(Decision::Continue) => Some(original),
                Ok(Decision::Replace { value }) => {
                    crate::mcp_result_control::validated(&value.to_string())
                }
                _ => None,
            };
            if let Some(edited) = edited {
                let _ = control.publish_result(&owner, Some(&owner.cancelled), || {
                    *input.result = edited;
                });
            }
        })
    }
}

pub struct Provider {
    pub inner: Arc<dyn CodeModeSessionProvider>,
    pub boundary: Arc<Boundary>,
}
impl CodeModeSessionProvider for Provider {
    fn create_session_for_thread(
        &self,
        id: codex_protocol::ThreadId,
    ) -> CodeModeSessionProviderFuture<'_> {
        self.create_session_for_thread_with_reference_profile(
            id,
            codex_protocol::provider_reference_profile::ProviderImageReferencePolicy::PrepareInline,
        )
    }

    fn create_session_for_thread_with_reference_profile(
        &self,
        id: codex_protocol::ThreadId,
        profile: codex_protocol::provider_reference_profile::ProviderImageReferencePolicy,
    ) -> CodeModeSessionProviderFuture<'_> {
        Box::pin(async move {
            let Some(state) = self.boundary.pending_code_state(id) else {
                let session = self
                    .inner
                    .create_session_for_thread_with_reference_profile(id, profile)
                    .await?;
                return Ok(Session::new(session, self.boundary.clone()) as Arc<dyn CodeModeSession>);
            };
            let control = self
                .boundary
                .threads
                .lock()
                .expect("interception index poisoned")
                .get(&id.to_string())
                .and_then(Weak::upgrade)
                .ok_or("code-mode import requires a current thread owner")?;
            control
                .close_pending_code_import(None)
                .await
                .map_err(|_| "Native CodeMode import cleanup failed")?;
            let session = self
                .inner
                .create_session_for_thread_with_reference_profile(id, profile)
                .await?;
            let turn = control
                .direct_turn
                .lock()
                .expect("interception turn poisoned")
                .clone();
            let pending = Arc::new(PendingCodeImport {
                session: session.clone(),
                turn: turn.as_ref().map(Arc::downgrade),
            });
            let admitted = {
                let mut current = control
                    .pending_code_import
                    .lock()
                    .expect("pending code import poisoned");
                if control.stopped.is_cancelled()
                    || turn
                        .as_ref()
                        .is_some_and(|owner| owner.cancelled.is_cancelled())
                    || current.is_some()
                {
                    false
                } else {
                    *current = Some(pending.clone());
                    true
                }
            };
            if !admitted {
                session.shutdown().await?;
                return Err("code-mode import owner is no longer active".to_string());
            }
            if let Err(error) = session.import_state(state.as_ref().clone()).await {
                if control
                    .close_pending_code_import(turn.as_ref())
                    .await
                    .is_err()
                {
                    self.boundary
                        .import_cleanup_failed
                        .store(true, Ordering::Release);
                }
                return Err(error);
            }
            let current = {
                let mut import = control
                    .pending_code_import
                    .lock()
                    .expect("pending code import poisoned");
                let mut snapshots = self
                    .boundary
                    .restored_code
                    .lock()
                    .expect("restored code state poisoned");
                let current = import
                    .as_ref()
                    .is_some_and(|owner| Arc::ptr_eq(owner, &pending))
                    && snapshots
                        .get(&id)
                        .is_some_and(|current| Arc::ptr_eq(current, &state))
                    && !control.stopped.is_cancelled()
                    && !turn
                        .as_ref()
                        .is_some_and(|owner| owner.cancelled.is_cancelled());
                if current {
                    import.take();
                    snapshots.remove(&id);
                }
                current
            };
            if !current {
                if control
                    .close_pending_code_import(turn.as_ref())
                    .await
                    .is_err()
                {
                    self.boundary
                        .import_cleanup_failed
                        .store(true, Ordering::Release);
                }
                return Err("code-mode import owner changed".to_string());
            }
            Ok(Session::new(session, self.boundary.clone()) as Arc<dyn CodeModeSession>)
        })
    }

    fn availability(&self) -> Result<(), String> {
        self.inner.availability()
    }
    fn create_session(&self) -> CodeModeSessionProviderFuture<'_> {
        Box::pin(async move {
            Ok(
                Session::new(self.inner.create_session().await?, self.boundary.clone())
                    as Arc<dyn CodeModeSession>,
            )
        })
    }
    fn create_session_with_limits<'a>(
        &'a self,
        limits: CodeModeSessionCellExecutionLimits,
    ) -> CodeModeSessionProviderFuture<'a> {
        Box::pin(async move {
            Ok(Session::new(
                self.inner.create_session_with_limits(limits).await?,
                self.boundary.clone(),
            ) as Arc<dyn CodeModeSession>)
        })
    }
}
struct Session {
    inner: Arc<dyn CodeModeSession>,
    boundary: Arc<Boundary>,
    self_link: Weak<Session>,
    // One owner from native start through the last observation. A starting
    // future may retain its delegate only until it is admitted, stopped, or
    // closed. Yielded/completed entries never retain a StepContext.
    cells: Arc<Mutex<HashMap<CellKey, Arc<Cell>>>>,
}

#[derive(Clone, PartialEq, Eq, Hash)]
enum CellKey {
    Starting(codex_protocol::ThreadId),
    Started(CellId),
}

struct Cell {
    control: Arc<Control>,
    turn: Arc<DirectTurn>,
    live: Arc<AtomicBool>,
    // An originally opted-out or rules-only cell cannot acquire a later policy.
    interception_origin: Option<String>,
    operation: AsyncMutex<CellOperation>,
}

enum CellOperation {
    // Poll by borrow, never take this future before it resolves. If Core drops
    // the execute caller, a scope stop can still join its actual host start.
    Starting(CodeModeSessionResultFuture<'static, StartedCell>),
    Running,
    Saved(RuntimeResponse),
    Consumed,
}

#[derive(Serialize)]
struct WaitInput<'a> {
    cell_id: &'a CellId,
    #[serde(skip_serializing_if = "Option::is_none")]
    yield_time_ms: Option<u64>,
    terminate: bool,
}

impl Session {
    async fn execute_with_owner(
        &self,
        request: ExecuteRequest,
        delegate: Arc<dyn CodeModeSessionDelegate>,
        preempt: Option<CancellationToken>,
        owner: Option<StartedExec>,
    ) -> Result<StartedCell, String> {
        let Some(mut owner) = owner else {
            return self.inner.execute(request, delegate, preempt).await;
        };
        // Resolve captured source errors before admitting native code or a backend call.
        let profile_source = owner.profile_source.clone()?;
        let configured = owner.control.configured();
        let checked = configured || owner.rules.is_some();
        let accounted = if configured {
            let bytes = owner.control.account(&request.source, None)?;
            Some((json!(request.source), bytes))
        } else {
            None
        };
        // Host closure may happen before the caller receives the ID.
        let live = Arc::new(AtomicBool::new(true));
        let cell_owner = owner
            .control
            .cell_policy_owner(&owner.turn, &request.tool_call_id)?;
        let delegate = Arc::new(Delegate {
            profile_source,
            cell_owner: Mutex::new(cell_owner.clone()),
            inner: delegate,
            live: live.clone(),
            control: configured.then(|| owner.control.clone()),
            native_control: (checked && owner.control.native_agents.get().is_some())
                .then(|| owner.control.clone()),
            turn: owner.turn.clone(),
            model: owner.model.clone(),
            outer_call_id: request.tool_call_id.clone(),
            rules: owner.rules.take(),
        });
        {
            let mut links = owner
                .control
                .code_mode_sessions
                .lock()
                .expect("code mode session links poisoned");
            links.retain(|session| session.strong_count() != 0);
            if !links
                .iter()
                .any(|session| Weak::ptr_eq(session, &self.self_link))
            {
                links.push(self.self_link.clone());
            }
        }
        let inner = self.inner.clone();
        let start = Box::pin(async move { inner.execute(request, delegate, preempt).await });
        let key = CellKey::Starting(codex_protocol::ThreadId::new());
        let cell = Arc::new(Cell {
            control: owner.control.clone(),
            turn: owner.turn.clone(),
            live,
            interception_origin: configured.then(|| owner.call_id.clone()),
            operation: AsyncMutex::new(CellOperation::Starting(start)),
        });
        self.cells
            .lock()
            .expect("interception cells poisoned")
            .insert(key.clone(), cell.clone());
        let mut operation = cell.operation.lock().await;
        let CellOperation::Starting(start) = &mut *operation else {
            return Err("Code mode execution stopped before it was admitted".into());
        };
        let result = start.as_mut().await;
        *operation = CellOperation::Running;
        let started = match result {
            Ok(started) => started,
            Err(error) => {
                *operation = CellOperation::Consumed;
                self.remove_cell(&key, &cell);
                return Err(error);
            }
        };
        let cell_id = started.cell_id.clone();
        let key = {
            let mut cells = self.cells.lock().expect("interception cells poisoned");
            if !cells
                .get(&key)
                .is_some_and(|current| Arc::ptr_eq(current, &cell))
            {
                *operation = CellOperation::Consumed;
                return Err("Code mode session closed during execute".into());
            }
            cells.remove(&key);
            let key = CellKey::Started(cell_id.clone());
            cells.insert(key.clone(), cell.clone());
            key
        };
        drop(operation);
        let cells = self.cells.clone();
        // Core must mark the cell ready before polling nested dispatch.
        Ok(StartedCell::from_future(cell_id, async move {
            let response = started.initial_response().await?;
            if let Some(cell_owner) = &cell_owner {
                cell_owner
                    .bind(match &key {
                        CellKey::Started(id) => id,
                        _ => return Err(FAILURE.into()),
                    })
                    .await?;
            }
            if !matches!(&response, RuntimeResponse::Yielded { .. }) {
                let mut operation = cell.operation.lock().await;
                *operation = CellOperation::Consumed;
                let mut cells = cells.lock().expect("interception cells poisoned");
                if cells
                    .get(&key)
                    .is_some_and(|current| Arc::ptr_eq(current, &cell))
                {
                    cells.remove(&key);
                }
            }
            match accounted {
                Some((input, bytes)) => owner.edit_response(response, input, None, bytes).await,
                None => Ok(response),
            }
        }))
    }

    fn new(inner: Arc<dyn CodeModeSession>, boundary: Arc<Boundary>) -> Arc<Self> {
        Arc::new_cyclic(|self_link| Self {
            inner,
            boundary,
            self_link: self_link.clone(),
            cells: Arc::default(),
        })
    }

    fn remove_cell(&self, key: &CellKey, cell: &Arc<Cell>) {
        let mut cells = self.cells.lock().expect("interception cells poisoned");
        if cells
            .get(key)
            .is_some_and(|current| Arc::ptr_eq(current, cell))
        {
            cells.remove(key);
        }
    }

    // Caller holds the cell operation until its genuine terminate ACK arrives.
    // Core also calls terminate directly during task abort and discards that
    // outcome: preserve a completed, unread Result for a later genuine wait.
    async fn terminate_cell(
        &self,
        key: &CellKey,
        cell: &Arc<Cell>,
        operation: &mut CellOperation,
        observation: CodeModeSessionResultFuture<'_, WaitOutcome>,
    ) -> Result<WaitOutcome, String> {
        if let CellOperation::Saved(result) = operation {
            return Ok(WaitOutcome::LiveCell(result.clone()));
        }
        let outcome = observation.await?;
        match &outcome {
            WaitOutcome::LiveCell(result @ RuntimeResponse::Result { .. }) => {
                *operation = CellOperation::Saved(result.clone());
            }
            WaitOutcome::LiveCell(RuntimeResponse::Yielded { .. }) => {
                return Err("Code mode termination yielded instead of stopping".to_owned());
            }
            _ => {
                *operation = CellOperation::Consumed;
                self.remove_cell(key, cell);
            }
        }
        Ok(outcome)
    }

    async fn interrupt_owned_cells(
        &self,
        control: &Arc<Control>,
        stopped_only: bool,
        expected_turn: Option<&str>,
        exact_owner: Option<&Arc<DirectTurn>>,
    ) -> Result<(), String> {
        let selected: Vec<_> = self
            .cells
            .lock()
            .expect("interception cells poisoned")
            .values()
            .filter(|cell| {
                Arc::ptr_eq(&cell.control, control)
                    && (!stopped_only || cell.turn.policy_stopped.load(Ordering::Acquire))
                    && expected_turn.is_none_or(|id| cell.turn.id == id)
                    && exact_owner.is_none_or(|owner| Arc::ptr_eq(owner, &cell.turn))
            })
            .cloned()
            .collect();
        let mut failure = None;
        for cell in selected {
            // A successor's passive wait may hold operation while this original
            // invocation awaits HoldForStop. Signal exact Started termination
            // first so the wait can settle; Starting keeps its old join path.
            let eager_key = if exact_owner.is_some() {
                self.cells
                    .lock()
                    .expect("interception cells poisoned")
                    .iter()
                    .find(|(_, current)| Arc::ptr_eq(current, &cell))
                    .and_then(|(key, _)| match key {
                        CellKey::Started(id) => Some((key.clone(), id.clone())),
                        _ => None,
                    })
            } else {
                None
            };
            let eager = if let Some((key, id)) = eager_key {
                Some((key, self.inner.terminate(id).await))
            } else {
                None
            };
            let mut operation = cell.operation.lock().await;
            // Execute may have changed Starting to Started while this sweep
            // waited. Only the sole map's current identity is authoritative.
            let key = self
                .cells
                .lock()
                .expect("interception cells poisoned")
                .iter()
                .find(|(_, current)| Arc::ptr_eq(current, &cell))
                .map(|(key, _)| key.clone());
            let Some(key) = key else {
                continue;
            };
            if let Some((target, outcome)) = eager {
                // A concurrent observer may already have consumed this Arc.
                // Never reinsert it or replace an earlier saved unread result.
                if key != target {
                    continue;
                }
                if matches!(
                    *operation,
                    CellOperation::Saved(_) | CellOperation::Consumed
                ) {
                    continue;
                }
                match outcome {
                    Ok(WaitOutcome::LiveCell(result @ RuntimeResponse::Result { .. })) => {
                        *operation = CellOperation::Saved(result);
                    }
                    Ok(WaitOutcome::LiveCell(RuntimeResponse::Yielded { .. })) => {
                        failure.get_or_insert_with(|| {
                            "Code mode termination yielded instead of stopping".into()
                        });
                    }
                    Ok(_) => {
                        *operation = CellOperation::Consumed;
                        self.remove_cell(&key, &cell);
                    }
                    Err(error) => {
                        failure.get_or_insert(error);
                    }
                }
                continue;
            }
            // This is after the lock: CellClosed may arrive before another
            // termination caller has received its acknowledgment.
            if let CellOperation::Starting(start) = &mut *operation {
                let result = start.as_mut().await;
                match result {
                    Ok(started) => {
                        // No caller received an ID, so a result has no public
                        // reader. Keep it indexed until termination is joined.
                        let new_key = CellKey::Started(started.cell_id.clone());
                        {
                            let mut cells = self.cells.lock().expect("interception cells poisoned");
                            if !cells
                                .get(&key)
                                .is_some_and(|current| Arc::ptr_eq(current, &cell))
                            {
                                // Only joined shutdown can remove Starting
                                // while this cell's operation lock is held.
                                *operation = CellOperation::Consumed;
                                continue;
                            }
                            cells.remove(&key);
                            cells.insert(new_key.clone(), cell.clone());
                        }
                        *operation = CellOperation::Running;
                        match self.inner.terminate(started.cell_id).await {
                            Ok(WaitOutcome::LiveCell(RuntimeResponse::Yielded { .. })) => {
                                failure.get_or_insert_with(|| {
                                    "Code mode termination yielded instead of stopping".to_owned()
                                });
                            }
                            Ok(_) => {
                                *operation = CellOperation::Consumed;
                                self.remove_cell(&new_key, &cell);
                            }
                            Err(error) => {
                                failure.get_or_insert(error);
                            }
                        }
                    }
                    Err(error) => {
                        *operation = CellOperation::Consumed;
                        self.remove_cell(&key, &cell);
                        failure.get_or_insert(error);
                    }
                }
                continue;
            }
            let CellKey::Started(id) = &key else {
                continue;
            };
            if !matches!(*operation, CellOperation::Running) || !cell.live.load(Ordering::Acquire) {
                continue;
            }
            if let Err(error) = self
                .terminate_cell(
                    &key,
                    &cell,
                    &mut operation,
                    self.inner.terminate(id.clone()),
                )
                .await
            {
                failure.get_or_insert(error);
            }
        }
        match failure {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }

    async fn observe_wait(
        &self,
        cell_id: CellId,
        yield_time_ms: Option<u64>,
        terminate: bool,
        observation: CodeModeSessionResultFuture<'_, WaitOutcome>,
    ) -> Result<WaitOutcome, String> {
        let current = self.boundary.take_start(codex_code_mode::WAIT_TOOL_NAME);
        let key = CellKey::Started(cell_id.clone());
        let cell = self
            .cells
            .lock()
            .expect("interception cells poisoned")
            .get(&key)
            .cloned();
        let Some(cell) = cell else {
            return observation.await;
        };
        let mut operation = cell.operation.lock().await;
        if terminate && current.is_none() {
            return self
                .terminate_cell(&key, &cell, &mut operation, observation)
                .await;
        }
        let mut outcome = if let CellOperation::Saved(result) = &*operation {
            WaitOutcome::LiveCell(result.clone())
        } else {
            observation.await?
        };
        if !matches!(
            &outcome,
            WaitOutcome::LiveCell(RuntimeResponse::Yielded { .. })
        ) {
            *operation = CellOperation::Consumed;
            self.remove_cell(&key, &cell);
        }
        // Python policies may be indefinite; native abort drops the observation
        // before stop sweeps, so release the per-cell lock before any policy.
        drop(operation);
        if let (Some(current), Some(exec_id)) = (current, &cell.interception_origin) {
            if Arc::ptr_eq(&current.control, &cell.control) {
                if let WaitOutcome::LiveCell(response) = outcome {
                    let wait = WaitInput {
                        cell_id: &cell_id,
                        yield_time_ms,
                        terminate,
                    };
                    let bytes = current.control.account(&wait, None)?;
                    let input = Value::String(
                        serde_json::to_value(&wait)
                            .map_err(|_| FAILURE)?
                            .to_string(),
                    );
                    outcome = WaitOutcome::LiveCell(
                        current
                            .edit_response(response, input, Some(exec_id), bytes)
                            .await?,
                    );
                }
            }
        }
        Ok(outcome)
    }
}

impl CodeModeSession for Session {
    fn export_state(&self) -> CodeModeSessionResultFuture<'_, codex_code_mode::CodeModeSnapshot> {
        Box::pin(async move {
            let cells = self
                .cells
                .lock()
                .expect("code-mode cells poisoned")
                .values()
                .cloned()
                .collect::<Vec<_>>();
            for cell in cells {
                let operation = cell
                    .operation
                    .try_lock()
                    .map_err(|_| "code-mode observation is active")?;
                if matches!(
                    *operation,
                    CellOperation::Starting(_) | CellOperation::Saved(_)
                ) {
                    return Err("code-mode observation is unsettled".to_string());
                }
            }
            self.inner.export_state().await
        })
    }
    fn import_state(
        &self,
        state: codex_code_mode::CodeModeSnapshot,
    ) -> CodeModeSessionResultFuture<'_, ()> {
        self.inner.import_state(state)
    }

    fn execute<'a>(
        &'a self,
        request: ExecuteRequest,
        delegate: Arc<dyn CodeModeSessionDelegate>,
        preempt: Option<CancellationToken>,
    ) -> CodeModeSessionResultFuture<'a, StartedCell> {
        Box::pin(async move {
            let owner = self.boundary.take_exec(&request.tool_call_id)?;
            self.execute_with_owner(request, delegate, preempt, owner)
                .await
        })
    }
    fn execute_owned(
        self: Arc<Self>,
        request: ExecuteRequest,
        delegate: Arc<dyn CodeModeSessionDelegate>,
        preempt: Option<CancellationToken>,
    ) -> CodeModeSessionResultFuture<'static, StartedCell> {
        // Core constructs this future on the issuing task before transferring
        // startup to its tracked owner. Never rediscover that task's authority.
        let owner = self
            .boundary
            .take_exec(&request.tool_call_id)
            .and_then(|owner| owner.ok_or_else(|| FAILURE.to_owned()));
        Box::pin(async move {
            self.execute_with_owner(request, delegate, preempt, Some(owner?))
                .await
        })
    }
    fn wait<'a>(
        &'a self,
        request: WaitRequest,
        preempt: Option<CancellationToken>,
    ) -> CodeModeSessionResultFuture<'a, WaitOutcome> {
        Box::pin(async move {
            let cell_id = request.cell_id.clone();
            let yield_time_ms = request.yield_time_ms;
            self.observe_wait(
                cell_id,
                Some(yield_time_ms),
                false,
                self.inner.wait(request, preempt),
            )
            .await
        })
    }
    fn terminate<'a>(&'a self, cell_id: CellId) -> CodeModeSessionResultFuture<'a, WaitOutcome> {
        Box::pin(async move {
            self.observe_wait(cell_id.clone(), None, true, self.inner.terminate(cell_id))
                .await
        })
    }
    fn shutdown<'a>(&'a self) -> CodeModeSessionResultFuture<'a, ()> {
        Box::pin(async move {
            let result = self.inner.shutdown().await;
            if result.is_ok() {
                self.cells
                    .lock()
                    .expect("interception cells poisoned")
                    .clear();
            }
            result
        })
    }
}
struct Delegate {
    profile_source: Option<Arc<ProfileMcpSource>>,
    cell_owner: Mutex<Option<Arc<CellPolicyOwner>>>,
    inner: Arc<dyn CodeModeSessionDelegate>,
    live: Arc<AtomicBool>,
    control: Option<Arc<Control>>,
    native_control: Option<Arc<Control>>,
    turn: Arc<DirectTurn>,
    model: Option<Arc<str>>,
    outer_call_id: String,
    rules: Option<crate::canonical_rules::CellImageOrigin>,
}
impl Delegate {
    fn check_native(&self, invocation: &CodeModeNestedToolCall) -> Result<()> {
        ensure!(
            !self.turn.policy_stopped.load(Ordering::Acquire),
            "Native turn was stopped by policy"
        );
        if let Some(control) = &self.native_control {
            ensure!(
                !control.stopped.is_cancelled()
                    && control.native_agents.get().is_some_and(|policy| {
                        policy.admits_nested(control.native_depth, invocation)
                    }),
                "Native child tool is not admitted by the current Session policy"
            );
            if let Some(admission) = control.executor_admission.get() {
                let name = &invocation.tool_name;
                if name.is_default_namespace()
                    && (name.name == "exec_command" && admission.exec_command
                        || name.name == "write_stdin" && admission.write_stdin)
                {
                    ensure!(
                        invocation.input.as_ref().is_some_and(|input| {
                            admission.valid_arguments(&name.name, &input.to_string())
                        }),
                        "Native executor rejected invalid arguments before dispatch"
                    );
                }
            }
        }
        Ok(())
    }
}
impl CodeModeSessionDelegate for Delegate {
    fn invoke_tool<'a>(
        &'a self,
        mut invocation: CodeModeNestedToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolInvocationFuture<'a> {
        Box::pin(async move {
            self.check_native(&invocation).map_err(|_| FAILURE)?;
            let cell_owner = self
                .cell_owner
                .lock()
                .expect("cell policy owner poisoned")
                .clone();
            if let Some(owner) = &cell_owner {
                owner.identify(&invocation.cell_id)?;
            }
            let Some(control) = &self.control else {
                let _observation = self
                    .rules
                    .as_ref()
                    .and_then(|rules| rules.track(&self.turn.id, &invocation));
                return self.inner.invoke_tool(invocation, cancellation_token).await;
            };
            let tracing = codex_otel::sdk_trace::ContextLease::capture();
            let request = Request {
                tracing_context: tracing.as_ref().map(|context| context.token()),
                owner: Some(&self.turn),
                phase: "before",
                route: "code_mode",
                boundary: "pre_dispatch",
                turn_id: &self.turn.id,
                model: self.model.as_deref(),
                call_id: None,
                outer_call_id: Some(&self.outer_call_id),
                cell_id: Some(&invocation.cell_id),
                runtime_tool_call_id: Some(&invocation.runtime_tool_call_id),
                tool_name: Name {
                    namespace: invocation.tool_name.namespace.as_deref(),
                    name: &invocation.tool_name.name,
                },
                tool_kind: invocation.tool_kind,
                input: &invocation.input,
                result: (),
            };
            let request = ProfiledRequest {
                request,
                source: self.profile_source.clone(),
            };
            let _invocation_owner = cell_owner.clone();
            let cell_owner =
                cell_owner.filter(|_| control.controls_environment_builtin(&invocation.tool_name));
            let decision = if let Some(lease) = &cell_owner {
                control
                    .decide(
                        CellRequest {
                            request,
                            cell_owner_id: lease.id,
                            lease,
                            before: true,
                        },
                        &cancellation_token,
                    )
                    .await?
            } else {
                control.decide(request, &cancellation_token).await?
            };
            if let Decision::Replace { value } = decision {
                invocation.input = Some(value);
            }
            // An argument editor cannot select another model, unknown role, or
            // grant a child's nested spawn after admission of the original call.
            self.check_native(&invocation).map_err(|_| FAILURE)?;
            // Pass the original token; observation preemption must never cancel
            // nested work. Original errors propagate as errors, not editable JSON.
            // Hold the effective input/identity for the after notice before
            // passing the actual invocation by value to native dispatch.
            let early_builtin_result = control.controls_builtin_result(&invocation.tool_name);
            let bytes = if early_builtin_result {
                None
            } else {
                Some(control.account(&invocation, None)?)
            };
            let retained = invocation.clone();
            // Core queues nested dispatch on a different task. Carry only the
            // actual cell/runtime IDs across that queue until native completion.
            let observation = self
                .rules
                .as_ref()
                .and_then(|rules| rules.track(&self.turn.id, &invocation));
            let result = self
                .inner
                .invoke_tool(invocation, cancellation_token.clone())
                .await?;
            drop(observation);
            if early_builtin_result {
                return control.publish_result(&self.turn, Some(&cancellation_token), || result);
            }
            let tracing = codex_otel::sdk_trace::ContextLease::capture();
            let request = Request {
                tracing_context: tracing.as_ref().map(|context| context.token()),
                owner: Some(&self.turn),
                phase: "after",
                route: "code_mode",
                boundary: "nested_return",
                turn_id: &self.turn.id,
                model: self.model.as_deref(),
                call_id: None,
                outer_call_id: Some(&self.outer_call_id),
                cell_id: Some(&retained.cell_id),
                runtime_tool_call_id: Some(&retained.runtime_tool_call_id),
                tool_name: Name {
                    namespace: retained.tool_name.namespace.as_deref(),
                    name: &retained.tool_name.name,
                },
                tool_kind: retained.tool_kind,
                input: &retained.input,
                result: &result,
            };
            let request = ProfiledRequest {
                request,
                source: self.profile_source.clone(),
            };
            let decision = if let Some(lease) = &cell_owner {
                control
                    .decide_retained(
                        CellRequest {
                            request,
                            cell_owner_id: lease.id,
                            lease,
                            before: false,
                        },
                        &cancellation_token,
                        bytes,
                        None,
                    )
                    .await?
            } else {
                control
                    .decide_retained(request, &cancellation_token, bytes, None)
                    .await?
            };
            control.publish_result(&self.turn, Some(&cancellation_token), || match decision {
                Decision::Replace { value } => value,
                _ => result,
            })
        })
    }
    fn notify<'a>(
        &'a self,
        call_id: String,
        cell_id: CellId,
        text: String,
        cancellation_token: CancellationToken,
    ) -> NotificationFuture<'a> {
        self.inner
            .notify(call_id, cell_id, text, cancellation_token)
    }
    fn cell_closed(&self, cell_id: &CellId) {
        self.cell_owner
            .lock()
            .expect("cell policy owner poisoned")
            .take();
        self.live.store(false, Ordering::Release);
        self.inner.cell_closed(cell_id);
    }
}

// Only installed around the canonical rules contributor. Python and SDK-MCP
// keep their captured approval/control owner; do not intercept them twice.
pub struct Contributor(pub Arc<dyn ToolContributor>);

/// Only for the audited native extension owners in extensions::build.
/// Nested results already pass through the RPC policy.
pub struct DirectContributor(pub Arc<dyn ToolContributor>, pub DirectOutput);

#[derive(Clone, Copy)]
pub enum DirectOutput {
    /// Board and skills return JsonToolOutput, including to PostToolUse.
    Json,
    /// Web search and image generation return structured model bodies and opt
    /// out of PostToolUse. Never flatten image/audio/encrypted items to logs.
    ResponseBody,
}
impl Contributor {
    fn wrap(
        &self,
        tools: Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>>,
        thread: &ExtensionData,
        direct: Option<DirectOutput>,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        let Some(control) = thread.get::<Control>() else {
            return tools;
        };
        tools
            .into_iter()
            .map(|inner| {
                Arc::new(Executor {
                    inner,
                    control: control.clone(),
                    direct,
                }) as Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>
            })
            .collect()
    }
}
impl ToolContributor for Contributor {
    fn tools(
        &self,
        session: &ExtensionData,
        thread: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        self.wrap(self.0.tools(session, thread), thread, None)
    }
    fn tools_for_step(
        &self,
        session: &ExtensionData,
        thread: &ExtensionData,
        step: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        self.wrap(self.0.tools_for_step(session, thread, step), thread, None)
    }
}
impl ToolContributor for DirectContributor {
    fn tools(
        &self,
        session: &ExtensionData,
        thread: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        Contributor(self.0.clone()).wrap(self.0.tools(session, thread), thread, Some(self.1))
    }
    fn tools_for_step(
        &self,
        session: &ExtensionData,
        thread: &ExtensionData,
        step: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        Contributor(self.0.clone()).wrap(
            self.0.tools_for_step(session, thread, step),
            thread,
            Some(self.1),
        )
    }
}
struct Executor {
    inner: Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>,
    control: Arc<Control>,
    direct: Option<DirectOutput>,
}
#[derive(Serialize)]
#[serde(untagged)]
enum Projected {
    Body(FunctionCallOutputBody),
    Json(Value),
}
impl<'call> ToolExecutor<ToolCall<'call>> for Executor {
    fn tool_name(&self) -> ToolName {
        self.inner.tool_name()
    }
    fn spec(&self) -> ToolSpec {
        self.inner.spec()
    }
    fn exposure(&self) -> ToolExposure {
        self.inner.exposure()
    }
    fn search_info(&self) -> Option<ToolSearchInfo> {
        self.inner.search_info()
    }
    fn supports_parallel_tool_calls(&self) -> bool {
        self.inner.supports_parallel_tool_calls()
    }
    fn handle<'a>(&'a self, call: ToolCall<'call>) -> ToolExecutorFuture<'a>
    where
        ToolCall<'call>: 'a,
    {
        Box::pin(async move {
            if !self.control.configured()
                || (self.direct.is_some() && !matches!(&call.source, ToolCallSource::Direct))
            {
                return self.inner.handle(call).await;
            }
            let token = CancellationToken::new(); // Core cancels by dropping this future.
            let ToolPayload::Function { arguments } = &call.payload else {
                return self.inner.handle(call).await;
            };
            let failed = || FunctionCallError::RespondToModel(FAILURE.into());
            let bytes = self
                .control
                .account(
                    &(&call.turn_id, &call.call_id, &call.tool_name, arguments),
                    None,
                )
                .map_err(|_| failed())?;
            let arguments = arguments.clone();
            let payload = ToolPayload::Function { arguments };
            let call_id = call.call_id.clone();
            let turn_id = call.turn_id.clone();
            let model = call.model.clone();
            let name = call.tool_name.clone();
            let result = self.inner.handle(call).await?;
            let projection = match self.direct {
                Some(DirectOutput::ResponseBody) => {
                    // These audited owners return FunctionCallOutput and do
                    // not participate in PostToolUse. Fail closed if the pin's
                    // contract unexpectedly changes; never silently emit O
                    // while acknowledging R to a result policy.
                    if result.post_tool_use_response(&call_id, &payload).is_some() {
                        return Err(failed());
                    }
                    match result.to_response_item(&call_id, &payload) {
                        ResponseInputItem::FunctionCallOutput { output, .. } => {
                            Projected::Body(output.body)
                        }
                        _ => return Err(failed()),
                    }
                }
                _ => Projected::Json(result.code_mode_result(&payload)), // audited JsonToolOutput only
            };
            let ToolPayload::Function { arguments } = &payload else {
                unreachable!()
            };
            let tracing = codex_otel::sdk_trace::ContextLease::capture();
            let request = Request {
                tracing_context: tracing.as_ref().map(|context| context.token()),
                // This extension's existing post-effect projection is not a
                // public before policy and cannot confer StopTurn authority.
                owner: None,
                phase: "after",
                route: "extension",
                boundary: "executor_return",
                turn_id: &turn_id,
                model: Some(&model),
                call_id: Some(&call_id),
                outer_call_id: None,
                cell_id: None,
                runtime_tool_call_id: None,
                tool_name: Name {
                    namespace: name.namespace.as_deref(),
                    name: &name.name,
                },
                tool_kind: "function_json",
                input: arguments,
                result: &projection,
            };
            match self
                .control
                .decide_retained(request, &token, Some(bytes), None)
                .await
                .map_err(|_| failed())?
            {
                Decision::Replace { value } => {
                    let body = if matches!(self.direct, Some(DirectOutput::ResponseBody)) {
                        Some(
                            serde_json::from_value::<FunctionCallOutputBody>(value.clone())
                                .map_err(|_| failed())?,
                        )
                    } else {
                        None
                    };
                    Ok(Box::new(EditedOutput {
                        original: result,
                        value,
                        body,
                    }) as Box<dyn ToolOutput>)
                }
                _ => Ok(result),
            }
        })
    }
}
struct EditedOutput {
    original: Box<dyn ToolOutput>,
    value: Value,
    body: Option<FunctionCallOutputBody>,
}
impl ToolOutput for EditedOutput {
    fn log_output(&self) -> String {
        self.original.log_output()
    }
    fn success_for_logging(&self) -> bool {
        self.original.success_for_logging()
    }
    fn set_handler_duration_ms(&mut self, ms: u64) {
        self.original.set_handler_duration_ms(ms);
    }
    fn contains_external_context(&self) -> bool {
        self.original.contains_external_context()
    }
    fn fallback_token_limit_override(&self) -> Option<usize> {
        self.original.fallback_token_limit_override()
    }
    fn post_tool_use_id(&self, id: &str) -> String {
        self.original.post_tool_use_id(id)
    }
    fn post_tool_use_input(&self, payload: &ToolPayload) -> Option<Value> {
        self.original.post_tool_use_input(payload)
    }
    fn post_tool_use_response(&self, id: &str, payload: &ToolPayload) -> Option<Value> {
        self.original
            .post_tool_use_response(id, payload)
            .map(|_| self.value.clone())
    }
    fn code_mode_result(&self, _: &ToolPayload) -> Value {
        self.value.clone()
    }
    fn tool_result_metadata(&self) -> Option<&Value> {
        self.original.tool_result_metadata()
    }
    fn to_response_item(&self, id: &str, payload: &ToolPayload) -> ResponseInputItem {
        let mut response = self.original.to_response_item(id, payload);
        match &mut response {
            ResponseInputItem::FunctionCallOutput { output, .. }
            | ResponseInputItem::CustomToolCallOutput { output, .. } => {
                output.body = self
                    .body
                    .clone()
                    .unwrap_or_else(|| FunctionCallOutputBody::Text(self.value.to_string()));
            }
            _ => {}
        }
        response
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use codex_code_mode::CodeModeToolKind;
    use codex_code_mode::RuntimeResponse;
    use std::sync::atomic::{AtomicUsize, Ordering};

    pub(crate) fn bind_owner(thread: &ExtensionData, turn: &ExtensionData) -> Arc<Control> {
        let control = thread.get_or_init(|| Control::new(thread.level_id().to_owned(), true));
        let _ = control
            .creation_tracing
            .set(codex_otel::sdk_trace::CapturedContext::capture().context);
        turn.get_or_init(|| DirectTurn {
            sdk_admission: OnceLock::new(),
            id: turn.level_id().to_owned(),
            model: Mutex::new(None),
            cancelled: CancellationToken::new(),
            policy_stopped: AtomicBool::new(false),
            mcp_calls: Mutex::default(),
        });
        control
    }

    pub(crate) fn bind_child_tracing_owner(
        thread: &ExtensionData,
        tracing: Option<Arc<codex_otel::sdk_trace::ContextLease>>,
    ) {
        let parent = Control::new("tracing-parent".into(), true);
        let control = Control::descendant(thread.level_id().to_owned(), &parent);
        let _ = control.creation_tracing.set(tracing);
        thread.insert(control);
    }

    #[tokio::test(start_paused = true)]
    async fn captured_mcp_source_retains_rule_deadline_and_generation_after_refresh() {
        let control = Arc::new(Control::new("root".into(), true));
        control.enable(30.0, Scope::ThreadTree).unwrap();
        let rule = |generation: u64, seconds| ToolRule {
            name: "lookup".into(),
            namespace: Some("mcp__records".into()),
            timeout: Some(seconds),
            scope: Scope::ThreadTree,
            generation: Some(generation.to_string()),
            result: false,
        };
        control.configure_tools(vec![rule(41, 0.025)]).unwrap();
        let old = Arc::new(control.capture_mcp_source());
        control.configure_tools(vec![rule(42, 0.007)]).unwrap();
        let accepted = Arc::new(control.capture_mcp_source());
        let name = ToolName::new(Some("mcp__records".to_owned()), "lookup".to_owned())
            .with_default_namespace();
        for (source, generation, milliseconds, turn) in [
            (old.clone(), 41, 25, "old-source"),
            (accepted.clone(), 42, 7, "accepted-source"),
        ] {
            let captured = source.policy.as_ref().unwrap().rules[&name];
            assert_eq!(captured.0, Some(Duration::from_millis(milliseconds)));
            assert_eq!(captured.3, Some(generation));
            let owner = direct_request(control.clone(), turn).owner;
            let mut before = before_request(&owner, "lookup");
            before.tool_name.namespace = Some("mcp__records");
            let request = ProfiledRequest {
                request: before,
                source: Some(source),
            };
            let token = CancellationToken::new();
            let mut decision = Box::pin(control.decide(request, &token));
            assert!(futures_util::poll!(&mut decision).is_pending());
            let notice = control.next().await.unwrap();
            let nonce = notice["nonce"].as_u64().unwrap();
            assert_eq!(notice["generation"], generation);
            assert!(control.check(nonce));
            tokio::time::advance(Duration::from_millis(milliseconds - 1)).await;
            assert!(control.check(nonce));
            tokio::time::advance(Duration::from_millis(2)).await;
            assert!(!control.check(nonce));
            assert!(!control.resolve(nonce, r#"{"action":"continue"}"#));
            assert!(decision.await.is_err());
            // next() claimed a physical callback lease; timeout cannot release it.
            assert!(control.release(nonce));
            assert_eq!(control.next().await.unwrap(), json!({"cancel": nonce}));
            assert!(control.channel.state.lock().unwrap().pending.is_empty());
            control.join_expiry_cleanup().await.unwrap();
        }
        assert_eq!(old.policy.as_ref().unwrap().rules[&name].3, Some(41));
        assert_eq!(accepted.policy.as_ref().unwrap().rules[&name].3, Some(42));
    }

    #[test]
    fn pinned_executor_schema_requires_fields_and_rejects_wrong_types() {
        let admission = PinnedExecutorAdmission {
            exec_command: true,
            write_stdin: true,
            login: true,
        };
        let exec = json!({
            "cmd": "printf unchanged",
            "workdir": "",
            "shell": "zsh",
            "justification": "",
            "tty": false,
            "login": true,
            "yield_time_ms": 0,
            "max_output_tokens": 100,
            "prefix_rule": ["printf", "unchanged"],
            "sandbox_permissions": "use_default"
        });
        let stdin = json!({
            "session_id": 1,
            "chars": "",
            "yield_time_ms": 0,
            "max_output_tokens": 100
        });
        for (name, required, valid) in [
            ("exec_command", "cmd", exec),
            ("write_stdin", "session_id", stdin),
        ] {
            assert!(admission.valid_arguments(name, &valid.to_string()));
            for key in valid.as_object().unwrap().keys() {
                let mut omitted = valid.clone();
                omitted.as_object_mut().unwrap().remove(key);
                assert_eq!(
                    admission.valid_arguments(name, &omitted.to_string()),
                    key != required,
                    "omitted {name}.{key}"
                );
                for invalid in [Value::Null, json!({}), json!([null])] {
                    let mut changed = valid.clone();
                    changed[key] = invalid;
                    assert!(
                        !admission.valid_arguments(name, &changed.to_string()),
                        "invalid {name}.{key}"
                    );
                }
            }
            let mut extra = valid.clone();
            extra["undeclared"] = json!(true);
            assert!(!admission.valid_arguments(name, &extra.to_string()));
        }
        for arguments in [
            r#"{"cmd":1}"#,
            r#"{"cmd":"ok","tty":1}"#,
            r#"{"cmd":"ok","login":"false"}"#,
            r#"{"cmd":"ok","prefix_rule":"printf"}"#,
            r#"{"cmd":"ok","sandbox_permissions":"with_additional_permissions"}"#,
            r#"{"cmd":"ok","yield_time_ms":true}"#,
        ] {
            assert!(!admission.valid_arguments("exec_command", arguments));
        }
        for arguments in [
            r#"{"session_id":"1"}"#,
            r#"{"session_id":true}"#,
            r#"{"session_id":1,"chars":0}"#,
        ] {
            assert!(!admission.valid_arguments("write_stdin", arguments));
        }
    }

    #[test]
    fn pinned_executor_schema_keeps_numbers_distinct_from_core_integer_parsing() {
        let admission = PinnedExecutorAdmission {
            exec_command: true,
            write_stdin: true,
            login: true,
        };
        // The advertised schema says number, without minimum or integer rules.
        // Native deserialization may still reject these values; never coerce them.
        for number in [json!(-1), json!(1.25), json!(0)] {
            for name in ["exec_command", "write_stdin"] {
                let mut arguments = if name == "exec_command" {
                    json!({"cmd":""})
                } else {
                    json!({"session_id":number})
                };
                arguments["yield_time_ms"] = number.clone();
                arguments["max_output_tokens"] = number.clone();
                assert!(admission.valid_arguments(name, &arguments.to_string()));
            }
        }
        assert!(admission.valid_arguments(
            "exec_command",
            r#"{"cmd":"","prefix_rule":[],"sandbox_permissions":"require_escalated"}"#
        ));
    }

    #[test]
    fn pinned_executor_schema_rejects_parse_failures_and_withheld_login() {
        let admission = PinnedExecutorAdmission {
            exec_command: true,
            write_stdin: true,
            login: false,
        };
        for arguments in ["null", "[]", "{}", "{", r#"{"cmd":"ok"} trailing"#] {
            assert!(!admission.valid_arguments("exec_command", arguments));
            assert!(!admission.valid_arguments("write_stdin", arguments));
        }
        assert!(admission.valid_arguments("exec_command", r#"{"cmd":"ok"}"#));
        assert!(!admission.valid_arguments("exec_command", r#"{"cmd":"ok","login":false}"#));
        let deep = format!(
            r#"{{"cmd":"ok","extra":{}0{}}}"#,
            "[".repeat(140),
            "]".repeat(140)
        );
        assert!(!admission.valid_arguments("exec_command", &deep));
    }

    #[tokio::test]
    async fn native_rule_selection_uses_core_aliases_and_retains_raw_namespace() {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3.0, Scope::ThreadTree).unwrap();
        let child = Arc::new(Control::descendant("child".into(), &root));
        for scope in [Scope::Root, Scope::ThreadTree] {
            root.configure_tools(
                ["exec_command", "write_stdin", "apply_patch"]
                    .into_iter()
                    .map(|name| ToolRule {
                        generation: None,
                        result: false,
                        name: name.into(),
                        namespace: Some("functions".into()),
                        timeout: Some(3.0),
                        scope,
                    })
                    .collect(),
            )
            .unwrap();
            for (control, is_child) in [(&root, false), (&child, true)] {
                for name in ["exec_command", "write_stdin", "apply_patch"] {
                    for namespace in [None, Some(""), Some("functions"), Some("mcp__other")] {
                        let token = CancellationToken::new();
                        let request = json!({
                            "tool_name": {"namespace": namespace, "name": name},
                            "input": "synthetic"
                        });
                        let mut decision = Box::pin(control.decide(request, &token));
                        if namespace == Some("mcp__other") || (is_child && scope == Scope::Root) {
                            assert!(matches!(decision.await, Ok(Decision::Continue)));
                            assert!(root.channel.receiver.lock().await.try_recv().is_err());
                            continue;
                        }
                        assert!(futures_util::poll!(&mut decision).is_pending());
                        let notice = root.next().await.unwrap();
                        assert_eq!(
                            notice["request"]["tool_name"]["namespace"],
                            json!(namespace)
                        );
                        assert_eq!(notice["request"]["tool_name"]["name"], name);
                        assert_eq!(notice["request"]["thread_id"], control.id);
                        let nonce = notice["nonce"].as_u64().unwrap();
                        assert!(root.check(nonce));
                        assert!(root.handoff(nonce));
                        assert!(root.resolve(nonce, r#"{"action":"deny"}"#));
                        assert!(decision.await.is_err());
                        assert!(root.release(nonce));
                        assert_eq!(root.next().await.unwrap(), json!({"cancel": nonce}));
                    }
                }
            }
        }
        assert!(root.channel.state.lock().unwrap().pending.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn approval_can_wait_past_an_hour_then_use_its_transform_window() {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3600.0, Scope::ThreadTree).unwrap();
        root.configure_tools(
            serde_json::from_value(json!([
                {"namespace":"functions", "name":"exec_command", "timeout":null,
                 "scope":"thread_tree"},
                {"namespace":"functions", "name":"apply_patch", "timeout":30.0,
                 "scope":"thread_tree"},
                {"namespace":"collaboration", "name":"spawn_agent", "timeout":30.0,
                 "scope":"thread_tree"}
            ]))
            .unwrap(),
        )
        .unwrap();
        let initial_bytes = root.channel.bytes.available_permits();
        let token = CancellationToken::new();
        let mut approval = Box::pin(root.decide(
            json!({"tool_name":{"namespace":"functions", "name":"exec_command"},
                   "input":"original"}),
            &token,
        ));
        assert!(futures_util::poll!(&mut approval).is_pending());
        let wire = root.next_wire(10).await.unwrap();
        let nonce = serde_json::from_str::<Value>(&wire).unwrap()["nonce"]
            .as_u64()
            .unwrap();
        tokio::time::advance(Duration::from_secs(7200)).await;
        assert!(root.check(nonce));
        assert!(root.handoff(nonce));
        assert!(futures_util::poll!(&mut approval).is_pending());
        // Approval has already taken hours. A full independent transform
        // window still fits before Python resolves this native decision.
        tokio::time::advance(Duration::from_secs(30)).await;
        assert!(root.check(nonce));
        assert!(root.resolve(nonce, r#"{"action":"replace","value":{"cmd":"approved"}}"#));
        assert!(
            matches!(approval.await, Ok(Decision::Replace { value }) if value == json!({"cmd":"approved"}))
        );
        assert_eq!(
            root.channel.bytes.available_permits(),
            initial_bytes - wire.len()
        );
        assert!(root.release(nonce));
        assert_eq!(root.next().await.unwrap(), json!({"cancel":nonce}));
        assert_eq!(root.channel.bytes.available_permits(), initial_bytes);

        // Rules without an approval keep their configured finite window.
        for (namespace, name) in [
            ("functions", "apply_patch"),
            ("collaboration", "spawn_agent"),
        ] {
            let mut timed = Box::pin(root.decide(
                json!({"tool_name":{"namespace":namespace, "name":name}}),
                &token,
            ));
            assert!(futures_util::poll!(&mut timed).is_pending());
            let notice = root.next().await.unwrap();
            let nonce = notice["nonce"].as_u64().unwrap();
            assert!(root.check(nonce));
            assert!(root.handoff(nonce));
            tokio::time::advance(Duration::from_secs(31)).await;
            assert!(!root.check(nonce));
            assert!(!root.resolve(nonce, r#"{"action":"continue"}"#));
            assert!(timed.await.is_err());
            assert!(root.release(nonce));
            assert_eq!(root.next().await.unwrap(), json!({"cancel":nonce}));
        }
        assert_eq!(root.channel.bytes.available_permits(), initial_bytes);
        assert!(root.channel.state.lock().unwrap().pending.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn approval_without_deadline_still_joins_after_root_revoke() {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3600.0, Scope::ThreadTree).unwrap();
        root.configure_tools(
            serde_json::from_value(json!([
                {"namespace":"functions", "name":"exec_command", "timeout":null,
                 "scope":"thread_tree"}
            ]))
            .unwrap(),
        )
        .unwrap();
        let child = Arc::new(Control::descendant("child".into(), &root));
        let initial_bytes = root.channel.bytes.available_permits();
        let token = CancellationToken::new();
        let mut approval = Box::pin(child.decide(
            json!({"tool_name":{"namespace":"functions", "name":"exec_command"},
                   "input":"retained application approval"}),
            &token,
        ));
        assert!(futures_util::poll!(&mut approval).is_pending());
        let wire = root.next_wire(11).await.unwrap();
        let nonce = serde_json::from_str::<Value>(&wire).unwrap()["nonce"]
            .as_u64()
            .unwrap();
        assert!(root.handoff(nonce));
        tokio::time::advance(Duration::from_secs(24 * 3600)).await;
        assert!(root.check(nonce));
        root.revoke();
        assert!(!root.check(nonce));
        assert!(!root.resolve(nonce, r#"{"action":"continue"}"#));
        assert!(approval.await.is_err());
        assert_eq!(root.next().await.unwrap(), json!({"cancel":nonce}));
        // Joining the reader cannot release a physical application callback.
        root.reader_finished();
        assert_eq!(
            root.channel.bytes.available_permits(),
            initial_bytes - wire.len()
        );
        assert!(root.release(nonce));
        assert!(!root.release(nonce));
        assert!(!root.resolve(nonce, r#"{"action":"continue"}"#));
        assert_eq!(root.channel.bytes.available_permits(), initial_bytes);
        assert!(root.channel.state.lock().unwrap().pending.is_empty());
    }

    #[tokio::test]
    async fn request_cancellation_fences_before_repoll_without_releasing_callback_storage() {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3.0, Scope::ThreadTree).unwrap();
        let child = Arc::new(Control::descendant("child".into(), &root));
        let bytes_before = root.channel.bytes.available_permits();
        let child_token = CancellationToken::new();
        let sibling_token = CancellationToken::new();
        let mut child_request =
            Box::pin(child.decide(json!({"input":"cancelled child's old turn"}), &child_token));
        let mut sibling =
            Box::pin(child.decide(json!({"input":"unaffected child request"}), &sibling_token));
        assert!(futures_util::poll!(&mut child_request).is_pending());
        let child_wire = root.next_wire(21).await.unwrap();
        let child_notice: Value = serde_json::from_str(&child_wire).unwrap();
        let child_nonce = child_notice["nonce"].as_u64().unwrap();
        assert!(root.handoff(child_nonce));
        assert!(futures_util::poll!(&mut sibling).is_pending());
        let sibling_wire = root.next_wire(22).await.unwrap();
        let sibling_notice: Value = serde_json::from_str(&sibling_wire).unwrap();
        let sibling_nonce = sibling_notice["nonce"].as_u64().unwrap();
        assert!(root.handoff(sibling_nonce));
        assert!(root.check(child_nonce));
        assert!(root.check(sibling_nonce));
        let reserved = bytes_before - child_wire.len() - sibling_wire.len();
        assert_eq!(root.channel.bytes.available_permits(), reserved);

        child_token.cancel();
        // Do not poll the decision future yet: Python can enter here as soon
        // as Core cancels that request, before Reservation has been dropped.
        assert!(!root.check(child_nonce));
        assert!(!root.resolve(child_nonce, r#"{"action":"continue"}"#));
        assert!(!root.resolve(child_nonce, r#"{"action":"replace","value":{}}"#));
        assert_eq!(root.channel.bytes.available_permits(), reserved);
        assert!(root.check(sibling_nonce));
        assert!(root.resolve(sibling_nonce, r#"{"action":"continue"}"#));
        assert!(matches!(sibling.await, Ok(Decision::Continue)));
        assert!(root.release(sibling_nonce));
        assert_eq!(
            root.channel.bytes.available_permits(),
            bytes_before - child_wire.len()
        );
        assert!(child_request.await.is_err());
        // A completed native denial still cannot discard storage retained by
        // a running application callback.
        assert_eq!(
            root.channel.bytes.available_permits(),
            bytes_before - child_wire.len()
        );
        assert!(root.release(child_nonce));
        assert_eq!(root.channel.bytes.available_permits(), bytes_before);
        assert!(root.channel.state.lock().unwrap().pending.is_empty());
    }

    #[tokio::test]
    async fn expired_decisions_reject_admission_and_replies_without_waiter_polling() {
        for handed in [false, true] {
            let root = Arc::new(Control::new("root".into(), true));
            root.enable(0.1, Scope::Root).unwrap();
            let initial_bytes = root.channel.bytes.available_permits();
            let token = CancellationToken::new();
            let mut decision = Box::pin(root.decide(json!({"input":"expired"}), &token));
            assert!(futures_util::poll!(&mut decision).is_pending());
            let early_wire = if handed {
                let wire = root.next_wire(11).await.unwrap();
                let notice: Value = serde_json::from_str(&wire).unwrap();
                let nonce = notice["nonce"].as_u64().unwrap();
                assert!(root.check(nonce));
                assert!(root.handoff(nonce));
                Some(wire)
            } else {
                None
            };

            // Keep the native waiter unpolled while its absolute window ends.
            std::thread::sleep(Duration::from_millis(150));
            let wire = match early_wire {
                Some(wire) => wire,
                None => root.next_wire(11).await.unwrap(),
            };
            let notice: Value = serde_json::from_str(&wire).unwrap();
            let nonce = notice["nonce"].as_u64().unwrap();
            assert!(!root.check(nonce));
            assert!(!root.resolve(nonce, r#"{"action":"continue"}"#));
            if !handed {
                // Expiry removes authority, not the physical storage owner.
                assert!(root.handoff(nonce));
            }
            assert!(decision.await.is_err());
            assert_eq!(
                root.channel.bytes.available_permits(),
                initial_bytes - wire.len()
            );
            assert!(root.release(nonce));
            assert_eq!(root.channel.bytes.available_permits(), initial_bytes);
            assert!(root.channel.state.lock().unwrap().pending.is_empty());
        }
    }

    #[tokio::test]
    async fn on_time_decision_remains_accepted_when_consumed_after_deadline() {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(0.1, Scope::Root).unwrap();
        let initial_bytes = root.channel.bytes.available_permits();
        let token = CancellationToken::new();
        let mut decision = Box::pin(root.decide(json!({"input":"on-time"}), &token));
        assert!(futures_util::poll!(&mut decision).is_pending());
        let notice = root.next().await.unwrap();
        let nonce = notice["nonce"].as_u64().unwrap();
        assert!(root.handoff(nonce));
        assert!(root.resolve(nonce, r#"{"action":"continue"}"#));

        std::thread::sleep(Duration::from_millis(150));
        assert!(matches!(decision.await, Ok(Decision::Continue)));
        assert!(root.release(nonce));
        assert_eq!(root.channel.bytes.available_permits(), initial_bytes);
        assert!(root.channel.state.lock().unwrap().pending.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn claimed_stop_survives_timeout_resolution_until_its_request_is_cancelled() {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(1.0, Scope::Root).unwrap();
        let initial_bytes = root.channel.bytes.available_permits();
        let token = CancellationToken::new();
        let sibling_token = CancellationToken::new();
        let turn = direct_request(root.clone(), "turn").owner;
        let mut decision = Box::pin(root.decide(before_request(&turn, "exec_command"), &token));
        let mut sibling = Box::pin(root.decide(json!({"input":"ordinary"}), &sibling_token));
        assert!(futures_util::poll!(&mut decision).is_pending());
        let wire = root.next_wire(11).await.unwrap();
        let nonce = serde_json::from_str::<Value>(&wire).unwrap()["nonce"]
            .as_u64()
            .unwrap();
        // The SDK may only hold a nonce after native admission handed it off.
        assert!(!root.hold_for_stop(nonce));
        assert!(root.handoff(nonce));
        assert!(futures_util::poll!(&mut sibling).is_pending());
        let sibling_wire = root.next_wire(12).await.unwrap();
        let sibling_nonce = serde_json::from_str::<Value>(&sibling_wire).unwrap()["nonce"]
            .as_u64()
            .unwrap();
        assert!(root.handoff(sibling_nonce));
        assert!(root.hold_for_stop(nonce));
        assert!(turn.policy_stopped.load(Ordering::Acquire));
        assert!(!root.hold_for_stop(nonce));
        assert!(!root.resolve(nonce, r#"{"action":"deny"}"#));
        assert!(root.resolve(sibling_nonce, r#"{"action":"continue"}"#));

        tokio::time::advance(Duration::from_secs(2)).await;
        // Force the production timeout resolution before repolling. This is
        // the interleaving where select polled the receiver before the claim
        // but selected the timer after it. Scheduler luck must not be an oracle.
        root.expire(nonce);
        root.expire(sibling_nonce);
        assert!(futures_util::poll!(&mut decision).is_pending());
        assert!(matches!(sibling.await, Ok(Decision::Continue)));
        assert!(!sibling_token.is_cancelled());
        assert!(root.release(sibling_nonce));
        assert_eq!(
            root.channel.bytes.available_permits(),
            initial_bytes - wire.len()
        );
        assert!(futures_util::poll!(&mut decision).is_pending());
        token.cancel();
        assert!(decision.await.is_err());
        // Native retirement must not release the physical Python owner.
        assert_eq!(
            root.channel.bytes.available_permits(),
            initial_bytes - wire.len()
        );
        assert!(root.release(nonce));
        assert!(!root.release(nonce));
        assert_eq!(root.channel.bytes.available_permits(), initial_bytes);
        assert!(root.channel.state.lock().unwrap().pending.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn unclaimed_stop_window_expires_without_waiting_for_callback_release() {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(1.0, Scope::Root).unwrap();
        let initial_bytes = root.channel.bytes.available_permits();
        let token = CancellationToken::new();
        let turn = direct_request(root.clone(), "turn").owner;
        let mut decision = Box::pin(root.decide(before_request(&turn, "exec_command"), &token));
        assert!(futures_util::poll!(&mut decision).is_pending());
        let wire = root.next_wire(11).await.unwrap();
        let nonce = serde_json::from_str::<Value>(&wire).unwrap()["nonce"]
            .as_u64()
            .unwrap();
        assert!(root.handoff(nonce));

        tokio::time::advance(Duration::from_secs(2)).await;
        assert!(matches!(
            futures_util::poll!(&mut decision),
            std::task::Poll::Ready(Err(error)) if error == FAILURE
        ));
        assert!(!root.hold_for_stop(nonce));
        assert!(!turn.policy_stopped.load(Ordering::Acquire));
        assert!(!root.resolve(nonce, r#"{"action":"continue"}"#));
        assert!(!token.is_cancelled());
        assert_eq!(
            root.channel.bytes.available_permits(),
            initial_bytes - wire.len()
        );
        assert!(root.release(nonce));
        assert_eq!(root.channel.bytes.available_permits(), initial_bytes);
        assert!(root.channel.state.lock().unwrap().pending.is_empty());
    }

    #[tokio::test]
    async fn accepted_stop_fences_all_delegates_but_not_other_turns_or_sessions() {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3.0, Scope::Root).unwrap();
        root.configure_tools(vec![ToolRule {
            generation: None,
            result: false,
            name: "exec_command".into(),
            namespace: None,
            timeout: None,
            scope: Scope::Root,
        }])
        .unwrap();
        let initial_bytes = root.channel.bytes.available_permits();
        let turn = direct_request(root.clone(), "same-id").owner;
        let token = CancellationToken::new();
        let other_token = CancellationToken::new();
        let mut stop = Box::pin(root.decide(before_request(&turn, "exec_command"), &token));
        let mut overlapping =
            Box::pin(root.decide(before_request(&turn, "exec_command"), &other_token));
        assert!(futures_util::poll!(&mut stop).is_pending());
        let nonce = root.next().await.unwrap()["nonce"].as_u64().unwrap();
        assert!(root.handoff(nonce));
        assert!(futures_util::poll!(&mut overlapping).is_pending());
        let other_nonce = root.next().await.unwrap()["nonce"].as_u64().unwrap();
        assert!(root.handoff(other_nonce));
        // A native terminal does not itself fence a still-owned yielded turn.
        turn.cancelled.cancel();
        assert!(!turn.policy_stopped.load(Ordering::Acquire));
        assert!(root.hold_for_stop(nonce));
        assert!(!root.check(other_nonce));
        assert!(!root.hold_for_stop(other_nonce));
        assert!(!root.resolve(other_nonce, r#"{"action":"continue"}"#));

        let actual = Arc::new(Actual(AtomicUsize::new(0)));
        let survivor = direct_request(root.clone(), "same-id").owner;
        let separate = Arc::new(Control::new("separate".into(), true));
        let separate_turn = direct_request(separate.clone(), "same-id").owner;
        for (control, owner, allowed) in [
            (Some(root.clone()), turn.clone(), false),
            // Originally unconfigured cells from that same turn are also fenced.
            (None, turn.clone(), false),
            (Some(root.clone()), survivor, true),
            (Some(separate), separate_turn, true),
        ] {
            let delegate = Delegate {
                profile_source: None,
                cell_owner: Mutex::new(None),
                inner: actual.clone(),
                live: Arc::new(AtomicBool::new(true)),
                control,
                native_control: None,
                turn: owner,
                model: None,
                outer_call_id: "same-outer-call".into(),
                rules: None,
            };
            let invocation = CodeModeNestedToolCall {
                cell_id: CellId::new("cell".into()),
                runtime_tool_call_id: "next".into(),
                tool_name: ToolName::plain("update_plan"),
                tool_kind: CodeModeToolKind::Function,
                input: Some(json!({"plan":"effective"})),
            };
            assert_eq!(
                delegate
                    .invoke_tool(invocation, CancellationToken::new())
                    .await
                    .is_ok(),
                allowed
            );
        }
        assert_eq!(actual.0.load(Ordering::SeqCst), 2);
        // No request with an unselected name escapes the shared channel fence.
        assert!(
            root.decide(before_request(&turn, "update_plan"), &token)
                .await
                .is_err()
        );
        assert!(futures_util::poll!(&mut stop).is_pending());
        token.cancel();
        other_token.cancel();
        assert!(stop.await.is_err());
        assert!(overlapping.await.is_err());
        // Neither the turn fence nor dropping native futures frees Python storage.
        assert!(root.channel.bytes.available_permits() < initial_bytes);
        assert!(root.release(nonce));
        assert!(root.release(other_nonce));
        assert_eq!(root.channel.bytes.available_permits(), initial_bytes);
    }

    #[tokio::test]
    async fn queued_unselected_tool_start_is_fenced_until_dispatch_abort() {
        struct EmptyHistory;
        impl codex_extension_api::ConversationHistorySnapshot for EmptyHistory {
            fn history_version(&self) -> u64 {
                0
            }
            fn user_message_revision(&self) -> u64 {
                0
            }
            fn items(&self) -> Box<dyn Iterator<Item = &ResponseItem> + Send + '_> {
                Box::new(std::iter::empty())
            }
        }

        let boundary = Arc::new(Boundary::new(Arc::new(
            crate::storage::MemoryAgentGraph::default(),
        )));
        let session = Arc::new(ExtensionData::new("session"));
        let thread = Arc::new(ExtensionData::new("root"));
        let root = thread.get_or_init(|| Control::new("root".into(), true));
        root.enable(3.0, Scope::Root).unwrap();
        root.configure_tools(vec![ToolRule {
            generation: None,
            result: false,
            name: "exec_command".into(),
            namespace: None,
            timeout: None,
            scope: Scope::Root,
        }])
        .unwrap();
        let turn = Arc::new(ExtensionData::new("same-id"));
        let owner = turn.get_or_init(|| DirectTurn {
            sdk_admission: OnceLock::new(),
            id: turn.level_id().to_owned(),
            model: Mutex::new(None),
            cancelled: CancellationToken::new(),
            policy_stopped: AtomicBool::new(false),
            mcp_calls: Mutex::default(),
        });
        let token = CancellationToken::new();
        let mut stop = Box::pin(root.decide(before_request(&owner, "exec_command"), &token));
        assert!(futures_util::poll!(&mut stop).is_pending());
        let nonce = root.next().await.unwrap()["nonce"].as_u64().unwrap();
        assert!(root.handoff(nonce));

        // The dispatch is already queued. Start the hook only after stop is
        // accepted, for both native direct and nested dispatch sources.
        for source in [
            ToolCallSource::Direct,
            ToolCallSource::CodeMode {
                cell_id: "already-running".into(),
                runtime_tool_call_id: "unselected".into(),
            },
        ] {
            let (go, ready) = oneshot::channel();
            let (entered, started) = oneshot::channel();
            let boundary = boundary.clone();
            let session = session.clone();
            let thread = thread.clone();
            let turn = turn.clone();
            let mut dispatch = tokio::spawn(async move {
                ready.await.unwrap();
                let name = ToolName::plain("update_plan");
                let payload = ToolPayload::Function {
                    arguments: r#"{"plan":[]}"#.into(),
                };
                entered.send(()).unwrap();
                boundary
                    .on_tool_start(ToolStartInput {
                        session_store: &session,
                        thread_store: &thread,
                        step_store: None,
                        turn_store: &turn,
                        turn_id: turn.level_id(),
                        root_turn_id: Some(turn.level_id()),
                        call_id: "queued",
                        originating_item_id: None,
                        tool_name: &name,
                        mcp_tool: None,
                        permissions: Box::pin(std::future::ready(None)),
                        payload: &payload,
                        conversation_history: Arc::new(EmptyHistory),
                        source,
                    })
                    .await;
                panic!("a stopped queued dispatch must not enter its native handler");
            });
            if !owner.policy_stopped.load(Ordering::Acquire) {
                assert!(root.hold_for_stop(nonce));
            }
            go.send(()).unwrap();
            started.await.unwrap();
            assert!(futures_util::poll!(&mut dispatch).is_pending());
            // Terminal and timeout events must not turn this unit hook into
            // admission. There is no lock held across the pending await.
            owner.cancelled.cancel();
            assert!(root.channel.state.try_lock().is_ok());
            assert!(futures_util::poll!(&mut dispatch).is_pending());
            dispatch.abort();
            assert!(dispatch.await.unwrap_err().is_cancelled());
        }
        assert!(futures_util::poll!(&mut stop).is_pending());
        token.cancel();
        assert!(stop.await.is_err());
        assert!(root.release(nonce));
    }

    #[tokio::test]
    async fn stopped_cell_sweep_selects_captured_turn_and_joins_retirement() {
        let (session, mut calls) = probed_session();
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3.0, Scope::Root).unwrap();
        let stopped = direct_request(root.clone(), "same-id").owner;
        let survivor = direct_request(root.clone(), "same-id").owner;
        let token = CancellationToken::new();
        let mut stop = Box::pin(root.decide(before_request(&stopped, "exec_command"), &token));
        assert!(futures_util::poll!(&mut stop).is_pending());
        let nonce = root.next().await.unwrap()["nonce"].as_u64().unwrap();
        assert!(root.handoff(nonce));
        assert!(root.hold_for_stop(nonce));
        for (id, turn) in [
            ("stopped-cell", stopped.clone()),
            ("survivor-cell", survivor),
        ] {
            session.cells.lock().unwrap().insert(
                CellKey::Started(CellId::new(id.into())),
                Arc::new(Cell {
                    control: root.clone(),
                    turn,
                    live: Arc::new(AtomicBool::new(true)),
                    interception_origin: None,
                    operation: AsyncMutex::new(CellOperation::Running),
                }),
            );
        }
        let mut sweep = Box::pin(session.interrupt_owned_cells(&root, true, None, None));
        assert!(futures_util::poll!(&mut sweep).is_pending());
        let CellProbe::Terminate(id, acknowledgement) = calls.recv().await.unwrap() else {
            panic!("only a stopped cell should be terminated");
        };
        assert_eq!(id, CellId::new("stopped-cell".into()));
        assert!(futures_util::poll!(&mut sweep).is_pending());
        assert!(calls.try_recv().is_err());
        acknowledgement.send(Ok(terminated(id))).unwrap();
        sweep.await.unwrap();
        assert!(
            session
                .cells
                .lock()
                .unwrap()
                .contains_key(&CellKey::Started(CellId::new("survivor-cell".into())))
        );
        assert!(calls.try_recv().is_err());
        token.cancel();
        assert!(stop.await.is_err());
        assert!(root.release(nonce));
    }

    #[tokio::test]
    async fn cancelled_callbacks_count_until_release_without_corrupting_live_sibling() {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3.0, Scope::ThreadTree).unwrap();
        let child = Arc::new(Control::descendant("leaf".into(), &root));
        let initial_bytes = root.channel.bytes.available_permits();
        let mut retained = Vec::new();
        for _ in 0..127 {
            let token = CancellationToken::new();
            let mut held = Box::pin(child.decide(json!({"input":"held"}), &token));
            assert!(futures_util::poll!(&mut held).is_pending());
            let wire = root.next_wire(11).await.unwrap();
            let notice: Value = serde_json::from_str(&wire).unwrap();
            let nonce = notice["nonce"].as_u64().unwrap();
            assert_eq!(
                notice["request"],
                json!({"thread_id":"leaf","instance":child.incarnation,"input":"held"})
            );
            assert!(root.handoff(nonce));
            assert!(root.check(nonce));
            token.cancel();
            assert!(held.await.is_err());
            assert!(!root.check(nonce));
            assert!(!root.resolve(nonce, r#"{"action":"continue"}"#));
            assert_eq!(
                root.next_wire(12).await.unwrap(),
                format!(r#"{{"cancel":{nonce}}}"#)
            );
            retained.push((nonce, wire.len()));
        }

        let live_token = CancellationToken::new();
        let mut live = Box::pin(root.decide(json!({"input":"survivor"}), &live_token));
        assert!(futures_util::poll!(&mut live).is_pending());
        let live_wire = root.next_wire(13).await.unwrap();
        let live_notice: Value = serde_json::from_str(&live_wire).unwrap();
        let live_nonce = live_notice["nonce"].as_u64().unwrap();
        assert_eq!(
            live_notice["request"],
            json!({"thread_id":"root","input":"survivor"})
        );
        assert!(root.handoff(live_nonce));
        let held_bytes: usize = retained.iter().map(|(_, size)| size).sum();
        let free_at_capacity = initial_bytes - held_bytes - live_wire.len();
        assert_eq!(root.channel.bytes.available_permits(), free_at_capacity);

        let denied_token = CancellationToken::new();
        let mut denied =
            Box::pin(child.decide(json!({"input":"must not dispatch"}), &denied_token));
        assert!(matches!(
            futures_util::poll!(&mut denied),
            std::task::Poll::Ready(Err(error)) if error == FAILURE
        ));
        assert_eq!(root.channel.bytes.available_permits(), free_at_capacity);
        let (released, size) = retained.remove(0);
        assert!(root.release(released));
        assert!(!root.release(released));
        assert_eq!(
            root.channel.bytes.available_permits(),
            free_at_capacity + size
        );
        assert!(futures_util::poll!(&mut live).is_pending());

        let next_token = CancellationToken::new();
        let mut next = Box::pin(child.decide(json!({"input":"replacement"}), &next_token));
        assert!(futures_util::poll!(&mut next).is_pending());
        let wire = root.next_wire(14).await.unwrap();
        let notice: Value = serde_json::from_str(&wire).unwrap();
        let nonce = notice["nonce"].as_u64().unwrap();
        assert_ne!(nonce, released);
        assert_eq!(
            notice["request"],
            json!({"thread_id":"leaf","instance":child.incarnation,"input":"replacement"})
        );
        assert!(root.handoff(nonce));
        assert!(!root.resolve(released, r#"{"action":"deny"}"#));
        assert!(root.release(nonce));
        assert!(root.resolve(nonce, r#"{"action":"continue"}"#));
        assert!(matches!(next.await, Ok(Decision::Continue)));
        assert_eq!(
            root.channel.bytes.available_permits(),
            free_at_capacity + size
        );
        assert!(root.resolve(
            live_nonce,
            r#"{"action":"replace","value":"only-survivor"}"#
        ));
        assert!(matches!(
            live.await,
            Ok(Decision::Replace { value }) if value == json!("only-survivor")
        ));
        assert!(root.release(live_nonce));
        for (nonce, _) in retained {
            assert!(root.release(nonce));
        }
        assert_eq!(root.channel.bytes.available_permits(), initial_bytes);
        assert!(root.channel.state.lock().unwrap().pending.is_empty());
    }

    #[tokio::test]
    async fn budget_retires_reply_but_holds_handed_payload_until_physical_release() {
        let root = Arc::new(Control::with_budget("root".into(), true, 210));
        root.enable(3.0, Scope::ThreadTree).unwrap();
        let child = Arc::new(Control::descendant("leaf".into(), &root));
        let first = {
            let child = child.clone();
            tokio::spawn(async move {
                child
                    .decide(json!({"input":"one"}), &CancellationToken::new())
                    .await
            })
        };
        let wire = root.next_wire(11).await.unwrap();
        // Full request, including receipt and origin metadata, rather than
        // just the length of the payload. No dynamic production constants.
        assert_eq!(
            serde_json::from_str::<Value>(&wire).unwrap(),
            json!({"nonce":1,"request":{"thread_id":"leaf","instance":child.incarnation,"input":"one"}})
        );
        assert_eq!(root.channel.bytes.available_permits(), 210 - wire.len());
        let second = {
            let root = root.clone();
            tokio::spawn(async move {
                root.decide(json!({"input":"two"}), &CancellationToken::new())
                    .await
            })
        };
        let other = root.next_wire(12).await.unwrap();
        let free_with_both = 210 - wire.len() - other.len();
        assert_eq!(root.channel.bytes.available_permits(), free_with_both);
        // A reply and native return alone must not free callback data.
        assert!(root.resolve(1, r#"{"action":"continue"}"#));
        assert!(matches!(first.await.unwrap(), Ok(Decision::Continue)));
        assert_eq!(root.channel.bytes.available_permits(), free_with_both);
        assert!(!root.resolve(1, r#"{"action":"continue"}"#));
        assert!(
            root.decide(
                json!({"input":"ordinary third notice"}),
                &CancellationToken::new()
            )
            .await
            .is_err()
        );
        assert!(root.release(1));
        assert!(!root.release(1));
        assert_eq!(root.channel.bytes.available_permits(), 210 - other.len());
        // Physical return may win before dispatch resolves. Keep charged and
        // reply-capable until the last owner drops its reference.
        assert!(root.release(2));
        assert_eq!(root.channel.bytes.available_permits(), 210 - other.len());
        assert!(root.resolve(2, r#"{"action":"continue"}"#));
        assert!(matches!(second.await.unwrap(), Ok(Decision::Continue)));
        assert_eq!(root.channel.bytes.available_permits(), 210);
    }

    #[tokio::test]
    async fn budget_queued_abort_and_cancelled_ipc_delivery_release_only_their_payload() {
        let root = Arc::new(Control::with_budget("root".into(), true, 256));
        root.enable(3.0, Scope::Root).unwrap();
        // Admission and queue publication are synchronous through the first
        // pending poll; abort before any receiver claims the body.
        let token = CancellationToken::new();
        let mut queued = Box::pin(root.decide(json!({"input":"queued"}), &token));
        assert!(futures_util::poll!(&mut queued).is_pending());
        assert!(root.channel.bytes.available_permits() < 256);
        drop(queued);
        assert_eq!(root.channel.bytes.available_permits(), 256);
        // A stale queue nonce must not deliver the retired body.
        assert_eq!(root.next_wire(1).await.unwrap(), r#"{"cancel":1}"#);
        let mut handed = Box::pin(root.decide(json!({"input":"read lost before Python"}), &token));
        assert!(futures_util::poll!(&mut handed).is_pending());
        let wire = root.next_wire(41).await.unwrap();
        assert!(wire.contains("read lost before Python"));
        root.abandon(40); // another IPC job has no rights to this delivery
        assert_eq!(root.channel.bytes.available_permits(), 256 - wire.len());
        root.abandon(41);
        assert_eq!(root.channel.bytes.available_permits(), 256);
        assert!(handed.await.is_err());
        assert!(!root.release(2));
        root.abandon(41);
        assert_eq!(root.channel.bytes.available_permits(), 256);
    }

    #[tokio::test]
    async fn budget_retained_effective_input_merges_and_failed_growth_releases_it() {
        let control = Arc::new(Control::with_budget("root".into(), true, 96));
        control.enable(3.0, Scope::Root).unwrap();
        let input = json!({"quoted":"a\\b\n\"c"});
        let bytes = control.account(&input, None).unwrap();
        assert!(control.channel.bytes.available_permits() < 96);
        let result = control.decide_retained(
            json!({"input":input,"result":"projected result does not fit the tiny fixture budget"}),
            &CancellationToken::new(),
            Some(bytes),
            None,
        ).await;
        assert!(result.is_err());
        assert_eq!(control.channel.bytes.available_permits(), 96);
        assert!(control.channel.state.lock().unwrap().pending.is_empty());
        // Error doesn't poison the owner or make the next small notice free.
        let small = {
            let control = control.clone();
            tokio::spawn(async move { control.decide(json!({}), &CancellationToken::new()).await })
        };
        let wire = control.next_wire(17).await.unwrap();
        let nonce = serde_json::from_str::<Value>(&wire).unwrap()["nonce"]
            .as_u64()
            .unwrap();
        assert_eq!(control.channel.bytes.available_permits(), 96 - wire.len());
        assert!(control.resolve(nonce, r#"{"action":"continue"}"#));
        assert!(matches!(small.await.unwrap(), Ok(Decision::Continue)));
        assert!(control.release(nonce));
        assert_eq!(control.channel.bytes.available_permits(), 96);
    }

    #[tokio::test]
    async fn shared_mcp_borrowed_concrete_result_exceeding_budget_is_never_queued() {
        let root = Arc::new(Control::with_budget("root".into(), true, 384));
        root.enable(3.0, Scope::ThreadTree).unwrap();
        let result = codex_protocol::mcp::CallToolResult {
            content: vec![json!({"type":"text", "text":"large".repeat(1024)})],
            structured_content: Some(json!({"receipt":"original"})),
            is_error: Some(false),
            meta: Some(json!({"fixture":"shared"})),
        };
        let arguments = json!({"query":"escaped\n\t\"and backslash\\"});
        let tracing = codex_otel::sdk_trace::ContextLease::capture();
        let request = Request {
            tracing_context: tracing.as_ref().map(|context| context.token()),
            owner: None,
            phase: "after",
            route: "mcp",
            boundary: "executor_return",
            turn_id: "turn",
            model: None,
            call_id: Some("first"),
            outer_call_id: None,
            cell_id: None,
            runtime_tool_call_id: None,
            tool_name: Name {
                namespace: Some("mcp__fixture"),
                name: "lookup",
            },
            tool_kind: "function_json",
            input: Input::JsonText(&arguments),
            result: &result,
        };
        assert!(
            root.decide(request, &CancellationToken::new())
                .await
                .is_err()
        );
        assert_eq!(root.channel.bytes.available_permits(), 384);
        assert!(root.channel.state.lock().unwrap().pending.is_empty());
        assert!(root.channel.receiver.lock().await.try_recv().is_err());
        // The failing bridge never takes or rewrites the concrete native result.
        assert_eq!(result.content[0]["text"].as_str().unwrap().len(), 5120);
        assert_eq!(
            result.structured_content,
            Some(json!({"receipt":"original"}))
        );
    }

    #[tokio::test]
    async fn shared_mcp_remaining_budget_is_retained_until_physical_callback_returns() {
        let root = Arc::new(Control::with_budget("root".into(), true, 600));
        root.enable(3.0, Scope::ThreadTree).unwrap();
        let child = Arc::new(Control::descendant("child".into(), &root));
        let result = codex_protocol::mcp::CallToolResult {
            content: vec![json!({"type":"text", "text":"borrowed normal MCP response"})],
            structured_content: Some(json!({"receipt":"original"})),
            is_error: Some(false),
            meta: Some(json!({"marker":"ordinary\nmetadata"})),
        };
        let arguments = json!({"query":"a\nb\"c\\tail"});
        let tracing = codex_otel::sdk_trace::ContextLease::capture();
        let request = Request {
            tracing_context: tracing.as_ref().map(|context| context.token()),
            owner: None,
            phase: "after",
            route: "mcp",
            boundary: "executor_return",
            turn_id: "turn",
            model: None,
            call_id: Some("mcp-call"),
            outer_call_id: None,
            cell_id: None,
            runtime_tool_call_id: None,
            tool_name: Name {
                namespace: Some("mcp__fixture"),
                name: "lookup",
            },
            tool_kind: "function_json",
            input: Input::JsonText(&arguments),
            result: &result,
        };
        let token = CancellationToken::new();
        let mut live = Box::pin(child.decide(&request, &token));
        assert!(futures_util::poll!(&mut live).is_pending());
        let wire = root.next_wire(11).await.unwrap();
        let value: Value = serde_json::from_str(&wire).unwrap();
        assert_eq!(value["request"]["thread_id"], "child");
        assert_eq!(value["request"]["input"], arguments.to_string());
        assert_eq!(
            value["request"]["result"],
            serde_json::to_value(&result).unwrap()
        );
        assert_eq!(root.channel.bytes.available_permits(), 600 - wire.len());
        assert!(root.check(1));
        assert!(root.handoff(1));
        // Fits by itself; does not fit what remains of the shared channel.
        let remaining = root.channel.bytes.available_permits();
        assert!(remaining < wire.len());
        assert!(
            root.decide(&request, &CancellationToken::new())
                .await
                .is_err()
        );
        assert_eq!(root.channel.state.lock().unwrap().pending.len(), 1);
        assert!(root.channel.receiver.lock().await.try_recv().is_err());
        token.cancel();
        assert!(live.await.is_err());
        assert!(!root.check(1));
        assert_eq!(root.channel.bytes.available_permits(), remaining);
        assert!(!root.resolve(1, r#"{"action":"continue"}"#));
        assert!(root.release(1));
        assert_eq!(root.channel.bytes.available_permits(), 600);
        let next_token = CancellationToken::new();
        let mut next = Box::pin(root.decide(&request, &next_token));
        assert!(futures_util::poll!(&mut next).is_pending());
        assert_eq!(root.next_wire(12).await.unwrap(), r#"{"cancel":1}"#);
        let recovered = root.next_wire(12).await.unwrap();
        let recovered: Value = serde_json::from_str(&recovered).unwrap();
        let nonce = recovered["nonce"].as_u64().unwrap();
        assert!(root.check(nonce));
        assert!(root.resolve(nonce, r#"{"action":"continue"}"#));
        assert!(!root.check(nonce));
        assert!(matches!(next.await, Ok(Decision::Continue)));
        assert!(root.release(nonce));
        assert_eq!(root.channel.bytes.available_permits(), 600);
    }

    #[tokio::test]
    async fn finished_reader_releases_unclaimed_but_not_physical_callback() {
        let control = Arc::new(Control::with_budget("root".into(), true, 256));
        control.enable(3.0, Scope::Root).unwrap();
        let token = CancellationToken::new();
        let mut active = Box::pin(control.decide(json!({"input":"callback"}), &token));
        assert!(futures_util::poll!(&mut active).is_pending());
        let wire = control.next_wire(31).await.unwrap();
        assert!(control.handoff(1));
        assert!(!control.handoff(1));
        let mut lost = Box::pin(control.decide(json!({"input":"reader lost"}), &token));
        assert!(futures_util::poll!(&mut lost).is_pending());
        control.next_wire(32).await.unwrap();
        control.revoke();
        assert!(active.await.is_err());
        assert!(lost.await.is_err());
        control.reader_finished();
        assert_eq!(control.channel.bytes.available_permits(), 256 - wire.len());
        assert!(!control.release(2));
        assert!(control.release(1));
        assert_eq!(control.channel.bytes.available_permits(), 256);
        assert!(!control.release(1));
    }

    struct CapturingSession {
        delegates: mpsc::UnboundedSender<Arc<dyn CodeModeSessionDelegate>>,
    }
    impl CodeModeSession for CapturingSession {
        fn execute<'a>(
            &'a self,
            _: ExecuteRequest,
            delegate: Arc<dyn CodeModeSessionDelegate>,
            _: Option<CancellationToken>,
        ) -> CodeModeSessionResultFuture<'a, StartedCell> {
            Box::pin(async move {
                self.delegates.send(delegate).unwrap();
                let cell_id = CellId::new("cell".into());
                let result_id = cell_id.clone();
                Ok(StartedCell::from_future(cell_id, async move {
                    Ok(RuntimeResponse::Result {
                        cell_id: result_id,
                        content_items: Vec::new(),
                        error_text: None,
                        code_mode_host_duration: None,
                    })
                }))
            })
        }
        fn wait<'a>(
            &'a self,
            _: WaitRequest,
            _: Option<CancellationToken>,
        ) -> CodeModeSessionResultFuture<'a, WaitOutcome> {
            Box::pin(async { Err("unused".into()) })
        }
        fn terminate<'a>(&'a self, _: CellId) -> CodeModeSessionResultFuture<'a, WaitOutcome> {
            Box::pin(async { Err("unused".into()) })
        }
        fn shutdown<'a>(&'a self) -> CodeModeSessionResultFuture<'a, ()> {
            Box::pin(async { Ok(()) })
        }
    }

    fn exec_request() -> ExecuteRequest {
        ExecuteRequest {
            tool_call_id: "same-model-id".into(),
            enabled_tools: Vec::new(),
            source: "text(1)".into(),
            yield_time_ms: None,
            max_output_tokens: None,
        }
    }

    // The provider owns these acknowledgments. A stop has to wait for them,
    // even when Core cancelled its execute before receiving the host CellId.
    enum CellProbe {
        Start(
            Arc<dyn CodeModeSessionDelegate>,
            oneshot::Sender<Result<StartedCell, String>>,
        ),
        Wait(CellId, oneshot::Sender<Result<WaitOutcome, String>>),
        Terminate(CellId, oneshot::Sender<Result<WaitOutcome, String>>),
    }

    struct ProbedSession(mpsc::UnboundedSender<CellProbe>);
    impl CodeModeSession for ProbedSession {
        fn execute<'a>(
            &'a self,
            _: ExecuteRequest,
            delegate: Arc<dyn CodeModeSessionDelegate>,
            _: Option<CancellationToken>,
        ) -> CodeModeSessionResultFuture<'a, StartedCell> {
            Box::pin(async move {
                let (tx, rx) = oneshot::channel();
                self.0
                    .send(CellProbe::Start(delegate, tx))
                    .map_err(|_| "probe closed")?;
                rx.await.map_err(|_| "start response closed".to_owned())?
            })
        }
        fn wait<'a>(
            &'a self,
            request: WaitRequest,
            _: Option<CancellationToken>,
        ) -> CodeModeSessionResultFuture<'a, WaitOutcome> {
            Box::pin(async move {
                let (tx, rx) = oneshot::channel();
                self.0
                    .send(CellProbe::Wait(request.cell_id, tx))
                    .map_err(|_| "probe closed")?;
                rx.await.map_err(|_| "wait response closed".to_owned())?
            })
        }
        fn terminate<'a>(&'a self, id: CellId) -> CodeModeSessionResultFuture<'a, WaitOutcome> {
            Box::pin(async move {
                let (tx, rx) = oneshot::channel();
                self.0
                    .send(CellProbe::Terminate(id, tx))
                    .map_err(|_| "probe closed")?;
                rx.await
                    .map_err(|_| "termination response closed".to_owned())?
            })
        }
        fn shutdown<'a>(&'a self) -> CodeModeSessionResultFuture<'a, ()> {
            Box::pin(async { Ok(()) })
        }
    }

    fn probed_session() -> (Arc<Session>, mpsc::UnboundedReceiver<CellProbe>) {
        let boundary = Arc::new(Boundary::new(Arc::new(
            crate::storage::MemoryAgentGraph::default(),
        )));
        let (tx, rx) = mpsc::unbounded_channel();
        (Session::new(Arc::new(ProbedSession(tx)), boundary), rx)
    }

    fn terminated(id: CellId) -> WaitOutcome {
        WaitOutcome::LiveCell(RuntimeResponse::Terminated {
            cell_id: id,
            content_items: Vec::new(),
            code_mode_host_duration: None,
        })
    }

    fn yielded_start(id: &str) -> StartedCell {
        let id = CellId::new(id.to_owned());
        let response = RuntimeResponse::Yielded {
            cell_id: id.clone(),
            content_items: Vec::new(),
            code_mode_host_duration: None,
        };
        StartedCell::from_future(id, async { Ok(response) })
    }

    fn start_managed_cell(
        session: Arc<Session>,
        control: Arc<Control>,
    ) -> tokio::task::JoinHandle<Result<StartedCell, String>> {
        tokio::spawn(async move {
            session.boundary.start_exec(
                control.clone(),
                direct_request(control, "turn").owner,
                "same-model-id",
                codex_code_mode::PUBLIC_TOOL_NAME,
                None,
                Ok(None),
            );
            session
                .execute(exec_request(), Arc::new(Actual(AtomicUsize::new(0))), None)
                .await
        })
    }

    #[tokio::test]
    async fn child_cancel_captures_terminal_owner_from_starting_and_started_cells() {
        for started in [false, true] {
            let (session, mut calls) = probed_session();
            let child = Arc::new(Control::new("child".into(), false));
            let caller = start_managed_cell(session.clone(), child.clone());
            let CellProbe::Start(_, ready) = calls.recv().await.unwrap() else {
                panic!("expected owned start");
            };
            let mut ready = Some(ready);
            if started {
                ready
                    .take()
                    .unwrap()
                    .send(Ok(yielded_start("child-cell")))
                    .unwrap_or_else(|_| panic!("start lost"));
                caller.await.unwrap().unwrap();
            } else {
                caller.abort();
                assert!(matches!(caller.await, Err(error) if error.is_cancelled()));
            }
            // on_turn_stop has removed the current owner. The sole cell index
            // still owns both an unpublished start and a yielded cell.
            assert!(child.direct_turn.lock().unwrap().is_none());
            assert!(child.capture_turn_stop("unrelated-turn").is_none());
            let stop = child.capture_turn_stop("turn").expect("retained owner");
            stop.stop();
            assert!(stop.owner.policy_stopped.load(Ordering::Acquire));
            let mut cleanup = Box::pin(stop.finish());
            assert!(futures_util::poll!(&mut cleanup).is_pending());
            if !started {
                assert!(calls.try_recv().is_err());
                ready
                    .take()
                    .unwrap()
                    .send(Ok(yielded_start("child-cell")))
                    .unwrap_or_else(|_| panic!("start owner lost"));
                assert!(futures_util::poll!(&mut cleanup).is_pending());
            }
            let CellProbe::Terminate(id, acknowledged) = calls.recv().await.unwrap() else {
                panic!("expected exact cell retirement");
            };
            assert_eq!(id.as_str(), "child-cell");
            assert!(futures_util::poll!(&mut cleanup).is_pending());
            acknowledged.send(Ok(terminated(id))).unwrap();
            cleanup.await.unwrap();
            assert!(calls.try_recv().is_err());
        }
    }

    #[tokio::test]
    async fn child_cancel_keeps_failed_native_cells_owned_for_retry() {
        let (session, mut calls) = probed_session();
        let child = Arc::new(Control::new("child".into(), false));
        let caller = start_managed_cell(session.clone(), child.clone());
        let CellProbe::Start(_, ready) = calls.recv().await.unwrap() else {
            panic!("expected owned start");
        };
        ready
            .send(Ok(yielded_start("child-cell")))
            .unwrap_or_else(|_| panic!("start owner lost"));
        caller.await.unwrap().unwrap();
        let stop = child.capture_turn_stop("turn").unwrap();
        stop.stop();
        let mut cleanup = Box::pin(stop.finish());
        assert!(futures_util::poll!(&mut cleanup).is_pending());
        let CellProbe::Terminate(id, acknowledged) = calls.recv().await.unwrap() else {
            panic!("expected exact cell retirement");
        };
        assert_eq!(id.as_str(), "child-cell");
        acknowledged
            .send(Err("private-native-termination-error".into()))
            .unwrap();
        assert_eq!(
            cleanup.await.unwrap_err().to_string(),
            "private-native-termination-error"
        );
        // A failed termination cannot discard ownership or claim effects ended.
        let retry = child.capture_turn_stop("turn").unwrap();
        retry.stop();
        let mut cleanup = Box::pin(retry.finish());
        assert!(futures_util::poll!(&mut cleanup).is_pending());
        let CellProbe::Terminate(id, acknowledged) = calls.recv().await.unwrap() else {
            panic!("expected exact retry");
        };
        assert_eq!(id.as_str(), "child-cell");
        acknowledged.send(Ok(terminated(id))).unwrap();
        cleanup.await.unwrap();
        assert!(child.capture_turn_stop("turn").is_none());
        assert!(calls.try_recv().is_err());
    }

    #[tokio::test]
    async fn code_mode_scoped_stop_joins_start_cancelled_before_id_and_early_close() {
        let (session, mut calls) = probed_session();
        let root = Arc::new(Control::new("root".into(), true));
        let caller = start_managed_cell(session.clone(), root.clone());
        let CellProbe::Start(delegate, start_reply) = calls.recv().await.unwrap() else {
            panic!("execute")
        };
        caller.abort();
        assert!(matches!(caller.await, Err(error) if error.is_cancelled()));

        let mut first = Box::pin(root.interrupt_code_mode_cells(false, None));
        assert!(futures_util::poll!(&mut first).is_pending());
        assert!(
            calls.try_recv().is_err(),
            "no cell ID exists to terminate yet"
        );
        start_reply
            .send(Ok(yielded_start("root-cell")))
            .unwrap_or_else(|_| panic!("start owner was lost"));
        assert!(futures_util::poll!(&mut first).is_pending());
        let CellProbe::Terminate(id, reply) = calls.try_recv().unwrap() else {
            panic!("terminate")
        };
        assert_eq!(id.as_str(), "root-cell");

        // Closure can precede the termination ACK. A second force must join it,
        // even though the current live flag is already false.
        delegate.cell_closed(&id);
        let mut second = Box::pin(root.interrupt_code_mode_cells(false, None));
        assert!(futures_util::poll!(&mut second).is_pending());
        reply.send(Ok(terminated(id))).unwrap();
        first.await.unwrap();
        second.await.unwrap();
        assert!(calls.try_recv().is_err());
    }

    #[tokio::test]
    async fn code_mode_scoped_stop_tracks_start_rekey_while_waiting_and_closed_start_cannot_revive()
    {
        let (session, mut calls) = probed_session();
        let root = Arc::new(Control::new("root".into(), true));
        let caller = start_managed_cell(session.clone(), root.clone());
        let CellProbe::Start(_, ready) = calls.recv().await.unwrap() else {
            panic!("execute")
        };
        let mut stop = Box::pin(root.interrupt_code_mode_cells(false, None));
        // The selector sees Starting, while execute owns the per-cell lock.
        assert!(futures_util::poll!(&mut stop).is_pending());
        ready
            .send(Ok(yielded_start("rekeyed")))
            .unwrap_or_else(|_| panic!("caller lost"));
        let started = caller.await.unwrap().unwrap();
        assert_eq!(started.cell_id.as_str(), "rekeyed");
        assert!(futures_util::poll!(&mut stop).is_pending());
        let CellProbe::Terminate(actual, reply) = calls.try_recv().unwrap() else {
            panic!("terminate after rekey")
        };
        assert_eq!(actual.as_str(), "rekeyed");
        reply.send(Ok(terminated(actual))).unwrap();
        stop.await.unwrap();

        let caller = start_managed_cell(session.clone(), root.clone());
        let CellProbe::Start(_, ready) = calls.recv().await.unwrap() else {
            panic!("execute")
        };
        session.shutdown().await.unwrap();
        // The runtime may have delivered Started before its shutdown ACK;
        // the normal Rust caller is allowed to poll that receiver afterward.
        ready
            .send(Ok(yielded_start("already-closed")))
            .unwrap_or_else(|_| panic!("caller lost"));
        assert!(matches!(caller.await.unwrap(), Err(error) if error.contains("closed")));
        root.interrupt_code_mode_cells(false, None).await.unwrap();
        assert!(
            calls.try_recv().is_err(),
            "closed start cannot re-enter the live table"
        );
    }

    #[tokio::test]
    async fn code_mode_scoped_stop_keeps_other_control_cells_and_saved_core_result() {
        let (session, mut calls) = probed_session();
        let root = Arc::new(Control::new("root".into(), true));
        let child = Arc::new(Control::descendant("child".into(), &root));
        for (control, id) in [(root.clone(), "root-cell"), (child.clone(), "child-cell")] {
            let caller = start_managed_cell(session.clone(), control);
            let CellProbe::Start(_, ready) = calls.recv().await.unwrap() else {
                panic!("execute")
            };
            ready
                .send(Ok(yielded_start(id)))
                .unwrap_or_else(|_| panic!("start caller closed"));
            let started = caller.await.unwrap().unwrap();
            assert!(matches!(
                started.initial_response().await.unwrap(),
                RuntimeResponse::Yielded { .. }
            ));
        }

        // Emulate Core's abort cleanup: no tool wait handoff, and it discards
        // the returned value. It must remain available to a later real wait.
        let id = CellId::new("root-cell".into());
        let mut core_stop = Box::pin(session.terminate(id.clone()));
        assert!(futures_util::poll!(&mut core_stop).is_pending());
        let CellProbe::Terminate(actual, reply) = calls.try_recv().unwrap() else {
            panic!("terminate")
        };
        assert_eq!(actual, id);
        let completed = RuntimeResponse::Result {
            cell_id: id.clone(),
            content_items: Vec::new(),
            error_text: Some("original completed result".into()),
            code_mode_host_duration: None,
        };
        reply
            .send(Ok(WaitOutcome::LiveCell(completed.clone())))
            .unwrap();
        assert!(matches!(
            core_stop.await.unwrap(),
            WaitOutcome::LiveCell(RuntimeResponse::Result { .. })
        ));
        root.interrupt_code_mode_cells(false, None).await.unwrap();
        assert!(
            calls.try_recv().is_err(),
            "root sweep must spare child and saved result"
        );
        // Originally unconfigured cells must not enroll in a controller later.
        root.enable(3.0, Scope::Root).unwrap();
        let waiter = tokio::spawn({
            let session = session.clone();
            let root = root.clone();
            let id = id.clone();
            async move {
                session.boundary.start_exec(
                    root.clone(),
                    direct_request(root, "next").owner,
                    "wait-one",
                    codex_code_mode::WAIT_TOOL_NAME,
                    None,
                    Ok(None),
                );
                session
                    .wait(
                        WaitRequest {
                            cell_id: id,
                            yield_time_ms: 1,
                        },
                        None,
                    )
                    .await
            }
        });
        assert_eq!(
            RuntimeResponse::from(waiter.await.unwrap().unwrap()),
            completed
        );
        assert!(
            calls.try_recv().is_err(),
            "saved response already came from Core"
        );
        // A second wait reaches the real provider; the saved result was consumed.
        let mut again = Box::pin(session.wait(
            WaitRequest {
                cell_id: id.clone(),
                yield_time_ms: 1,
            },
            None,
        ));
        assert!(futures_util::poll!(&mut again).is_pending());
        let CellProbe::Wait(actual, reply) = calls.try_recv().unwrap() else {
            panic!("wait")
        };
        assert_eq!(actual, id);
        reply
            .send(Ok(WaitOutcome::MissingCell(RuntimeResponse::from(
                terminated(id),
            ))))
            .unwrap();
        assert!(matches!(again.await.unwrap(), WaitOutcome::MissingCell(_)));

        let mut child_stop = Box::pin(child.interrupt_code_mode_cells(false, None));
        assert!(futures_util::poll!(&mut child_stop).is_pending());
        let CellProbe::Terminate(actual, reply) = calls.try_recv().unwrap() else {
            panic!("child terminate")
        };
        assert_eq!(actual.as_str(), "child-cell");
        reply.send(Ok(terminated(actual))).unwrap();
        child_stop.await.unwrap();
    }

    #[tokio::test]
    async fn code_mode_scoped_stop_propagates_pending_start_failure_and_close_releases_it() {
        let (session, mut calls) = probed_session();
        let root = Arc::new(Control::new("root".into(), true));
        let caller = start_managed_cell(session.clone(), root.clone());
        let CellProbe::Start(_, reply) = calls.recv().await.unwrap() else {
            panic!("execute")
        };
        caller.abort();
        assert!(matches!(caller.await, Err(error) if error.is_cancelled()));
        let mut stop = Box::pin(root.interrupt_code_mode_cells(false, None));
        assert!(futures_util::poll!(&mut stop).is_pending());
        reply
            .send(Err("host failed to create cell".into()))
            .unwrap_or_else(|_| panic!("lost owner"));
        let error = stop.await.unwrap_err().to_string();
        assert!(error.contains("host failed"));
        assert!(calls.try_recv().is_err());

        let caller = start_managed_cell(session.clone(), root.clone());
        let CellProbe::Start(_, reply) = calls.recv().await.unwrap() else {
            panic!("execute")
        };
        caller.abort();
        assert!(matches!(caller.await, Err(error) if error.is_cancelled()));
        session.shutdown().await.unwrap();
        assert!(
            reply.is_closed(),
            "joined provider shutdown releases retained start"
        );
        root.interrupt_code_mode_cells(false, None).await.unwrap();
        assert!(
            calls.try_recv().is_err(),
            "cannot start JS after joined shutdown"
        );
    }

    #[tokio::test]
    async fn code_mode_scoped_stop_borrowed_start_cannot_revive_joined_shutdown() {
        let (session, mut calls) = probed_session();
        let root = Arc::new(Control::new("root".into(), true));
        let caller = start_managed_cell(session.clone(), root.clone());
        let CellProbe::Start(_, reply) = calls.recv().await.unwrap() else {
            panic!("execute")
        };
        caller.abort();
        assert!(matches!(caller.await, Err(error) if error.is_cancelled()));
        let mut stop = Box::pin(root.interrupt_code_mode_cells(false, None));
        assert!(futures_util::poll!(&mut stop).is_pending());
        session.shutdown().await.unwrap();
        reply
            .send(Ok(yielded_start("result-polled-after-close")))
            .unwrap_or_else(|_| panic!("sweep should retain borrowed startup"));
        stop.await.unwrap();
        root.interrupt_code_mode_cells(false, None).await.unwrap();
        assert!(
            calls.try_recv().is_err(),
            "cannot issue terminate after joined shutdown"
        );
        assert!(session.cells.lock().unwrap().is_empty());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn native_task_handoff_mixed_child_and_root_same_call_no_cross_owner_or_lock() {
        let boundary = Arc::new(Boundary::new(Arc::new(
            crate::storage::MemoryAgentGraph::default(),
        )));
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3.0, Scope::Root).unwrap();
        let child = Arc::new(Control::descendant("child".into(), &root));
        // Both starts must be held concurrently. Serializing owners deadlocks
        // this barrier. Each real native dispatch task retains its own Id.
        let ready = Arc::new(tokio::sync::Barrier::new(3));
        let actual = Arc::new(Actual(AtomicUsize::new(0)));
        let original: Arc<dyn CodeModeSessionDelegate> = actual.clone();
        let run = |control: Arc<Control>| {
            let boundary = boundary.clone();
            let ready = ready.clone();
            let original = original.clone();
            tokio::spawn(async move {
                let (tx, mut rx) = mpsc::unbounded_channel();
                let session = Session::new(
                    Arc::new(CapturingSession { delegates: tx }),
                    boundary.clone(),
                );
                boundary.start_exec(
                    control.clone(),
                    direct_request(control.clone(), "same-turn").owner,
                    "same-model-id",
                    codex_code_mode::PUBLIC_TOOL_NAME,
                    None,
                    Ok(None),
                );
                ready.wait().await;
                session
                    .execute(exec_request(), original.clone(), None)
                    .await
                    .unwrap();
                let forwarded = rx.recv().await.unwrap();
                (control, Arc::ptr_eq(&original, &forwarded), forwarded)
            })
        };
        let parent_task = run(root);
        let child_task = run(child);
        tokio::time::timeout(Duration::from_secs(3), ready.wait())
            .await
            .unwrap();
        let (parent, child) = tokio::time::timeout(Duration::from_secs(3), async {
            tokio::join!(parent_task, child_task)
        })
        .await
        .unwrap();
        let (root, parent_unchanged, retained) = parent.unwrap();
        let (child, child_unchanged, forwarded) = child.unwrap();
        assert_eq!(root.id, "root");
        assert!(!parent_unchanged);
        assert_eq!(child.id, "child");
        // Tracking wraps every managed cell, while the assertion below proves
        // the opted-out child still forwards directly without policy callbacks.
        assert!(!child_unchanged);
        assert!(boundary.starts.lock().unwrap().is_empty());

        // The dispatch task is gone and native finish may have run. Only the
        // retained delegate owns this cell; it cannot adopt the next turn.
        boundary.finish_exec("root", "same-turn", "same-model-id");
        let invocation = CodeModeNestedToolCall {
            cell_id: CellId::new("retained".into()),
            runtime_tool_call_id: "rpc".into(),
            tool_name: ToolName::plain("update_plan"),
            tool_kind: CodeModeToolKind::Function,
            input: Some(json!({"plan":"effective"})),
        };
        let owner = async {
            let before = root.next().await.unwrap();
            assert_eq!(before["request"]["thread_id"], "root");
            assert_eq!(before["request"]["turn_id"], "same-turn");
            assert_eq!(before["request"]["outer_call_id"], "same-model-id");
            assert!(root.resolve(before["nonce"].as_u64().unwrap(), r#"{"action":"deny"}"#));
        };
        let (result, ()) = tokio::time::timeout(Duration::from_secs(3), async {
            tokio::join!(
                retained.invoke_tool(invocation.clone(), CancellationToken::new()),
                owner
            )
        })
        .await
        .unwrap();
        assert!(result.is_err());
        assert_eq!(actual.0.load(Ordering::SeqCst), 0);
        assert_eq!(
            forwarded
                .invoke_tool(invocation, CancellationToken::new())
                .await
                .unwrap(),
            json!({"from_actual":true})
        );
        assert_eq!(actual.0.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn tree_channel_nonces_stops_and_isolates_explicit_roots() {
        let root = Arc::new(Control::new("root".into(), true));
        // Ancestry may be registered before enable; never copy a timeout.
        let child = Arc::new(Control::descendant("a".into(), &root));
        let sibling = Arc::new(Control::descendant("b".into(), &root));
        let leaf = Arc::new(Control::descendant("leaf".into(), &child));
        let unrelated = Arc::new(Control::new("unrelated".into(), true));
        assert!(!child.configured());
        root.enable(3.0, Scope::ThreadTree).unwrap();
        unrelated.enable(3.0, Scope::Root).unwrap();
        assert!(leaf.configured());
        assert!(child.enable(3.0, Scope::ThreadTree).is_err());
        let run = |control: Arc<Control>| {
            tokio::spawn(async move {
                control
                    .decide(json!({"call_id":"collision"}), &CancellationToken::new())
                    .await
            })
        };
        let a = run(child.clone());
        let b = run(sibling);
        let g = run(leaf);
        let other = run(unrelated.clone());
        let mut notices = HashMap::new();
        for _ in 0..3 {
            let notice = root.next().await.unwrap();
            notices.insert(
                notice["request"]["thread_id"].as_str().unwrap().to_owned(),
                notice["nonce"].as_u64().unwrap(),
            );
        }
        assert_eq!(notices.len(), 3);
        let unique: std::collections::HashSet<_> = notices.values().collect();
        assert_eq!(unique.len(), 3);
        let isolated = unrelated.next().await.unwrap()["nonce"].as_u64().unwrap();
        assert_eq!(isolated, 1);
        child.revoke();
        // Must reject immediately, even before decide is scheduled to cancel.
        assert!(!root.resolve(notices["a"], r#"{"action":"continue"}"#));
        assert!(a.await.unwrap().is_err());
        assert!(root.resolve(notices["b"], r#"{"action":"continue"}"#));
        assert!(matches!(b.await.unwrap(), Ok(Decision::Continue)));
        root.revoke();
        assert!(!root.resolve(notices["leaf"], r#"{"action":"continue"}"#));
        assert!(g.await.unwrap().is_err());
        assert!(unrelated.resolve(isolated, r#"{"action":"continue"}"#));
        assert!(matches!(other.await.unwrap(), Ok(Decision::Continue)));
    }

    #[tokio::test]
    async fn delivered_child_decision_cannot_outlive_root_revoke() {
        // Poll by hand: resolving must NOT allow the child future to consume
        // its ready oneshot until the owning root has already been revoked.
        for decision in [
            json!({"action":"continue"}),
            json!({"action":"replace","value":{"quantity":7}}),
        ] {
            let root = Arc::new(Control::new("root".into(), true));
            let child = Arc::new(Control::descendant("child".into(), &root));
            let independent = Arc::new(Control::new("independent".into(), true));
            root.enable(3.0, Scope::ThreadTree).unwrap();
            independent.enable(3.0, Scope::Root).unwrap();
            let item = response(json!({
                "type":"function_call","call_id":"same-id","name":"record",
                "arguments":r#"{"quantity":2}"#,
            }));
            let child_request = direct_request(child, "child-turn");
            let other_request = direct_request(independent.clone(), "other-turn");
            let mut waiting = Box::pin(child_request.before(item.clone(), None));
            let mut other_waiting = Box::pin(other_request.before(item, None));
            assert!(futures_util::poll!(waiting.as_mut()).is_pending());
            assert!(futures_util::poll!(other_waiting.as_mut()).is_pending());
            let child_notice = root.next().await.unwrap();
            let other_notice = independent.next().await.unwrap();
            assert_eq!(child_notice["request"]["thread_id"], "child");
            assert_eq!(other_notice["request"]["thread_id"], "independent");
            assert!(root.resolve(
                child_notice["nonce"].as_u64().unwrap(),
                &decision.to_string()
            ));
            assert!(independent.resolve(
                other_notice["nonce"].as_u64().unwrap(),
                &decision.to_string()
            ));
            root.revoke();
            assert!(
                matches!(waiting.await, Err(ModelResponseError::InvalidRequest { message }) if message == FAILURE)
            );
            let admitted = serde_json::to_value(other_waiting.await.unwrap().0).unwrap();
            assert_eq!(admitted["call_id"], "same-id");
            assert_eq!(
                serde_json::from_str::<Value>(admitted["arguments"].as_str().unwrap()).unwrap(),
                if decision["action"] == "replace" {
                    json!({"quantity":7})
                } else {
                    json!({"quantity":2})
                }
            );
        }
    }

    #[tokio::test]
    async fn cold_spawn_ancestry_stops_at_the_nearest_live_control() {
        use codex_agent_graph_store::ThreadSpawnEdgeStatus;
        use codex_protocol::ThreadId;
        use codex_protocol::protocol::SubAgentSource;

        let graph = Arc::new(crate::storage::MemoryAgentGraph::default());
        let boundary = Boundary::new(graph.clone());
        let root_id = ThreadId::new();
        let parent_id = ThreadId::new();
        let source = SessionSource::SubAgent(SubAgentSource::ThreadSpawn {
            parent_thread_id: parent_id,
            depth: 2,
            agent_path: None,
            agent_nickname: None,
            agent_role: None,
        });
        let root = Arc::new(Control::new(root_id.to_string(), true));
        boundary
            .threads
            .lock()
            .unwrap()
            .insert(root.id.clone(), Arc::downgrade(&root));
        // Missing graph ancestry cannot select even the only live root.
        assert!(boundary.parent_control(&source).await.is_none());
        graph
            .restore_edge(
                parent_id,
                crate::storage::SpawnEdge {
                    parent: root_id,
                    status: ThreadSpawnEdgeStatus::Open,
                    closed_at: None,
                },
            )
            .await;
        let (ancestor, depth) = boundary.parent_control(&source).await.unwrap();
        assert!(Arc::ptr_eq(&ancestor, &root));
        assert_eq!(depth, 2);
        let cold_leaf = Arc::new(Control::descendant("leaf".into(), &ancestor));
        assert!(!cold_leaf.configured());
        // A live enable, not the restored edge, is what grants callbacks.
        root.enable(3.0, Scope::ThreadTree).unwrap();
        assert!(cold_leaf.configured());
        for unrelated_source in [
            SessionSource::Exec,
            SessionSource::SubAgent(SubAgentSource::Review),
            SessionSource::SubAgent(SubAgentSource::Compact),
            SessionSource::SubAgent(SubAgentSource::Other("not a spawn".into())),
        ] {
            assert!(boundary.parent_control(&unrelated_source).await.is_none());
        }
        let opted_out = Arc::new(Control::new(parent_id.to_string(), false));
        boundary
            .threads
            .lock()
            .unwrap()
            .insert(opted_out.id.clone(), Arc::downgrade(&opted_out));
        // The graph may lead to an enabled root, but it cannot override the
        // current intermediate owner (including a revoked one).
        let (first, depth) = boundary.parent_control(&source).await.unwrap();
        assert!(Arc::ptr_eq(&first, &opted_out));
        assert_eq!(depth, usize::MAX);
        assert!(!Control::descendant("other-leaf".into(), &first).configured());
        root.revoke();
        boundary.threads.lock().unwrap().remove(&opted_out.id);
        let (revoked, depth) = boundary.parent_control(&source).await.unwrap();
        assert!(Arc::ptr_eq(&revoked, &root));
        assert_eq!(depth, 2);
        let after_revoke = Arc::new(Control::descendant("last-leaf".into(), &revoked));
        assert!(after_revoke.configured());
        assert!(
            after_revoke
                .decide(json!({}), &CancellationToken::new())
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn abort_before_execute_cleans_native_tuple_from_different_task() {
        let boundary = Arc::new(Boundary::new(Arc::new(
            crate::storage::MemoryAgentGraph::default(),
        )));
        let ready = Arc::new(tokio::sync::Barrier::new(3));
        let survivor_release = Arc::new(tokio::sync::Notify::new());
        let first = {
            let boundary = boundary.clone();
            let ready = ready.clone();
            tokio::spawn(async move {
                let owner = Arc::new(Control::new("abort-root".into(), true));
                owner.enable(3.0, Scope::Root).unwrap();
                boundary.start_exec(
                    owner.clone(),
                    direct_request(owner, "turn").owner,
                    "same-model-id",
                    codex_code_mode::PUBLIC_TOOL_NAME,
                    None,
                    Ok(None),
                );
                ready.wait().await;
                std::future::pending::<()>().await;
            })
        };
        let survivor = {
            let boundary = boundary.clone();
            let ready = ready.clone();
            let release = survivor_release.clone();
            tokio::spawn(async move {
                let owner = Arc::new(Control::new("survivor".into(), true));
                owner.enable(3.0, Scope::Root).unwrap();
                boundary.start_exec(
                    owner.clone(),
                    direct_request(owner, "turn").owner,
                    "same-model-id",
                    codex_code_mode::PUBLIC_TOOL_NAME,
                    None,
                    Ok(None),
                );
                ready.wait().await;
                release.notified().await;
                boundary
                    .take_exec("same-model-id")
                    .unwrap()
                    .unwrap()
                    .control
                    .id
                    .clone()
            })
        };
        tokio::time::timeout(Duration::from_secs(3), ready.wait())
            .await
            .unwrap();
        first.abort();
        assert!(first.await.unwrap_err().is_cancelled());
        boundary.finish_exec("abort-root", "turn", "same-model-id");
        assert_eq!(boundary.starts.lock().unwrap().len(), 1);
        survivor_release.notify_one();
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(3), survivor)
                .await
                .unwrap()
                .unwrap(),
            "survivor"
        );
        assert!(boundary.starts.lock().unwrap().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn tools_only_policy_join_waits_for_callback_release_after_scoped_stop() {
        const BUDGET: usize = 8192;
        for finish in ["call", "root", "timeout", "decision"] {
            let control = Arc::new(Control::with_budget("root".into(), true, BUDGET));
            control.enable(3.0, Scope::Root).unwrap();
            let request = direct_request(control.clone(), "turn");
            let cancellation = CancellationToken::new();
            let item=serde_json::from_value(json!({"type":"function_call","call_id":"policy-call","name":"lookup","arguments":"{}"})).unwrap();
            let mut joined = Box::pin(request.admit_tools_only(item, &cancellation));
            assert!(futures_util::poll!(&mut joined).is_pending());
            let wire = control.next_wire(71).await.unwrap();
            let nonce = serde_json::from_str::<Value>(&wire).unwrap()["nonce"]
                .as_u64()
                .unwrap();
            assert!(control.handoff(nonce));
            match finish {
                "call" => cancellation.cancel(),
                "root" => request.owner.cancelled.cancel(),
                "timeout" => tokio::time::advance(Duration::from_secs(4)).await,
                _ => assert!(control.resolve(nonce, r#"{"action":"continue"}"#)),
            }
            assert!(futures_util::poll!(&mut joined).is_pending());
            // Cancellation must actively reach the SDK callback before joining
            // its finally cleanup; nonce fencing alone cannot wake that task.
            let cancel = control.next_wire(71).await.unwrap();
            assert_eq!(
                serde_json::from_str::<Value>(&cancel).unwrap()["cancel"],
                json!(nonce)
            );
            assert!(control.channel.bytes.available_permits() < BUDGET);
            if finish != "root" {
                assert!(!request.owner.cancelled.is_cancelled());
            }
            assert!(!control.resolve(nonce, r#"{"action":"continue"}"#));
            assert!(control.release(nonce));
            assert_eq!(joined.await.is_ok(), finish == "decision");
            assert_eq!(control.channel.bytes.available_permits(), BUDGET);
            assert!(control.channel.state.lock().unwrap().pending.is_empty());
        }
    }

    #[tokio::test]
    async fn cancelling_one_tools_only_policy_keeps_its_sibling_live() {
        let control = Arc::new(Control::new("root".into(), true));
        control.enable(3.0, Scope::Root).unwrap();
        let request = direct_request(control.clone(), "turn");
        let token = CancellationToken::new();
        let sibling_token = CancellationToken::new();
        let item = |id: &str| {
            serde_json::from_value(
                json!({"type":"function_call","call_id":id,"name":"lookup","arguments":"{}"}),
            )
            .unwrap()
        };
        let mut first = Box::pin(request.admit_tools_only(item("cancelled"), &token));
        let mut sibling = Box::pin(request.admit_tools_only(item("sibling"), &sibling_token));
        assert!(futures_util::poll!(&mut first).is_pending());
        assert!(futures_util::poll!(&mut sibling).is_pending());
        let mut nonces = Vec::new();
        for _ in 0..2 {
            let wire = control.next_wire(71).await.unwrap();
            let nonce = serde_json::from_str::<Value>(&wire).unwrap()["nonce"]
                .as_u64()
                .unwrap();
            assert!(control.handoff(nonce));
            nonces.push(nonce);
        }
        token.cancel();
        assert!(futures_util::poll!(&mut first).is_pending());
        let cancel = control.next_wire(71).await.unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&cancel).unwrap()["cancel"],
            json!(nonces[0])
        );
        assert!(!request.owner.cancelled.is_cancelled() && !sibling_token.is_cancelled());
        assert!(control.resolve(nonces[1], r#"{"action":"continue"}"#));
        assert!(futures_util::poll!(&mut sibling).is_pending());
        assert!(control.release(nonces[1]));
        assert!(sibling.await.is_ok());
        assert!(futures_util::poll!(&mut first).is_pending());
        assert!(control.release(nonces[0]));
        assert!(first.await.is_err());
    }

    fn direct_request(control: Arc<Control>, turn: &str) -> DirectRequest {
        direct_request_with_tools(control, turn, None)
    }

    fn direct_request_with_tools(
        control: Arc<Control>,
        turn: &str,
        selected: Option<Vec<(String, bool)>>,
    ) -> DirectRequest {
        let turn_store = Arc::new(ExtensionData::new(turn));
        let owner = turn_store.get_or_init(|| DirectTurn {
            sdk_admission: OnceLock::new(),
            id: turn.into(),
            model: Mutex::new(Some("actual-model".into())),
            cancelled: CancellationToken::new(),
            policy_stopped: AtomicBool::new(false),
            mcp_calls: Mutex::default(),
        });
        DirectRequest {
            tool_admission: Some(Arc::new(TestToolAdmission {
                thread: control.id.clone(),
                turn_store,
                selected,
            })),
            control,
            model: "actual-model".into(),
            owner,
        }
    }

    struct TestToolAdmission {
        thread: String,
        turn_store: Arc<ExtensionData>,
        selected: Option<Vec<(String, bool)>>,
    }
    impl std::fmt::Debug for TestToolAdmission {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("TestToolAdmission")
        }
    }
    impl ModelToolAdmission for TestToolAdmission {
        fn thread_id(&self) -> &str {
            &self.thread
        }
        fn turn_store(&self) -> &ExtensionData {
            &self.turn_store
        }
        fn admits(&self, item: &ResponseItem) -> bool {
            self.selected.as_ref().is_none_or(|selected| {
                let (name, function) = match item {
                    ResponseItem::FunctionCall { name, .. } => (name, true),
                    ResponseItem::CustomToolCall { name, .. } => (name, false),
                    _ => return false,
                };
                selected
                    .iter()
                    .any(|entry| entry == &(name.clone(), function))
            })
        }
    }

    #[tokio::test]
    async fn direct_unselected_and_wrong_kind_items_bypass_public_policy() {
        let control = Arc::new(Control::new("root".into(), true));
        control.enable(3.0, Scope::Root).unwrap();
        let request = direct_request_with_tools(
            control.clone(),
            "turn",
            Some(vec![("apply_patch".into(), false)]),
        );
        for item in [
            response(
                json!({"type":"function_call", "call_id":"same-id", "name":"exec_command", "arguments":"{}"}),
            ),
            response(
                json!({"type":"function_call", "call_id":"same-id", "name":"apply_patch", "arguments":"{}"}),
            ),
            response(
                json!({"type":"custom_tool_call", "call_id":"same-id", "name":"other_patch", "input":"patch"}),
            ),
        ] {
            let original = serde_json::to_value(&item).unwrap();
            let (forwarded, changed, release) = request.before(item, None).await.unwrap();
            assert_eq!(serde_json::to_value(forwarded).unwrap(), original);
            assert!(!changed && release.is_none());
            assert!(control.channel.receiver.lock().await.try_recv().is_err());
        }
    }

    #[tokio::test]
    async fn managed_generation_without_retained_owner_rejects_without_public_notice() {
        for (configured, kind) in [
            (false, ModelRequestKind::Generation),
            (true, ModelRequestKind::Warmup),
            (true, ModelRequestKind::Generation),
        ] {
            let control = Arc::new(Control::new("root".into(), true));
            if configured {
                control.enable(3.0, Scope::Root).unwrap();
            }
            let mut metadata = None;
            let interceptor = Boundary::response_interceptor(
                control.clone(),
                None,
                ModelRequestInput {
                    kind,
                    thread_id: "root",
                    model: "actual-model",
                    client_metadata: &mut metadata,
                },
            );
            if !configured || kind == ModelRequestKind::Warmup {
                assert!(interceptor.is_none());
                continue;
            }
            let item = response(
                json!({"type":"function_call", "call_id":"same-id", "name":"exec_command", "arguments":"{}"}),
            );
            let mut intercepted = interceptor.unwrap().intercept(Box::pin(stream::iter([Ok(
                ResponseEvent::OutputItemDone(item),
            )])));
            let error = intercepted.next().await.unwrap().unwrap_err();
            assert_eq!(error.to_string(), rejected().to_string());
            assert!(intercepted.next().await.is_none());
            assert!(control.channel.receiver.lock().await.try_recv().is_err());
        }
    }

    #[tokio::test]
    async fn direct_missing_or_mismatched_admission_rejects_before_public_policy() {
        for mismatch in ["missing", "thread", "same-id-new-turn"] {
            let control = Arc::new(Control::new("root".into(), true));
            control.enable(3.0, Scope::Root).unwrap();
            let mut request = direct_request(control.clone(), "same-turn-id");
            request.tool_admission = match mismatch {
                "missing" => None,
                "thread" => {
                    direct_request(Arc::new(Control::new("other".into(), true)), "same-turn-id")
                        .tool_admission
                }
                _ => direct_request(control.clone(), "same-turn-id").tool_admission,
            };
            let item = response(
                json!({"type":"function_call", "call_id":"same-id", "name":"exec_command", "arguments":"{}"}),
            );
            assert!(request.before(item, None).await.is_err());
            assert!(control.channel.receiver.lock().await.try_recv().is_err());
        }
    }

    #[tokio::test]
    async fn independent_and_child_direct_requests_retain_their_own_policy_admission() {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3.0, Scope::ThreadTree).unwrap();
        let child = Arc::new(Control::descendant("child".into(), &root));
        let root_request = direct_request_with_tools(
            root.clone(),
            "same-turn-id",
            Some(vec![("exec_command".into(), true)]),
        );
        let child_request = direct_request_with_tools(
            child.clone(),
            "same-turn-id",
            Some(vec![("apply_patch".into(), false)]),
        );
        let root_item = response(
            json!({"type":"function_call", "call_id":"same-id", "name":"exec_command", "arguments":"{}"}),
        );
        let child_item = response(
            json!({"type":"custom_tool_call", "call_id":"same-id", "name":"apply_patch", "input":"patch"}),
        );
        let approve = async {
            let mut seen = HashSet::new();
            for _ in 0..2 {
                let notice = root.next().await.unwrap();
                let thread = notice["request"]["thread_id"].as_str().unwrap();
                let expected_tool = match thread {
                    "root" => "exec_command",
                    "child" => "apply_patch",
                    _ => panic!("unknown owner"),
                };
                assert!(seen.insert(thread.to_owned()));
                assert_eq!(notice["request"]["tool_name"]["name"], expected_tool);
                assert_eq!(notice["request"]["call_id"], "same-id");
                assert!(root.resolve(
                    notice["nonce"].as_u64().unwrap(),
                    r#"{"action":"continue"}"#
                ));
            }
        };
        let (root_result, child_result, ()) = tokio::join!(
            root_request.before(root_item, None),
            child_request.before(child_item, None),
            approve,
        );
        assert!(root_result.is_ok() && child_result.is_ok());
    }

    fn before_request<'a>(
        owner: &'a Arc<DirectTurn>,
        name: &'a str,
    ) -> Request<'a, (), (), &'static str> {
        Request {
            tracing_context: None,
            owner: Some(owner),
            phase: "before",
            route: "code_mode",
            boundary: "pre_dispatch",
            turn_id: &owner.id,
            model: None,
            call_id: None,
            outer_call_id: Some("exec"),
            cell_id: None,
            runtime_tool_call_id: Some("selected"),
            tool_name: Name {
                namespace: None,
                name,
            },
            tool_kind: "function_json",
            input: (),
            result: (),
        }
    }

    fn response(value: Value) -> ResponseItem {
        serde_json::from_value(value).expect("test ResponseItem")
    }

    #[tokio::test(start_paused = true)]
    async fn first_edited_overlap_waits_for_physical_notice_release_or_stop() {
        const BUDGET: usize = 8192;
        for (elapsed, finish) in [
            (false, "release"),
            (false, "cancel"),
            (false, "revoke"),
            (true, "release"),
            (true, "cancel"),
            (true, "revoke"),
        ] {
            let control = Arc::new(Control::with_budget("root".into(), true, BUDGET));
            control.enable(3.0, Scope::Root).unwrap();
            // An independent reservation remains charged throughout. Both
            // the first notice and its edited item fit alone, but not together.
            let sibling = control
                .account(&json!({"sibling": "s".repeat(900)}), None)
                .unwrap();
            let after_sibling = control.channel.bytes.available_permits();
            let display = response(json!({
                "type":"message", "id":"overlap", "role":"assistant",
                "content":[{"type":"output_text", "text":"still displayed"}]
            }));
            let first = response(json!({
                "type":"function_call", "id":"first-item", "call_id":"first",
                "name":"exec_command", "arguments":json!({"cmd": "a".repeat(4500)}).to_string(),
                "encrypted_function_args":[]
            }));
            let upstream = Box::pin(stream::iter(vec![
                Ok(ResponseEvent::OutputItemAdded(display.clone())),
                Ok(ResponseEvent::OutputItemDone(first)),
                Ok(ResponseEvent::OutputItemDone(display)),
            ]));
            let request = direct_request(control.clone(), "turn");
            let owner = request.owner.clone();
            let mut intercepted = Box::new(request).intercept(upstream);
            assert!(matches!(
                intercepted.next().await,
                Some(Ok(ResponseEvent::OutputItemAdded(_)))
            ));
            let mut display_done = Box::pin(intercepted.next());
            assert!(futures_util::poll!(&mut display_done).is_pending());
            let wire = control.next_wire(71).await.unwrap();
            let nonce = serde_json::from_str::<Value>(&wire).unwrap()["nonce"]
                .as_u64()
                .unwrap();
            assert!(control.handoff(nonce));
            let after_notice = control.channel.bytes.available_permits();
            assert_eq!(after_notice, after_sibling - wire.len());
            assert!(
                control.resolve(
                    nonce,
                    &json!({
                        "action":"replace", "value":{"cmd": "b".repeat(4500)}
                    })
                    .to_string()
                )
            );
            assert!(futures_util::poll!(&mut display_done).is_pending());
            assert_eq!(control.channel.bytes.available_permits(), after_notice);
            if elapsed {
                // No scheduler guess: accept within the three-second decision
                // deadline, then hold Python's notice beyond it.
                tokio::time::advance(Duration::from_secs(4)).await;
                assert!(futures_util::poll!(&mut display_done).is_pending());
                assert_eq!(control.channel.bytes.available_permits(), after_notice);
            }
            if finish != "release" {
                if finish == "cancel" {
                    owner.cancelled.cancel();
                } else {
                    control.revoke();
                }
                assert!(matches!(display_done.await, Some(Err(_))));
                // Stop cannot free a still physically owned notice.
                assert_eq!(control.channel.bytes.available_permits(), after_notice);
                assert!(control.release(nonce));
            } else {
                assert!(control.release(nonce));
                assert!(matches!(
                    display_done.await,
                    Some(Ok(ResponseEvent::OutputItemDone(
                        ResponseItem::Message { .. }
                    )))
                ));
                let Some(Ok(ResponseEvent::OutputItemDone(first))) = intercepted.next().await
                else {
                    panic!("first edited call must follow its display item");
                };
                let first = serde_json::to_value(first).unwrap();
                assert_eq!(first["call_id"], "first");
                assert_eq!(
                    serde_json::from_str::<Value>(first["arguments"].as_str().unwrap()).unwrap()["cmd"],
                    "b".repeat(4500)
                );
            }
            assert!(!control.release(nonce));
            assert!(!control.resolve(nonce, r#"{"action":"continue"}"#));
            assert_eq!(control.channel.bytes.available_permits(), after_sibling);
            drop(sibling);
            assert_eq!(control.channel.bytes.available_permits(), BUDGET);
        }
    }

    #[tokio::test]
    async fn fifo_queued_notice_keeps_bytes_until_physical_release() {
        const BUDGET: usize = 8192;
        let control = Arc::new(Control::with_budget("root".into(), true, BUDGET));
        control.enable(3.0, Scope::Root).unwrap();
        let display = response(json!({
            "type":"message", "id":"msg_overlap", "role":"assistant",
            "content":[{"type":"output_text", "text":"display"}]
        }));
        let first = response(json!({
            "type":"function_call", "id":"fc_first", "call_id":"first",
            "name":"exec_command", "arguments":"{\"cmd\":\"original\"}",
            "encrypted_function_args":[]
        }));
        // The before notice does not include the provider item ID. This makes
        // its existing reservation large enough for the notice without growth.
        let second = response(json!({
            "type":"function_call", "id":format!("fc_{}", "q".repeat(4096)), "call_id":"second",
            "name":"exec_command", "arguments":"{\"cmd\":\"later\"}",
            "encrypted_function_args":[]
        }));
        let second_bytes = serde_json::to_vec(&second).unwrap().len();
        let upstream = Box::pin(stream::iter(vec![
            Ok(ResponseEvent::OutputItemAdded(display.clone())),
            Ok(ResponseEvent::OutputItemDone(first)),
            Ok(ResponseEvent::OutputItemDone(second)),
            Ok(ResponseEvent::OutputItemDone(display)),
            Ok(ResponseEvent::Created {
                response_id: Some("only-after-fifo".into()),
            }),
        ]));
        let mut intercepted = Box::new(direct_request(control.clone(), "turn")).intercept(upstream);
        assert!(matches!(
            intercepted.next().await,
            Some(Ok(ResponseEvent::OutputItemAdded(
                ResponseItem::Message { .. }
            )))
        ));
        let mut display_done = Box::pin(intercepted.next());
        assert!(futures_util::poll!(&mut display_done).is_pending());
        let first_notice = control.next().await.unwrap();
        assert_eq!(first_notice["request"]["call_id"], "first");
        let first_nonce = first_notice["nonce"].as_u64().unwrap();
        assert!(control.handoff(first_nonce));
        assert!(control.resolve(
            first_nonce,
            r#"{"action":"replace","value":{"cmd":"edited"}}"#
        ));
        assert!(control.release(first_nonce));
        assert!(matches!(
            display_done.await,
            Some(Ok(ResponseEvent::OutputItemDone(
                ResponseItem::Message { .. }
            )))
        ));
        assert_eq!(control.next().await.unwrap(), json!({"cancel":first_nonce}));
        let Some(Ok(ResponseEvent::OutputItemDone(first))) = intercepted.next().await else {
            panic!("first queued edited call must follow its display item");
        };
        let first = serde_json::to_value(first).unwrap();
        assert_eq!(first["call_id"], "first");
        assert_eq!(first["arguments"], r#"{"cmd":"edited"}"#);
        assert_eq!(
            control.channel.bytes.available_permits(),
            BUDGET - second_bytes
        );

        // This real semaphore waiter observes when ALL bytes cease to be
        // owned. Tokio assigns released credits to its FIFO waiters before a
        // try-acquire can take them; no scheduler timing or sleeping is needed.
        let mut all_bytes = Box::pin(
            control
                .channel
                .bytes
                .clone()
                .acquire_many_owned(BUDGET as u32),
        );
        assert!(futures_util::poll!(&mut all_bytes).is_pending());
        let mut second_done = Box::pin(intercepted.next());
        assert!(
            futures_util::poll!(&mut second_done).is_pending(),
            "queued second call released its bytes before awaiting its policy notice"
        );
        assert!(futures_util::poll!(&mut all_bytes).is_pending());
        let wire = control.next_wire(23).await.unwrap();
        let notice: Value = serde_json::from_str(&wire).unwrap();
        assert_eq!(notice["request"]["call_id"], "second");
        assert!(wire.len() <= second_bytes);
        let nonce = notice["nonce"].as_u64().unwrap();
        assert_ne!(nonce, first_nonce);
        assert!(control.handoff(nonce));
        assert!(control.resolve(nonce, r#"{"action":"continue"}"#));
        let Some(Ok(ResponseEvent::OutputItemDone(second))) = second_done.await else {
            panic!("second queued call must retain FIFO position and receive its policy");
        };
        assert_eq!(serde_json::to_value(second).unwrap()["call_id"], "second");
        assert!(!control.resolve(nonce, r#"{"action":"continue"}"#));
        assert!(futures_util::poll!(&mut all_bytes).is_pending());
        assert!(control.release(nonce));
        assert!(!control.release(nonce));
        let all_bytes = all_bytes.await.unwrap();
        assert_eq!(all_bytes.num_permits(), BUDGET);
        drop(all_bytes);
        assert_eq!(control.channel.bytes.available_permits(), BUDGET);
        assert_eq!(control.next().await.unwrap(), json!({"cancel":nonce}));
        let Some(Ok(ResponseEvent::Created { response_id })) = intercepted.next().await else {
            panic!("upstream must resume after both queued calls");
        };
        assert_eq!(response_id.as_deref(), Some("only-after-fifo"));
        assert!(intercepted.next().await.is_none());
    }

    #[tokio::test]
    async fn direct_stream_preserves_identity_shape_and_native_event_sequence() {
        let control = Arc::new(Control::new("root".into(), true));
        control.enable(3.0, Scope::Root).unwrap();
        let item = response(json!({
            "type":"function_call", "id":"fc_server", "call_id":"real-native-call",
            "name":"update_plan", "namespace":"functions",
            "arguments":"{\"plan\":[]}", "encrypted_function_args":[],
        }));
        assert_eq!(item.id().unwrap().as_str(), "fc_server");
        let upstream = Box::pin(stream::iter(vec![
            Ok(ResponseEvent::Created {
                response_id: Some("real-provider".into()),
            }),
            Ok(ResponseEvent::OutputItemDone(item)),
            Ok(ResponseEvent::Created {
                response_id: Some("next-provider-event".into()),
            }),
        ]));
        let request = direct_request(control.clone(), "turn-a");
        // A later request must not relabel a provider response still in flight.
        *request.owner.model.lock().unwrap() = Some("later-model".into());
        let mut intercepted = Box::new(request).intercept(upstream);
        assert!(matches!(
            intercepted.next().await,
            Some(Ok(ResponseEvent::Created { response_id: Some(id) })) if id == "real-provider"
        ));
        let edit = async {
            let req = control.next().await.unwrap();
            assert_eq!(req["request"]["route"], "direct");
            assert_eq!(req["request"]["turn_id"], "turn-a");
            assert_eq!(req["request"]["model"], "actual-model");
            assert_eq!(req["request"]["agent_role"], Value::Null);
            assert_eq!(req["request"]["call_id"], "real-native-call");
            assert!(control.resolve(
                req["nonce"].as_u64().unwrap(),
                r#"{"action":"replace","value":{"plan":[],"explanation":"effective"}}"#
            ));
        };
        let (event, ()) = tokio::join!(intercepted.next(), edit);
        let Some(Ok(ResponseEvent::OutputItemDone(item))) = event else {
            panic!("missing actual item")
        };
        assert!(item.id().is_none());
        assert!(matches!(&item, ResponseItem::FunctionCall { call_id, .. }
            if call_id == "real-native-call"));
        assert_eq!(
            serde_json::to_value(item).unwrap(),
            json!({
                "type": "function_call", "call_id": "real-native-call",
                "name": "update_plan", "namespace": "functions",
                "arguments": json!({"plan": [], "explanation": "effective"}).to_string(),
                "encrypted_function_args": [],
            })
        );
        assert!(matches!(
            intercepted.next().await,
            Some(Ok(ResponseEvent::Created { response_id: Some(id) })) if id == "next-provider-event"
        ));
        assert!(intercepted.next().await.is_none());
    }

    // The boundary cannot produce real provider ciphertext. Exercise native
    // metadata semantics here with opaque stand-ins that never reach a handler.
    // Native integration covers actual plaintext [] dispatch and rejected
    // collaboration edits without a plaintext marker.
    #[tokio::test]
    async fn encrypted_function_replacements_preserve_named_and_implicit_fields() {
        for (metadata, value, accepted) in [
            (
                json!(["message"]),
                json!({"task_name":"new","message":"opaque"}),
                true,
            ),
            (
                json!(["message"]),
                json!({"task_name":"old","message":"edited"}),
                false,
            ),
            (json!(["message"]), json!({"task_name":"old"}), false),
            (
                json!(["message"]),
                json!({"task_name":"old","message":null}),
                false,
            ),
            // Core assumes message encryption even if only another arg is
            // listed. Never infer message plaintext from a nonempty list.
            (
                json!(["task_name"]),
                json!({"task_name":"old","message":"edited"}),
                false,
            ),
            (
                json!(["task_name"]),
                json!({"task_name":"new","message":"opaque"}),
                false,
            ),
            (
                Value::Null,
                json!({"task_name":"new","message":"opaque"}),
                true,
            ),
            (
                Value::Null,
                json!({"task_name":"old","message":"edited"}),
                false,
            ),
            (
                json!([]),
                json!({"task_name":"new","message":"edited"}),
                true,
            ),
        ] {
            let control = Arc::new(Control::new("root".into(), true));
            control.enable(3.0, Scope::Root).unwrap();
            let item = response(json!({
                "type":"function_call", "id":"fc_original", "call_id":"original",
                "name":"spawn_agent", "namespace":"collaboration",
                "arguments":r#"{"task_name":"old","message":"opaque"}"#,
                "encrypted_function_args":metadata,
            }));
            assert_eq!(item.id().unwrap().as_str(), "fc_original");
            let request = direct_request(control.clone(), "turn");
            let edit = async {
                let req = control.next().await.unwrap();
                assert!(control.resolve(
                    req["nonce"].as_u64().unwrap(),
                    &json!({"action":"replace","value":value}).to_string(),
                ));
            };
            let (result, ()) = tokio::join!(request.before(item, None), edit);
            if accepted {
                let (item, changed, _) = result.unwrap();
                assert!(changed);
                assert!(item.id().is_none());
                assert!(matches!(&item, ResponseItem::FunctionCall { call_id, .. }
                    if call_id == "original"));
                let mut expected = json!({
                    "type": "function_call", "call_id": "original",
                    "name": "spawn_agent", "namespace": "collaboration",
                    "arguments": value.to_string(),
                });
                if !metadata.is_null() {
                    expected["encrypted_function_args"] = metadata;
                }
                assert_eq!(serde_json::to_value(item).unwrap(), expected);
            } else {
                assert!(
                    matches!(result, Err(ModelResponseError::InvalidRequest { message })
                    if message == FAILURE)
                );
            }
        }
    }

    #[tokio::test]
    async fn continue_never_parses_protected_arguments() {
        let control = Arc::new(Control::new("root".into(), true));
        control.enable(3.0, Scope::Root).unwrap();
        let item = response(json!({
            "type":"function_call","call_id":"unchanged","name":"send_message",
            "namespace":"collaboration", "arguments":"not JSON",
            "encrypted_function_args":["message"],
        }));
        let original = serde_json::to_value(&item).unwrap();
        let request = direct_request(control.clone(), "turn");
        let proceed = async {
            let req = control.next().await.unwrap();
            assert!(control.resolve(req["nonce"].as_u64().unwrap(), r#"{"action":"continue"}"#));
        };
        let (result, ()) = tokio::join!(request.before(item, None), proceed);
        assert_eq!(serde_json::to_value(result.unwrap().0).unwrap(), original);
    }

    #[tokio::test]
    async fn deny_stops_only_our_stream_original_errors_do_not_invent_completion() {
        let control = Arc::new(Control::new("root".into(), true));
        control.enable(3.0, Scope::Root).unwrap();
        let upstream = Box::pin(stream::iter(vec![
            Err(ModelResponseError::InvalidRequest {
                message: "original-provider-error".into(),
            }),
            Ok(ResponseEvent::OutputItemDone(response(json!({
                "type":"custom_tool_call", "call_id":"cm", "name":"exec", "input":"text(1)",
            })))),
            Ok(ResponseEvent::Created {
                response_id: Some("must-not-surface".into()),
            }),
        ]));
        let mut intercepted =
            Box::new(direct_request(control.clone(), "turn-a")).intercept(upstream);
        assert!(
            matches!(intercepted.next().await, Some(Err(ModelResponseError::InvalidRequest { message }))
            if message == "original-provider-error")
        );
        let deny = async {
            let req = control.next().await.unwrap();
            assert_eq!(req["request"]["tool_kind"], "freeform");
            assert!(control.resolve(req["nonce"].as_u64().unwrap(), r#"{"action":"deny"}"#));
        };
        let (event, ()) = tokio::join!(intercepted.next(), deny);
        assert!(
            matches!(event, Some(Err(ModelResponseError::InvalidRequest { message })) if message == FAILURE)
        );
        assert!(intercepted.next().await.is_none());
    }

    #[tokio::test]
    async fn turn_retirement_and_dropped_stream_cancel_python_reservations() {
        let control = Arc::new(Control::new("root".into(), true));
        control.enable(3.0, Scope::Root).unwrap();
        let request = direct_request(control.clone(), "old-turn");
        let retired = request.owner.cancelled.clone();
        let upstream = Box::pin(stream::iter(vec![Ok(ResponseEvent::OutputItemDone(
            response(json!({
                "type":"function_call", "call_id":"same-id", "name":"update_plan", "arguments":"{}"
            })),
        ))]));
        let mut intercepted = Box::new(request).intercept(upstream);
        let cancel = async {
            let req = control.next().await.unwrap();
            retired.cancel();
            let end = control.next().await.unwrap();
            let nonce = req["nonce"].as_u64().unwrap();
            assert_eq!(end["cancel"], nonce);
            assert!(!control.resolve(nonce, r#"{"action":"replace","value":{}}"#));
        };
        let (event, ()) = tokio::join!(intercepted.next(), cancel);
        assert!(matches!(
            event,
            Some(Err(ModelResponseError::InvalidRequest { .. }))
        ));

        let next_request = direct_request(control.clone(), "next-turn");
        let upstream = Box::pin(stream::iter(vec![Ok(ResponseEvent::OutputItemDone(
            response(json!({
                "type":"function_call", "call_id":"same-id", "name":"update_plan", "arguments":"{}"
            })),
        ))]));
        let mut fresh = Box::new(next_request).intercept(upstream);
        let mut in_progress = Box::pin(fresh.next());
        let nonce = tokio::select! {
            _ = &mut in_progress => panic!("callback did not await"),
            msg = control.next() => {
                let msg = msg.unwrap();
                assert_eq!(msg["request"]["turn_id"], "next-turn");
                msg["nonce"].as_u64().unwrap()
            }
        };
        drop(in_progress);
        drop(fresh); // same as native mapper cancelling on consumer drop
        assert_eq!(control.next().await.unwrap()["cancel"], nonce);
        assert!(!control.resolve(nonce, r#"{"action":"continue"}"#));
    }

    #[tokio::test]
    async fn client_search_validates_replacement_but_does_not_gate_server_or_missing_id() {
        let control = Arc::new(Control::new("root".into(), true));
        control.enable(3.0, Scope::Root).unwrap();
        let request = direct_request(control.clone(), "turn-search");
        for value in [
            json!({"type":"tool_search_call","call_id":"server","execution":"server","arguments":{"query":"x"}}),
            json!({"type":"tool_search_call","execution":"client","arguments":{"query":"x"}}),
            json!({"type":"web_search_call","status":"completed","action":{"type":"search","query":"x"}}),
        ] {
            let input = response(value);
            let before = serde_json::to_value(&input).unwrap();
            let out = request.before(input, None).await.unwrap();
            assert_eq!(serde_json::to_value(out.0).unwrap(), before);
        }
        let client = response(json!({
            "type":"tool_search_call","call_id":"real-search","execution":"client",
            "arguments":{"query":"old","limit":1}
        }));
        let edit = async {
            let req = control.next().await.unwrap();
            assert_eq!(req["request"]["tool_kind"], "tool_search");
            assert_eq!(req["request"]["input"], json!({"query":"old","limit":1}));
            assert!(control.resolve(
                req["nonce"].as_u64().unwrap(),
                r#"{"action":"replace","value":{"query":23,"limit":-1}}"#
            ));
        };
        let (event, ()) = tokio::join!(request.before(client, None), edit);
        assert!(matches!(
            event,
            Err(ModelResponseError::InvalidRequest { .. })
        ));
    }

    #[test]
    fn direct_edit_keeps_native_success_identity_and_external_context() {
        let payload = ToolPayload::Function {
            arguments: r#"{"path":"file"}"#.into(),
        };
        let output = EditedOutput {
            original: Box::new(
                codex_tools::JsonToolOutput::with_success(json!("original"), Some(false))
                    .with_external_context(),
            ),
            value: json!("edited"),
            body: None,
        };
        assert!(!output.success_for_logging());
        assert!(output.contains_external_context());
        // Logging remains the original diagnostic, and is never the edited
        // authoritative value supplied to Core.
        assert_eq!(output.log_output(), r#""original""#);
        let ResponseInputItem::FunctionCallOutput {
            call_id,
            output: response,
        } = output.to_response_item("native-call-id", &payload)
        else {
            panic!("wrong response kind");
        };
        assert_eq!(call_id, "native-call-id");
        assert_eq!(response.success, Some(false));
        assert_eq!(response.body.to_text().unwrap(), r#""edited""#);
        assert_eq!(
            output.post_tool_use_response("native-call-id", &payload),
            Some(json!("edited"))
        );
    }

    struct Actual(AtomicUsize);
    impl CodeModeSessionDelegate for Actual {
        fn invoke_tool<'a>(
            &'a self,
            inv: CodeModeNestedToolCall,
            _: CancellationToken,
        ) -> ToolInvocationFuture<'a> {
            Box::pin(async move {
                self.0.fetch_add(1, Ordering::SeqCst);
                assert_eq!(inv.input, Some(json!({"plan":"effective"})));
                Ok(json!({"from_actual":true}))
            })
        }
        fn notify<'a>(
            &'a self,
            _: String,
            _: CellId,
            _: String,
            _: CancellationToken,
        ) -> NotificationFuture<'a> {
            Box::pin(async { Ok(()) })
        }
        fn cell_closed(&self, _: &CellId) {}
    }

    #[tokio::test]
    async fn owned_execute_keeps_before_policy_on_its_issuing_owner_across_startup_task() {
        tokio::spawn(async move {
            let control = cell_owner_control(None, Some("write_stdin"), false);
            let owner = direct_request(control.clone(), "original-turn").owner;
            *owner.model.lock().unwrap() = Some(Arc::from("issuing-model"));
            let boundary = Arc::new(Boundary::new(Arc::new(
                crate::storage::MemoryAgentGraph::default(),
            )));
            let (tx, mut rx) = mpsc::unbounded_channel();
            let session = Session::new(
                Arc::new(CapturingSession { delegates: tx }),
                boundary.clone(),
            );
            boundary.start_exec(
                control.clone(),
                owner.clone(),
                "same-model-id",
                codex_code_mode::PUBLIC_TOOL_NAME,
                None,
                Ok(None),
            );
            let actual = Arc::new(Actual(AtomicUsize::new(0)));
            let issuing_task = tokio::task::id();
            let execution = session
                .clone()
                .execute_owned(exec_request(), actual.clone(), None);
            assert!(boundary.starts.lock().unwrap().is_empty());
            let started = tokio::spawn(async move {
                assert_ne!(tokio::task::id(), issuing_task);
                execution.await
            })
            .await
            .unwrap()
            .unwrap();
            let delegate = rx.recv().await.unwrap();
            let invocation = CodeModeNestedToolCall {
                cell_id: started.cell_id.clone(),
                runtime_tool_call_id: "runtime-original".into(),
                tool_name: ToolName::plain("write_stdin"),
                tool_kind: CodeModeToolKind::Function,
                input: Some(json!({"session_id": 731031})),
            };
            let mut call = Box::pin(delegate.invoke_tool(invocation, CancellationToken::new()));
            assert!(futures_util::poll!(&mut call).is_pending());
            let notice = next_cell_notice(&control).await;
            assert_eq!(notice["request"]["phase"], "before");
            assert_eq!(notice["request"]["tool_kind"], "function");
            assert_eq!(notice["request"]["input"], json!({"session_id":731031}));
            assert_eq!(notice["request"]["turn_id"], owner.id);
            assert_eq!(notice["request"]["model"], "issuing-model");
            assert_eq!(actual.0.load(Ordering::SeqCst), 0);
            let nonce = notice["nonce"].as_u64().unwrap();
            assert!(control.check(nonce) && control.handoff(nonce));
            assert!(control.resolve(nonce, r#"{"action":"deny"}"#));
            assert!(call.await.is_err());
            assert!(control.release(nonce));
            assert_eq!(actual.0.load(Ordering::SeqCst), 0);
            let mut returned = Box::pin(started.initial_response());
            assert!(futures_util::poll!(&mut returned).is_pending());
            let binding = next_cell_notice(&control).await;
            assert!(binding.get("cell_owner").is_some());
            let nonce = binding["nonce"].as_u64().unwrap();
            assert!(control.check(nonce) && control.handoff(nonce));
            assert!(control.resolve(nonce, r#"{"action":"continue"}"#));
            assert!(matches!(
                returned.await.unwrap(),
                RuntimeResponse::Result { .. }
            ));
            assert!(control.release(nonce));
            assert!(session.cells.lock().unwrap().is_empty());
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn owned_execute_rejects_missing_or_mismatched_handoff_without_starting_backend() {
        tokio::spawn(async move {
            for mismatch in [false, true] {
                let boundary = Arc::new(Boundary::new(Arc::new(
                    crate::storage::MemoryAgentGraph::default(),
                )));
                let (tx, mut rx) = mpsc::unbounded_channel();
                let session = Session::new(
                    Arc::new(CapturingSession { delegates: tx }),
                    boundary.clone(),
                );
                if mismatch {
                    let control = Arc::new(Control::new("root".into(), true));
                    let turn = direct_request(control.clone(), "turn").owner;
                    boundary.start_exec(
                        control,
                        turn,
                        "different-call",
                        codex_code_mode::PUBLIC_TOOL_NAME,
                        None,
                        Ok(None),
                    );
                }
                let result = session
                    .clone()
                    .execute_owned(
                        exec_request(),
                        Arc::new(codex_code_mode::NoopCodeModeSessionDelegate),
                        None,
                    )
                    .await;
                assert_eq!(result.err().as_deref(), Some(FAILURE));
                assert!(matches!(
                    rx.try_recv(),
                    Err(mpsc::error::TryRecvError::Empty)
                ));
                assert!(boundary.starts.lock().unwrap().is_empty());
                assert!(session.cells.lock().unwrap().is_empty());
            }
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn unpolled_owned_capture_drops_without_consuming_another_tasks_handoff() {
        tokio::spawn(async move {
            let boundary = Arc::new(Boundary::new(Arc::new(
                crate::storage::MemoryAgentGraph::default(),
            )));
            let (tx, mut rx) = mpsc::unbounded_channel();
            let session = Session::new(
                Arc::new(CapturingSession { delegates: tx }),
                boundary.clone(),
            );
            let control = Arc::new(Control::new("root".into(), true));
            let weak = Arc::downgrade(&control);
            let turn = direct_request(control.clone(), "turn").owner;
            boundary.start_exec(
                control,
                turn,
                "same-model-id",
                codex_code_mode::PUBLIC_TOOL_NAME,
                None,
                Ok(None),
            );
            let other = session.clone();
            let rejected = tokio::spawn(async move {
                other
                    .execute_owned(
                        exec_request(),
                        Arc::new(codex_code_mode::NoopCodeModeSessionDelegate),
                        None,
                    )
                    .await
            })
            .await
            .unwrap();
            assert_eq!(rejected.err().as_deref(), Some(FAILURE));
            assert_eq!(boundary.starts.lock().unwrap().len(), 1);
            let execution = session.clone().execute_owned(
                exec_request(),
                Arc::new(codex_code_mode::NoopCodeModeSessionDelegate),
                None,
            );
            assert!(boundary.starts.lock().unwrap().is_empty());
            assert!(weak.upgrade().is_some());
            drop(execution);
            assert!(weak.upgrade().is_none());
            assert!(matches!(
                rx.try_recv(),
                Err(mpsc::error::TryRecvError::Empty)
            ));
            assert!(session.cells.lock().unwrap().is_empty());
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn unconfigured_root_and_child_owned_calls_keep_their_exact_handoffs() {
        tokio::spawn(async move {
            for root in [true, false] {
                let boundary = Arc::new(Boundary::new(Arc::new(
                    crate::storage::MemoryAgentGraph::default(),
                )));
                let (tx, mut rx) = mpsc::unbounded_channel();
                let session = Session::new(
                    Arc::new(CapturingSession { delegates: tx }),
                    boundary.clone(),
                );
                let control = Arc::new(Control::new(
                    if root { "root" } else { "child" }.into(),
                    root,
                ));
                let turn = direct_request(control.clone(), "turn").owner;
                assert!(!control.configured());
                boundary.start_exec(
                    control.clone(),
                    turn.clone(),
                    "same-model-id",
                    codex_code_mode::PUBLIC_TOOL_NAME,
                    None,
                    Ok(None),
                );
                let execution = session.clone().execute_owned(
                    exec_request(),
                    Arc::new(codex_code_mode::NoopCodeModeSessionDelegate),
                    None,
                );
                let started = tokio::spawn(execution).await.unwrap().unwrap();
                rx.recv().await.unwrap();
                {
                    let cells = session.cells.lock().unwrap();
                    let cell = cells
                        .get(&CellKey::Started(started.cell_id.clone()))
                        .unwrap();
                    assert!(Arc::ptr_eq(&cell.control, &control));
                    assert!(Arc::ptr_eq(&cell.turn, &turn));
                }
                assert!(matches!(
                    started.initial_response().await.unwrap(),
                    RuntimeResponse::Result { .. }
                ));
                assert!(session.cells.lock().unwrap().is_empty());
            }
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn borrowed_internal_execute_preserves_its_unmanaged_passthrough() {
        let boundary = Arc::new(Boundary::new(Arc::new(
            crate::storage::MemoryAgentGraph::default(),
        )));
        let (tx, mut rx) = mpsc::unbounded_channel();
        let session = Session::new(
            Arc::new(CapturingSession { delegates: tx }),
            boundary.clone(),
        );
        let original: Arc<dyn CodeModeSessionDelegate> =
            Arc::new(codex_code_mode::NoopCodeModeSessionDelegate);
        let started = session
            .execute(exec_request(), original.clone(), None)
            .await
            .unwrap();
        assert!(Arc::ptr_eq(&rx.recv().await.unwrap(), &original));
        assert!(matches!(
            started.initial_response().await.unwrap(),
            RuntimeResponse::Result { .. }
        ));
        assert!(session.cells.lock().unwrap().is_empty());
        assert!(boundary.starts.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn delegate_awaits_actual_once_before_returning_edited_result() {
        let control = Arc::new(Control::new("thread-a".into(), true));
        control.enable(3.0, Scope::Root).unwrap();
        let actual = Arc::new(Actual(AtomicUsize::new(0)));
        let delegate = Delegate {
            profile_source: None,
            cell_owner: Mutex::new(None),
            inner: actual.clone(),
            live: Arc::new(AtomicBool::new(true)),
            control: Some(control.clone()),
            native_control: None,
            turn: direct_request(control.clone(), "turn-a").owner,
            model: Some("cell-model".into()),
            outer_call_id: "model-exec".into(),
            rules: None,
        };
        let invocation = CodeModeNestedToolCall {
            cell_id: CellId::new("cell-a".into()),
            runtime_tool_call_id: "rpc-a".into(),
            tool_name: ToolName::plain("update_plan"),
            tool_kind: CodeModeToolKind::Function,
            input: Some(json!({"plan":"old"})),
        };
        let owner = async {
            let before = control.next().await.unwrap();
            assert_eq!(before["request"]["boundary"], "pre_dispatch");
            assert_eq!(before["request"]["call_id"], Value::Null);
            assert_eq!(before["request"]["runtime_tool_call_id"], "rpc-a");
            assert_eq!(before["request"]["model"], "cell-model");
            assert_eq!(actual.0.load(Ordering::SeqCst), 0);
            assert!(control.resolve(
                before["nonce"].as_u64().unwrap(),
                r#"{"action":"replace","value":{"plan":"effective"}}"#
            ));
            let cancel = control.next().await.unwrap();
            assert!(cancel.get("cancel").is_some());
            let after = control.next().await.unwrap();
            assert_eq!(actual.0.load(Ordering::SeqCst), 1);
            assert_eq!(after["request"]["result"], json!({"from_actual":true}));
            assert_eq!(after["request"]["input"], json!({"plan":"effective"}));
            assert_eq!(after["request"]["model"], "cell-model");
            assert!(control.resolve(
                after["nonce"].as_u64().unwrap(),
                r#"{"action":"replace","value":{"receipt":"consumer"}}"#
            ));
        };
        let token = CancellationToken::new();
        let (result, ()) = tokio::join!(delegate.invoke_tool(invocation, token), owner);
        assert_eq!(result.unwrap(), json!({"receipt":"consumer"}));
        assert_eq!(actual.0.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn result_decided_before_stop_cannot_publish_but_equal_id_survivor_can() {
        let control = Arc::new(Control::new("root".into(), true));
        control.enable(3.0, Scope::Root).unwrap();
        let owner = direct_request(control.clone(), "same-id").owner;
        let survivor = direct_request(control.clone(), "same-id").owner;
        let original_capacity = control.channel.bytes.available_permits();
        let token = CancellationToken::new();
        let mut stop = Box::pin(control.decide(before_request(&owner, "exec_command"), &token));
        assert!(futures_util::poll!(&mut stop).is_pending());
        let stop_nonce = control.next().await.unwrap()["nonce"].as_u64().unwrap();
        assert!(control.handoff(stop_nonce));

        // Complete a real after-result nonce, without yet publishing its reply.
        // This is the window after decide and before MCP assignment or nested return.
        let result = json!({"content":[{"type":"text","text":"synthetic-original"}]});
        let arguments = json!({});
        let mut after = Box::pin(control.decide(
            Request {
                tracing_context: None,
                owner: Some(&owner),
                phase: "after",
                route: "mcp",
                boundary: "executor_return",
                turn_id: &owner.id,
                model: None,
                call_id: Some("already-executed"),
                outer_call_id: None,
                cell_id: None,
                runtime_tool_call_id: None,
                tool_name: Name {
                    namespace: Some("tools"),
                    name: "lookup",
                },
                tool_kind: "function_json",
                input: Input::JsonText(&arguments),
                result: &result,
            },
            &owner.cancelled,
        ));
        assert!(futures_util::poll!(&mut after).is_pending());
        let after_nonce = control.next().await.unwrap()["nonce"].as_u64().unwrap();
        assert!(control.handoff(after_nonce));
        assert!(control.resolve(after_nonce,
            r#"{"action":"replace","value":{"content":[{"type":"text","text":"synthetic-edited"}]}}"#));
        let Decision::Replace { value } = after.await.unwrap() else {
            panic!("live result decision should be delivered before the stop");
        };
        assert!(control.release(after_nonce));
        assert!(control.hold_for_stop(stop_nonce));
        // Stop is accepted before cancellation has propagated through Python.
        assert!(!owner.cancelled.is_cancelled());
        assert!(!token.is_cancelled());

        let mut published = json!("withheld");
        assert!(
            control
                .publish_result(&owner, Some(&owner.cancelled), || published = value)
                .is_err()
        );
        assert_eq!(published, json!("withheld"));
        assert_eq!(
            control
                .publish_result(&survivor, Some(&survivor.cancelled), || json!("survivor"))
                .unwrap(),
            json!("survivor")
        );
        assert!(futures_util::poll!(&mut stop).is_pending());
        token.cancel();
        assert!(stop.await.is_err());
        assert!(control.release(stop_nonce));
        assert_eq!(control.channel.bytes.available_permits(), original_capacity);
    }

    #[tokio::test]
    async fn dedicated_mcp_resolved_before_stop_is_withheld_and_retained_survivor_can_publish() {
        let thread = ExtensionData::new("root");
        let turn = ExtensionData::new("stopping");
        let retained = ExtensionData::new("retained");
        let control = bind_owner(&thread, &turn);
        bind_owner(&thread, &retained);
        control.enable(3.0, Scope::Root).unwrap();
        let owner = turn.get::<DirectTurn>().unwrap();
        let token = CancellationToken::new();
        let mut stop = Box::pin(control.decide(before_request(&owner, "exec_command"), &token));
        assert!(futures_util::poll!(&mut stop).is_pending());
        let nonce = control.next().await.unwrap()["nonce"].as_u64().unwrap();
        assert!(control.handoff(nonce));

        let mut stopped_output = crate::mcp_result_control::withheld();
        let mut retained_output = crate::mcp_result_control::withheld();
        let (mcp, stopped_ack, stopped_assignment) =
            crate::mcp_result_control::tests::resolved_edit(&thread, &turn, &mut stopped_output)
                .await;
        let (_, retained_ack, retained_assignment) =
            crate::mcp_result_control::tests::resolved_edit(
                &thread,
                &retained,
                &mut retained_output,
            )
            .await;
        // Both edits are resolved, but neither native receiver has assigned.
        // Ordinary terminal must preserve a retained cell's result. Accept the
        // other owner's stop before letting either MCP receiver run.
        control.end_direct_turn(&retained);
        assert!(control.hold_for_stop(nonce));
        assert!(!owner.cancelled.is_cancelled());
        assert!(!token.is_cancelled());

        stopped_assignment.await;
        retained_assignment.await;
        assert!(stopped_ack.await.is_err());
        assert!(retained_ack.await.is_ok());
        assert_eq!(stopped_output, crate::mcp_result_control::withheld());
        assert_eq!(
            retained_output.content,
            vec![json!({"type":"text", "text":"synthetic-edited"})]
        );
        assert_eq!(retained_output.is_error, Some(false));
        assert!(!mcp.take_unresolved("retained"));
        assert!(mcp.take_unresolved("stopping"));

        assert!(futures_util::poll!(&mut stop).is_pending());
        token.cancel();
        assert!(stop.await.is_err());
        assert!(control.release(nonce));
    }

    #[tokio::test]
    async fn cancellation_and_revoke_reject_stale_replies() {
        let control = Arc::new(Control::new("thread-a".into(), true));
        control.enable(3.0, Scope::Root).unwrap();
        let token = CancellationToken::new();
        let owner = async {
            let request = control.next().await.unwrap();
            token.cancel();
            let cancel = control.next().await.unwrap();
            let nonce = request["nonce"].as_u64().unwrap();
            assert_eq!(cancel["cancel"], nonce);
            assert!(!control.resolve(nonce, r#"{"action":"replace","value":"late"}"#));
        };
        let (result, ()) = tokio::join!(control.decide(json!({}), &token), owner);
        assert!(result.is_err());
        control.revoke();
        assert!(control.decide(json!({}), &token).await.is_err());
    }
    fn cell_owner_control(
        namespace: Option<&str>,
        name: Option<&str>,
        result: bool,
    ) -> Arc<Control> {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3.0, Scope::ThreadTree).unwrap();
        root.configure_tools(
            name.into_iter()
                .map(|name| ToolRule {
                    generation: None,
                    namespace: namespace.map(str::to_owned),
                    name: name.into(),
                    result,
                    timeout: None,
                    scope: Scope::ThreadTree,
                })
                .collect(),
        )
        .unwrap();
        root
    }
    async fn next_cell_notice(root: &Control) -> Value {
        loop {
            let message = root.next().await.expect("native notice");
            if message.get("cancel").is_none() {
                return message;
            }
        }
    }
    #[test]
    fn retained_cell_owner_eligibility_is_only_actual_environment_callbacks() {
        for (namespace, name, result, expected) in [
            (None, Some("apply_patch"), true, true),
            (None, Some("exec_command"), false, true), // approval/arguments only
            (None, Some("project_glob"), true, true),
            (None, Some("project_grep"), false, true),
            (None, None, false, false), // no callbacks
            (Some("mcp__test"), Some("exec_command"), true, false),
            (Some("mcp__test"), Some("project_grep"), true, false),
            (Some("collaboration"), Some("spawn_agent"), false, false),
        ] {
            let root = cell_owner_control(namespace, name, result);
            let turn = direct_request(root.clone(), "turn").owner;
            assert_eq!(
                root.cell_policy_owner(&turn, "exec").unwrap().is_some(),
                expected
            );
            let child = Arc::new(Control::descendant("child".into(), &root));
            assert!(child.cell_policy_owner(&turn, "exec").unwrap().is_none());
        }
    }
    #[tokio::test]
    async fn initial_cell_return_binds_owner_without_any_tool_invocation_before_yield() {
        let (session, mut calls) = probed_session();
        let root = cell_owner_control(None, Some("exec_command"), true);
        let caller = start_managed_cell(session.clone(), root.clone());
        let CellProbe::Start(delegate, ready) = calls.recv().await.unwrap() else {
            panic!("start")
        };
        ready
            .send(Ok(yielded_start("empty-cell")))
            .unwrap_or_else(|_| panic!("start response"));
        let started = caller.await.unwrap().unwrap();
        assert!(
            calls.try_recv().is_err(),
            "No actual tool invocation precedes this handshake"
        );
        let mut returned = Box::pin(started.initial_response());
        assert!(futures_util::poll!(&mut returned).is_pending());
        let wire = next_cell_notice(&root).await;
        assert!(
            wire.get("request").is_none(),
            "Owner binding carries metadata, not tool output"
        );
        assert_eq!(wire["cell_owner"]["cell_id"], "empty-cell");
        assert_eq!(wire["cell_owner"]["outer_call_id"], "same-model-id");
        let owner_id = wire["cell_owner"]["owner_id"].as_u64().unwrap();
        let nonce = wire["nonce"].as_u64().unwrap();
        assert!(root.check(nonce) && root.handoff(nonce));
        assert!(
            !root.hold_for_stop(nonce),
            "Binding is not application approval"
        );
        assert!(root.resolve(nonce, r#"{"action":"continue"}"#));
        assert!(matches!(
            returned.await,
            Ok(RuntimeResponse::Yielded { .. })
        ));
        assert!(root.release(nonce));
        assert_eq!(root.channel.bytes.available_permits(), MAX_PAYLOAD_BYTES);
        delegate.cell_closed(&CellId::new("empty-cell".into()));
        assert_eq!(
            next_cell_notice(&root).await,
            json!({"cell_owner_retired":owner_id})
        );
    }
    #[tokio::test]
    async fn retained_cell_before_and_nested_after_keep_issuing_owner_after_admission_replacement()
    {
        let root = cell_owner_control(None, Some("exec_command"), false);
        let owner = direct_request(root.clone(), "original-turn").owner;
        let lease = root
            .cell_policy_owner(&owner, "same-outer")
            .unwrap()
            .unwrap();
        lease.identify(&CellId::new("cell-a".into())).unwrap();
        let owner_id = lease.id;
        let actual = Arc::new(Actual(AtomicUsize::new(0)));
        let delegate = Delegate {
            profile_source: None,
            cell_owner: Mutex::new(Some(lease)),
            inner: actual.clone(),
            live: Arc::new(AtomicBool::new(true)),
            control: Some(root.clone()),
            native_control: None,
            turn: owner.clone(),
            model: Some("issuing-model".into()),
            outer_call_id: "same-outer".into(),
            rules: None,
        };
        owner.sdk_admission.set(Weak::new()).unwrap(); // original SDK admission is no longer current
        assert!(!owner.admits_policy());
        let token = CancellationToken::new();
        let mut invocation = Box::pin(delegate.invoke_tool(
            CodeModeNestedToolCall {
                cell_id: CellId::new("cell-a".into()),
                runtime_tool_call_id: "runtime-a".into(),
                tool_name: ToolName::plain("exec_command"),
                tool_kind: CodeModeToolKind::Function,
                input: Some(json!({"plan":"old"})),
            },
            token.clone(),
        ));
        assert!(futures_util::poll!(&mut invocation).is_pending());
        let before = next_cell_notice(&root).await;
        assert_eq!(before["request"]["cell_owner_id"], owner_id);
        assert_eq!(before["request"]["model"], "issuing-model");
        assert_eq!(actual.0.load(Ordering::SeqCst), 0);
        let nonce = before["nonce"].as_u64().unwrap();
        assert!(root.check(nonce) && root.handoff(nonce));
        assert!(root.resolve(
            nonce,
            r#"{"action":"replace","value":{"plan":"effective"}}"#
        ));
        assert!(futures_util::poll!(&mut invocation).is_pending());
        assert!(root.release(nonce));
        let after = next_cell_notice(&root).await;
        assert_eq!(after["request"]["cell_owner_id"], owner_id);
        assert_eq!(after["request"]["boundary"], "nested_return");
        assert_eq!(actual.0.load(Ordering::SeqCst), 1);
        let nonce = after["nonce"].as_u64().unwrap();
        assert!(root.check(nonce) && root.handoff(nonce));
        assert!(
            !root.hold_for_stop(nonce),
            "An after notice cannot stop an old root"
        );
        assert!(root.resolve(nonce, r#"{"action":"continue"}"#));
        assert_eq!(invocation.await.unwrap(), json!({"from_actual":true}));
        assert!(root.release(nonce));
        delegate.cell_closed(&CellId::new("cell-a".into()));
        assert_eq!(
            next_cell_notice(&root).await,
            json!({"cell_owner_retired":owner_id})
        );
    }
    #[tokio::test]
    async fn cell_owner_last_drop_waits_for_actual_invocation_and_not_handler_arc() {
        struct Held(Arc<tokio::sync::Notify>, Arc<tokio::sync::Notify>);
        impl CodeModeSessionDelegate for Held {
            fn invoke_tool<'a>(
                &'a self,
                _: CodeModeNestedToolCall,
                _: CancellationToken,
            ) -> ToolInvocationFuture<'a> {
                Box::pin(async move {
                    self.0.notify_one();
                    self.1.notified().await;
                    Err("synthetic inner failure".into())
                })
            }
            fn notify<'a>(
                &'a self,
                _: String,
                _: CellId,
                _: String,
                _: CancellationToken,
            ) -> NotificationFuture<'a> {
                Box::pin(async { Ok(()) })
            }
            fn cell_closed(&self, _: &CellId) {}
        }
        let root = cell_owner_control(None, Some("exec_command"), true);
        let owner = direct_request(root.clone(), "turn").owner;
        let lease = root.cell_policy_owner(&owner, "outer").unwrap().unwrap();
        let id = lease.id;
        let entered = Arc::new(tokio::sync::Notify::new());
        let release = Arc::new(tokio::sync::Notify::new());
        let delegate = Delegate {
            profile_source: None,
            cell_owner: Mutex::new(Some(lease)),
            inner: Arc::new(Held(entered.clone(), release.clone())),
            live: Arc::new(AtomicBool::new(true)),
            control: Some(root.clone()),
            native_control: None,
            turn: owner,
            model: None,
            outer_call_id: "outer".into(),
            rules: None,
        };
        let mut call = Box::pin(delegate.invoke_tool(
            CodeModeNestedToolCall {
                cell_id: CellId::new("cell".into()),
                runtime_tool_call_id: "runtime".into(),
                tool_name: ToolName::plain("exec_command"),
                tool_kind: CodeModeToolKind::Function,
                input: Some(json!({})),
            },
            CancellationToken::new(),
        ));
        assert!(futures_util::poll!(&mut call).is_pending());
        let before = next_cell_notice(&root).await;
        let nonce = before["nonce"].as_u64().unwrap();
        assert!(root.resolve(nonce, r#"{"action":"continue"}"#));
        assert!(futures_util::poll!(&mut call).is_pending());
        entered.notified().await;
        assert!(root.release(nonce));
        delegate.cell_closed(&CellId::new("cell".into()));
        let mut retirement = Box::pin(next_cell_notice(&root));
        assert!(
            futures_util::poll!(&mut retirement).is_pending(),
            "Invocation still owns the lease after cell close"
        );
        release.notify_one();
        assert!(call.await.is_err());
        assert_eq!(retirement.await, json!({"cell_owner_retired":id}));
        assert!(
            !delegate.live.load(Ordering::Acquire),
            "Handler Arc itself remains retained"
        );
    }
    #[tokio::test]
    async fn retained_pending_preserves_nonce_cancellation_stop_and_current_scope_fences() {
        let root = cell_owner_control(None, Some("exec_command"), true);
        let owner = direct_request(root.clone(), "turn").owner;
        let lease = root.cell_policy_owner(&owner, "outer").unwrap().unwrap();
        lease.identify(&CellId::new("cell".into())).unwrap();
        owner.sdk_admission.set(Weak::new()).unwrap();
        assert!(
            root.cell_policy_owner(&owner, "new-execute").is_err(),
            "A retained cell does not admit a new execute"
        );
        let token = CancellationToken::new();
        let mut direct = Box::pin(root.decide(before_request(&owner, "exec_command"), &token));
        assert!(futures_util::poll!(&mut direct).is_pending());
        let notice = next_cell_notice(&root).await;
        let nonce = notice["nonce"].as_u64().unwrap();
        assert!(!root.check(nonce) && !root.resolve(nonce, r#"{"action":"continue"}"#));
        token.cancel();
        assert!(direct.await.is_err());
        assert!(root.release(nonce));
        for revoke in [false, true] {
            let token = CancellationToken::new();
            let mut request = before_request(&owner, "exec_command");
            request.phase = "after";
            request.boundary = "executor_return";
            let mut after = Box::pin(root.decide(BuiltinResult { request }, &token));
            assert!(futures_util::poll!(&mut after).is_pending());
            let notice = next_cell_notice(&root).await;
            let nonce = notice["nonce"].as_u64().unwrap();
            assert!(root.check(nonce) && root.handoff(nonce));
            assert!(!root.hold_for_stop(nonce));
            if revoke {
                root.revoke();
            } else {
                token.cancel();
            }
            assert!(
                !root.check(nonce)
                    && !root.resolve(nonce, r#"{"action":"replace","value":"late"}"#)
            );
            assert!(after.await.is_err());
            assert!(root.release(nonce));
        }
        assert!(
            root.publish_result(&owner, None, || "must not publish")
                .is_err()
        );
    }
    #[tokio::test]
    async fn exact_held_stop_terminates_before_passive_wait_lock_and_never_reinserts_consumed_cell()
    {
        let (session, mut calls) = probed_session();
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3.0, Scope::Root).unwrap();
        root.code_mode_sessions
            .lock()
            .unwrap()
            .push(Arc::downgrade(&session));
        let stopped = direct_request(root.clone(), "same-id").owner;
        let survivor = direct_request(root.clone(), "same-id").owner;
        survivor.policy_stopped.store(true, Ordering::Release); // same displayed ID and stopped state still do not confer exact ownership
        for (name, turn) in [("stopped", stopped.clone()), ("survivor", survivor)] {
            session.cells.lock().unwrap().insert(
                CellKey::Started(CellId::new(name.into())),
                Arc::new(Cell {
                    control: root.clone(),
                    turn,
                    live: Arc::new(AtomicBool::new(true)),
                    interception_origin: None,
                    operation: AsyncMutex::new(CellOperation::Running),
                }),
            );
        }
        let token = CancellationToken::new();
        let mut decision = Box::pin(root.decide(before_request(&stopped, "exec_command"), &token));
        assert!(futures_util::poll!(&mut decision).is_pending());
        let nonce = next_cell_notice(&root).await["nonce"].as_u64().unwrap();
        assert!(root.handoff(nonce) && root.hold_for_stop(nonce));
        let held = root.capture_held_stop(nonce).unwrap();
        let mut wait = Box::pin(session.wait(
            WaitRequest {
                cell_id: CellId::new("stopped".into()),
                yield_time_ms: 10000,
            },
            None,
        ));
        assert!(futures_util::poll!(&mut wait).is_pending());
        let CellProbe::Wait(id, wait_ack) = calls.recv().await.unwrap() else {
            panic!("passive wait")
        };
        let mut finish = Box::pin(held.finish());
        assert!(futures_util::poll!(&mut finish).is_pending());
        let CellProbe::Terminate(actual, stop_ack) = calls.recv().await.unwrap() else {
            panic!("terminate before operation lock")
        };
        assert_eq!(id, actual);
        token.cancel();
        assert!(decision.await.is_err());
        assert!(root.release(nonce)); // captured held owner survives reservation removal
        wait_ack.send(Ok(terminated(id))).unwrap();
        assert!(wait.await.is_ok());
        assert!(
            futures_util::poll!(&mut finish).is_pending(),
            "Exact stop still requires its ACK"
        );
        stop_ack.send(Ok(terminated(actual))).unwrap();
        finish.await.unwrap();
        assert!(
            !session
                .cells
                .lock()
                .unwrap()
                .contains_key(&CellKey::Started(CellId::new("stopped".into())))
        );
        assert!(
            session
                .cells
                .lock()
                .unwrap()
                .contains_key(&CellKey::Started(CellId::new("survivor".into())))
        );
        assert!(calls.try_recv().is_err());
    }
    #[tokio::test]
    async fn trusted_builtin_after_retains_root_invocation_but_not_direct_or_child_admission() {
        for (nested, is_root, accepted) in [
            (true, true, true),
            (false, true, false),
            (true, false, false),
        ] {
            let boundary = Boundary::new(Arc::new(crate::storage::MemoryAgentGraph::default()));
            let parent = cell_owner_control(None, Some("exec_command"), true);
            let thread = ExtensionData::new(if is_root { "root" } else { "child" });
            let control = thread.get_or_init(|| {
                if is_root {
                    Control::new("root".into(), true)
                } else {
                    Control::descendant("child".into(), &parent)
                }
            });
            if is_root {
                control.enable(3.0, Scope::Root).unwrap();
                control
                    .configure_tools(vec![ToolRule {
                        generation: None,
                        namespace: None,
                        name: "exec_command".into(),
                        result: true,
                        timeout: None,
                        scope: Scope::ThreadTree,
                    }])
                    .unwrap();
            }
            let turn = ExtensionData::new("original-turn");
            let owner = turn.get_or_init(|| DirectTurn {
                id: "original-turn".into(),
                model: Mutex::new(None),
                sdk_admission: OnceLock::new(),
                cancelled: CancellationToken::new(),
                policy_stopped: AtomicBool::new(false),
                mcp_calls: Mutex::default(),
            });
            owner.sdk_admission.set(Weak::new()).unwrap();
            let name = ToolName::plain("exec_command");
            let payload = ToolPayload::Function {
                arguments: "{}".into(),
            };
            let raw = json!({"private":"never-publish-original"});
            let token = CancellationToken::new();
            let source = if nested {
                ToolCallSource::CodeMode {
                    cell_id: "cell".into(),
                    runtime_tool_call_id: "runtime".into(),
                }
            } else {
                ToolCallSource::Direct
            };
            let mut hook = Box::pin(boundary.on_builtin_tool_result(BuiltinToolResultInput {
                model: "issuing-model",
                thread_store: &thread,
                turn_store: &turn,
                turn_id: "original-turn",
                call_id: "actual-native",
                outer_call_id: Some("original-outer"),
                tool_name: &name,
                payload: &payload,
                source,
                result: &raw,
                cancelled: Box::pin(token.cancelled()),
            }));
            assert!(futures_util::poll!(&mut hook).is_pending());
            let reader = if is_root { &control } else { &parent };
            let notice = next_cell_notice(reader).await;
            let nonce = notice["nonce"].as_u64().unwrap();
            assert_eq!(notice["request"]["model"], "issuing-model");
            assert_eq!(notice["request"]["turn_id"], "original-turn");
            assert_eq!(control.check(nonce), accepted);
            if accepted {
                assert!(control.handoff(nonce));
                assert!(!control.hold_for_stop(nonce));
                assert!(control.resolve(
                    nonce,
                    r#"{"action":"replace","value":"accepted-original-invocation"}"#
                ));
                assert_eq!(hook.await.unwrap(), json!("accepted-original-invocation"));
            } else {
                assert!(!control.resolve(nonce, r#"{"action":"replace","value":"stale"}"#));
                token.cancel();
                assert!(hook.await.is_err());
            }
            assert!(control.release(nonce));
        }
    }
    #[tokio::test]
    async fn exact_held_stop_propagates_failed_native_ack_without_claiming_retirement() {
        let (session, mut calls) = probed_session();
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3.0, Scope::Root).unwrap();
        root.code_mode_sessions
            .lock()
            .unwrap()
            .push(Arc::downgrade(&session));
        let owner = direct_request(root.clone(), "turn").owner;
        let id = CellId::new("cell".into());
        let key = CellKey::Started(id.clone());
        session.cells.lock().unwrap().insert(
            key.clone(),
            Arc::new(Cell {
                control: root.clone(),
                turn: owner.clone(),
                live: Arc::new(AtomicBool::new(true)),
                interception_origin: None,
                operation: AsyncMutex::new(CellOperation::Running),
            }),
        );
        let token = CancellationToken::new();
        let mut decision = Box::pin(root.decide(before_request(&owner, "exec_command"), &token));
        assert!(futures_util::poll!(&mut decision).is_pending());
        let nonce = next_cell_notice(&root).await["nonce"].as_u64().unwrap();
        assert!(root.handoff(nonce) && root.hold_for_stop(nonce));
        let held = root.capture_held_stop(nonce).unwrap();
        let mut finish = Box::pin(held.finish());
        assert!(futures_util::poll!(&mut finish).is_pending());
        let CellProbe::Terminate(actual, ack) = calls.recv().await.unwrap() else {
            panic!("terminate")
        };
        assert_eq!(actual, id);
        ack.send(Err("synthetic termination failure".into()))
            .unwrap();
        assert!(finish.await.is_err());
        assert!(session.cells.lock().unwrap().contains_key(&key));
        token.cancel();
        assert!(decision.await.is_err());
        assert!(root.release(nonce));
    }
    #[tokio::test]
    async fn non_environment_cells_keep_no_owner_acknowledgement_path() {
        for (namespace, name) in [
            (None, None),
            (Some("mcp__test"), Some("exec_command")),
            (Some("collaboration"), Some("spawn_agent")),
        ] {
            let (session, mut calls) = probed_session();
            let root = cell_owner_control(namespace, name, false);
            let caller = start_managed_cell(session, root.clone());
            let CellProbe::Start(_, ready) = calls.recv().await.unwrap() else {
                panic!("start")
            };
            ready
                .send(Ok(yielded_start("cell")))
                .unwrap_or_else(|_| panic!("start response"));
            let started = caller.await.unwrap().unwrap();
            let mut result = Box::pin(started.initial_response());
            assert!(matches!(
                futures_util::poll!(&mut result),
                std::task::Poll::Ready(Ok(RuntimeResponse::Yielded { .. }))
            ));
            let state = root.channel.state.lock().unwrap();
            assert!(state.pending.is_empty());
            assert_eq!(state.nonce, 0);
        }
    }
    #[tokio::test]
    async fn cell_owner_metadata_uses_existing_byte_budget_and_failed_start_retires() {
        let root = Arc::new(Control::with_budget("root".into(), true, 128));
        root.enable(3.0, Scope::Root).unwrap();
        root.configure_tools(vec![ToolRule {
            generation: None,
            namespace: None,
            name: "apply_patch".into(),
            result: false,
            timeout: None,
            scope: Scope::Root,
        }])
        .unwrap();
        let turn = direct_request(root.clone(), "turn").owner;
        let lease = root
            .cell_policy_owner(&turn, &"x".repeat(512))
            .unwrap()
            .unwrap();
        let id = lease.id;
        assert!(lease.bind(&CellId::new("cell".into())).await.is_err());
        assert_eq!(root.channel.bytes.available_permits(), 128);
        assert!(root.channel.state.lock().unwrap().pending.is_empty());
        drop(lease);
        assert_eq!(
            next_cell_notice(&root).await,
            json!({"cell_owner_retired":id})
        );
        let (session, mut calls) = probed_session();
        let root = cell_owner_control(None, Some("exec_command"), true);
        let caller = start_managed_cell(session.clone(), root.clone());
        let CellProbe::Start(delegate, reply) = calls.recv().await.unwrap() else {
            panic!("start")
        };
        reply
            .send(Err("synthetic start failure".into()))
            .unwrap_or_else(|_| panic!("start response"));
        assert!(caller.await.unwrap().is_err());
        assert!(session.cells.lock().unwrap().is_empty());
        // The underlying session releases its delegate when start fails. No cell ID
        // or published owner exists; no invented cell_closed ID is required.
        drop(delegate);
        let retired = next_cell_notice(&root).await;
        assert!(retired["cell_owner_retired"].is_u64());
        assert!(retired.get("nonce").is_none());
        assert_eq!(root.channel.bytes.available_permits(), MAX_PAYLOAD_BYTES);
    }
    #[tokio::test]
    async fn exact_stop_ack_never_removes_replacement_cell_with_reused_displayed_id() {
        let (session, mut calls) = probed_session();
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(3.0, Scope::Root).unwrap();
        root.code_mode_sessions
            .lock()
            .unwrap()
            .push(Arc::downgrade(&session));
        let original = direct_request(root.clone(), "same-turn-id").owner;
        let id = CellId::new("same-cell-id".into());
        let key = CellKey::Started(id.clone());
        let make_cell = |turn| {
            Arc::new(Cell {
                control: root.clone(),
                turn,
                live: Arc::new(AtomicBool::new(true)),
                interception_origin: None,
                operation: AsyncMutex::new(CellOperation::Running),
            })
        };
        session
            .cells
            .lock()
            .unwrap()
            .insert(key.clone(), make_cell(original.clone()));
        let token = CancellationToken::new();
        let mut before = Box::pin(root.decide(before_request(&original, "exec_command"), &token));
        assert!(futures_util::poll!(&mut before).is_pending());
        let nonce = next_cell_notice(&root).await["nonce"].as_u64().unwrap();
        assert!(root.handoff(nonce) && root.hold_for_stop(nonce));
        let held = root.capture_held_stop(nonce).unwrap();
        let mut stop = Box::pin(held.finish());
        assert!(futures_util::poll!(&mut stop).is_pending());
        let CellProbe::Terminate(actual, ack) = calls.recv().await.unwrap() else {
            panic!("terminate")
        };
        assert_eq!(actual, id);
        let replacement = make_cell(direct_request(root.clone(), "same-turn-id").owner);
        session
            .cells
            .lock()
            .unwrap()
            .insert(key.clone(), replacement.clone());
        ack.send(Ok(terminated(actual))).unwrap();
        stop.await.unwrap();
        assert!(Arc::ptr_eq(
            session.cells.lock().unwrap().get(&key).unwrap(),
            &replacement
        ));
        assert!(calls.try_recv().is_err());
        token.cancel();
        assert!(before.await.is_err());
        assert!(root.release(nonce));
    }
    #[tokio::test(start_paused = true)]
    async fn queued_result_expiry_fences_only_its_captured_owner_and_rejects_late_reply() {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(0.1, Scope::ThreadTree).unwrap();
        let child = Arc::new(Control::descendant("child".into(), &root));
        let owner = direct_request(child.clone(), "same-id").owner;
        let survivor = direct_request(child.clone(), "same-id").owner;
        let token = CancellationToken::new();
        let mut request = before_request(&owner, "exec_command");
        request.phase = "after";
        let mut decision = Box::pin(child.decide(request, &token));
        assert!(futures_util::poll!(&mut decision).is_pending());
        let nonce = root.channel.state.lock().unwrap().nonce;
        tokio::time::advance(Duration::from_millis(101)).await;
        assert!(!root.resolve(nonce, r#"{"action":"replace","value":"private-late"}"#));
        assert!(
            root.channel.state.lock().unwrap().pending[&nonce]
                .reply
                .is_some()
        );
        // The root bridge owns this shared queue, but never the child turn.
        root.expire(nonce);
        assert!(owner.policy_stopped.load(Ordering::Acquire));
        assert!(!survivor.policy_stopped.load(Ordering::Acquire));
        assert!(!root.check(nonce));
        assert!(!root.resolve(nonce, r#"{"action":"replace","value":"private-late"}"#));
        assert!(decision.await.is_err());
        assert!(child.publish_result(&owner, None, || ()).is_err());
        assert!(child.publish_result(&survivor, None, || ()).is_ok());
        assert!(root.channel.state.lock().unwrap().pending.is_empty());
        assert_eq!(root.channel.bytes.available_permits(), MAX_PAYLOAD_BYTES);
        child.join_expiry_cleanup().await.unwrap();
        assert!(root.expiry_cleanup.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn accepted_result_reply_wins_over_later_expiry() {
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(0.1, Scope::Root).unwrap();
        let owner = direct_request(root.clone(), "turn").owner;
        let mut request = before_request(&owner, "exec_command");
        request.phase = "after";
        let token = CancellationToken::new();
        let mut decision = Box::pin(root.decide(request, &token));
        assert!(futures_util::poll!(&mut decision).is_pending());
        let nonce = root.next().await.unwrap()["nonce"].as_u64().unwrap();
        assert!(root.handoff(nonce));
        assert!(root.resolve(nonce, r#"{"action":"replace","value":"approved"}"#));
        tokio::time::advance(Duration::from_millis(101)).await;
        root.expire(nonce);
        assert!(!owner.policy_stopped.load(Ordering::Acquire));
        assert!(matches!(decision.await, Ok(Decision::Replace { .. })));
        assert!(root.release(nonce));
        assert!(root.expiry_cleanup.is_empty());
    }

    #[tokio::test]
    async fn expiry_cleanup_registered_before_revoke_is_joined_and_failure_stays_visible() {
        let (session, mut calls) = probed_session();
        let root = Arc::new(Control::new("root".into(), true));
        root.enable(30.0, Scope::Root).unwrap();
        root.code_mode_sessions
            .lock()
            .unwrap()
            .push(Arc::downgrade(&session));
        let owner = direct_request(root.clone(), "turn").owner;
        session.cells.lock().unwrap().insert(
            CellKey::Started(CellId::new("cell".into())),
            Arc::new(Cell {
                control: root.clone(),
                turn: owner.clone(),
                live: Arc::new(AtomicBool::new(true)),
                interception_origin: None,
                operation: AsyncMutex::new(CellOperation::Running),
            }),
        );
        let token = CancellationToken::new();
        let mut request = before_request(&owner, "exec_command");
        request.phase = "after";
        let mut decision = Box::pin(root.decide(request, &token));
        assert!(futures_util::poll!(&mut decision).is_pending());
        let nonce = root.channel.state.lock().unwrap().nonce;
        root.expire(nonce);
        root.revoke();
        let mut close = Box::pin(root.join_expiry_cleanup());
        assert!(futures_util::poll!(&mut close).is_pending());
        let CellProbe::Terminate(_, ack) = calls.recv().await.unwrap() else {
            panic!("terminate");
        };
        assert!(futures_util::poll!(&mut close).is_pending());
        ack.send(Err("private-backend-cleanup-error".into()))
            .unwrap();
        assert_eq!(
            close.await.unwrap_err().to_string(),
            "Native result policy cleanup failed"
        );
        assert_eq!(
            root.join_expiry_cleanup().await.unwrap_err().to_string(),
            "Native result policy cleanup failed"
        );
        assert!(decision.await.is_err());
        assert!(root.expiry_cleanup.is_empty());
        // An expiry arriving after revoke cannot register work behind its join.
        root.expire(nonce);
        assert!(root.expiry_cleanup.is_empty());
        assert!(calls.try_recv().is_err());
    }
    #[tokio::test]
    async fn expired_captured_request_cannot_adopt_a_same_id_successors_admission() {
        let root = Arc::new(Control::new("root".into(), true));
        let original = direct_request(root.clone(), "same-id");
        let survivor = direct_request(root.clone(), "same-id");
        let unconfigured = AdmissionOnly {
            control: root.clone(),
            owner: original.owner.clone(),
        };
        assert!(original.admit_request().is_ok());
        assert!(unconfigured.admit_request().is_ok());
        {
            let _state = root.channel.state.lock().unwrap();
            root.stop_expired_owner(&original.owner, "Executor policy failed");
        }
        assert!(original.admit_request().is_err());
        assert!(unconfigured.admit_request().is_err());
        assert!(survivor.admit_request().is_ok());
        root.join_expiry_cleanup().await.unwrap();
    }
}

impl Boundary {
    pub(crate) fn model_producer_tools(
        &self,
        thread: &str,
    ) -> Option<Arc<crate::python_tools::PythonTools>> {
        self.context_tools(thread)
    }
}
