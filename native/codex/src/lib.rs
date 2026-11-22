//! Private, asynchronous Core bindings for the opt-in Codex model provider.
mod config;
mod native_approval;
mod operation_tracing;
mod tool_hooks;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Once, OnceLock};

use codex_core_api::{
    CodexThread, StartIfIdleSubmission, StartThreadOptions, TurnInputRequest, TurnStartOptions,
    WithTurnExtensionData,
};
use codex_protocol::ThreadId;
use codex_protocol::dynamic_tools::normalize_dynamic_tool_specs;
use codex_protocol::models::ResponseItem;
use codex_protocol::protocol::{EventMsg, Op};
use codex_protocol::user_input::UserInput;
use futures::FutureExt;
use futures::future::{BoxFuture, Shared, join_all};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use tokio::sync::{mpsc, oneshot};

use config::{CoreServices, build_services, prepare_config};

type NativeResult<T> = Result<T, String>;
type Cleanup = Shared<BoxFuture<'static, NativeResult<()>>>;

fn py_error(message: impl Into<String>) -> PyErr {
    PyRuntimeError::new_err(message.into())
}

fn runtime() -> &'static tokio::runtime::Runtime {
    static INIT: Once = Once::new();
    INIT.call_once(|| {
        let mut builder = tokio::runtime::Builder::new_multi_thread();
        builder.enable_all().thread_stack_size(16 * 1024 * 1024);
        pyo3_async_runtimes::tokio::init(builder);
    });
    pyo3_async_runtimes::tokio::get_runtime()
}

/// A detached cleanup task keeps running when a Python awaitable is cancelled.
struct ThreadRecord {
    services: Arc<CoreServices>,
    thread: Arc<CodexThread>,
    id: ThreadId,
    model: String,
    operations: Arc<operation_tracing::Root>,
    cleanup: OnceLock<Cleanup>,
}

impl ThreadRecord {
    fn begin_close(&self) -> Cleanup {
        self.cleanup
            .get_or_init(|| {
                let thread = self.thread.clone();
                let services = self.services.clone();
                let id = self.id;
                let operations = self.operations.clone();
                let task = runtime().spawn(async move {
                    let result = async {
                        // Each fresh root owns an independent local agent runtime. Fence that
                        // exact tree before waiting, so native subagents cannot outlive an attempt.
                        let shutdown = services.manager.request_agent_tree_shutdown(id).await;
                        let result = match shutdown {
                            Ok(shutdown) => shutdown.wait().await,
                            Err(error) => {
                                let _ = thread.shutdown_and_wait().await;
                                Err(error)
                            }
                        };
                        if result.is_ok() {
                            let ids = services
                                .manager
                                .list_agent_subtree_thread_ids(id)
                                .await
                                .map_err(|_| {
                                    "Unable to release the Codex agent tree.".to_owned()
                                })?;
                            for id in ids {
                                if let Ok(current) = services.manager.get_thread(id).await {
                                    services
                                        .manager
                                        .remove_thread_if_matches(&id, &current)
                                        .await;
                                }
                            }
                        }
                        result.map_err(|_| "Unable to shut down the Codex agent tree.".to_owned())
                    }
                    .await;
                    // Detach even when a joined shutdown returns an error; the buffer remains drainable.
                    operations.close();
                    result
                });
                async move {
                    task.await
                        .map_err(|_| "Codex thread cleanup failed.".to_owned())?
                }
                .boxed()
                .shared()
            })
            .clone()
    }

    fn ensure_open(&self) -> NativeResult<()> {
        if self.cleanup.get().is_some() {
            Err("The Codex thread is closed.".to_owned())
        } else {
            Ok(())
        }
    }
}

/// The manager retains the cleanup record without retaining this user lease.
/// Dropping a cancelled startup result therefore closes the thread, while manager
/// shutdown can still wait for the detached cleanup task to finish.
struct ThreadLease {
    record: Arc<ThreadRecord>,
}

impl std::ops::Deref for ThreadLease {
    type Target = ThreadRecord;
    fn deref(&self) -> &Self::Target {
        &self.record
    }
}

impl Drop for ThreadLease {
    fn drop(&mut self) {
        drop(self.begin_close());
    }
}

struct Settings {
    cwd: String,
    helper: PathBuf,
    overrides: Vec<String>,
    api_key: Option<String>,
}

