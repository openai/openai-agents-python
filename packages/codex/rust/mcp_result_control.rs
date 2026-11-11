//! Opt-in result editing for root native MCP calls at the public result boundary.
use std::collections::HashMap;
use std::io::{self, Write};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock, Weak};
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use codex_config::McpServerConfig;
use codex_core_api::{Config, SessionSource, ThreadManager};
use codex_extension_api::{
    ExtensionData, ExtensionFuture, McpToolRequestMetadataInput, McpToolResultInput, McpToolSource,
    ThreadLifecycleContributor, ThreadStartInput, ThreadStopInput, ToolLifecycleContributor,
    ToolLifecycleFuture, TurnAbortInput, TurnLifecycleContributor, TurnStartInput, TurnStopInput,
};
use codex_protocol::mcp::CallToolResult;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::sync::{Mutex as AsyncMutex, OwnedSemaphorePermit, Semaphore, mpsc, oneshot};
use tokio::time::Instant;

const MAX_REPLY: usize = 1024 * 1024;
const MAX_PENDING: usize = 32;
// Same private serialized bridge budget as native interception. A single
// notice may use it, but 32 concurrently retained notices must share it.
const MAX_BYTES: usize = 128 * 1024 * 1024;

#[derive(Serialize)]
struct Notice<'a> {
    #[serde(skip_serializing_if = "Option::is_none")]
    tracing_context: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    phase: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    model: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    route: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    cell_id: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    runtime_tool_call_id: Option<&'a str>,
    nonce: u64,
    generation: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    profile_id: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    profile_epoch: Option<u64>,
    thread_id: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    instance: Option<&'a str>,
    turn_id: &'a str,
    call_id: &'a str,
    source: &'a str,
    server_name: &'a str,
    tool_name: &'a str,
    callable_namespace: &'a str,
    callable_name: &'a str,
    arguments: &'a Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<&'a CallToolResult>,
}

struct Counter {
    bytes: usize,
    limit: usize,
}

impl Write for Counter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.bytes = self
            .bytes
            .checked_add(buf.len())
            .filter(|bytes| *bytes <= self.limit)
            .ok_or_else(|| io::Error::other("MCP notice budget exceeded"))?;
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

pub(crate) fn withheld() -> CallToolResult {
    CallToolResult {
        content: vec![json!({"type":"text","text":"MCP result withheld"})],
        structured_content: None,
        is_error: Some(true),
        meta: None,
    }
}

// Require exactly the public result envelope; rmcp validates the content forms,
// field types and metadata. Keep the protocol fields after validation (no retyping
// arbitrary structured JSON or discarding harmless content extensions).
#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Envelope {
    content: Vec<Value>,
    #[serde(default)]
    structured_content: Option<Value>,
    #[serde(default)]
    is_error: Option<bool>,
    #[serde(default, rename = "_meta")]
    meta: Option<Value>,
}

pub(crate) fn validated(reply: &str) -> Option<CallToolResult> {
    if reply.len() > MAX_REPLY {
        return None;
    }
    let value: Envelope = serde_json::from_str(reply).ok()?;
    let _: rmcp::model::CallToolResult = serde_json::from_str(reply).ok()?;
    Some(CallToolResult {
        content: value.content,
        structured_content: value.structured_content,
        is_error: value.is_error,
        meta: value.meta,
    })
}

#[derive(Debug)]
enum AcceptedReply {
    Result {
        edited: CallToolResult,
        assignment: Option<oneshot::Sender<()>>,
    },
    Metadata(Result<serde_json::Map<String, Value>, ()>),
}

fn validated_metadata(reply: &str) -> Option<serde_json::Map<String, Value>> {
    if reply.len() > MAX_REPLY {
        return None;
    }
    let value: Value = serde_json::from_str(reply).ok()?;
    let map = match value {
        Value::Null => serde_json::Map::new(),
        Value::Object(map) => map,
        _ => return None,
    };
    map.keys()
        .all(|key| !codex_extension_api::is_reserved_mcp_request_metadata_key(key))
        .then_some(map)
}

struct Pending {
    turn_id: String,
    call_id: String,
    deadline: Instant,
    metadata: bool,
    is_cancelled: Option<Arc<dyn Fn() -> bool + Send + Sync>>,
    sender: Option<oneshot::Sender<Option<AcceptedReply>>>,
}

// Transport retention never keeps its issuer or reply sender alive. A handed
// payload may still occupy application memory after the child itself retires.
struct Route {
    owner: Weak<McpResultControl>,
    payload: Payload,
    _bytes: OwnedSemaphorePermit,
    // Retain only the private trace context until exact physical return, never
    // the issuer or its reply authority. Queue discard releases it as well.
    _tracing: Option<codex_otel::sdk_trace::ContextLease>,
}

enum Payload {
    Queued(String),
    InFlight(Option<u64>),
    Handed,
    Returned,
}

#[derive(Default)]
struct TurnResults {
    // Existing call nonce floor identifies this insertion without a new lifecycle.
    first_nonce: u64,
    count: usize,
    failed: bool,
}

/// SDK selection captured alongside the idle roster. Never persisted in Core config.
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Selection {
    pub(crate) generation: u64,
    servers: HashMap<String, HashMap<String, f64>>,
    #[serde(default)]
    metadata_servers: HashMap<String, Vec<String>>,
}

impl Selection {
    pub(crate) fn validate(&self, requested: &HashMap<String, McpServerConfig>) -> Result<()> {
        for (name, selected) in &self.servers {
            let server = requested.get(name);
            ensure!(
                server.is_some_and(|server| {
                    !crate::mcp_roster::enterprise(server)
                        && server.enabled
                        && selected.keys().all(|tool| {
                            server
                                .enabled_tools
                                .as_ref()
                                .is_some_and(|names| names.contains(tool))
                        })
                }),
                "MCP result policy must select admitted ordinary raw tool names"
            );
            ensure!(
                selected
                    .values()
                    .all(|timeout| timeout.is_finite() && *timeout > 0.0 && *timeout <= 3600.0),
                "Native MCP result timeout must be in (0, 3600] seconds"
            );
        }
        for (name, tools) in &self.metadata_servers {
            ensure!(
                requested.get(name).is_some_and(|server| {
                    !crate::mcp_roster::enterprise(server)
                        && server.enabled
                        && tools.iter().all(|tool| {
                            server
                                .enabled_tools
                                .as_ref()
                                .is_some_and(|names| names.contains(tool))
                        })
                }),
                "MCP metadata policy must select admitted ordinary raw tool names"
            );
        }
        Ok(())
    }
}

// Keep explicit unselected turns unselected after later thread reconfiguration.
struct TurnSelection(Option<Selection>);

fn source_profile(
    thread: &ExtensionData,
    step: Option<&ExtensionData>,
) -> Result<Option<codex_extension_api::AgentProfileReceipt>> {
    if let Some(source) =
        step.and_then(|step| step.get::<crate::native_interception::ProfileMcpSource>())
    {
        return Ok(source.receipt.clone());
    }
    match thread.get::<crate::python_tools::PythonTools>() {
        Some(tools) => tools.source_profile(step),
        None => Ok(step
            .and_then(|step| step.get::<codex_extension_api::CapturedAgentProfile>())
            .map(|profile| profile.receipt.clone())),
    }
}

fn source_selection(
    thread: &ExtensionData,
    turn: &ExtensionData,
    step: Option<&ExtensionData>,
) -> Result<Option<Arc<Selection>>> {
    if let Some(source) =
        step.and_then(|step| step.get::<crate::native_interception::ProfileMcpSource>())
    {
        ensure!(
            source.policy.is_some(),
            "Missing captured MCP callback policy"
        );
        let selection = if source.receipt.is_none() {
            source.result_selection.clone()
        } else {
            step.and_then(|step| step.get::<Selection>())
        };
        ensure!(
            source.receipt.is_none() || selection.is_some(),
            "Missing captured Agent MCP result selection"
        );
        return Ok(selection);
    }
    if source_profile(thread, step)?.is_some() {
        return Ok(Some(
            step.and_then(|step| step.get::<Selection>())
                .context("Missing captured Agent MCP result selection")?,
        ));
    }
    Ok(turn
        .get::<TurnSelection>()
        .and_then(|snapshot| snapshot.0.clone())
        .map(Arc::new))
}

struct State {
    selection: Option<Selection>,
    revoked: bool,
    routes: Arc<Mutex<HashMap<u64, Route>>>,
    pending: HashMap<u64, Pending>,
    // Created before queuing, even when Python never receives a notice.
    // The matching terminal consumes pending or failed obligations. Earlier
    // reads can distinguish settled failure from work still awaiting assignment.
    // Never clear on native turn-stop (it precedes the event).
    unresolved: HashMap<String, TurnResults>,
}

impl State {
    fn retire(&mut self, nonce: u64) -> Option<oneshot::Sender<Option<AcceptedReply>>> {
        let item = self.pending.get_mut(&nonce)?;
        let sender = item.sender.take();
        let mut routes = self.routes.lock().expect("MCP routes poisoned");
        if routes
            .get(&nonce)
            .is_none_or(|route| matches!(route.payload, Payload::Queued(_) | Payload::Returned))
        {
            self.pending.remove(&nonce);
            routes.remove(&nonce);
        }
        sender
    }
}

impl Drop for State {
    fn drop(&mut self) {
        // Also cover a dropped thread whose lifecycle future did not reach
        // stop. In-flight and handed bytes remain until physical return.
        for nonce in self.pending.keys().copied().collect::<Vec<_>>() {
            self.retire(nonce);
        }
    }
}