struct StartRequest {
    tools: String,
    instructions: Option<String>,
    settings: String,
    reply: oneshot::Sender<NativeResult<Arc<ThreadLease>>>,
}

enum ManagerCommand {
    Start(StartRequest),
    Close(oneshot::Sender<NativeResult<()>>),
}

/// Cleanup records outlive abandoned Python leases until shutdown finishes.
#[derive(Default)]
struct ActorThreads(Vec<Arc<ThreadRecord>>);

impl ActorThreads {
    async fn close(&self) -> NativeResult<()> {
        let results = join_all(self.0.iter().map(|thread| thread.begin_close())).await;
        results
            .into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map(|_| ())
    }
}

impl Drop for ActorThreads {
    fn drop(&mut self) {
        for thread in &self.0 {
            drop(thread.begin_close());
        }
    }
}

async fn start_thread(
    services: Arc<CoreServices>,
    request: &StartRequest,
) -> NativeResult<Arc<ThreadLease>> {
    let settings = serde_json::from_str(&request.settings)
        .map_err(|_| "Invalid Codex attempt settings.".to_owned())?;
    let mut config = prepare_config(&services, &settings).await?;
    if let Some(instructions) = &request.instructions {
        config.developer_instructions = Some(instructions.clone());
    }
    let values = serde_json::from_str(&request.tools)
        .map_err(|_| "Invalid Codex dynamic tools.".to_owned())?;
    let mut options = StartThreadOptions::new(config);
    if settings
        .get("sdk_sandbox")
        .and_then(serde_json::Value::as_bool)
        == Some(true)
    {
        options.environments = Some(Vec::new());
    }
    options.dynamic_tools = normalize_dynamic_tool_specs(values)
        .map_err(|_| "Invalid Codex dynamic tools.".to_owned())?;
    let created = Box::pin(services.manager.start_thread(options))
        .await
        .map_err(|_| "Unable to start the Codex thread.".to_owned())?;
    let operations = services.operations.root(created.thread_id.to_string());
    Ok(Arc::new(ThreadLease {
        record: Arc::new(ThreadRecord {
            model: created.session_configured.model,
            id: created.thread_id,
            thread: created.thread,
            services,
            operations,
            cleanup: OnceLock::new(),
        }),
    }))
}

async fn manager_actor(
    mut commands: mpsc::UnboundedReceiver<ManagerCommand>,
    settings: Settings,
    closed: Arc<AtomicBool>,
) {
    let mut services: Option<Arc<CoreServices>> = None;
    let mut threads = ActorThreads::default();
    while let Some(command) = commands.recv().await {
        match command {
            ManagerCommand::Start(request) => {
                if closed.load(Ordering::Acquire) || request.reply.is_closed() {
                    let _ = request
                        .reply
                        .send(Err("The Codex provider is closed.".to_owned()));
                    continue;
                }
                if services.is_none() {
                    match build_services(
                        settings.cwd.clone(),
                        settings.helper.clone(),
                        settings.overrides.clone(),
                        settings.api_key.clone(),
                    )
                    .await
                    {
                        Ok(host) => services = Some(Arc::new(host)),
                        Err(error) => {
                            let _ = request.reply.send(Err(error));
                            continue;
                        }
                    }
                }
                if closed.load(Ordering::Acquire) || request.reply.is_closed() {
                    let _ = request
                        .reply
                        .send(Err("The Codex provider is closed.".to_owned()));
                    continue;
                }
                let result = start_thread(
                    services.as_ref().expect("initialized services").clone(),
                    &request,
                )
                .await;
                if let Ok(thread) = &result {
                    threads.0.retain(|entry| {
                        entry.cleanup.get().is_none_or(|cleanup| {
                            !matches!(cleanup.clone().now_or_never(), Some(Ok(())))
                        })
                    });
                    threads.0.push(thread.record.clone());
                    if closed.load(Ordering::Acquire) || request.reply.is_closed() {
                        let _ = thread.begin_close().await;
                        let _ = request
                            .reply
                            .send(Err("The Codex startup was cancelled.".to_owned()));
                        continue;
                    }
                }
                // A cancelled receiver drops the lease, whose Drop starts cleanup.
                let _ = request.reply.send(result);
            }
            ManagerCommand::Close(reply) => {
                let _ = reply.send(threads.close().await);
                return;
            }
        }
    }
    let _ = threads.close().await;
}

struct ManagerState {
    commands: mpsc::UnboundedSender<ManagerCommand>,
    closed: Arc<AtomicBool>,
    cleanup: OnceLock<Cleanup>,
}

impl ManagerState {
    fn begin_close(&self) -> Cleanup {
        self.cleanup
            .get_or_init(|| {
                self.closed.store(true, Ordering::Release);
                let (reply, result) = oneshot::channel();
                let sent = self.commands.send(ManagerCommand::Close(reply)).is_ok();
                async move {
                    if !sent {
                        return Err("The Codex manager stopped unexpectedly.".to_owned());
                    }
                    result
                        .await
                        .map_err(|_| "The Codex manager stopped unexpectedly.".to_owned())?
                }
                .boxed()
                .shared()
            })
            .clone()
    }
}

impl Drop for ManagerState {
    fn drop(&mut self) {
        drop(self.begin_close());
    }
}

#[pyclass]
struct Manager {
    state: ManagerState,
}

#[pymethods]
impl Manager {
    #[new]
    #[pyo3(signature = (cwd, helper_executable, config_overrides, api_key=None))]
    fn new(
        cwd: String,
        helper_executable: String,
        config_overrides: Vec<String>,
        api_key: Option<String>,
    ) -> Self {
        let (commands, receiver) = mpsc::unbounded_channel();
        let closed = Arc::new(AtomicBool::new(false));
        runtime().spawn(manager_actor(
            receiver,
            Settings {
                cwd,
                helper: PathBuf::from(helper_executable),
                overrides: config_overrides,
                api_key,
            },
            closed.clone(),
        ));
        Self {
            state: ManagerState {
                commands,
                closed,
                cleanup: OnceLock::new(),
            },
        }
    }

    fn start_thread<'py>(
        &self,
        py: Python<'py>,
        dynamic_tools_json: String,
        instructions: Option<String>,
        settings_json: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        if self.state.closed.load(Ordering::Acquire) {
            return Err(py_error("The Codex provider is closed."));
        }
        let (reply, result) = oneshot::channel();
        self.state
            .commands
            .send(ManagerCommand::Start(StartRequest {
                tools: dynamic_tools_json,
                instructions,
                settings: settings_json,
                reply,
            }))
            .map_err(|_| py_error("The Codex manager stopped unexpectedly."))?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let lease = result
                .await
                .map_err(|_| py_error("The Codex manager stopped unexpectedly."))?
                .map_err(py_error)?;
            Python::attach(|py| Py::new(py, Thread { lease }))
        })
    }

    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let cleanup = self.state.begin_close();
        pyo3_async_runtimes::tokio::future_into_py(
            py,
            async move { cleanup.await.map_err(py_error) },
        )
    }
}

// The Python attempt owns joined close in its finally block. Native lease Drop
// and the manager's retained cleanup records cover abandoned startup/results.
#[pyclass]
struct Thread {
    lease: Arc<ThreadLease>,
}

#[pymethods]
impl Thread {
    #[pyo3(signature=(mode=None))]
    fn enable_tracing(&self, mode: Option<&str>) -> PyResult<()> {
        self.lease.ensure_open().map_err(py_error)?;
        self.lease
            .operations
            .enable(self.lease.thread.thread_extension_data(), mode)
            .map_err(|message| py_error(message.to_owned()))
    }