pub struct McpResultControl {
    id: String,
    instance: Option<String>,
    root: bool,
    state: Mutex<State>,
    channel: Arc<ResultChannel>,
}

// Senders and failures stay on their issuing thread. Shared transport charges
// survive a dropped issuer, but only its weak/exact owner can admit a reply.
struct ResultChannel {
    next_nonce: AtomicU64,
    routes: Arc<Mutex<HashMap<u64, Route>>>,
    members: RwLock<HashMap<String, Weak<McpResultControl>>>,
    sender: mpsc::Sender<(Weak<McpResultControl>, u64)>,
    receiver: AsyncMutex<mpsc::Receiver<(Weak<McpResultControl>, u64)>>,
    bytes: Arc<Semaphore>,
}

impl McpResultControl {
    fn capture_selection(&self, turn: &ExtensionData) {
        let selection = self
            .state
            .lock()
            .expect("MCP result control poisoned")
            .selection
            .clone();
        turn.get_or_init(|| TurnSelection(selection));
    }

    pub(crate) fn capture_source_selection(&self) -> Option<Selection> {
        self.state
            .lock()
            .expect("MCP result control poisoned")
            .selection
            .clone()
    }

    pub(crate) fn owns_result(
        &self,
        thread: &ExtensionData,
        turn: &ExtensionData,
        step: Option<&ExtensionData>,
        server: &str,
        tool: &str,
        configured: bool,
    ) -> bool {
        configured
            && match source_selection(thread, turn, step) {
                Ok(selection) => selection
                    .as_ref()
                    .and_then(|selection| selection.servers.get(server))
                    .is_some_and(|tools| tools.contains_key(tool)),
                // The dedicated boundary withholds an invalid captured source.
                // Do not expose its result to the generic interceptor first.
                Err(_) => true,
            }
    }

    pub(crate) fn check_selection(&self, generation: u64) -> Result<()> {
        let state = self.state.lock().expect("MCP result control poisoned");
        ensure!(
            self.root && !state.revoked,
            "Selected MCP results require a live managed root"
        );
        ensure!(
            state
                .selection
                .as_ref()
                .is_none_or(|current| generation > current.generation),
            "MCP result selection generation must advance"
        );
        Ok(())
    }

    // Caller holds the thread mutation lock from idle validation through roster
    // publication. No native turn or Python callback can cross this generation.
    pub(crate) fn publish_selection(&self, selection: Selection) {
        self.state
            .lock()
            .expect("MCP result control poisoned")
            .selection = Some(selection);
    }

    pub(crate) fn is_pending(&self, nonce: u64) -> bool {
        let state = self.state.lock().expect("MCP result control poisoned");
        !state.revoked
            && state.pending.get(&nonce).is_some_and(|item| {
                item.sender.is_some()
                    && item.is_cancelled.as_ref().is_none_or(|probe| !probe())
                    && Instant::now() < item.deadline
                    && state
                        .routes
                        .lock()
                        .expect("MCP routes poisoned")
                        .get(&nonce)
                        .is_some_and(|route| matches!(route.payload, Payload::Handed))
            })
    }

    fn new(id: String, root: bool) -> Self {
        Self::with_budget(id, root, MAX_BYTES)
    }

    fn with_budget(id: String, root: bool, bytes: usize) -> Self {
        let (sender, receiver) = mpsc::channel(MAX_PENDING);
        let channel = Arc::new(ResultChannel {
            next_nonce: AtomicU64::new(1),
            routes: Arc::default(),
            members: RwLock::default(),
            sender,
            receiver: AsyncMutex::new(receiver),
            bytes: Arc::new(Semaphore::new(bytes)),
        });
        Self {
            id,
            instance: None,
            root,
            state: Mutex::new(State {
                selection: None,
                revoked: false,
                routes: Arc::clone(&channel.routes),
                pending: HashMap::new(),
                unresolved: HashMap::new(),
            }),
            channel,
        }
    }

    fn descendant(parent: &Self, id: String, instance: String) -> Self {
        let selection = parent
            .state
            .lock()
            .expect("MCP result control poisoned")
            .selection
            .clone();
        Self {
            id,
            instance: Some(instance),
            root: false,
            state: Mutex::new(State {
                selection,
                revoked: false,
                routes: Arc::clone(&parent.channel.routes),
                pending: HashMap::new(),
                unresolved: HashMap::new(),
            }),
            channel: Arc::clone(&parent.channel),
        }
    }

    pub(crate) fn owner(&self, nonce: u64) -> Option<Arc<Self>> {
        self.channel
            .routes
            .lock()
            .ok()?
            .get(&nonce)?
            .owner
            .upgrade()
            .filter(|owner| Arc::ptr_eq(&owner.channel, &self.channel))
    }

    pub(crate) fn fail_pending(&self, nonce: u64, turn_id: &str, call_id: &str) -> bool {
        let mut state = self.state.lock().expect("MCP result control poisoned");
        if !state.pending.get(&nonce).is_some_and(|p| {
            p.turn_id == turn_id
                && p.call_id == call_id
                && p.sender.is_some()
                && p.is_cancelled.as_ref().is_none_or(|probe| !probe())
                && Instant::now() < p.deadline
        }) {
            return false;
        }
        if let Some(results) = state.unresolved.get_mut(turn_id) {
            results.failed = true;
        }
        // Release even if Core cannot interrupt a turn that has queued input.
        // The lifecycle keeps the raw result withheld.
        drop(state.retire(nonce));
        true
    }

    pub(crate) fn child(&self, id: &str) -> Option<Arc<Self>> {
        self.channel
            .members
            .read()
            .ok()?
            .get(id)?
            .upgrade()
            .filter(|owner| Arc::ptr_eq(&owner.channel, &self.channel))
    }

    pub(crate) fn revoke_family(&self) {
        self.revoke();
        let children: Vec<_> = self
            .channel
            .members
            .read()
            .expect("MCP family poisoned")
            .values()
            .filter_map(Weak::upgrade)
            .collect();
        for child in children {
            child.revoke();
        }
    }

    pub(crate) fn abandon_family_read(&self, delivery: Option<u64>) {
        self.abandon_read(delivery);
        let children: Vec<_> = self
            .channel
            .members
            .read()
            .expect("MCP family poisoned")
            .values()
            .filter_map(Weak::upgrade)
            .collect();
        for child in children {
            child.abandon_read(delivery);
        }
        // A child can have disappeared before a reader relinquishes delivery.
        // Never abandon Handed: the application still physically owns it.
        self.channel
            .routes
            .lock()
            .expect("MCP routes poisoned")
            .retain(|_, route| {
                route.owner.strong_count() != 0
                    || !matches!(route.payload, Payload::InFlight(owner)
                        if delivery.is_none() || owner == delivery)
            });
    }

    pub fn revoke(&self) {
        let mut state = self.state.lock().expect("MCP result control poisoned");
        state.revoked = true;
        // Dropping each sender leaves the result already set to withheld.
        let nonces: Vec<_> = state.pending.keys().copied().collect();
        for nonce in nonces {
            state.retire(nonce);
        }
    }

    fn end_turn(&self, turn_id: &str) {
        let mut state = self.state.lock().expect("MCP result control poisoned");
        let nonces: Vec<_> = state
            .pending
            .iter()
            .filter_map(|(nonce, item)| {
                (item.turn_id == turn_id
                    && (item.sender.is_none() || Instant::now() < item.deadline))
                    .then_some(*nonce)
            })
            .collect();
        for nonce in nonces {
            state.retire(nonce);
        }
    }

    pub fn take_unresolved(&self, turn_id: &str) -> bool {
        self.state
            .lock()
            .expect("MCP result control poisoned")
            .unresolved
            .remove(turn_id)
            .is_some_and(|results| results.count != 0)
    }

    pub fn has_settled_failure(&self, turn_id: &str) -> bool {
        self.state
            .lock()
            .expect("MCP result control poisoned")
            .unresolved
            .get(turn_id)
            .is_some_and(|results| results.failed)
    }

    // Checked before bridge-owned strings/Values, including all escaped input,
    // result, metadata and identifiers. Upstream's original allocation is not
    // ours. Failure never exposes it or bypasses the unresolved result owner.
    #[cfg(test)]
    fn enqueue(
        self: &Arc<Self>,
        state: &mut State,
        notice: &Notice<'_>,
        deadline: Instant,
    ) -> Option<oneshot::Receiver<Option<AcceptedReply>>> {
        self.enqueue_with_context(state, notice, deadline, None)
    }

    fn enqueue_with_context(
        self: &Arc<Self>,
        state: &mut State,
        notice: &Notice<'_>,
        deadline: Instant,
        tracing: Option<codex_otel::sdk_trace::ContextLease>,
    ) -> Option<oneshot::Receiver<Option<AcceptedReply>>> {
        if state.revoked || state.pending.len() >= MAX_PENDING {
            return None;
        }
        let mut counter = Counter {
            bytes: 0,
            limit: self.channel.bytes.available_permits().min(MAX_BYTES),
        };
        serde_json::to_writer(&mut counter, notice).ok()?;
        let bytes = self
            .channel
            .bytes
            .clone()
            .try_acquire_many_owned(counter.bytes.try_into().ok()?)
            .ok()?;
        let wire = serde_json::to_string(notice).ok()?;
        let (tx, rx) = oneshot::channel();
        state.pending.insert(
            notice.nonce,
            Pending {
                turn_id: notice.turn_id.to_owned(),
                call_id: notice.call_id.to_owned(),
                deadline,
                metadata: notice.phase == Some("metadata"),
                is_cancelled: None,
                sender: Some(tx),
            },
        );
        state.routes.lock().expect("MCP routes poisoned").insert(
            notice.nonce,
            Route {
                owner: Arc::downgrade(self),
                payload: Payload::Queued(wire),
                _bytes: bytes,
                _tracing: tracing,
            },
        );
        if self
            .channel
            .sender
            .try_send((Arc::downgrade(self), notice.nonce))
            .is_err()
        {
            state.retire(notice.nonce);
            return None;
        }
        Some(rx)
    }

    pub async fn next(&self, delivery: Option<u64>) -> Option<String> {
        let mut rx = self.channel.receiver.lock().await;
        while let Some((owner, nonce)) = rx.recv().await {
            let Some(owner) = owner.upgrade() else {
                continue;
            };
            let mut state = owner.state.lock().expect("MCP result control poisoned");
            if let Some(pending) = state.pending.get(&nonce) {
                if pending.sender.is_some() && Instant::now() < pending.deadline {
                    let mut routes = state.routes.lock().expect("MCP routes poisoned");
                    if let Some(route) = routes.get_mut(&nonce) {
                        if let Payload::Queued(wire) =
                            std::mem::replace(&mut route.payload, Payload::InFlight(delivery))
                        {
                            return Some(wire);
                        }
                    }
                }
                // An unanswered decision still belongs to native expiry, even
                // if delivery observes its deadline before the timer is polled.
                if pending.sender.is_none() {
                    state.retire(nonce);
                }
            }
        }
        None
    }

    // Claim only transport delivery. No authority to edit is conveyed here.
    pub fn handoff(&self, nonce: u64) -> bool {
        let state = self.state.lock().expect("MCP result control poisoned");
        let revoked = state.revoked;
        let Some(item) = state.pending.get(&nonce) else {
            return false;
        };
        let mut routes = state.routes.lock().expect("MCP routes poisoned");
        let Some(route) = routes.get_mut(&nonce) else {
            return false;
        };
        if revoked
            || item.sender.is_none()
            || Instant::now() >= item.deadline
            || !matches!(route.payload, Payload::InFlight(_))
        {
            return false;
        }
        route.payload = Payload::Handed;
        true
    }

    pub fn release(&self, nonce: u64) -> bool {
        let mut state = self.state.lock().expect("MCP result control poisoned");
        let Some(item) = state.pending.get_mut(&nonce) else {
            return false;
        };
        let replied = item.sender.is_none();
        let mut routes = state.routes.lock().expect("MCP routes poisoned");
        let Some(route) = routes.get_mut(&nonce) else {
            return false;
        };
        if !matches!(route.payload, Payload::InFlight(_) | Payload::Handed) {
            return false;
        }
        route.payload = Payload::Returned;
        if replied {
            routes.remove(&nonce);
            drop(routes);
            state.pending.remove(&nonce);
        }
        true
    }

    pub(crate) fn release_family(&self, nonce: u64) -> bool {
        if let Some(owner) = self.owner(nonce) {
            return owner.release(nonce);
        }
        let mut routes = self.channel.routes.lock().expect("MCP routes poisoned");
        let can_release = routes.get(&nonce).is_some_and(|route| {
            route.owner.strong_count() == 0
                && matches!(route.payload, Payload::InFlight(_) | Payload::Handed)
        });
        if can_release {
            routes.remove(&nonce);
        }
        can_release
    }

    pub fn abandon_read(&self, delivery: Option<u64>) {
        let mut state = self.state.lock().expect("MCP result control poisoned");
        let nonces: Vec<_> = {
            let mut routes = state.routes.lock().expect("MCP routes poisoned");
            state
                .pending
                .keys()
                .copied()
                .filter(|nonce| {
                    let Some(route) = routes.get_mut(nonce) else {
                        return false;
                    };
                    if matches!(route.payload, Payload::InFlight(owner)
                    if delivery.is_none() || owner == delivery)
                    {
                        route.payload = Payload::Returned;
                        true
                    } else {
                        false
                    }
                })
                .collect()
        };
        for nonce in nonces {
            // Returning bytes does not claim the unanswered policy decision.
            // Native expiry/lifecycle still owns its sender and stop authority.
            if state
                .pending
                .get(&nonce)
                .is_some_and(|item| item.sender.is_none())
            {
                state.retire(nonce);
            }
        }
    }

    // No error strings derived from a reply cross the bridge. Return false on
    // invalid or expired replies, withhold if the original owner is still live.
    pub fn resolve(&self, nonce: u64, turn_id: &str, call_id: &str, reply: &str) -> bool {
        self.resolve_reply(nonce, turn_id, call_id, reply, None)
    }

    pub fn resolve_with_assignment(
        &self,
        nonce: u64,
        turn_id: &str,
        call_id: &str,
        reply: &str,
    ) -> Option<oneshot::Receiver<()>> {
        let (assignment, receiver) = oneshot::channel();
        self.resolve_reply(nonce, turn_id, call_id, reply, Some(assignment))
            .then_some(receiver)
    }

    fn resolve_reply(
        &self,
        nonce: u64,
        turn_id: &str,
        call_id: &str,
        reply: &str,
        assignment: Option<oneshot::Sender<()>>,
    ) -> bool {
        // Validation grants no authority. Claim only after it finishes, so a
        // deadline crossed while decoding still belongs to native expiry.
        let metadata = {
            let state = self.state.lock().expect("MCP result control poisoned");
            let Some(pending) = state.pending.get(&nonce) else {
                return false;
            };
            pending.metadata
        };
        let edited = if metadata {
            if reply == "false" {
                // A denied call settles the metadata receipt without a policy failure.
                Some(AcceptedReply::Metadata(Err(())))
            } else {
                validated_metadata(reply).map(|metadata| AcceptedReply::Metadata(Ok(metadata)))
            }
        } else {
            validated(reply).map(|edited| AcceptedReply::Result { edited, assignment })
        };
        let mut state = self.state.lock().expect("MCP result control poisoned");
        if state.revoked
            || !state.pending.get(&nonce).is_some_and(|p| {
                p.sender.is_some()
                    && p.is_cancelled.as_ref().is_none_or(|probe| !probe())
                    && p.turn_id == turn_id
                    && p.call_id == call_id
                    && Instant::now() < p.deadline
            })
        {
            return false;
        }
        let sender = state.retire(nonce).expect("checked owner");
        let accepted = edited.is_some();
        let delivered = sender.send(edited).is_ok();
        accepted && delivered
    }
}

// Also remove a reservation on drop of the native lifecycle future, including
// cancellation while waiting. A later reply cannot edit another turn/call.
struct Reservation<'a> {
    control: Arc<McpResultControl>,
    nonce: u64,
    turn_id: &'a str,
    assigned: bool,
}

impl Reservation<'_> {
    // The receiver owns this still-unassigned obligation even after resolve
    // retires its nonce. Never recreate a turn consumed by its finishing owner.
    fn claim_rejection(&self) -> bool {
        let mut state = self
            .control
            .state
            .lock()
            .expect("MCP result control poisoned");
        if self.assigned || state.revoked {
            return false;
        }
        let Some(results) = state.unresolved.get_mut(self.turn_id) else {
            return false;
        };
        if results.count == 0 || self.nonce < results.first_nonce {
            return false;
        }
        results.failed = true;
        state.retire(self.nonce);
        true
    }

    fn assign_result(
        &mut self,
        thread_store: &ExtensionData,
        turn_store: &ExtensionData,
        result: &mut CallToolResult,
        mut edited: CallToolResult,
        peer_failed: bool,
        assignment: Option<oneshot::Sender<()>>,
    ) {
        crate::native_interception::publish_mcp_result(thread_store, turn_store, || {
            let mut state = self
                .control
                .state
                .lock()
                .expect("MCP result control poisoned");
            if !state.revoked
                && state
                    .unresolved
                    .get(self.turn_id)
                    .is_some_and(|entry| self.nonce >= entry.first_nonce && entry.count > 0)
            {
                // A successful send is insufficient: a cancelled receiver might
                // never apply it. Discharge only at raw assignment.
                if peer_failed {
                    edited.is_error = Some(true);
                }
                *result = edited;
                self.assigned = true;
                if let Some(results) = state.unresolved.get_mut(self.turn_id) {
                    results.count -= 1;
                    if results.count == 0 {
                        state.unresolved.remove(self.turn_id);
                    }
                }
            }
        });
        if self.assigned
            && let Some(assignment) = assignment
        {
            let _ = assignment.send(());
        }
    }
}

impl Drop for Reservation<'_> {
    fn drop(&mut self) {
        let mut state = self
            .control
            .state
            .lock()
            .expect("MCP result control poisoned");
        state.retire(self.nonce);
        if !self.assigned {
            // A terminal may already have consumed this turn. Never recreate it.
            if let Some(results) = state.unresolved.get_mut(self.turn_id)
                && self.nonce >= results.first_nonce
            {
                results.failed = true;
            }
        }
    }
}

#[derive(Default)]
pub struct McpResultContributor {
    pub(crate) manager: Weak<ThreadManager>,
}