    fn wait_for_operations<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let buffer = self.lease.operations.buffer.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            buffer.updates.notified().await;
            Ok(())
        })
    }

    fn drain_operations(&self) -> Option<String> {
        // Draining a closed lease is intentional: shutdown can finish or cancel observations.
        self.lease.operations.buffer.drain()
    }

    #[getter]
    fn model(&self) -> String {
        self.lease.model.clone()
    }

    fn tool_hook_orders(&self, server: &str) -> PyResult<Vec<(&'static str, i64)>> {
        self.lease.ensure_open().map_err(py_error)?;
        Ok(tool_hooks::registered(&self.lease.thread, server))
    }

    fn inject_items<'py>(
        &self,
        py: Python<'py>,
        items_json: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let lease = self.lease.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = async {
                lease.ensure_open()?;
                let items: Vec<ResponseItem> = serde_json::from_str(&items_json)
                    .map_err(|_| "Invalid Codex replay items.".to_owned())?;
                lease
                    .thread
                    .inject_response_items(items)
                    .await
                    .map_err(|_| "Unable to replay Codex input history.".to_owned())
            }
            .await;
            result.map_err(py_error)
        })
    }

    #[pyo3(signature=(input_json, output_schema_json, recover_approvals=false))]
    fn start_turn<'py>(
        &self,
        py: Python<'py>,
        input_json: String,
        output_schema_json: String,
        recover_approvals: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let lease = self.lease.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = async {
                lease.ensure_open()?;
                let input: Vec<UserInput> = serde_json::from_str(&input_json)
                    .map_err(|_| "Invalid Codex turn input.".to_owned())?;
                let schema: Option<serde_json::Value> =
                    serde_json::from_str(&output_schema_json)
                        .map_err(|_| "Invalid Codex output schema.".to_owned())?;
                let request = TurnInputRequest::user_input(input).on_start(TurnStartOptions {
                    final_output_json_schema: schema,
                    ..Default::default()
                });
                let mut request = WithTurnExtensionData::from(request);
                if recover_approvals {
                    request = request.capture_native_approvals();
                }
                match Box::pin(lease.thread.start_turn_if_idle(request))
                    .await
                    .map_err(|_| "Unable to start the Codex turn.".to_owned())?
                {
                    StartIfIdleSubmission::Started { turn_id, .. } => Ok(turn_id),
                    StartIfIdleSubmission::NotSubmitted { .. } => {
                        Err("The Codex thread could not accept a new turn.".to_owned())
                    }
                }
            }
            .await;
            result.map_err(py_error)
        })
    }

    fn next_event<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let lease = self.lease.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            lease.ensure_open().map_err(py_error)?;
            let event = lease
                .thread
                .next_event()
                .await
                .map_err(|_| py_error("The Codex event stream ended unexpectedly."))?;
            match &event.msg {
                EventMsg::ExecApprovalRequest(_) | EventMsg::ApplyPatchApprovalRequest(_) => {
                    let (request, preconditions) = lease.thread.capture_tool_approval(event.msg)
                        .await.map_err(|_| py_error(
                            "Cannot save this Codex approval. Use session=ResumableSession with a supported local command or file edit."
                        ))?;
                    return Ok(serde_json::json!({
                        "id": event.id, "msg": request, "preconditions": preconditions
                    })
                    .to_string());
                }
                EventMsg::RequestPermissions(_)
                | EventMsg::RequestUserInput(_)
                | EventMsg::ElicitationRequest(_) => {
                    return Err(py_error(
                        "Codex requested an unsupported kind of interactive approval.",
                    ));
                }
                EventMsg::RawResponseItem(raw) => lease.operations.observe_item(&raw.item),
                _ => {}
            }
            serde_json::to_string(&event).map_err(|_| py_error("Unable to decode the Codex event."))
        })
    }

    fn resolve_approval<'py>(
        &self,
        py: Python<'py>,
        call_json: String,
        request_json: String,
        preconditions_json: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let call =
            serde_json::from_str(&call_json).map_err(|_| py_error("Invalid saved Codex call."))?;
        let request = serde_json::from_str(&request_json)
            .map_err(|_| py_error("Invalid saved Codex request."))?;
        let preconditions = serde_json::from_str(&preconditions_json)
            .map_err(|_| py_error("Invalid saved Codex preconditions."))?;
        let lease = self.lease.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            lease.ensure_open().map_err(py_error)?;
            native_approval::resolve(
                &lease.thread,
                call,
                request,
                preconditions,
                &lease.operations,
            )
            .await
            .map_err(py_error)
        })
    }

    fn interrupt<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let lease = self.lease.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = async {
                lease.ensure_open()?;
                lease
                    .thread
                    .submit(Op::Interrupt)
                    .await
                    .map(|_| ())
                    .map_err(|_| "Unable to interrupt the Codex turn.".to_owned())
            }
            .await;
            result.map_err(py_error)
        })
    }

    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let cleanup = self.lease.begin_close();
        pyo3_async_runtimes::tokio::future_into_py(
            py,
            async move { cleanup.await.map_err(py_error) },
        )
    }
}

mod goals;

#[pymodule]
fn _codex_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(goals::open_goal, m)?)?;
    m.add("CODEX_VERSION", "0.166.0")?;
    m.add("CODEX_REVISION", "6ff1e1ca3b30fc8b914720681fcf5fe62e95f856")?;
    m.add_class::<Manager>()?;
    m.add_class::<Thread>()
}