impl ThreadLifecycleContributor<Config> for McpResultContributor {
    fn on_thread_start<'a>(
        &'a self,
        input: ThreadStartInput<'a, Config>,
    ) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            let root = !matches!(
                input.session_source,
                SessionSource::Internal(_) | SessionSource::SubAgent(_)
            );
            let id = input.thread_store.level_id().to_owned();
            if let Some(issuer) = input
                .thread_store
                .get::<crate::native_interception::Control>()
                && let Some(parent) = issuer.native_agents_parent(&self.manager).await
                && let Some(parent_control) =
                    parent.thread_extension_data().get::<McpResultControl>()
            {
                let control = input.thread_store.get_or_init(|| {
                    McpResultControl::descendant(
                        &parent_control,
                        id.clone(),
                        issuer.incarnation.clone(),
                    )
                });
                parent_control
                    .channel
                    .members
                    .write()
                    .expect("MCP family poisoned")
                    .insert(id, Arc::downgrade(&control));
            } else {
                input
                    .thread_store
                    .get_or_init(|| McpResultControl::new(id, root));
            }
            if let (Some(issuer), Some(results)) = (
                input
                    .thread_store
                    .get::<crate::native_interception::Control>(),
                input.thread_store.get::<McpResultControl>(),
            ) {
                issuer.bind_mcp_results(&results);
            }
        })
    }

    fn on_thread_stop<'a>(&'a self, input: ThreadStopInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(control) = input.thread_store.get::<McpResultControl>() {
                control.revoke();
            }
        })
    }
}

impl TurnLifecycleContributor for McpResultContributor {
    fn on_turn_start<'a>(&'a self, input: TurnStartInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(control) = input.thread_store.get::<McpResultControl>() {
                control.capture_selection(input.turn_store);
            } else {
                input.turn_store.get_or_init(|| TurnSelection(None));
            }
        })
    }

    fn on_turn_stop<'a>(&'a self, input: TurnStopInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(control) = input.thread_store.get::<McpResultControl>() {
                control.end_turn(input.turn_store.level_id());
            }
        })
    }

    fn on_turn_abort<'a>(&'a self, input: TurnAbortInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(control) = input.thread_store.get::<McpResultControl>() {
                control.end_turn(input.turn_store.level_id());
            }
        })
    }
}

// Connector provenance alone also covers ambient Apps. Require the explicit
// application capability on this actual owner. Core excludes codex_apps from
// the effective catalog unless this capability admits its exact registration.
// Connectorless tools from that same host registration have Other provenance.
fn result_source_admitted(thread: &ExtensionData, source: &McpToolSource, server: &str) -> bool {
    matches!(source, McpToolSource::Config)
        || (matches!(source, McpToolSource::Connector | McpToolSource::Other)
            && server == codex_mcp::CODEX_APPS_MCP_SERVER_NAME
            && thread.get::<codex_mcp::ApplicationApps>().is_some())
}

impl ToolLifecycleContributor for McpResultContributor {
    fn on_mcp_tool_request_metadata<'a>(
        &'a self,
        input: McpToolRequestMetadataInput<'a>,
    ) -> ExtensionFuture<'a, Result<Option<serde_json::Map<String, Value>>, ()>> {
        Box::pin(async move {
            let Some(selection) =
                source_selection(input.thread_store, input.turn_store, input.step_store)
                    .map_err(|_| ())?
            else {
                return Ok(None);
            };
            let profile = source_profile(input.thread_store, input.step_store).map_err(|_| ())?;
            let tool = input.mcp_tool.tool_info();
            if !selection
                .metadata_servers
                .get(&tool.server_name)
                .is_some_and(|names| names.iter().any(|name| name == tool.tool.name.as_ref()))
            {
                return Ok(None);
            }
            // A configured declaration owns failure too; never fall through to an unedited send.
            if !matches!(input.mcp_tool.source(), McpToolSource::Config) {
                return Err(());
            }
            let control = input.thread_store.get::<McpResultControl>().ok_or(())?;
            if (input.is_cancelled)() {
                return Err(());
            }
            let deadline = Instant::now() + Duration::from_secs(30);
            let (nonce, rx) = {
                let mut state = control.state.lock().expect("MCP result control poisoned");
                let results = state
                    .unresolved
                    .entry(input.turn_id.to_owned())
                    .or_insert_with(|| TurnResults {
                        first_nonce: control.channel.next_nonce.load(Ordering::Relaxed),
                        ..Default::default()
                    });
                results.count = results.count.saturating_add(1);
                let nonce = control
                    .channel
                    .next_nonce
                    .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |nonce| {
                        nonce.checked_add(1)
                    })
                    .map_err(|_| {
                        state
                            .unresolved
                            .get_mut(input.turn_id)
                            .expect("registered turn")
                            .failed = true;
                    })?;
                let (route, cell_id, runtime_id) = match &input.source {
                    codex_tools::ToolCallSource::Direct => ("direct", None, None),
                    codex_tools::ToolCallSource::CodeMode {
                        cell_id,
                        runtime_tool_call_id,
                    } => (
                        "code",
                        Some(cell_id.as_str()),
                        Some(runtime_tool_call_id.as_str()),
                    ),
                };
                let tracing = codex_otel::sdk_trace::ContextLease::capture();
                let token = tracing.as_ref().map(|context| context.token().to_owned());
                let notice = Notice {
                    phase: Some("metadata"),
                    model: Some(input.model),
                    route: Some(route),
                    cell_id,
                    runtime_tool_call_id: runtime_id,
                    tracing_context: token.as_deref(),
                    nonce,
                    generation: selection.generation,
                    profile_id: profile.as_ref().map(|profile| profile.profile_id.as_str()),
                    profile_epoch: profile.as_ref().map(|profile| profile.epoch),
                    thread_id: &control.id,
                    instance: control.instance.as_deref(),
                    turn_id: input.turn_id,
                    call_id: input.call_id,
                    source: "config",
                    server_name: &tool.server_name,
                    tool_name: &tool.tool.name,
                    callable_namespace: &tool.callable_namespace,
                    callable_name: &tool.callable_name,
                    arguments: input.arguments,
                    result: None,
                };
                let rx = control.enqueue_with_context(&mut state, &notice, deadline, tracing);
                if let Some(pending) = state.pending.get_mut(&nonce) {
                    pending.is_cancelled = Some(Arc::clone(&input.is_cancelled));
                }
                (nonce, rx)
            };
            let mut reservation = Reservation {
                control,
                nonce,
                turn_id: input.turn_id,
                assigned: false,
            };
            let Some(rx) = rx else {
                crate::native_interception::expire_mcp_result(
                    input.thread_store,
                    input.turn_store,
                    || !(input.is_cancelled)() && reservation.claim_rejection(),
                );
                return Err(());
            };
            let mut rx = rx;
            let reply = tokio::select! { biased;
                reply = &mut rx => reply,
                _ = tokio::time::sleep_until(deadline) => {
                    crate::native_interception::expire_mcp_result(input.thread_store, input.turn_store, || {
                        reservation.control.state.lock().expect("MCP result control poisoned").retire(nonce).is_some()
                    });
                    rx.await
                }
            };
            let Ok(Some(AcceptedReply::Metadata(metadata))) = reply else {
                crate::native_interception::expire_mcp_result(
                    input.thread_store,
                    input.turn_store,
                    || !(input.is_cancelled)() && reservation.claim_rejection(),
                );
                return Err(());
            };
            let mut accepted = Err(());
            crate::native_interception::publish_mcp_result(
                input.thread_store,
                input.turn_store,
                || {
                    if (input.is_cancelled)() {
                        return;
                    }
                    let mut state = reservation
                        .control
                        .state
                        .lock()
                        .expect("MCP result control poisoned");
                    if !state.revoked
                        && state.unresolved.get(input.turn_id).is_some_and(|entry| {
                            reservation.nonce >= entry.first_nonce && entry.count > 0
                        })
                    {
                        reservation.assigned = true;
                        if let Some(results) = state.unresolved.get_mut(input.turn_id) {
                            results.count -= 1;
                            if results.count == 0 {
                                state.unresolved.remove(input.turn_id);
                            }
                        }
                        accepted = metadata;
                    }
                },
            );
            accepted.map(Some)
        })
    }

    fn on_mcp_tool_result<'a>(&'a self, input: McpToolResultInput<'a>) -> ToolLifecycleFuture<'a> {
        Box::pin(async move {
            let Some(control) = input.thread_store.get::<McpResultControl>() else {
                return;
            };
            let selection =
                match source_selection(input.thread_store, input.turn_store, input.step_store) {
                    Ok(Some(selection)) => selection,
                    Ok(None) => return,
                    Err(_) => {
                        *input.result = withheld();
                        control
                            .state
                            .lock()
                            .expect("MCP result control poisoned")
                            .unresolved
                            .entry(input.turn_id.to_owned())
                            .or_default()
                            .failed = true;
                        return;
                    }
                };
            let profile = source_profile(input.thread_store, input.step_store)
                .ok()
                .flatten();
            let peer_failed = input.result.is_error == Some(true);
            let (deadline, nonce, rx) = {
                let mut state = control.state.lock().expect("MCP result control poisoned");
                let tool = input.mcp_tool.tool_info();
                let timeout = result_source_admitted(
                    input.thread_store,
                    input.mcp_tool.source(),
                    &tool.server_name,
                )
                .then(|| selection.servers.get(&tool.server_name))
                .flatten()
                .and_then(|tools| tools.get(tool.tool.name.as_ref()))
                .copied()
                .map(Duration::from_secs_f64);
                let Some(timeout) = timeout else {
                    return; // Unselected calls have no queue or editor.
                };
                let generation = selection.generation;
                let deadline = Instant::now() + timeout;
                // Do this before queuing or awaiting anything. On drop the only
                // value the rest of native can see is this fixed error.
                let original = std::mem::replace(input.result, withheld());
                let results = state
                    .unresolved
                    .entry(input.turn_id.to_owned())
                    .or_insert_with(|| TurnResults {
                        first_nonce: control.channel.next_nonce.load(Ordering::Relaxed),
                        ..Default::default()
                    });
                results.count = results.count.saturating_add(1);
                let nonce = match control.channel.next_nonce.fetch_update(
                    Ordering::Relaxed,
                    Ordering::Relaxed,
                    |nonce| nonce.checked_add(1),
                ) {
                    Ok(nonce) => nonce,
                    Err(_) => {
                        state
                            .unresolved
                            .get_mut(input.turn_id)
                            .expect("registered turn")
                            .failed = true;
                        return;
                    }
                };
                let source = match input.mcp_tool.source() {
                    McpToolSource::Config => "config",
                    McpToolSource::Connector => "connector",
                    McpToolSource::Plugin { .. } => "plugin",
                    McpToolSource::SelectedPlugin => "selected_plugin",
                    McpToolSource::Other => "other",
                };
                let tracing = codex_otel::sdk_trace::ContextLease::capture();
                let tracing_token = tracing.as_ref().map(|context| context.token().to_owned());
                let notice = Notice {
                    phase: None,
                    model: None,
                    route: None,
                    cell_id: None,
                    runtime_tool_call_id: None,
                    tracing_context: tracing_token.as_deref(),
                    nonce,
                    generation,
                    profile_id: profile.as_ref().map(|profile| profile.profile_id.as_str()),
                    profile_epoch: profile.as_ref().map(|profile| profile.epoch),
                    thread_id: &control.id,
                    instance: control.instance.as_deref(),
                    turn_id: input.turn_id,
                    call_id: input.call_id,
                    source,
                    server_name: &tool.server_name,
                    tool_name: &tool.tool.name,
                    callable_namespace: &tool.callable_namespace,
                    callable_name: &tool.callable_name,
                    arguments: input.arguments,
                    result: Some(&original),
                };
                let rx = control.enqueue_with_context(&mut state, &notice, deadline, tracing);
                (deadline, nonce, rx)
            };
            let mut reservation = Reservation {
                control,
                nonce,
                turn_id: input.turn_id,
                assigned: false,
            };
            let Some(rx) = rx else {
                crate::native_interception::expire_mcp_result(
                    input.thread_store,
                    input.turn_store,
                    || reservation.claim_rejection(),
                );
                return;
            };
            let mut rx = rx;
            let reply = tokio::select! {
                biased;
                reply = &mut rx => reply,
                _ = tokio::time::sleep_until(deadline) => {
                    crate::native_interception::expire_mcp_result(input.thread_store, input.turn_store, || {
                        reservation.control.state.lock().expect("MCP result control poisoned")
                            .retire(nonce).is_some()
                    });
                    rx.await
                }
            };
            if let Ok(Some(AcceptedReply::Result { edited, assignment })) = reply {
                reservation.assign_result(
                    input.thread_store,
                    input.turn_store,
                    input.result,
                    edited,
                    peer_failed,
                    assignment,
                );
            } else {
                crate::native_interception::expire_mcp_result(
                    input.thread_store,
                    input.turn_store,
                    || reservation.claim_rejection(),
                );
            }
        })
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    #[test]
    fn mono_source_freezes_selected_and_unselected_result_policy_for_children() {
        let control = Arc::new(McpResultControl::new("root".into(), true));
        let issuer = crate::native_interception::Control::new("root".into(), true);
        issuer.bind_mcp_results(&control);
        let unselected = issuer.capture_mcp_source();
        control.publish_selection(
            serde_json::from_value(json!({
                "generation": 7, "servers": {"vault": {"record": 0.25}}
            }))
            .unwrap(),
        );
        let selected = issuer.capture_mcp_source();
        control.publish_selection(
            serde_json::from_value(json!({"generation": 8, "servers": {}})).unwrap(),
        );
        for (source, expected) in [(unselected, None), (selected, Some(7))] {
            // Thread/step inheritance clones the exact captured source, including None.
            let child_thread = ExtensionData::new("child");
            child_thread.insert(source.clone());
            let child_step = ExtensionData::new("child-first-step");
            child_step.insert(
                (*child_thread
                    .get::<crate::native_interception::ProfileMcpSource>()
                    .unwrap())
                .clone(),
            );
            let turn = ExtensionData::new("new-turn");
            control.capture_selection(&turn);
            let actual = source_selection(&child_thread, &turn, Some(&child_step)).unwrap();
            assert_eq!(
                actual.as_ref().map(|selection| selection.generation),
                expected
            );
        }
    }

    #[test]
    fn captured_unselected_mono_source_cannot_inherit_newer_result_policy() {
        let control = McpResultControl::new("root".into(), true);
        let thread = ExtensionData::new("root");
        let step = ExtensionData::new("captured-A");
        assert!(control.capture_source_selection().is_none());
        step.insert(crate::native_interception::ProfileMcpSource {
            result_selection: None,
            receipt: None,
            policy: Some(Arc::new(
                crate::native_interception::ProfileMcpPolicy::default(),
            )),
        });
        control.publish_selection(
            serde_json::from_value(json!({
                "generation": 7, "servers": {"vault": {"record": 0.25}}
            }))
            .unwrap(),
        );
        let turn = ExtensionData::new("new-turn");
        control.capture_selection(&turn);
        assert!(!control.owns_result(&thread, &turn, Some(&step), "vault", "record", true));
        assert!(control.owns_result(&thread, &turn, None, "vault", "record", true));
    }

    #[test]
    fn captured_child_result_policy_keeps_exact_receipt_without_python_tools() {
        let control = McpResultControl::new("child".into(), false);
        let thread = ExtensionData::new("child");
        let step = ExtensionData::new("actual-parent-source");
        let receipt = codex_extension_api::AgentProfileReceipt {
            profile_id: "agent_B".into(),
            epoch: 3,
        };
        step.insert(crate::native_interception::ProfileMcpSource {
            result_selection: None,
            receipt: Some(receipt.clone()),
            policy: Some(Arc::new(
                crate::native_interception::ProfileMcpPolicy::default(),
            )),
        });
        step.insert(
            serde_json::from_value::<Selection>(json!({
                "generation": 5, "servers": {"vault": {"record": 0.25}}
            }))
            .unwrap(),
        );
        control.publish_selection(
            serde_json::from_value(json!({
                "generation": 8, "servers": {}
            }))
            .unwrap(),
        );
        let turn = ExtensionData::new("new-turn");
        control.capture_selection(&turn);
        assert!(control.owns_result(&thread, &turn, Some(&step), "vault", "record", true));
        assert_eq!(source_profile(&thread, Some(&step)).unwrap(), Some(receipt));
        assert_eq!(
            source_selection(&thread, &turn, Some(&step))
                .unwrap()
                .unwrap()
                .generation,
            5
        );
        let missing_selection = ExtensionData::new("missing-source");
        missing_selection.insert(
            (*step
                .get::<crate::native_interception::ProfileMcpSource>()
                .unwrap())
            .clone(),
        );
        assert!(source_selection(&thread, &turn, Some(&missing_selection)).is_err());
    }

    fn selected(generation: u64, enabled: bool) -> Selection {
        Selection {
            metadata_servers: HashMap::new(),
            generation,
            servers: if enabled {
                HashMap::from([("vault".into(), HashMap::from([("record".into(), 0.25)]))])
            } else {
                HashMap::new()
            },
        }
    }

    #[tokio::test]
    async fn old_reservation_cannot_reject_or_discharge_reinserted_same_turn_key() {
        let control = Arc::new(McpResultControl::new("root".into(), true));
        let mut old = Reservation {
            control: control.clone(),
            nonce: 1,
            turn_id: "same",
            assigned: false,
        };
        control.state.lock().unwrap().unresolved.insert(
            "same".into(),
            TurnResults {
                first_nonce: 2,
                count: 1,
                ..Default::default()
            },
        );
        assert!(!old.claim_rejection());
        let mut raw = withheld();
        let mut edited = withheld();
        edited.content = vec![json!({"type":"text","text":"stale edit"})];
        let thread = ExtensionData::new("root");
        let turn = ExtensionData::new("same");
        let _owner = crate::native_interception::tests::bind_owner(&thread, &turn);
        old.assign_result(&thread, &turn, &mut raw, edited, false, None);
        assert_eq!(raw.content, withheld().content);
        drop(old);
        let state = control.state.lock().unwrap();
        assert_eq!(state.unresolved["same"].count, 1);
        assert!(!state.unresolved["same"].failed);
    }

    #[tokio::test]
    async fn bounded_notice_failure_keeps_a_live_rejection_claim() {
        for metadata in [false, true] {
            let control = Arc::new(McpResultControl::with_budget("root".into(), true, 1));
            let args = json!({});
            let result = withheld();
            let mut request = notice(1, "old", &args, &result);
            request.phase = metadata.then_some("metadata");
            let reservation = Reservation {
                control: control.clone(),
                nonce: 1,
                turn_id: "old",
                assigned: false,
            };
            {
                let mut state = control.state.lock().unwrap();
                state.unresolved.insert(
                    "old".into(),
                    TurnResults {
                        first_nonce: 1,
                        count: 1,
                        ..Default::default()
                    },
                );
                assert!(
                    control
                        .enqueue(
                            &mut state,
                            &request,
                            Instant::now() + Duration::from_secs(30)
                        )
                        .is_none()
                );
            }
            assert!(reservation.claim_rejection());
            assert!(control.state.lock().unwrap().unresolved["old"].failed);
        }
    }

    #[test]
    fn rejected_obligation_is_scoped_and_never_recreated() {
        let control = Arc::new(McpResultControl::new("root".into(), true));
        let reservation = Reservation {
            control: control.clone(),
            nonce: 1,
            turn_id: "old",
            assigned: false,
        };
        assert!(!reservation.claim_rejection());
        {
            let mut state = control.state.lock().unwrap();
            state.unresolved.insert(
                "old".into(),
                TurnResults {
                    count: 1,
                    ..Default::default()
                },
            );
            state.unresolved.insert(
                "new".into(),
                TurnResults {
                    count: 1,
                    ..Default::default()
                },
            );
        }
        assert!(reservation.claim_rejection());
        {
            let mut state = control.state.lock().unwrap();
            assert_eq!(state.unresolved["new"].count, 1);
            assert!(state.unresolved["old"].failed);
            assert!(!state.unresolved["new"].failed);
            state.unresolved.remove("old");
        }
        assert!(!reservation.claim_rejection());
        assert!(!control.state.lock().unwrap().unresolved.contains_key("old"));
        control.state.lock().unwrap().unresolved.insert(
            "old".into(),
            TurnResults {
                count: 1,
                ..Default::default()
            },
        );
        control.state.lock().unwrap().revoked = true;
        assert!(!reservation.claim_rejection());
    }

    #[test]
    fn retained_turn_selection_survives_successor_removing_policy() {
        let control = McpResultControl::new("root".into(), true);
        let old = ExtensionData::new("old");
        control.publish_selection(selected(1, true));
        control.capture_selection(&old);
        control.publish_selection(selected(2, false));
        let new = ExtensionData::new("new");
        control.capture_selection(&new);
        control.capture_selection(&old);
        assert!(control.owns_result(&old, &old, None, "vault", "record", true));
        assert!(!control.owns_result(&new, &new, None, "vault", "record", true));
        assert!(!control.owns_result(&old, &old, None, "vault", "record", false));
        let snapshot = old.get::<TurnSelection>().unwrap();
        let selection = snapshot.0.as_ref().unwrap();
        assert_eq!(selection.generation, 1);
        assert_eq!(selection.servers["vault"]["record"], 0.25);
    }

    #[test]
    fn unselected_turn_cannot_inherit_successor_result_policy() {
        let control = McpResultControl::new("root".into(), true);
        let old = ExtensionData::new("old");
        control.capture_selection(&old);
        control.publish_selection(selected(3, true));
        let new = ExtensionData::new("new");
        control.capture_selection(&new);
        control.capture_selection(&old);
        assert!(old.get::<TurnSelection>().unwrap().0.is_none());
        assert!(!control.owns_result(&old, &old, None, "vault", "record", true));
        assert!(control.owns_result(&new, &new, None, "vault", "record", true));
        assert!(!control.owns_result(
            &old,
            &ExtensionData::new("missing"),
            None,
            "vault",
            "record",
            true
        ));
        assert_eq!(
            new.get::<TurnSelection>()
                .unwrap()
                .0
                .as_ref()
                .unwrap()
                .generation,
            3
        );
    }

    #[tokio::test(start_paused = true)]
    async fn expired_sender_survives_transport_and_terminal_competitors() {
        for competitor in ["next", "abandon", "end_turn", "fail_pending"] {
            let thread = ExtensionData::new("root");
            let original_turn = ExtensionData::new("old");
            let sibling_turn = ExtensionData::new("new");
            let native = crate::native_interception::tests::bind_owner(&thread, &original_turn);
            crate::native_interception::tests::bind_owner(&thread, &sibling_turn);
            let args = json!({});
            let result = withheld();
            let old = notice(1, "old", &args, &result);
            let sibling = notice(2, "new", &args, &result);
            let bytes = serde_json::to_vec(&old).unwrap().len();
            let control = Arc::new(McpResultControl::with_budget(
                "root".into(),
                true,
                2 * bytes,
            ));
            let deadline = Instant::now() + Duration::from_secs(30);
            let mut original = control
                .enqueue(&mut control.state.lock().unwrap(), &old, deadline)
                .unwrap();
            let mut other = control
                .enqueue(
                    &mut control.state.lock().unwrap(),
                    &sibling,
                    deadline + Duration::from_secs(60),
                )
                .unwrap();
            if competitor == "abandon" {
                assert!(control.next(Some(11)).await.is_some());
            }
            tokio::time::advance(Duration::from_secs(30)).await;
            match competitor {
                "next" => {
                    let wire: Value =
                        serde_json::from_str(&control.next(Some(12)).await.unwrap()).unwrap();
                    assert_eq!(wire["nonce"], 2);
                }
                "abandon" => {
                    control.abandon_read(Some(11));
                    assert!(!control.handoff(1));
                }
                "end_turn" => control.end_turn("old"),
                "fail_pending" => assert!(!control.fail_pending(1, "old", "call")),
                _ => unreachable!(),
            }
            assert!(
                matches!(
                    original.try_recv(),
                    Err(oneshot::error::TryRecvError::Empty)
                ),
                "{competitor}"
            );
            assert!(matches!(
                other.try_recv(),
                Err(oneshot::error::TryRecvError::Empty)
            ));
            assert_eq!(control.channel.bytes.available_permits(), 0);
            crate::native_interception::expire_mcp_result(&thread, &original_turn, || {
                control.state.lock().unwrap().retire(1).is_some()
            });
            let mut original_published = false;
            let mut sibling_published = false;
            crate::native_interception::publish_mcp_result(&thread, &original_turn, || {
                original_published = true
            });
            crate::native_interception::publish_mcp_result(&thread, &sibling_turn, || {
                sibling_published = true
            });
            assert!(!original_published && sibling_published, "{competitor}");
            native
                .interrupt_code_mode_cells(true, Some("old"))
                .await
                .unwrap();
            assert!(original.await.is_err());
            assert!(matches!(
                other.try_recv(),
                Err(oneshot::error::TryRecvError::Empty)
            ));
            assert_eq!(control.channel.bytes.available_permits(), bytes);
            control.revoke();
            assert!(other.await.is_err());
            if competitor == "next" {
                assert!(control.release(2));
            }
            assert_eq!(control.channel.bytes.available_permits(), 2 * bytes);
        }
    }

    #[test]
    fn connector_result_policy_requires_this_owners_explicit_apps_registration() {
        let owner = ExtensionData::new("root");
        let apps = codex_mcp::CODEX_APPS_MCP_SERVER_NAME;
        assert!(result_source_admitted(
            &owner,
            &McpToolSource::Config,
            "vault"
        ));
        assert!(!result_source_admitted(
            &owner,
            &McpToolSource::Connector,
            apps
        ));

        assert!(!result_source_admitted(&owner, &McpToolSource::Other, apps));

        owner.insert(codex_mcp::ApplicationApps::verification(
            codex_mcp::codex_apps_mcp_server_config("https://example.invalid", None, None),
        ));
        assert!(result_source_admitted(
            &owner,
            &McpToolSource::Connector,
            apps
        ));
        assert!(!result_source_admitted(
            &owner,
            &McpToolSource::Connector,
            "other"
        ));
        assert!(result_source_admitted(&owner, &McpToolSource::Other, apps));
        assert!(!result_source_admitted(
            &owner,
            &McpToolSource::Other,
            "other"
        ));
        assert!(!result_source_admitted(
            &owner,
            &McpToolSource::SelectedPlugin,
            apps
        ));
        let unrelated = ExtensionData::new("unrelated");
        assert!(!result_source_admitted(
            &unrelated,
            &McpToolSource::Connector,
            apps
        ));
    }

    fn notice<'a>(
        nonce: u64,
        turn: &'a str,
        arguments: &'a Value,
        result: &'a CallToolResult,
    ) -> Notice<'a> {
        Notice {
            phase: None,
            model: None,
            route: None,
            cell_id: None,
            runtime_tool_call_id: None,
            tracing_context: None,
            nonce,
            generation: 0,
            profile_id: None,
            profile_epoch: None,
            thread_id: "root",
            instance: None,
            turn_id: turn,
            call_id: "call",
            source: "config",
            server_name: "vault",
            tool_name: "record",
            callable_namespace: "mcp__vault",
            callable_name: "record",
            arguments,
            result: Some(result),
        }
    }

    // McpToolContext can only be built from Core's prepared live MCP connection.
    // Exercise the SDK's real queue, resolver and final reservation here; the
    // caller controls polling between resolution and native result publication.
    pub(crate) async fn resolved_edit<'a>(
        thread: &'a ExtensionData,
        turn: &'a ExtensionData,
        output: &'a mut CallToolResult,
    ) -> (
        Arc<McpResultControl>,
        oneshot::Receiver<()>,
        impl Future<Output = ()> + 'a,
    ) {
        let control = thread.get_or_init(|| McpResultControl::new("root".into(), true));
        let nonce = control.channel.next_nonce.fetch_add(1, Ordering::Relaxed);
        let args = json!({});
        let peer_result = withheld();
        let item = notice(nonce, turn.level_id(), &args, &peer_result);
        let rx = {
            let mut state = control.state.lock().unwrap();
            state
                .unresolved
                .entry(turn.level_id().to_owned())
                .or_default()
                .count += 1;
            control
                .enqueue(&mut state, &item, Instant::now() + Duration::from_secs(30))
                .unwrap()
        };
        let mut reservation = Reservation {
            control: control.clone(),
            nonce,
            turn_id: turn.level_id(),
            assigned: false,
        };
        assert!(control.next(Some(nonce)).await.is_some());
        assert!(control.handoff(nonce));
        let assignment = control
            .resolve_with_assignment(
                nonce,
                turn.level_id(),
                "call",
                r#"{"content":[{"type":"text","text":"synthetic-edited"}],"isError":false}"#,
            )
            .unwrap();
        let result_control = control.clone();
        (result_control, assignment, async move {
            let AcceptedReply::Result { edited, assignment } = rx.await.unwrap().unwrap() else {
                panic!("expected result reply");
            };
            reservation.assign_result(thread, turn, output, edited, false, assignment);
            drop(reservation);
            assert!(control.release(nonce));
        })
    }

    #[tokio::test]
    async fn family_routes_reused_turn_and_call_ids_to_original_runtime() {
        let root = Arc::new(McpResultControl::new("root".into(), true));
        let first = Arc::new(McpResultControl::descendant(
            &root,
            "first".into(),
            "run-a".into(),
        ));
        let second = Arc::new(McpResultControl::descendant(
            &root,
            "second".into(),
            "run-b".into(),
        ));
        root.channel.members.write().unwrap().extend([
            ("first".into(), Arc::downgrade(&first)),
            ("second".into(), Arc::downgrade(&second)),
        ]);
        let args = json!({});
        let result = withheld();
        let mut replies = Vec::new();
        for owner in [&root, &first, &second] {
            let nonce = owner.channel.next_nonce.fetch_add(1, Ordering::Relaxed);
            let mut item = notice(nonce, "turn", &args, &result);
            item.thread_id = &owner.id;
            item.instance = owner.instance.as_deref();
            let rx = owner
                .enqueue(
                    &mut owner.state.lock().unwrap(),
                    &item,
                    Instant::now() + Duration::from_secs(30),
                )
                .unwrap();
            replies.push((nonce, owner.id.clone(), owner.instance.clone(), rx));
        }
        for (nonce, id, instance, rx) in replies {
            let wire: Value = serde_json::from_str(&root.next(Some(17)).await.unwrap()).unwrap();
            assert_eq!(wire["thread_id"], id);
            assert_eq!(wire["instance"].as_str(), instance.as_deref());
            let owner = root.owner(nonce).unwrap();
            assert!(owner.handoff(nonce));
            assert!(owner.resolve(
                nonce,
                "turn",
                "call",
                r#"{"content":[{"type":"text","text":"redacted"}]}"#
            ));
            assert!(rx.await.unwrap().is_some());
            assert!(owner.release(nonce));
            assert!(root.owner(nonce).is_none());
        }
    }

    #[tokio::test]
    async fn child_failure_releases_only_its_pending_call_and_preserves_family_budget() {
        let root = Arc::new(McpResultControl::new("root".into(), true));
        let child = Arc::new(McpResultControl::descendant(
            &root,
            "child".into(),
            "original".into(),
        ));
        let args = json!({});
        let result = withheld();
        let mut item = notice(1, "turn", &args, &result);
        item.thread_id = "child";
        item.instance = Some("original");
        let rx = child
            .enqueue(
                &mut child.state.lock().unwrap(),
                &item,
                Instant::now() + Duration::from_secs(30),
            )
            .unwrap();
        assert!(root.next(Some(29)).await.is_some());
        let owner = root.owner(1).unwrap();
        assert!(owner.handoff(1));
        assert!(!owner.fail_pending(1, "different-turn", "call"));
        assert!(owner.fail_pending(1, "turn", "call"));
        assert!(rx.await.is_err());
        assert!(root.channel.bytes.available_permits() < MAX_BYTES);
        assert!(owner.release(1));
        assert_eq!(root.channel.bytes.available_permits(), MAX_BYTES);
        assert!(root.owner(1).is_none());
    }

    #[tokio::test]
    async fn retired_issuer_keeps_family_charge_until_exact_transport_return() {
        for handed in [false, true] {
            let args = json!({"value": "still held in application memory"});
            let result = withheld();
            let mut first = notice(1, "same-turn", &args, &result);
            first.thread_id = "child";
            first.instance = Some("run-a");
            let budget = serde_json::to_vec(&first).unwrap().len();
            let root = Arc::new(McpResultControl::with_budget("root".into(), true, budget));
            let old = Arc::new(McpResultControl::descendant(
                &root,
                "child".into(),
                "run-a".into(),
            ));
            root.channel
                .members
                .write()
                .unwrap()
                .insert("child".into(), Arc::downgrade(&old));
            let deadline = Instant::now() + Duration::from_secs(30);
            let reply = old
                .enqueue(&mut old.state.lock().unwrap(), &first, deadline)
                .unwrap();
            let wire = root.next(Some(31)).await.unwrap();
            assert_eq!(wire.len(), budget);
            if handed {
                assert!(root.owner(1).unwrap().handoff(1));
            }
            old.revoke();
            assert!(reply.await.is_err());
            let weak = Arc::downgrade(&old);
            drop(old);
            assert!(
                weak.upgrade().is_none(),
                "transport must not keep reply authority alive"
            );
            assert!(root.owner(1).is_none());
            assert_eq!(root.channel.bytes.available_permits(), 0);

            // Reusing a durable ID and identical turn/call IDs cannot inherit
            // either the old grant or its still-charged application payload.
            let new = Arc::new(McpResultControl::descendant(
                &root,
                "child".into(),
                "run-b".into(),
            ));
            root.channel
                .members
                .write()
                .unwrap()
                .insert("child".into(), Arc::downgrade(&new));
            let mut second = notice(2, "same-turn", &args, &result);
            second.thread_id = "child";
            second.instance = Some("run-b");
            assert_eq!(serde_json::to_vec(&second).unwrap().len(), budget);
            assert!(!new.resolve(1, "same-turn", "call", r#"{"content":[]}"#));
            assert!(
                new.enqueue(&mut new.state.lock().unwrap(), &second, deadline)
                    .is_none()
            );
            assert!(!root.release_family(2));
            root.abandon_family_read(Some(30));
            assert_eq!(root.channel.bytes.available_permits(), 0);
            root.abandon_family_read(Some(31));
            if handed {
                assert_eq!(
                    root.channel.bytes.available_permits(),
                    0,
                    "reader abandonment cannot return a handed application callback"
                );
                assert!(root.release_family(1));
            }
            assert_eq!(root.channel.bytes.available_permits(), budget);
            assert!(!root.channel.routes.lock().unwrap().contains_key(&1));
            assert!(!root.release_family(1));

            let next = new
                .enqueue(&mut new.state.lock().unwrap(), &second, deadline)
                .unwrap();
            assert!(root.next(Some(32)).await.is_some());
            assert!(root.owner(2).unwrap().handoff(2));
            assert!(
                !root.release_family(1),
                "old release cannot free a new callback"
            );
            assert_eq!(root.channel.bytes.available_permits(), 0);
            assert!(new.resolve(
                2,
                "same-turn",
                "call",
                r#"{"content":[{"type":"text","text":"accepted"}]}"#
            ));
            assert!(next.await.unwrap().is_some());
            assert!(root.release_family(2));
            assert!(root.channel.routes.lock().unwrap().is_empty());
            assert_eq!(root.channel.bytes.available_permits(), budget);
        }
    }

    #[tokio::test]
    async fn bounded_notice_measures_complete_wire_before_queuing() {
        let args = json!({"value":"quote\" line\n tab\t"});
        let result = CallToolResult {
            content: vec![json!({"type":"text","text":"some \\ actual text"})],
            structured_content: Some(json!({"receipt":"complete"})),
            is_error: Some(false),
            meta: Some(json!({"marker":"metadata\n"})),
        };
        let notice = notice(1, "first", &args, &result);
        let expected = json!({
            "nonce":1, "generation":0, "thread_id":"root", "turn_id":"first", "call_id":"call",
            "source":"config", "server_name":"vault", "tool_name":"record",
            "callable_namespace":"mcp__vault", "callable_name":"record",
            "arguments":args, "result":result
        });
        let encoded = serde_json::to_vec(&notice).unwrap();
        let small = Arc::new(McpResultControl::with_budget(
            "root".into(),
            true,
            encoded.len() - 1,
        ));
        let deadline = Instant::now() + Duration::from_secs(30);
        assert!(
            small
                .enqueue(&mut small.state.lock().unwrap(), &notice, deadline)
                .is_none()
        );
        assert!(small.state.lock().unwrap().pending.is_empty());
        assert_eq!(small.channel.bytes.available_permits(), encoded.len() - 1);
        assert!(small.channel.receiver.lock().await.try_recv().is_err());

        let fits = Arc::new(McpResultControl::with_budget(
            "root".into(),
            true,
            encoded.len(),
        ));
        let rx = fits
            .enqueue(&mut fits.state.lock().unwrap(), &notice, deadline)
            .unwrap();
        assert_eq!(fits.channel.bytes.available_permits(), 0);
        let value: Value = serde_json::from_str(&fits.next(Some(51)).await.unwrap()).unwrap();
        assert_eq!(value, expected);
        assert!(fits.handoff(1));
        fits.end_turn("first");
        assert!(rx.await.is_err());
        assert_eq!(fits.channel.bytes.available_permits(), 0);
        assert!(fits.release(1));
        assert_eq!(fits.channel.bytes.available_permits(), encoded.len());
        assert!(!fits.release(1));
    }

    #[tokio::test]
    async fn handed_result_keeps_aggregate_capacity_after_reply_and_turn_exit() {
        let args = json!({"value": "harmless"});
        let result = withheld();
        let first = notice(1, "old", &args, &result);
        let second = notice(2, "new", &args, &result);
        let n = serde_json::to_vec(&first).unwrap().len();
        assert_eq!(serde_json::to_vec(&second).unwrap().len(), n);
        let control = Arc::new(McpResultControl::with_budget("root".into(), true, n));
        let deadline = Instant::now() + Duration::from_secs(30);
        let rx = control
            .enqueue(&mut control.state.lock().unwrap(), &first, deadline)
            .unwrap();
        assert!(control.next(Some(1)).await.is_some());
        assert!(control.handoff(1));
        assert!(!control.resolve(1, "new", "call", "null"));
        assert!(control.resolve(
            1,
            "old",
            "call",
            r#"{"content":[{"type":"text","text":"edited"}]}"#
        ));
        assert!(rx.await.unwrap().is_some());
        control.end_turn("old");
        assert!(
            control
                .enqueue(&mut control.state.lock().unwrap(), &second, deadline)
                .is_none()
        );
        control.abandon_read(None);
        assert_eq!(
            control.channel.bytes.available_permits(),
            0,
            "reader cannot release a handed callback"
        );
        assert!(!control.release(2));
        assert!(control.release(1));
        let next = control
            .enqueue(&mut control.state.lock().unwrap(), &second, deadline)
            .unwrap();
        assert!(control.next(None).await.is_some());
        assert!(control.handoff(2));
        assert!(
            !control.release(1),
            "late previous release cannot free the new callback"
        );
        control.revoke();
        assert!(next.await.is_err());
        assert_eq!(control.channel.bytes.available_permits(), 0);
        assert!(control.release(2));
        assert_eq!(control.channel.bytes.available_permits(), n);
    }

    #[tokio::test]
    async fn discarded_queue_frees_bytes_but_retains_live_other_read() {
        let args = json!({"value": "harmless"});
        let result = withheld();
        let first = notice(1, "old", &args, &result);
        let second = notice(2, "new", &args, &result);
        let n = serde_json::to_vec(&first).unwrap().len();
        let control = Arc::new(McpResultControl::with_budget("root".into(), true, 2 * n));
        let deadline = Instant::now() + Duration::from_secs(30);
        let rx1 = control
            .enqueue(&mut control.state.lock().unwrap(), &first, deadline)
            .unwrap();
        let rx2 = control
            .enqueue(&mut control.state.lock().unwrap(), &second, deadline)
            .unwrap();
        assert_eq!(control.channel.bytes.available_permits(), 0);
        control.end_turn("old");
        assert!(rx1.await.is_err());
        assert_eq!(
            control.channel.bytes.available_permits(),
            n,
            "discard an unclaimed queue entry now"
        );
        let wire: Value = serde_json::from_str(&control.next(Some(12)).await.unwrap()).unwrap();
        assert_eq!(wire["nonce"], 2);
        control.abandon_read(Some(11));
        assert_eq!(control.channel.bytes.available_permits(), n);
        control.abandon_read(Some(12));
        let mut rx2 = rx2;
        assert!(matches!(
            rx2.try_recv(),
            Err(oneshot::error::TryRecvError::Empty)
        ));
        assert!(!control.handoff(2));
        assert_eq!(control.channel.bytes.available_permits(), n);
        // Native expiry or lifecycle, not transport abandonment, retires it.
        control.revoke();
        assert!(rx2.await.is_err());
        assert_eq!(control.channel.bytes.available_permits(), 2 * n);
        control.abandon_read(Some(12));
        assert_eq!(control.channel.bytes.available_permits(), 2 * n);
    }

    #[tokio::test]
    async fn dropped_reservation_withholds_and_keeps_bytes_until_physical_return() {
        let args = json!({});
        let result = withheld();
        let notice = notice(1, "old", &args, &result);
        let n = serde_json::to_vec(&notice).unwrap().len();
        let control = Arc::new(McpResultControl::with_budget("root".into(), true, n));
        let deadline = Instant::now() + Duration::from_secs(30);
        let rx;
        {
            let mut state = control.state.lock().unwrap();
            state.unresolved.insert(
                "old".into(),
                TurnResults {
                    count: 1,
                    failed: false,
                    ..Default::default()
                },
            );
            rx = control.enqueue(&mut state, &notice, deadline).unwrap();
        }
        let reservation = Reservation {
            control: control.clone(),
            nonce: 1,
            turn_id: "old",
            assigned: false,
        };
        assert!(control.next(Some(4)).await.is_some());
        assert!(control.handoff(1));
        drop(reservation);
        assert!(rx.await.is_err());
        assert!(control.has_settled_failure("old"));
        assert_eq!(control.channel.bytes.available_permits(), 0);
        assert!(!control.resolve(1, "old", "call", r#"{"content":[]}"#));
        assert!(control.take_unresolved("old"));
        assert!(control.release(1));
        assert_eq!(control.channel.bytes.available_permits(), n);
    }

    #[tokio::test]
    async fn settled_failure_does_not_poison_another_turns_unassigned_valid_reply() {
        let thread = ExtensionData::new("root");
        let turn = ExtensionData::new("new-turn");
        let _ = crate::native_interception::tests::bind_owner(&thread, &turn);
        let control = Arc::new(McpResultControl::new("root".into(), true));
        let (old_sender, old_receiver) = oneshot::channel();
        let (new_sender, new_receiver) = oneshot::channel();
        {
            let mut state = control.state.lock().unwrap();
            for turn in ["old-turn", "new-turn"] {
                state.unresolved.insert(
                    turn.into(),
                    TurnResults {
                        count: 1,
                        failed: false,
                        ..Default::default()
                    },
                );
            }
            state.pending.insert(
                1,
                Pending {
                    turn_id: "old-turn".into(),
                    call_id: "old-call".into(),
                    deadline: Instant::now() + Duration::from_secs(30),
                    sender: Some(old_sender),
                    metadata: false,
                    is_cancelled: None,
                },
            );
            state.pending.insert(
                2,
                Pending {
                    turn_id: "new-turn".into(),
                    call_id: "new-call".into(),
                    deadline: Instant::now() + Duration::from_secs(30),
                    sender: Some(new_sender),
                    metadata: false,
                    is_cancelled: None,
                },
            );
        }
        let old = Reservation {
            control: control.clone(),
            nonce: 1,
            turn_id: "old-turn",
            assigned: false,
        };
        let mut new = Reservation {
            control: control.clone(),
            nonce: 2,
            turn_id: "new-turn",
            assigned: false,
        };
        assert!(!control.resolve(1, "old-turn", "old-call", "null"));
        assert!(old_receiver.await.unwrap().is_none());
        drop(old);
        assert!(control.has_settled_failure("old-turn"));
        assert!(!control.has_settled_failure("new-turn"));
        assert!(control.resolve(
            2,
            "new-turn",
            "new-call",
            r#"{"content":[{"type":"text","text":"safe edited result"}],"isError":false}"#,
        ));
        assert!(!control.has_settled_failure("new-turn"));
        assert!(control.has_settled_failure("old-turn"));
        let AcceptedReply::Result { edited, assignment } = new_receiver.await.unwrap().unwrap()
        else {
            panic!("expected result reply")
        };
        assert!(!control.has_settled_failure("new-turn"));
        let mut raw_result = withheld();
        new.assign_result(&thread, &turn, &mut raw_result, edited, false, assignment);
        assert_eq!(
            raw_result.content,
            vec![json!({"type": "text", "text": "safe edited result"})]
        );
        assert_eq!(raw_result.is_error, Some(false));
        drop(new);
        assert!(!control.has_settled_failure("new-turn"));
        assert!(!control.take_unresolved("new-turn"));
        assert!(control.has_settled_failure("old-turn"));
        assert!(control.take_unresolved("old-turn"));
        assert!(!control.has_settled_failure("old-turn"));
        assert!(!control.take_unresolved("old-turn"));
    }
    #[tokio::test]
    async fn handed_callback_retains_trace_lease_through_revocation_until_physical_release() {
        let scope: Value = serde_json::from_str(
            &codex_otel::sdk_trace::start_trace_span(
                r#"{"name":"MCP lease test","kind":"workflow","disabled":true}"#,
            )
            .unwrap(),
        )
        .unwrap();
        let handle = scope["handle"].as_str().unwrap();
        let context = codex_otel::sdk_trace::context(handle).unwrap();
        let attached = context.attach();
        let lease = codex_otel::sdk_trace::ContextLease::capture().unwrap();
        let token = lease.token().to_owned();
        drop(attached);
        let args = json!({});
        let result = withheld();
        let control = Arc::new(McpResultControl::new("root".into(), true));
        let nonce = control.channel.next_nonce.fetch_add(1, Ordering::Relaxed);
        let mut message = notice(nonce, "turn", &args, &result);
        message.tracing_context = Some(&token);
        let _reply = control
            .enqueue_with_context(
                &mut control.state.lock().unwrap(),
                &message,
                Instant::now() + Duration::from_secs(30),
                Some(lease),
            )
            .unwrap();
        codex_otel::sdk_trace::release_trace_span(handle);
        let wire: Value = serde_json::from_str(&control.next(Some(1)).await.unwrap()).unwrap();
        assert_eq!(wire["tracing_context"], token);
        let delivered_nonce = wire["nonce"].as_u64().unwrap();
        assert_eq!(delivered_nonce, nonce);
        assert!(control.handoff(delivered_nonce));
        control.revoke();
        assert!(
            codex_otel::sdk_trace::context(&token).is_ok(),
            "handed callback still owns its context"
        );
        assert!(control.release_family(delivered_nonce));
        assert!(
            codex_otel::sdk_trace::context(&token).is_err(),
            "physical release must retire the token"
        );
    }
}
