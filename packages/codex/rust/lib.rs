//! Python ownership and asyncio boundaries for the unchanged Codex runtime.

mod application_instructions;
mod async_bridge;
mod auth;
mod canonical_rules;
mod checkpoint;
mod config;
mod context;
mod engine;
mod environment;
mod extensions;
mod goals;
mod live;
mod local_reconciliation;
mod local_sandbox;
mod manual_checkpoint;
mod mcp_result_control;
mod mcp_roster;
mod native_agents;
mod native_interception;
mod ops;
mod options;
mod provider_owner;
mod python_tools;
mod recovery_guard;
mod response_integrity;
mod storage;
mod subagents;
mod telemetry;
mod turn_settlement;
mod verification;

use codex_otel::sdk_trace::FutureExt as _;

use std::{collections::BTreeMap, sync::Arc};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use serde_json::{Value, value::RawValue};

fn py_error(error: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

// Inspect JSON syntax before visiting numbers: serde_json's arbitrary-precision
// markers otherwise make a literal object indistinguishable from a tagged f64.
fn checkpoint_json_value(raw: &RawValue, depth: usize) -> serde_json::Result<Value> {
    match raw.get().as_bytes().first() {
        Some(b'{' | b'[') if depth >= 127 => {
            // Match serde_json's default remaining_depth of 128.
            Err(serde::de::Error::custom("recursion limit exceeded"))
        }
        Some(b'{') => {
            let fields: BTreeMap<String, &RawValue> = serde_json::from_str(raw.get())?;
            fields
                .into_iter()
                .map(|(key, value)| Ok((key, checkpoint_json_value(value, depth + 1)?)))
                .collect::<Result<_, _>>()
                .map(Value::Object)
        }
        Some(b'[') => {
            let values: Vec<&RawValue> = serde_json::from_str(raw.get())?;
            values
                .into_iter()
                .map(|value| checkpoint_json_value(value, depth + 1))
                .collect::<Result<_, _>>()
                .map(Value::Array)
        }
        _ => serde_json::from_str(raw.get()),
    }
}

#[pyclass(frozen, module = "agents._native")]
struct NativeChildTurn {
    turn: Arc<native_agents::Turn>,
}

#[pymethods]
impl NativeChildTurn {
    #[getter]
    fn child_id(&self) -> String {
        self.turn.child_id.to_string()
    }
    #[getter]
    fn instance_id(&self) -> u64 {
        self.turn.instance_id
    }
    #[getter]
    fn incarnation(&self) -> Option<String> {
        self.turn.incarnation.clone()
    }
    #[getter]
    fn turn_id(&self) -> String {
        self.turn.turn_id.clone()
    }
    fn wait<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let turn = self.turn.clone();
        async_bridge::future_into_py(py, async move {
            let (output, usage) = turn.wait().await.map_err(py_error)?;
            let usage = serde_json::to_string(&usage).map_err(py_error)?;
            Ok((output, usage))
        })
    }
}

// Native process finalizers can spawn Tokio tasks, including after shutdown.
struct RuntimeOwned<T>(Option<Arc<T>>);

// A current() snapshot can outlive the wrapper's slot, including in a synchronous
// PyO3 call while a close future is running on Tokio.
impl<T> Clone for RuntimeOwned<T> {
    fn clone(&self) -> Self {
        Self(Some(Arc::clone(self)))
    }
}

impl<T> std::ops::Deref for RuntimeOwned<T> {
    type Target = Arc<T>;

    fn deref(&self) -> &Self::Target {
        self.0.as_ref().expect("native owner already dropped")
    }
}

impl<T> Drop for RuntimeOwned<T> {
    fn drop(&mut self) {
        let _runtime = pyo3_async_runtimes::tokio::get_runtime().enter();
        drop(self.0.take());
    }
}

#[pyclass(module = "agents._native")]
struct Engine {
    managed_lease: Option<std::sync::Weak<provider_owner::Lease>>,
    inner: Arc<std::sync::Mutex<EngineOwner>>,
    startup_warnings: Vec<String>,
}

struct EngineOwner {
    live: Option<RuntimeOwned<engine::EngineState>>,
    // A receiver holds no Environment/Engine. Keep it across cancelled or timed
    // out closes so retry cannot report completion before the senders drop.
    remote_drain:
        Option<tokio::sync::watch::Receiver<codex_exec_server::EnvironmentConnectionState>>,
}

async fn close_engine_owner(owner: Arc<std::sync::Mutex<EngineOwner>>) -> PyResult<()> {
    let deadline = tokio::time::Instant::now() + engine::CLOSE_TIMEOUT;
    let current = owner.lock().map_err(py_error)?.live.as_ref().cloned();
    if let Some(inner) = current {
        let _context_operation = inner.operation(true).map_err(py_error)?;
        inner.close().await.map_err(py_error)?;
        // A worker can also close an Engine directly while retaining
        // private restored NativeThreads. Shutdown has completed; release
        // their slots, just as Session.close does, before draining.
        inner.release_native_threads();
        let mut state = owner.lock().map_err(py_error)?;
        if state.live.is_some() {
            state.remote_drain =
                environment::selected_executor(&inner.manager.environment_manager())
                    .and_then(|environment| environment.subscribe_connection_state());
            state.live.take();
        }
    }
    // Drop every local live owner before waiting. Concurrent closers each
    // own a receiver, and never hold the slot mutex across the await.
    let drain = owner.lock().map_err(py_error)?.remote_drain.clone();
    if let Some(mut receiver) = drain {
        tokio::time::timeout_at(deadline, async {
            while receiver.changed().await.is_ok() {}
        })
        .await
        .map_err(|_| py_error("Codex environment shutdown timed out"))?;
        owner.lock().map_err(py_error)?.remote_drain.take();
    }
    Ok(())
}

impl Engine {
    fn new(state: engine::EngineState) -> Self {
        if let Some(store) = state
            .store
            .as_any()
            .downcast_ref::<storage::MemoryThreadStore>()
        {
            store.bind_fork_boundary(&state.interception);
        }
        let startup_warnings = state.startup_warnings.clone();
        Self {
            inner: Arc::new(std::sync::Mutex::new(EngineOwner {
                live: Some(RuntimeOwned(Some(Arc::new(state)))),
                remote_drain: None,
            })),
            startup_warnings,
            managed_lease: None,
        }
    }

    fn current(&self) -> PyResult<RuntimeOwned<engine::EngineState>> {
        self.inner
            .lock()
            .map_err(py_error)?
            .live
            .as_ref()
            .cloned()
            .ok_or_else(|| py_error("Codex engine is closed"))
    }
}

#[pymethods]
impl Engine {
    #[pyo3(signature = (payload_json, options_json="{}", specs_json=None))]
    fn restore_checkpoint<'py>(
        &self,
        py: Python<'py>,
        payload_json: &str,
        options_json: &str,
        specs_json: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let lease = self
            .managed_lease
            .as_ref()
            .and_then(std::sync::Weak::upgrade);
        let raw = serde_json::from_str(payload_json).map_err(py_error)?;
        let mut payload = checkpoint_json_value(raw, 0).map_err(py_error)?;
        let options: options::ThreadOptions =
            config::decode(serde_json::from_str(options_json).map_err(py_error)?)
                .map_err(py_error)?;
        let specs: Option<Vec<codex_protocol::dynamic_tools::DynamicToolSpec>> = specs_json
            .map(serde_json::from_str)
            .transpose()
            .map_err(py_error)?;
        let manual = if payload["format_version"] == 3 || payload["format_version"] == 5 {
            let frame = manual_checkpoint::validate(
                &payload,
                options.application_instructions.as_deref().unwrap_or(""),
                specs.as_deref().unwrap_or(&[]),
                &options.manual_tool_slots,
            )
            .map_err(py_error)?;
            payload["format_version"] =
                serde_json::json!(if payload["format_version"] == 5 { 4 } else { 2 });
            payload
                .as_object_mut()
                .expect("Validated checkpoint")
                .remove("native_continuation");
            Some(frame)
        } else {
            None
        };
        let payload = serde_json::from_value(payload).map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.operation(false).map_err(py_error)?;
            let thread = checkpoint::restore(&inner, payload, options, specs)
                .await
                .map_err(py_error)?;
            *thread.manual.lock().map_err(py_error)? = manual;
            if let Some(lease) = lease {
                lease.register_thread(thread.inner.clone())?;
            }
            Ok(thread)
        })
    }
    #[getter]
    fn _codex_account(&self) -> PyResult<Option<(Option<String>, String, String)>> {
        let inner = self.current()?;
        Ok(inner.codex_account.as_ref().map(|account| {
            (
                account.account_id.clone(),
                account.plan_type.clone(),
                account.binding.clone(),
            )
        }))
    }

    #[getter]
    fn startup_warnings(&self) -> Vec<String> {
        self.startup_warnings.clone()
    }

    #[pyo3(signature = (options_json="{}"))]
    fn start_thread<'py>(
        &self,
        py: Python<'py>,
        options_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let options = config::decode(serde_json::from_str(options_json).map_err(py_error)?)
            .map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.operation(false).map_err(py_error)?;
            inner.start(options, None).await.map_err(py_error)
        })
    }

    fn start_replaceable_thread<'py>(
        &self,
        py: Python<'py>,
        options_json: &str,
        specs_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let lease = self
            .managed_lease
            .as_ref()
            .and_then(std::sync::Weak::upgrade);
        let options = config::decode(serde_json::from_str(options_json).map_err(py_error)?)
            .map_err(py_error)?;
        let specs = serde_json::from_str(specs_json).map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.operation(false).map_err(py_error)?;
            let thread = inner
                .start_with_tools(options, None, Some(specs), false)
                .await
                .map_err(py_error)?;
            if let Some(lease) = lease {
                lease.register_thread(thread.inner.clone())?;
            }
            Ok(thread)
        })
    }

    fn _start_initial_replaceable_thread<'py>(
        &self,
        py: Python<'py>,
        options_json: &str,
        specs_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let options = config::decode(serde_json::from_str(options_json).map_err(py_error)?)
            .map_err(py_error)?;
        let specs = serde_json::from_str(specs_json).map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.operation(false).map_err(py_error)?;
            inner
                .start_with_tools(options, None, Some(specs), true)
                .await
                .map_err(py_error)
        })
    }

    #[pyo3(signature = (path, options_json="{}"))]
    fn resume_thread<'py>(
        &self,
        py: Python<'py>,
        path: String,
        options_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let options = config::decode(serde_json::from_str(options_json).map_err(py_error)?)
            .map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.operation(false).map_err(py_error)?;
            let history = inner.history(&path).await.map_err(py_error)?;
            inner.start(options, Some(history)).await.map_err(py_error)
        })
    }

    #[pyo3(signature = (thread_id, nth_user_message=None, options_json="{}", boundary_json=None, specs_json=None))]
    fn fork_thread<'py>(
        &self,
        py: Python<'py>,
        thread_id: String,
        nth_user_message: Option<usize>,
        options_json: &str,
        boundary_json: Option<&str>,
        specs_json: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        if nth_user_message.is_some() && boundary_json.is_some() {
            return Err(py_error("choose either nth_user_message or boundary"));
        }
        let boundary = boundary_json
            .map(serde_json::from_str)
            .transpose()
            .map_err(py_error)?
            .unwrap_or_default();
        let options = config::decode(serde_json::from_str(options_json).map_err(py_error)?)
            .map_err(py_error)?;
        let specs = specs_json
            .map(serde_json::from_str)
            .transpose()
            .map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.operation(false).map_err(py_error)?;
            inner
                .fork(&thread_id, nth_user_message, boundary, options, specs)
                .await
                .map_err(py_error)
        })
    }

    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        async_bridge::future_into_py(py, close_engine_owner(Arc::clone(&self.inner)))
    }

    fn _close_after_worker_loss<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let owner = Arc::clone(&self.inner);
        async_bridge::future_into_py(py, async move {
            let deadline = tokio::time::Instant::now() + engine::CLOSE_TIMEOUT;
            let current = owner.lock().map_err(py_error)?.live.as_ref().cloned();
            if let Some(inner) = current {
                let _context_operation = inner.operation(true).map_err(py_error)?;
                inner.close_after_worker_loss().await.map_err(py_error)?;
                inner.release_native_threads();
                let mut state = owner.lock().map_err(py_error)?;
                if state.live.is_some() {
                    state.remote_drain =
                        environment::selected_executor(&inner.manager.environment_manager())
                            .and_then(|environment| environment.subscribe_connection_state());
                    state.live.take();
                }
            }
            let drain = owner.lock().map_err(py_error)?.remote_drain.clone();
            if let Some(mut receiver) = drain {
                tokio::time::timeout_at(deadline, async {
                    while receiver.changed().await.is_ok() {}
                })
                .await
                .map_err(|_| py_error("Codex environment shutdown timed out"))?;
                owner.lock().map_err(py_error)?.remote_drain.take();
            }
            Ok(())
        })
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        if self
            .managed_lease
            .as_ref()
            .and_then(std::sync::Weak::upgrade)
            .is_some_and(|lease| lease.keeps_unpublished_owner())
        {
            return;
        }

        if let Some(inner) = self
            .inner
            .lock()
            .expect("native engine owner poisoned")
            .live
            .take()
        {
            pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
                let _ = inner.close().await;
            });
        }
    }
}

#[pyclass(module = "agents._native")]
struct NativeThread {
    inner: Arc<std::sync::Mutex<NativeThreadOwner>>,
    id: String,
    rollout_path: Option<String>,
    // Fresh IDs owned by a portable import or a managed fork of its history.
    // The SDK can discard them if opening is cancelled or fork export ends.
    checkpoint_import: Option<Vec<codex_core_api::ThreadId>>,
    manual: manual_checkpoint::Owner,
}

struct NativeThreadOwner {
    live: Option<RuntimeOwned<engine::ThreadState>>,
    context_change: std::sync::Weak<std::sync::Mutex<Option<ContextChangeOwner>>>,
}

async fn close_thread_owner(
    owner: Arc<std::sync::Mutex<NativeThreadOwner>>,
    imported: Option<Vec<codex_core_api::ThreadId>>,
) -> PyResult<()> {
    let current = owner.lock().map_err(py_error)?.live.as_ref().cloned();
    if let Some(inner) = current {
        let _context_operation = inner.engine.operation(true).map_err(py_error)?;
        let _lifecycle = if imported.is_some() {
            Some(inner.engine.lifecycle.lock().await)
        } else {
            None
        };
        if let Some(imported) = imported {
            inner
                .engine
                .rollback_checkpoint_import(&imported, Some(&inner))
                .await
                .map_err(py_error)?;
        } else {
            inner.close().await.map_err(py_error)?;
        }
        owner.lock().map_err(py_error)?.release();
    }
    Ok(())
}

impl NativeThreadOwner {
    // Call only after successful thread or engine shutdown. A failed close
    // leaves the slot live for the existing retry path.
    fn release(&mut self) {
        self.live.take();
    }
}

impl NativeThread {
    fn new(state: Arc<engine::ThreadState>) -> Self {
        let id = state.id.to_string();
        let rollout_path = state
            .thread
            .rollout_path()
            .map(|p| p.to_string_lossy().into_owned());
        let inner = Arc::new(std::sync::Mutex::new(NativeThreadOwner {
            live: Some(RuntimeOwned(Some(Arc::clone(&state)))),
            context_change: std::sync::Weak::new(),
        }));
        state.engine.register_native_thread(&inner);
        Self {
            inner,
            id,
            rollout_path,
            checkpoint_import: None,
            manual: Default::default(),
        }
    }

    fn live(&self) -> PyResult<Option<RuntimeOwned<engine::ThreadState>>> {
        Ok(self.inner.lock().map_err(py_error)?.live.as_ref().cloned())
    }

    fn current(&self) -> PyResult<RuntimeOwned<engine::ThreadState>> {
        self.live()?
            .ok_or_else(|| py_error("Codex thread is closed"))
    }

    fn native_interception(&self) -> PyResult<Option<Arc<native_interception::Control>>> {
        self.live()?
            .map(|inner| {
                inner
                    .thread
                    .thread_extension_data()
                    .get::<native_interception::Control>()
                    .ok_or_else(|| py_error("native interception was not initialized"))
            })
            .transpose()
    }

    fn mcp_result_control(&self) -> PyResult<Option<Arc<mcp_result_control::McpResultControl>>> {
        self.live()?
            .map(|inner| {
                inner
                    .thread
                    .thread_extension_data()
                    .get::<mcp_result_control::McpResultControl>()
                    .ok_or_else(|| py_error("native MCP result control was not initialized"))
            })
            .transpose()
    }
}

#[pymethods]
impl NativeThread {
    fn _enable_native_interception(&self, timeout: f64, scope: &str) -> PyResult<bool> {
        let scope = serde_json::from_value(serde_json::json!(scope)).map_err(py_error)?;
        self.native_interception()?
            .ok_or_else(|| py_error("Codex thread is closed"))?
            .enable(timeout, scope)
            .map_err(py_error)?;
        // This entered native channel owns physical payload receipts, even
        // before it has delivered its first notice to the reader.
        Ok(true)
    }

    fn _revoke_native_interception(&self) -> PyResult<()> {
        if let Some(control) = self.native_interception()? {
            control.revoke();
        }
        Ok(())
    }

    #[pyo3(signature = (delivery=0))]
    fn _next_native_interception<'py>(
        &self,
        py: Python<'py>,
        delivery: u64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let control = self
            .native_interception()?
            .ok_or_else(|| py_error("Codex thread is closed"))?;
        async_bridge::future_into_py(py, async move {
            Ok(control
                .next_wire(delivery)
                .await
                .unwrap_or_else(|| "null".into()))
        })
    }

    fn _resolve_native_interception(&self, nonce: u64, reply: &str) -> PyResult<bool> {
        // A missing live slot means successful shutdown has already revoked
        // the control. Never retain the old thread just to reject a stale nonce.
        Ok(self
            .native_interception()?
            .is_some_and(|control| control.resolve(nonce, reply)))
    }

    fn _check_native_interception(&self, nonce: u64) -> PyResult<bool> {
        Ok(self
            .native_interception()?
            .is_some_and(|control| control.check(nonce)))
    }

    fn _configure_native_interception(&self, rules: &str) -> PyResult<()> {
        let rules = serde_json::from_str(rules).map_err(py_error)?;
        self.native_interception()?
            .ok_or_else(|| py_error("Codex thread is closed"))?
            .configure_tools(rules)
            .map_err(py_error)
    }

    fn _release_native_interception(&self, nonce: u64) -> PyResult<bool> {
        Ok(self
            .native_interception()?
            .is_some_and(|control| control.release(nonce)))
    }

    fn _handoff_native_interception(&self, nonce: u64) -> PyResult<bool> {
        Ok(self
            .native_interception()?
            .is_some_and(|control| control.handoff(nonce)))
    }

    fn _expire_native_interception_result(&self, nonce: u64) -> PyResult<()> {
        if let Some(control) = self.native_interception()? {
            // The Python deadline callback calls synchronously; task ownership
            // must register before it releases the exact decision's authority.
            let _runtime = pyo3_async_runtimes::tokio::get_runtime().enter();
            control.expire(nonce);
        }
        Ok(())
    }

    fn _hold_native_interception_for_stop(&self, nonce: u64) -> PyResult<bool> {
        Ok(self
            .native_interception()?
            .is_some_and(|control| control.hold_for_stop(nonce)))
    }

    fn _finish_native_interception_stop<'py>(
        &self,
        py: Python<'py>,
        nonce: u64,
    ) -> PyResult<Bound<'py, PyAny>> {
        // Capture exact already-held authority before returning an awaitable:
        // invocation cancellation can retire its nonce before the next poll.
        let held = self
            .native_interception()?
            .ok_or_else(|| py_error("Native policy owner unavailable"))?
            .capture_held_stop(nonce)
            .map_err(py_error)?;
        let live = self.live()?;
        async_bridge::future_into_py(py, async move {
            let Some(inner) = live else {
                return Ok(());
            };
            let _operation = inner.engine.operation(true).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            held.finish().await.map_err(py_error)
        })
    }

    fn _finish_native_interception_reader(&self) -> PyResult<()> {
        if let Some(control) = self.native_interception()? {
            control.reader_finished();
        }
        Ok(())
    }

    fn _abandon_native_interception_read(&self, delivery: u64) -> PyResult<()> {
        if let Some(control) = self.native_interception()? {
            control.abandon(delivery);
        }
        Ok(())
    }

    fn _tool_output_body(&self, response_json: &str) -> PyResult<String> {
        use codex_protocol::models::{FunctionCallOutputBody, FunctionCallOutputContentItem};
        let response: codex_protocol::dynamic_tools::DynamicToolResponse =
            serde_json::from_str(response_json).map_err(py_error)?;
        let content: Vec<FunctionCallOutputContentItem> =
            response.content_items.into_iter().map(Into::into).collect();
        let body = match content.as_slice() {
            [FunctionCallOutputContentItem::InputText { text }] => {
                FunctionCallOutputBody::Text(text.clone())
            }
            _ => FunctionCallOutputBody::ContentItems(content),
        };
        serde_json::to_string(&body).map_err(py_error)
    }

    #[pyo3(signature = (options_json, specs_json, history_json, additions_json, receipt_json))]
    fn _rebuild_local<'py>(
        &self,
        py: Python<'py>,
        options_json: &str,
        specs_json: &str,
        history_json: &str,
        additions_json: &str,
        receipt_json: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let options = serde_json::from_str(options_json).map_err(py_error)?;
        let specs = serde_json::from_str(specs_json).map_err(py_error)?;
        let inner = self.current()?;
        let (history, prepared, receipt, lease) = if let Some(receipt_json) = receipt_json {
            let history = serde_json::from_str(history_json).map_err(py_error)?;
            let prepared = serde_json::from_str(additions_json).map_err(py_error)?;
            let receipt: storage::RebuildReceipt =
                serde_json::from_str(receipt_json).map_err(py_error)?;
            (history, prepared, storage::Rebuild::from(receipt), None)
        } else {
            // Never admit compaction from history arguments. The same thread
            // must still own the lease that natively prepared this candidate.
            let change = self
                .inner
                .lock()
                .map_err(py_error)?
                .context_change
                .upgrade()
                .ok_or_else(|| py_error("local compaction requires a native context lease"))?;
            let mut slot = change.lock().map_err(py_error)?;
            let change = slot
                .as_mut()
                .ok_or_else(|| py_error("context lease is closed"))?;
            let candidate = change
                .prepared
                .take()
                .ok_or_else(|| py_error("local compaction requires native preparation"))?;
            let history = serde_json::from_value(change.change.checkpoint["history"].clone())
                .map_err(py_error)?;
            let prepared =
                serde_json::from_value(candidate["history"].clone()).map_err(py_error)?;
            (
                history,
                prepared,
                storage::Rebuild::Compaction,
                Some(change.change.clone()),
            )
        };
        async_bridge::future_into_py(py, async move {
            let _lease = lease;
            inner
                .engine
                .rebuild_local(&inner, options, specs, history, prepared, receipt)
                .await
                .map_err(py_error)
        })
    }

    fn _begin_context_change<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let owner = Arc::clone(&self.inner);
        async_bridge::future_into_py(py, async move {
            let change = context::begin(Arc::clone(&inner)).await.map_err(py_error)?;
            let change = Arc::new(std::sync::Mutex::new(Some(ContextChangeOwner {
                change: RuntimeOwned(Some(Arc::new(change))),
                prepared: None,
            })));
            owner.lock().map_err(py_error)?.context_change = Arc::downgrade(&change);
            Ok(ContextChange { inner: change })
        })
    }

    fn _read_model_context<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let value = context::read(&inner).await.map_err(py_error)?;
            serde_json::to_string(&value).map_err(py_error)
        })
    }

    fn _revoke_native_mcp_results(&self) -> PyResult<()> {
        if let Some(control) = self.mcp_result_control()? {
            control.revoke_family();
        }
        Ok(())
    }

    fn _take_native_mcp_result_failure(&self, turn_id: &str) -> PyResult<bool> {
        Ok(self
            .mcp_result_control()?
            .ok_or_else(|| py_error("Codex thread is closed"))?
            .take_unresolved(turn_id))
    }

    #[pyo3(signature = (turn_id, thread_id=None))]
    fn _has_settled_native_mcp_result_failure(
        &self,
        turn_id: &str,
        thread_id: Option<&str>,
    ) -> PyResult<bool> {
        Ok(self
            .mcp_result_control()?
            .and_then(|control| match thread_id {
                Some(id) if id != self.id => control.child(id),
                _ => Some(control),
            })
            .is_some_and(|control| control.has_settled_failure(turn_id)))
    }

    #[pyo3(signature = (delivery=None))]
    fn _next_native_mcp_result<'py>(
        &self,
        py: Python<'py>,
        delivery: Option<u64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let control = self
            .mcp_result_control()?
            .ok_or_else(|| py_error("Codex thread is closed"))?;
        async_bridge::future_into_py(py, async move {
            Ok(control
                .next(delivery)
                .await
                .unwrap_or_else(|| "null".into()))
        })
    }

    fn _handoff_native_mcp_result(&self, nonce: u64) -> PyResult<bool> {
        Ok(self
            .mcp_result_control()?
            .and_then(|control| control.owner(nonce))
            .is_some_and(|control| control.handoff(nonce)))
    }

    fn _check_native_mcp_result(&self, nonce: u64) -> PyResult<bool> {
        Ok(self
            .mcp_result_control()?
            .and_then(|control| control.owner(nonce))
            .is_some_and(|control| control.is_pending(nonce)))
    }

    fn _release_native_mcp_result(&self, nonce: u64) -> PyResult<bool> {
        Ok(self
            .mcp_result_control()?
            .is_some_and(|control| control.release_family(nonce)))
    }

    #[pyo3(signature = (delivery=None))]
    fn _abandon_native_mcp_result_read(&self, delivery: Option<u64>) -> PyResult<()> {
        if let Some(control) = self.mcp_result_control()? {
            control.abandon_family_read(delivery);
        }
        Ok(())
    }

    fn _resolve_native_mcp_result(
        &self,
        nonce: u64,
        turn_id: &str,
        call_id: &str,
        reply: &str,
    ) -> PyResult<bool> {
        Ok(self
            .mcp_result_control()?
            .and_then(|control| control.owner(nonce))
            .is_some_and(|control| control.resolve(nonce, turn_id, call_id, reply)))
    }

    fn export_checkpoint<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        if inner
            .thread
            .thread_extension_data()
            .get::<python_tools::PythonTools>()
            .is_some()
        {
            return Err(py_error(
                "Managed checkpoints require Session.save_state() to verify an idle session, including Python workers",
            ));
        }
        if inner.engine.local_checklist_store.is_some() {
            return Err(py_error(
                "Native local threads cannot export portable checkpoints",
            ));
        }
        self._export_checkpoint(py, None)
    }

    // Internal SDK ingress, like _submit: its caller holds the Session state
    // gate and has checked host callbacks, approvals and surviving sync workers,
    // or is the executor capturing its approved native pending call pre-effect.
    #[pyo3(signature = (request_json=None))]
    fn _export_checkpoint<'py>(
        &self,
        py: Python<'py>,
        request_json: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let request = request_json
            .map(serde_json::from_str)
            .transpose()
            .map_err(py_error)?;
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            let mut state = checkpoint::export(&inner, request.as_ref())
                .await
                .map_err(py_error)?;
            if let Some(guard) = inner
                .thread
                .thread_extension_data()
                .get::<recovery_guard::Guard>()
            {
                state["root_tool_origins"] =
                    serde_json::to_value(guard.origins()).map_err(py_error)?;
            }
            // The current ordinary roster belongs to the native thread, including
            // managed filtering. Use the existing SessionState options shape.
            state["agent_options"] = serde_json::json!({
                "config": {"mcp_servers": mcp_roster::checkpoint_servers(&inner.thread).await}
            });
            serde_json::to_string(&state).map_err(py_error)
        })
    }

    // Recheck after caller-owned checkpoint storage has settled, immediately
    // before invoking the Python effect. A raw native interrupt may have raced it.
    #[pyo3(signature = (request_json, *, entering=false))]
    fn _check_python_tool(&self, request_json: &str, entering: bool) -> PyResult<()> {
        let request: serde_json::Value = serde_json::from_str(request_json).map_err(py_error)?;
        let inner = self.current()?;
        let key: python_tools::PendingKey =
            serde_json::from_value(request.clone()).map_err(py_error)?;
        if key.reply.thread != inner.id.to_string() {
            let tools = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .and_then(|tools| tools.owner(&inner.id.to_string(), &key))
                .ok_or_else(|| py_error("Expired or invalid managed Python tool request"))?;
            if entering {
                return Err(py_error(
                    "Native children cannot park or enter SDK manual approvals",
                ));
            }
            return tools
                .check_pending(&key.reply.thread, &key)
                .map_err(py_error);
        }
        checkpoint::check_capture(&inner, &request).map_err(py_error)?;
        if entering {
            let tools = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .ok_or_else(|| py_error("effect entry requires managed tools"))?;
            tools.enter(&inner.id.to_string(), &key).map_err(py_error)?;
        }
        Ok(())
    }

    // Cancellation from a child callback must target its still-live issuer,
    // even before the root event reader has observed TurnStarted. Bare IDs
    // are never authority to interrupt; require the original pending request.
    fn _interrupt_child_callback<'py>(
        &self,
        py: Python<'py>,
        request_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let request: serde_json::Value = serde_json::from_str(request_json).map_err(py_error)?;
        let live = self.live()?;
        async_bridge::future_into_py(py, async move {
            // A successful native close has already joined the entire graph.
            // A child callback may still be completing its Python cleanup.
            let Some(inner) = live else {
                return Ok(true);
            };
            let _operation = inner.engine.operation(false).map_err(py_error)?;
            if inner.is_closed() {
                return Ok(true);
            }
            if let Some(nonce) = request.get("nonce").and_then(serde_json::Value::as_u64) {
                let Some(id) = request.get("thread_id").and_then(serde_json::Value::as_str) else {
                    return Ok(false);
                };
                let Some(turn) = request.get("turn_id").and_then(serde_json::Value::as_str) else {
                    return Ok(false);
                };
                let Some(call) = request.get("call_id").and_then(serde_json::Value::as_str) else {
                    return Ok(false);
                };
                if id == inner.id.to_string() {
                    return Ok(false);
                }
                let Some(control) = inner
                    .thread
                    .thread_extension_data()
                    .get::<mcp_result_control::McpResultControl>()
                    .and_then(|root| root.owner(nonce))
                else {
                    return Ok(false);
                };
                let id = codex_core_api::ThreadId::from_string(id).map_err(py_error)?;
                let Ok(thread) = inner.engine.manager.get_thread(id).await else {
                    return Ok(false);
                };
                let Some(live) = thread
                    .thread_extension_data()
                    .get::<mcp_result_control::McpResultControl>()
                else {
                    return Ok(false);
                };
                if !Arc::ptr_eq(&live, &control) || !control.fail_pending(nonce, turn, call) {
                    return Ok(false);
                }
                return thread
                    .interrupt_if_no_pending_input(turn)
                    .await
                    .map_err(py_error);
            }
            let key: python_tools::PendingKey =
                serde_json::from_value(request).map_err(py_error)?;
            if key.reply.thread == inner.id.to_string() {
                return Ok(false);
            }
            let Some(tools) = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .and_then(|root| root.owner(&inner.id.to_string(), &key))
            else {
                return Ok(false);
            };
            let id = codex_core_api::ThreadId::from_string(&key.reply.thread).map_err(py_error)?;
            let Ok(thread) = inner.engine.manager.get_thread(id).await else {
                return Ok(false);
            };
            let Some(live) = thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
            else {
                return Ok(false);
            };
            if !Arc::ptr_eq(&live, &tools) || !tools.fail_pending(&key) {
                return Ok(false);
            }
            thread
                .interrupt_if_no_pending_input(&key.turn)
                .await
                .map_err(py_error)
        })
    }
    #[getter]
    fn id(&self) -> String {
        self.id.clone()
    }

    #[getter]
    fn rollout_path(&self) -> Option<String> {
        self.rollout_path.clone()
    }

    #[pyo3(signature = (subagent_id=None))]
    fn read_goal<'py>(
        &self,
        py: Python<'py>,
        subagent_id: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let id = subagent_id
            .map(codex_core_api::ThreadId::from_string)
            .transpose()
            .map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            let result = goals::read(&inner, id).await.map_err(py_error)?;
            serde_json::to_string(&result).map_err(py_error)
        })
    }

    #[pyo3(signature = (subagent_id=None))]
    fn read_checklist<'py>(
        &self,
        py: Python<'py>,
        subagent_id: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let id = subagent_id
            .map(codex_core_api::ThreadId::from_string)
            .transpose()
            .map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(true).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            if inner
                .engine
                .closed
                .load(std::sync::atomic::Ordering::Acquire)
                || inner
                    .engine
                    .context_retired
                    .load(std::sync::atomic::Ordering::Acquire)
            {
                return Err(py_error("native checklist read requires an open thread"));
            }
            let result = goals::read_checklist(&inner, id).await.map_err(py_error)?;
            serde_json::to_string(&result).map_err(py_error)
        })
    }

    #[pyo3(signature = (objective, subagent_id=None, token_budget=None))]
    fn set_paused_goal<'py>(
        &self,
        py: Python<'py>,
        objective: String,
        subagent_id: Option<&str>,
        token_budget: Option<i64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let id = subagent_id
            .map(codex_core_api::ThreadId::from_string)
            .transpose()
            .map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            let result = goals::set_paused(&inner, &objective, id, token_budget)
                .await
                .map_err(py_error)?;
            serde_json::to_string(&result).map_err(py_error)
        })
    }

    #[pyo3(signature = (subagent_id=None))]
    fn complete_goal<'py>(
        &self,
        py: Python<'py>,
        subagent_id: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let id = subagent_id
            .map(codex_core_api::ThreadId::from_string)
            .transpose()
            .map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            let result = goals::complete(&inner, id).await.map_err(py_error)?;
            serde_json::to_string(&result).map_err(py_error)
        })
    }

    #[pyo3(signature = (operation, params_json="{}"))]
    fn call<'py>(
        &self,
        py: Python<'py>,
        operation: String,
        params_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let params: Value = serde_json::from_str(params_json).map_err(py_error)?;
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            if inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .is_some()
            {
                python_tools::check_raw_operation(&operation).map_err(py_error)?;
            }
            let value = if operation == "list_subagents" {
                if !params.as_object().is_some_and(|params| params.is_empty()) {
                    return Err(py_error("list_subagents takes an empty parameters object"));
                }
                serde_json::to_value(subagents::list(&inner).await.map_err(py_error)?)
                    .map_err(py_error)?
            } else if operation == "connect_environment" {
                environment::connect_environment(Arc::clone(&inner), params)
                    .await
                    .map_err(py_error)?
            } else if operation == "environment_attached" {
                let _connection = inner.engine.environment_connection.lock().await;
                let _mutation = inner.mutations.lock().await;
                serde_json::json!({"attached": inner.engine.attached_environment.read().map_err(py_error)?.is_some()})
            } else {
                let _mutation = if matches!(
                    operation.as_str(),
                    "turn_input"
                        | "start_turn"
                        | "recover_turn"
                        | "recover_turn_if_idle"
                        | "thread_settings"
                        | "turn_settings"
                        | "interrupt"
                        | "shutdown"
                        | "suspend_turn_and_shutdown"
                        | "interrupt_if_no_pending_input"
                        | "refresh_mcp_servers"
                        | "inject_if_running"
                        | "inject_response_items"
                ) {
                    Some(inner.mutations.lock().await)
                } else {
                    None
                };
                if operation == "set_instructions" {
                    #[derive(serde::Deserialize)]
                    #[serde(deny_unknown_fields)]
                    struct Instructions {
                        instructions: Option<String>,
                    }
                    let value: Instructions = serde_json::from_value(params).map_err(py_error)?;
                    *inner.instructions.0.write().map_err(py_error)? = value.instructions;
                    Value::Null
                } else {
                    ops::call(&inner.thread, &operation, params)
                        .await
                        .map_err(py_error)?
                }
            };
            serde_json::to_string(&value).map_err(py_error)
        })
    }

    fn _request_sampling_stop(&self, expected_turn_id: &str) -> PyResult<()> {
        if let Some(inner) = self.live()?
            && let Some(tools) = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
        {
            tools.request_sampling_stop(expected_turn_id);
        }
        Ok(())
    }

    #[pyo3(signature = (expected_turn_id, *, queued_compaction=false))]
    fn _interrupt_session<'py>(
        &self,
        py: Python<'py>,
        expected_turn_id: String,
        queued_compaction: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            ops::interrupt_managed_root(&inner, &expected_turn_id, queued_compaction)
                .await
                .map_err(py_error)
        })
    }

    fn _interrupt_graph<'py>(&self, py: Python<'py>, mode: &str) -> PyResult<Bound<'py, PyAny>> {
        let force = match mode {
            "force" => true,
            "graceful" => false,
            _ => return Err(PyValueError::new_err("mode must be 'force' or 'graceful'")),
        };
        let Some(inner) = self.live()? else {
            // The slot is released only after native shutdown was joined.
            return async_bridge::future_into_py(py, async { Ok(()) });
        };
        let tools = inner
            .thread
            .thread_extension_data()
            .get::<python_tools::PythonTools>()
            .ok_or_else(|| py_error("Graph interruption requires a managed Session"))?;
        let request = {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            tools.request_graph_interrupt(force).map_err(py_error)?
        };
        async_bridge::future_into_py(py, async move {
            let _request = request;
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            tools
                .interrupt_graph(&inner.engine.manager, &inner.mutations, force)
                .await
                .map_err(py_error)
        })
    }

    #[pyo3(signature = (*, stopped_only=false, child=None))]
    fn _interrupt_code_mode_cells<'py>(
        &self,
        py: Python<'py>,
        stopped_only: bool,
        child: Option<(String, u64, String)>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if child.is_some() && !stopped_only {
            return Err(py_error("A child cell sweep requires a stopped turn"));
        }
        let child = child
            .map(|(id, instance, turn)| {
                codex_core_api::ThreadId::from_string(&id)
                    .map(|id| (id, instance, turn))
                    .map_err(py_error)
            })
            .transpose()?;
        let live = self.live()?;
        async_bridge::future_into_py(py, async move {
            let Some(inner) = live else {
                // Slots are released only after successful native shutdown.
                return Ok(());
            };
            let _operation = inner.engine.operation(true).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            if inner.is_closed() {
                return Ok(());
            }
            let (thread, expected_turn) = match child {
                None => (inner.thread.clone(), None),
                Some((id, instance, turn)) => {
                    let instances = inner
                        .child_instances
                        .read()
                        .map_err(|_| py_error("Native child state unavailable"))?;
                    let Some(thread) = instances
                        .get(&id)
                        .filter(|current| current.id == instance)
                        .and_then(|current| current.thread.upgrade())
                    else {
                        // A closed/replaced incarnation already lost its runtime.
                        return Ok(());
                    };
                    (thread, Some(turn))
                }
            };
            let control = thread
                .thread_extension_data()
                .get::<native_interception::Control>()
                .ok_or_else(|| py_error("Session interruption requires a managed Control"))?;
            control
                .interrupt_code_mode_cells(stopped_only, expected_turn.as_deref())
                .await
                .map_err(py_error)
        })
    }

    fn _request_graceful_interrupt<'py>(
        &self,
        py: Python<'py>,
        expected_turn_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            let tools = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .ok_or_else(|| py_error("Session interruption requires replaceable tools"))?;
            tools
                .request_graceful_interrupt(&expected_turn_id)
                .map_err(py_error)
        })
    }

    fn _finish_graceful_interrupt<'py>(
        &self,
        py: Python<'py>,
        expected_turn_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            let tools = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .ok_or_else(|| py_error("Session interruption requires replaceable tools"))?;
            tools
                .finish_graceful_interrupt(&inner.thread, &expected_turn_id)
                .await
                .map_err(py_error)
        })
    }

    fn _submit<'py>(&self, py: Python<'py>, params_json: &str) -> PyResult<Bound<'py, PyAny>> {
        let params: Value = serde_json::from_str(params_json).map_err(py_error)?;
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            if inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .is_none()
            {
                return Err(py_error(
                    "managed submit requires a replaceable tool session",
                ));
            }
            let result = ops::submit_replaceable(&inner, params)
                .await
                .map_err(py_error)?;
            serde_json::to_string(&result).map_err(py_error)
        })
    }

    fn _reconcile_local_calls<'py>(
        &self,
        py: Python<'py>,
        receipts_json: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            async_bridge::retain_writer_operation(async move {
                let _context_operation = _context_operation;
                local_reconciliation::reconcile(&inner, &receipts_json)
                    .await
                    .map_err(py_error)
            })
            .await
        })
    }

    fn _executor_policies<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let policies = local_reconciliation::executor_policies(&inner)
                .await
                .map_err(py_error)?;
            serde_json::to_string(&policies).map_err(py_error)
        })
    }

    // Managed ingress: the SDK holds its single active owner until terminal
    // settlement. Keep raw Compact blocked because it replaces active work.
    #[pyo3(signature = (tracing_context=None))]
    fn _compact<'py>(
        &self,
        py: Python<'py>,
        tracing_context: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let context = tracing_context
            .map(codex_otel::sdk_trace::context)
            .transpose()
            .map_err(py_error)?
            .unwrap_or_default();
        let inner = self.current()?;
        async_bridge::future_into_py(
            py,
            async move {
                let _context_operation = inner.engine.operation(false).map_err(py_error)?;
                let _mutation = inner.mutations.lock().await;
                let tools = inner
                    .thread
                    .thread_extension_data()
                    .get::<python_tools::PythonTools>()
                    .ok_or_else(|| py_error("compact requires a replaceable tool session"))?;
                let receipt = if let Err(error) = tools.check_idle(&inner.thread).await {
                    serde_json::json!({
                        "status": "not_submitted",
                        "reason": {"type": "not_idle", "message": error.to_string()}
                    })
                } else {
                    let turn_id = inner
                        .thread
                        .submit(codex_protocol::protocol::Op::Compact)
                        .await
                        .map_err(py_error)?;
                    serde_json::json!({"status": "started", "turn_id": turn_id})
                };
                serde_json::to_string(&receipt).map_err(py_error)
            }
            .with_context(context),
        )
    }

    fn _validate_hosted_search_model<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let current = inner.thread.config_snapshot().await;
            inner
                .engine
                .validate_hosted_search_model(current.collaboration_mode.model())
                .await
                .map_err(|error| PyValueError::new_err(error.to_string()))?;
            Ok(())
        })
    }

    fn _update_model_settings<'py>(
        &self,
        py: Python<'py>,
        settings_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let params = serde_json::from_str(settings_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let settings = ops::model_settings(params)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            let tools = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .ok_or_else(|| PyValueError::new_err("model updates require a managed Session"))?;
            tools
                .check_idle(&inner.thread)
                .await
                .map_err(|error| PyValueError::new_err(error.to_string()))?;
            if let Some(model) = &settings.model {
                inner
                    .engine
                    .validate_hosted_search_model(model)
                    .await
                    .map_err(|error| PyValueError::new_err(error.to_string()))?;
            }
            inner
                .thread
                .update_thread_settings(settings)
                .await
                .map_err(|error| {
                    if matches!(
                        error.details(),
                        codex_protocol::error::CodexErrorDetails::InvalidRequest(_)
                    ) {
                        PyValueError::new_err(error.to_string())
                    } else {
                        // A lost acknowledgement does not prove that Core rejected the update.
                        py_error(error)
                    }
                })?;
            Ok(true)
        })
    }

    #[pyo3(signature = (instructions, instruction_files_json=None))]
    fn _update_instructions<'py>(
        &self,
        py: Python<'py>,
        instructions: String,
        instruction_files_json: Option<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let files = instruction_files_json
            .as_deref()
            .map(serde_json::from_str)
            .transpose()
            .map_err(|error| PyValueError::new_err(error.to_string()))?
            .unwrap_or_default();
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            let tools = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .ok_or_else(|| {
                    PyValueError::new_err("instruction updates require a managed Session")
                })?;
            tools
                .check_idle(&inner.thread)
                .await
                .map_err(|error| PyValueError::new_err(error.to_string()))?;
            // Once publication begins, any error can conceal changed native history.
            application_instructions::publish_with_files(&inner.thread, instructions, files)
                .await
                .map_err(py_error)?;
            Ok(true)
        })
    }

    #[pyo3(signature = (generation, specs_json, manual_slots=Vec::new()))]
    fn _replace_tools<'py>(
        &self,
        py: Python<'py>,
        generation: u64,
        specs_json: &str,
        manual_slots: Vec<usize>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let specs = serde_json::from_str(specs_json).map_err(py_error)?;
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            let tools = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .ok_or_else(|| py_error("replace_tools requires a replaceable tool session"))?;
            tools
                .replace(&inner.thread, generation, specs, &manual_slots)
                .await
                .map_err(py_error)
        })
    }

    #[pyo3(signature = (enabled, defaults_json=None, children=false))]
    fn _select_steps(
        &self,
        enabled: bool,
        defaults_json: Option<&str>,
        children: bool,
    ) -> PyResult<()> {
        let inner = self.current()?;
        let tools = inner
            .thread
            .thread_extension_data()
            .get::<python_tools::PythonTools>()
            .ok_or_else(|| py_error("Step selection requires Python tools"))?;
        let defaults = defaults_json
            .map(serde_json::from_str)
            .transpose()
            .map_err(py_error)?;
        tools.select_steps(enabled, defaults, children);
        Ok(())
    }

    #[pyo3(signature = (id, thread=None, instance=None))]
    fn _check_step(&self, id: u64, thread: Option<&str>, instance: Option<&str>) -> PyResult<()> {
        let inner = self.current()?;
        let tools = inner
            .thread
            .thread_extension_data()
            .get::<python_tools::PythonTools>()
            .ok_or_else(|| py_error("Step selection requires Python tools"))?;
        tools.check_step(id, thread, instance).map_err(py_error)
    }

    #[pyo3(signature = (id, names_json, thread=None, instance=None))]
    fn _reply_step(
        &self,
        id: u64,
        names_json: Option<&str>,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> PyResult<()> {
        let inner = self.current()?;
        let tools = inner
            .thread
            .thread_extension_data()
            .get::<python_tools::PythonTools>()
            .ok_or_else(|| py_error("Step selection requires Python tools"))?;
        let names = names_json
            .map(serde_json::from_str)
            .transpose()
            .map_err(py_error)?;
        tools
            .reply_owned_step(id, names, thread, instance)
            .map_err(py_error)
    }

    // Private SDK ingress. Do not expose via NativeThread.call/ops::OPERATIONS.
    #[pyo3(signature = (servers_json, policies_json=None))]
    fn _replace_mcp_servers<'py>(
        &self,
        py: Python<'py>,
        servers_json: &str,
        policies_json: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let servers = serde_json::from_str(servers_json)
            .map_err(|_| PyValueError::new_err("invalid MCP servers JSON"))?;
        let servers =
            mcp_roster::decode(servers).map_err(|err| PyValueError::new_err(err.to_string()))?;
        let selection: Option<mcp_result_control::Selection> = policies_json
            .map(serde_json::from_str)
            .transpose()
            .map_err(|_| PyValueError::new_err("invalid MCP result policy selection"))?;
        if let Some(selection) = &selection {
            selection
                .validate(&servers)
                .map_err(|err| PyValueError::new_err(err.to_string()))?;
        }
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            let control = inner
                .thread
                .thread_extension_data()
                .get::<mcp_result_control::McpResultControl>()
                .ok_or_else(|| py_error("native MCP result control was not initialized"))?;
            if let Some(selection) = &selection {
                control
                    .check_selection(selection.generation)
                    .map_err(|err| PyValueError::new_err(err.to_string()))?;
            }
            // All errors from replace occur before refresh_mcp_config publishes.
            let mut receipt = mcp_roster::replace(&inner.thread, servers)
                .await
                .map_err(|err| PyValueError::new_err(err.to_string()))?;
            if receipt["result"]["publication"] == "published" {
                if let Some(selection) = selection {
                    receipt["result"]["policy_generation"] = selection.generation.into();
                    control.publish_selection(selection);
                }
            }
            serde_json::to_string(&receipt).map_err(py_error)
        })
    }

    fn _prepare_manual_restore(&self) -> PyResult<()> {
        let inner = self.current()?;
        manual_checkpoint::prepare(&inner, &self.manual).map_err(py_error)
    }

    #[pyo3(signature = (observation_submission_id=None, tracing_context=None))]
    fn _continue_manual<'py>(
        &self,
        py: Python<'py>,
        observation_submission_id: Option<String>,
        tracing_context: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let context = tracing_context
            .map(codex_otel::sdk_trace::context)
            .transpose()
            .map_err(py_error)?
            .unwrap_or_default();
        let inner = self.current()?;
        let manual = self.manual.clone();
        async_bridge::future_into_py(
            py,
            async move {
                let receipt = manual_checkpoint::continue_if_ready(
                    &inner,
                    &manual,
                    observation_submission_id,
                )
                .await
                .map_err(py_error)?;
                serde_json::to_string(&receipt).map_err(py_error)
            }
            .with_context(context),
        )
    }

    // The Python owner has committed to retiring an unrecovered manual import.
    // Keep that decision effective if its final reply is already resuming Core.
    fn _prepare_manual_retirement<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let manual = self.manual.clone();
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            {
                let _mutation = inner.mutations.lock().await;
                if let Some(frame) = manual
                    .lock()
                    .map_err(|_| py_error("Manual continuation poisoned"))?
                    .as_mut()
                {
                    // As with resealing a manual checkpoint, this expires grants
                    // and prevents any queued continuation from starting a turn.
                    frame.sealed = true;
                    frame.receivers.clear();
                }
            }
            let tools = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .ok_or_else(|| py_error("Manual retirement requires a managed Session"))?;
            // Recovery may already have started child work. Retiring this source
            // stops its entire graph and cells before the executor ACK and close.
            tools
                .interrupt_graph(&inner.engine.manager, &inner.mutations, true)
                .await
                .map_err(py_error)
        })
    }

    fn _manual_ready<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let manual = self.manual.clone();
        async_bridge::future_into_py(py, async move {
            manual_checkpoint::ready(&inner, &manual)
                .await
                .map_err(py_error)
        })
    }

    fn _manual_progress(&self) -> PyResult<u64> {
        let inner = self.current()?;
        let tools = inner
            .thread
            .thread_extension_data()
            .get::<python_tools::PythonTools>()
            .ok_or_else(|| py_error("Manual tools require a replaceable session"))?;
        Ok(tools.manual_progress())
    }

    fn _wait_manual_progress<'py>(
        &self,
        py: Python<'py>,
        version: u64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let tools = inner
            .thread
            .thread_extension_data()
            .get::<python_tools::PythonTools>()
            .ok_or_else(|| py_error("Manual tools require a replaceable session"))?;
        async_bridge::future_into_py(py, async move {
            tools.wait_manual_progress(version).await.map_err(py_error)
        })
    }

    #[pyo3(signature = (output_schema_json=None))]
    fn _seal_manual<'py>(
        &self,
        py: Python<'py>,
        output_schema_json: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let output_schema: Option<serde_json::Value> = output_schema_json
            .map(serde_json::from_str)
            .transpose()
            .map_err(py_error)?;
        if output_schema
            .as_ref()
            .is_some_and(|schema| !schema.is_object())
        {
            return Err(PyValueError::new_err("Output schema must be an object"));
        }
        let inner = self.current()?;
        let manual = self.manual.clone();
        async_bridge::future_into_py(py, async move {
            let checkpoint = async_bridge::retain_writer_operation(async move {
                manual_checkpoint::capture(&inner, &manual, output_schema)
                    .await
                    .map_err(py_error)
            })
            .await?;
            serde_json::to_string(&checkpoint).map_err(py_error)
        })
    }

    fn _pending_manual_tools(&self) -> PyResult<Vec<(u64, python_tools::ManualToolGrant, String)>> {
        let inner = self.current()?;
        let _operation = inner.engine.operation(false).map_err(py_error)?;
        let tools = inner
            .thread
            .thread_extension_data()
            .get::<python_tools::PythonTools>()
            .ok_or_else(|| py_error("Manual tools require a replaceable session"))?;
        tools
            .family_manual_snapshot(&inner.id.to_string())
            .map_err(py_error)
    }

    fn _check_elicitation_pending(&self, server: &str, token: &str) -> PyResult<bool> {
        let inner = self.current()?;
        let _operation = inner.engine.operation(false).map_err(py_error)?;
        Ok(inner.thread.is_elicitation_pending(server, token))
    }

    fn _check_manual_tool(&self, grant: &python_tools::ManualToolGrant) -> PyResult<bool> {
        let inner = self.current()?;
        let _operation = inner.engine.operation(false).map_err(py_error)?;
        Ok(inner
            .thread
            .thread_extension_data()
            .get::<python_tools::PythonTools>()
            .and_then(|tools| tools.owner(&inner.id.to_string(), &grant.key))
            .is_some_and(|tools| tools.check_manual(&grant.key.reply.thread, grant)))
    }

    fn _respond_manual_tool(
        &self,
        grant: &python_tools::ManualToolGrant,
        response_json: &str,
    ) -> PyResult<bool> {
        let inner = self.current()?;
        let _operation = inner.engine.operation(false).map_err(py_error)?;
        let Some(tools) = inner
            .thread
            .thread_extension_data()
            .get::<python_tools::PythonTools>()
            .and_then(|tools| tools.owner(&inner.id.to_string(), &grant.key))
        else {
            return Ok(false);
        };
        // Reject foreign/expired grants before parsing a supplied result.
        if !tools.check_manual(&grant.key.reply.thread, grant) {
            return Ok(false);
        }
        let response = serde_json::from_str(response_json).map_err(py_error)?;
        Ok(tools.respond_manual(&grant.key.reply.thread, grant, response))
    }

    fn _respond_python_tool<'py>(
        &self,
        py: Python<'py>,
        response_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let response: python_tools::Reply =
            serde_json::from_str(response_json).map_err(py_error)?;
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            // Suspension cannot seal while an acknowledged real receipt is
            // still in flight between removal from pending and writer flush.
            let _mutation = Arc::clone(&inner.mutations).lock_owned().await;
            let Some(tools) = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
            else {
                return Ok(false);
            };
            match response {
                python_tools::Reply::Park { request, park } => {
                    if !park {
                        return Err(py_error("park must be true"));
                    }
                    tools
                        .park(&inner.id.to_string(), &request)
                        .map_err(py_error)
                }
                python_tools::Reply::Continuation {
                    request,
                    continuation,
                } => tools
                    .park_continuation(&inner.id.to_string(), &request, continuation)
                    .map_err(py_error),
                python_tools::Reply::Output { request, response } => {
                    if request.reply.thread != inner.id.to_string() {
                        let Some(child_tools) = tools.owner(&inner.id.to_string(), &request) else {
                            return Ok(false);
                        };
                        let Some(sender) = child_tools
                            .take_reply(&request.reply.thread, &request)
                            .map_err(py_error)?
                        else {
                            return Ok(false);
                        };
                        // Native Codex owns child history. These calls never
                        // enter or park an SDK manual continuation.
                        return Ok(sender.send(response).is_ok());
                    }
                    let reentry = tools.is_reentry(&request);
                    let Some(sender) = tools
                        .take_reply(&inner.id.to_string(), &request)
                        .map_err(py_error)?
                    else {
                        return Ok(false);
                    };
                    if tools.sdk_owned() && inner.engine.local_checklist_store.is_some() {
                        let item = python_tools::PythonOutput(response.clone()).receipt(&request);
                        return async_bridge::retain_writer_operation(async move {
                            // Both leases move with the real result. Python
                            // cancellation cannot open the suspension race.
                            let _operation = _context_operation;
                            let _mutation = _mutation;
                            let store = inner
                                .engine
                                .local_checklist_store
                                .as_ref()
                                .expect("selected local SDK writer");
                            store
                                .commit_sdk_receipt(inner.id, &request, item, reentry)
                                .await
                                .map_err(py_error)?;
                            Ok(sender.send(response).is_ok())
                        })
                        .await;
                    }
                    Ok(sender.send(response).is_ok())
                }
            }
        })
    }

    // A parking signal cannot be represented as ToolOutput: Core must cancel
    // its futures, then the existing writer lease removes only their
    // attributable cancellation artifacts before any successor can reopen.
    fn _suspend_python_tools<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _operation = inner.engine.operation(false).map_err(py_error)?;
            let _mutation = Arc::clone(&inner.mutations).lock_owned().await;
            let tools = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .ok_or_else(|| py_error("approval suspension requires managed tools"))?;
            let keys = tools.parked(&inner.id.to_string()).map_err(py_error)?;
            let continuations = tools.continuations(&keys).map_err(py_error)?;
            if keys.iter().all(|key| tools.is_reentry(key)) {
                if let Some(store) = &inner.engine.local_checklist_store {
                    store
                        .seal_sdk_reentry(inner.id, &keys, &continuations)
                        .await
                        .map_err(py_error)?;
                }
                tools.suspend_reentry(&keys).map_err(py_error)?;
                return serde_json::to_string(&serde_json::json!({"turn_id": keys[0].turn}))
                    .map_err(py_error);
            }
            if keys.iter().any(|key| tools.is_reentry(key)) {
                return Err(py_error("cannot mix re-entry and live calls"));
            }
            async_bridge::retain_writer_operation(async move {
                // Cancel may arrive after Core removes the active turn but before
                // its final writer flush. Hold the same leases and acknowledgement
                // from before begin through finish (including Core refusal/error).
                // Ordinary shutdown must not clear another compaction or writer.
                let _operation = _operation;
                let _mutation = _mutation;
                let local = &inner.engine.local_checklist_store;
                if let Some(store) = local {
                    if !store
                        .begin_sdk_suspension(inner.id, keys, continuations)
                        .await
                        .map_err(py_error)?
                    {
                        return Err(py_error("native writer cannot seal the approval"));
                    }
                }
                let outcome = inner.thread.suspend_turn_and_shutdown().await;
                let turn = match &outcome {
                    Ok(codex_protocol::turn_input::SuspendTurnOutcome::Suspended { turn_id }) => {
                        Some(turn_id.as_str())
                    }
                    _ => None,
                };
                let preserved = if let Some(store) = local {
                    store
                        .finish_loss_suspension(inner.id, turn)
                        .await
                        .map_err(py_error)?
                } else {
                    turn.is_some()
                };
                if turn.is_some() {
                    inner.retire_suspended().await;
                }
                outcome.as_ref().map_err(py_error)?;
                if !preserved {
                    return Err(py_error(
                        "native approval suspension refused or attribution is ambiguous",
                    ));
                }
                serde_json::to_string(&serde_json::json!({"turn_id": turn})).map_err(py_error)
            })
            .await
        })
    }

    #[pyo3(signature = (calls_json, *, continuations_json="{}"))]
    fn _reenter_python_tools<'py>(
        &self,
        py: Python<'py>,
        calls_json: &str,
        continuations_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let calls: Vec<codex_protocol::models::ResponseItem> =
            serde_json::from_str(calls_json).map_err(py_error)?;
        let continuations: std::collections::HashMap<String, serde_json::Value> =
            serde_json::from_str(continuations_json).map_err(py_error)?;
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _operation = inner.engine.operation(false).map_err(py_error)?;
            // Retain admission and receipt writes, not the lifetime of Python
            // callbacks. Dropping the bridge signals the owned operation to
            // fence its callbacks before finalizing any known parking.
            let (cancellation, mut cancelled) = tokio::sync::oneshot::channel::<()>();
            let result = async_bridge::retain_writer_operation(async move {
                let _operation = _operation;
                let tools = inner
                    .thread
                    .thread_extension_data()
                    .get::<python_tools::PythonTools>()
                    .ok_or_else(|| py_error("approval re-entry requires managed tools"))?;
                let requests = {
                    let _mutation = inner.mutations.lock().await;
                    tools.check_idle(&inner.thread).await.map_err(py_error)?;
                    tools
                        .validate_reentry(&calls, &continuations)
                        .map_err(py_error)?;
                    let original_turn = if let Some(store) = &inner.engine.local_checklist_store {
                        store
                            .admit_sdk_reentry(inner.id, &calls, &continuations)
                            .await
                            .map_err(py_error)?
                    } else {
                        None
                    };
                    let turn = original_turn
                        .clone()
                        .unwrap_or_else(|| format!("sdk-import-{}", inner.id));
                    if original_turn.is_none() {
                        // This is caller-requested fresh admission (including released
                        // 1.22 state), not proof of the old process's effect state.
                        inner
                            .thread
                            .inject_response_items(calls.clone())
                            .await
                            .map_err(py_error)?;
                    }
                    let mut requests = Vec::with_capacity(calls.len());
                    for call in &calls {
                        match tools.enqueue_reentry(&inner.id.to_string(), &turn, call) {
                            Ok(request) => requests.push(request),
                            Err(error) => {
                                let keys = requests
                                    .iter()
                                    .map(|(key, _)| key.clone())
                                    .collect::<Vec<_>>();
                                tools.cancel_reentry(&keys);
                                return Err(py_error(error));
                            }
                        }
                    }
                    requests
                };
                // Enqueued together through the very same executor channel. There
                // is no Python batch replay and Core owns the next ordinary turn.
                let keys = requests
                    .iter()
                    .map(|(key, _)| key.clone())
                    .collect::<Vec<_>>();
                let _pending = python_tools::ReentryGuard {
                    tools: &tools,
                    keys,
                };
                let mut outputs = Vec::new();
                for (key, receiver) in requests {
                    let response = tokio::select! {
                        response = receiver => response,
                        _ = &mut cancelled => {
                            // The real receipt owner also holds this lock. A
                            // removed sender is therefore either fully committed
                            // or its store has recorded a sticky write failure.
                            let _mutation = inner.mutations.lock().await;
                            let parked = tools.park_cancelled_reentry(
                                &inner.id.to_string(), &_pending.keys,
                            );
                            if !parked.is_empty()
                                && let Some(store) = &inner.engine.local_checklist_store
                            {
                                let continuations = tools.continuations(&parked).map_err(py_error)?;
                                store.seal_sdk_reentry(inner.id, &parked, &continuations)
                                    .await.map_err(py_error)?;
                            }
                            // Local receipts are already flushed. The departing
                            // non-local owner is not reusable by another Runner.
                            return serde_json::to_string(&serde_json::json!({
                                "outputs": outputs, "parked": false
                            })).map_err(py_error);
                        }
                    };
                    match response {
                        Ok(response) => {
                            outputs.push(python_tools::PythonOutput(response).receipt(&key))
                        }
                        Err(_) if tools.suspended() => {}
                        Err(_) => {
                            return Err(py_error("SDK re-entry closed before an actual receipt"));
                        }
                    }
                }
                // Populate Core's live history in both storage modes. The local
                // writer consumes its early receipt fence instead of writing twice.
                if !outputs.is_empty() {
                    let _mutation = inner.mutations.lock().await;
                    inner
                        .thread
                        .inject_response_items(outputs.clone())
                        .await
                        .map_err(py_error)?;
                }
                serde_json::to_string(&serde_json::json!({
                    "outputs": outputs, "parked": tools.suspended()
                }))
                .map_err(py_error)
            })
            .await;
            drop(cancellation);
            result
        })
    }

    #[pyo3(signature = (subagent_id=None))]
    fn read_thread_history<'py>(
        &self,
        py: Python<'py>,
        subagent_id: Option<&str>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let id = subagent_id
            .map(codex_core_api::ThreadId::from_string)
            .transpose()
            .map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let history = subagents::read_history(&inner, id)
                .await
                .map_err(py_error)?;
            serde_json::to_string(&history).map_err(py_error)
        })
    }

    fn _child_turn<'py>(&self, py: Python<'py>, child_id: &str) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let id = codex_core_api::ThreadId::from_string(child_id).map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            Ok(native_agents::current(&inner, id)
                .await
                .map_err(py_error)?
                .map(|turn| NativeChildTurn { turn }))
        })
    }

    fn _child_instance<'py>(&self, py: Python<'py>, child_id: &str) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let id = codex_core_api::ThreadId::from_string(child_id).map_err(py_error)?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            Ok(native_agents::instance(&inner, id)
                .await
                .map_err(py_error)?
                .map(|(instance, incarnation, turn)| {
                    (
                        instance,
                        incarnation,
                        turn.map(|turn| NativeChildTurn { turn }),
                    )
                }))
        })
    }

    #[pyo3(signature = (child_id, instance_id, params_json, expected))]
    fn _submit_child<'py>(
        &self,
        py: Python<'py>,
        child_id: &str,
        instance_id: u64,
        params_json: &str,
        expected: Option<&NativeChildTurn>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let id = codex_core_api::ThreadId::from_string(child_id).map_err(py_error)?;
        let params = serde_json::from_str(params_json).map_err(py_error)?;
        let expected = expected.map(|handle| handle.turn.clone());
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let turn = native_agents::submit(&inner, id, instance_id, params, expected.as_ref())
                .await
                .map_err(py_error)?;
            Ok(NativeChildTurn { turn })
        })
    }

    fn _cancel_child_turn<'py>(
        &self,
        py: Python<'py>,
        handle: &NativeChildTurn,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let turn = handle.turn.clone();
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            native_agents::cancel(&inner, &turn).await.map_err(py_error)
        })
    }

    fn _continue_child_turn<'py>(
        &self,
        py: Python<'py>,
        handle: &NativeChildTurn,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        let turn = handle.turn.clone();
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            Ok(native_agents::continue_retained(&inner, &turn)
                .await
                .map_err(py_error)?
                .map(|turn| NativeChildTurn { turn }))
        })
    }

    fn next_event<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let value = inner.next_event().await.map_err(py_error)?;
            serde_json::to_string(&value).map_err(py_error)
        })
    }

    fn _next_session_event<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::session_event_into_py(py, async move {
            let value = inner.next_session_event().await.map_err(py_error)?;
            serde_json::to_string(&value).map_err(py_error)
        })
    }

    fn _finish_child_event_reader(&self) -> PyResult<()> {
        if let Some(inner) = self.inner.lock().map_err(py_error)?.live.as_ref() {
            inner.finish_child_event_reader().map_err(py_error)?;
        }
        Ok(())
    }

    fn _child_question_pending<'py>(
        &self,
        py: Python<'py>,
        question_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            Ok(inner.child_question_pending(&question_id).await)
        })
    }
    fn _respond_child_question<'py>(
        &self,
        py: Python<'py>,
        question_id: String,
        operation: String,
        params_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let params = serde_json::from_str(params_json).map_err(py_error)?;
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            inner
                .respond_child_question(&question_id, &operation, params)
                .await
                .map_err(py_error)
        })
    }

    fn _child_url_completion_status<'py>(
        &self,
        py: Python<'py>,
        question_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            Ok(inner.child_url_completion_status(&question_id).await)
        })
    }

    fn _child_url_pending<'py>(
        &self,
        py: Python<'py>,
        question_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            Ok(inner.child_url_pending(&question_id).await)
        })
    }

    fn _cancel_child_url_completion<'py>(
        &self,
        py: Python<'py>,
        question_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            Ok(inner.cancel_child_url_completion(&question_id).await)
        })
    }

    #[pyo3(signature = (subagent_id, subagent_instance_id, operation, params_json="{}"))]
    fn respond_to_subagent<'py>(
        &self,
        py: Python<'py>,
        subagent_id: &str,
        subagent_instance_id: u64,
        operation: String,
        params_json: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let id = codex_core_api::ThreadId::from_string(subagent_id).map_err(py_error)?;
        let params = serde_json::from_str(params_json).map_err(py_error)?;
        let inner = self.current()?;
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let result = inner
                .respond_to_subagent(id, subagent_instance_id, &operation, params)
                .await
                .map_err(py_error)?;
            serde_json::to_string(&result).map_err(py_error)
        })
    }

    // Session has stopped admission and joined its current turn before calling this.
    // Core acknowledges termination attempts, not physical exit or drained I/O.
    fn _terminate_executor_processes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let Some(inner) = self.live()? else {
            // Joined shutdown drained Core's terminal store and scheduled its
            // best-effort termination. No retained terminal remains to retry;
            // this does not acknowledge remote termination or physical exit.
            return async_bridge::future_into_py(py, async { Ok(()) });
        };
        let manual = self.manual.clone();
        async_bridge::future_into_py(py, async move {
            let _context_operation = inner.engine.operation(false).map_err(py_error)?;
            let _mutation = inner.mutations.lock().await;
            if environment::selected_executor(&inner.engine.manager.environment_manager()).is_none()
            {
                return Err(py_error("Session has no executor"));
            }
            let tools = inner
                .thread
                .thread_extension_data()
                .get::<python_tools::PythonTools>()
                .ok_or_else(|| py_error("executor cleanup requires a managed Session"))?;
            // A suspended root is shut down. A restored manual frame has
            // pending grants without a live turn. Both still need cleanup.
            let status = inner.thread.agent_status().await;
            if !matches!(status, codex_protocol::protocol::AgentStatus::Shutdown) {
                let restored_manual = manual
                    .lock()
                    .map_err(|_| py_error("Manual continuation poisoned"))?
                    .is_some();
                if restored_manual {
                    if matches!(
                        status,
                        codex_protocol::protocol::AgentStatus::Running
                            | codex_protocol::protocol::AgentStatus::NotFound
                    ) || inner
                        .thread
                        .active_turn_environment_selections()
                        .await
                        .is_some()
                    {
                        return Err(py_error("Executor cleanup requires an idle native root"));
                    }
                } else {
                    tools.check_idle(&inner.thread).await.map_err(py_error)?;
                }
            }
            if let Some(control) = inner
                .thread
                .thread_extension_data()
                .get::<native_interception::Control>()
            {
                control.revoke();
            }
            let acknowledged = tokio::time::timeout(engine::CLOSE_TIMEOUT, async {
                let mut acknowledged = true;
                for id in inner
                    .engine
                    .manager
                    .list_agent_subtree_thread_ids(inner.id)
                    .await
                    .map_err(py_error)?
                {
                    let thread = match inner.engine.manager.get_thread(id).await {
                        Ok(thread) => thread,
                        Err(error)
                            if matches!(
                                error.details(),
                                codex_protocol::error::CodexErrorDetails::ThreadNotFound(_)
                            ) =>
                        {
                            continue;
                        }
                        Err(error) => return Err(py_error(error)),
                    };
                    for process in thread.list_background_terminals().await {
                        let process_id = process.process_id.parse::<i32>().map_err(py_error)?;
                        acknowledged &= thread.terminate_background_terminal(process_id).await;
                    }
                }
                Ok::<_, PyErr>(acknowledged)
            })
            .await
            .map_err(|_| py_error("Remote process termination attempts timed out"))??;
            if !acknowledged {
                return Err(py_error(
                    "A remote process termination attempt was not acknowledged",
                ));
            }
            Ok(())
        })
    }

    #[pyo3(signature = (*, discard_checkpoint_import=false))]
    fn close<'py>(
        &self,
        py: Python<'py>,
        discard_checkpoint_import: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let imported = if discard_checkpoint_import {
            Some(self.checkpoint_import.clone().ok_or_else(|| {
                py_error("thread does not own an imported checkpoint or managed fork")
            })?)
        } else {
            None
        };
        let owner = Arc::clone(&self.inner);
        async_bridge::future_into_py(py, close_thread_owner(owner, imported))
    }
}

// Deliberately private: Session owns the transaction and its durable decision.
#[pyclass(module = "agents._native")]
struct ContextChange {
    inner: Arc<std::sync::Mutex<Option<ContextChangeOwner>>>,
}

struct ContextChangeOwner {
    change: RuntimeOwned<context::Change>,
    prepared: Option<serde_json::Value>,
}

impl ContextChange {
    fn current(&self) -> PyResult<Arc<context::Change>> {
        self.inner
            .lock()
            .map_err(py_error)?
            .as_ref()
            .map(|inner| Arc::clone(&*inner.change))
            .ok_or_else(|| py_error("context lease is closed"))
    }
}

#[pymethods]
impl ContextChange {
    fn context(&self) -> PyResult<String> {
        serde_json::to_string(&self.current()?.context()).map_err(py_error)
    }

    fn options(&self) -> PyResult<String> {
        serde_json::to_string(&self.current()?.options()).map_err(py_error)
    }

    fn validate_candidate<'py>(
        &self,
        py: Python<'py>,
        candidate: &NativeThread,
    ) -> PyResult<Bound<'py, PyAny>> {
        let change = self.current()?;
        let candidate = candidate.current()?;
        async_bridge::future_into_py(py, async move {
            change
                .validate_candidate(&candidate)
                .await
                .map_err(py_error)
        })
    }

    fn prepare(&self, summary: &str, retained_indices: Vec<usize>) -> PyResult<String> {
        let mut slot = self.inner.lock().map_err(py_error)?;
        let change = slot
            .as_mut()
            .ok_or_else(|| py_error("context lease is closed"))?;
        let candidate = change
            .change
            .prepare(summary, &retained_indices)
            .map_err(py_error)?;
        let result = serde_json::to_string(&candidate).map_err(py_error)?;
        change.prepared = Some(candidate);
        Ok(result)
    }

    fn fence(&self) -> PyResult<()> {
        self.current()?
            .state
            .engine
            .context_retired
            .store(true, std::sync::atomic::Ordering::Release);
        Ok(())
    }

    fn retire<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let change = self.current()?;
        async_bridge::future_into_py(py, async move {
            // Admission retirement is permanent even when teardown fails.
            // Keep close separately retryable rather than setting its done flag.
            change
                .state
                .engine
                .context_retired
                .store(true, std::sync::atomic::Ordering::Release);
            change.state.close().await.map_err(py_error)?;
            change.state.engine.close().await.map_err(py_error)?;
            change.state.engine.release_native_threads();
            Ok(())
        })
    }

    fn release(&self) -> PyResult<()> {
        self.inner.lock().map_err(py_error)?.take();
        Ok(())
    }
}

#[pyfunction]
#[pyo3(signature = (config_json="{}", runtime_json="{}", api_key=None, *, provider_lease=None))]
fn create_engine<'py>(
    py: Python<'py>,
    config_json: &str,
    runtime_json: &str,
    api_key: Option<String>,
    provider_lease: Option<PyRef<'py, provider_owner::NativeProviderLease>>,
) -> PyResult<Bound<'py, PyAny>> {
    let lease = provider_lease.map(|lease| lease.inner.clone());
    let config_json = config_json.to_owned();
    let runtime_json = runtime_json.to_owned();
    async_bridge::future_into_py(py, async move {
        if let Some(lease) = &lease {
            lease.check_live()?;
        }
        let inner =
            engine::EngineState::new_with_model(config_json, runtime_json, false, api_key, None)
                .await
                .map_err(py_error)?;
        let mut engine = Engine::new(inner);
        if let Some(lease) = lease {
            lease.register_engine(engine.inner.clone())?;
            engine.managed_lease = Some(Arc::downgrade(&lease));
        }
        Ok(engine)
    })
}

#[pyfunction]
#[pyo3(signature = (config_json="{}", runtime_json="{}", api_key=None, *, provider_lease=None, initial_checkpoint=None, initial_specs_json=None, initial_options_json="{}"))]
fn create_checkpoint_engine<'py>(
    py: Python<'py>,
    config_json: &str,
    runtime_json: &str,
    api_key: Option<String>,
    provider_lease: Option<PyRef<'py, provider_owner::NativeProviderLease>>,
    initial_checkpoint: Option<&str>,
    initial_specs_json: Option<&str>,
    initial_options_json: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let lease = provider_lease.map(|lease| lease.inner.clone());
    let config_json = config_json.to_owned();
    let runtime_json = runtime_json.to_owned();
    let initial_specs: Option<Vec<codex_protocol::dynamic_tools::DynamicToolSpec>> =
        initial_specs_json
            .map(serde_json::from_str)
            .transpose()
            .map_err(py_error)?;
    let initial_model = initial_checkpoint
        .map(|encoded| {
            let raw = serde_json::from_str(encoded).map_err(py_error)?;
            let payload = checkpoint_json_value(raw, 0).map_err(py_error)?;
            if payload["format_version"] == 3 || payload["format_version"] == 5 {
                // Check the same current manual authority and continuation frame
                // as restore_checkpoint before allocating environment services.
                let options: options::ThreadOptions =
                    config::decode(serde_json::from_str(initial_options_json).map_err(py_error)?)
                        .map_err(py_error)?;
                manual_checkpoint::validate(
                    &payload,
                    options.application_instructions.as_deref().unwrap_or(""),
                    initial_specs.as_deref().unwrap_or(&[]),
                    &options.manual_tool_slots,
                )
                .map_err(py_error)?;
            }
            let checkpoint: checkpoint::Checkpoint =
                serde_json::from_value(payload).map_err(py_error)?;
            checkpoint
                .initial_managed_model(initial_specs.as_deref())
                .map_err(py_error)
        })
        .transpose()?
        .flatten();
    async_bridge::future_into_py(py, async move {
        if let Some(lease) = &lease {
            lease.check_live()?;
        }
        let inner = engine::EngineState::new_with_model(
            config_json,
            runtime_json,
            true,
            api_key,
            initial_model,
        )
        .await
        .map_err(py_error)?;
        let mut engine = Engine::new(inner);
        if let Some(lease) = lease {
            lease.register_engine(engine.inner.clone())?;
            engine.managed_lease = Some(Arc::downgrade(&lease));
        }
        Ok(engine)
    })
}

#[pyfunction]
fn config_schema() -> &'static str {
    include_str!("../.upstream/codex/codex-rs/core/config.schema.json")
}

#[pyfunction]
fn _validate_python_tool_declarations(specs_json: &str) -> PyResult<()> {
    let specs: Vec<codex_protocol::dynamic_tools::DynamicToolSpec> =
        serde_json::from_str(specs_json)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
    python_tools::validate_declarations(&specs)
        .map_err(|error| PyValueError::new_err(error.to_string()))
}

#[pyfunction]
fn _python_tool_parameters(specs_json: &str) -> PyResult<String> {
    let specs = serde_json::from_str(specs_json).map_err(py_error)?;
    let parameters = python_tools::model_parameters(specs).map_err(py_error)?;
    serde_json::to_string(&parameters).map_err(py_error)
}

#[pyfunction]
fn _read_openai_api_key_from_env() -> Option<String> {
    codex_login::read_openai_api_key_from_env()
}

#[pyfunction]
fn _read_session_instructions(history_json: &str) -> PyResult<String> {
    let raw = serde_json::from_str(history_json).map_err(py_error)?;
    let value = checkpoint_json_value(raw, 0).map_err(py_error)?;
    let history =
        serde_json::from_value::<Vec<codex_history::RolloutItem>>(value).map_err(py_error)?;
    application_instructions::latest(&history)
        .map(|accepted| accepted.text)
        .map_err(|error| PyValueError::new_err(error.to_string()))
}

#[pyfunction]
fn _check_session_instructions(history_json: &str, expected: &str) -> PyResult<()> {
    let raw = serde_json::from_str(history_json).map_err(py_error)?;
    let value = checkpoint_json_value(raw, 0).map_err(py_error)?;
    let history =
        serde_json::from_value::<Vec<codex_history::RolloutItem>>(value).map_err(py_error)?;
    application_instructions::validate(&history, expected)
        .map(|_| ())
        .map_err(|error| PyValueError::new_err(error.to_string()))
}

#[pyfunction]
fn _validate_known_root_function_output(output_json: &str) -> PyResult<()> {
    serde_json::from_str::<codex_protocol::models::FunctionCallOutputPayload>(output_json)
        .map(|_| ())
        .map_err(|error| PyValueError::new_err(error.to_string()))
}

#[pyfunction]
#[pyo3(signature = (config_json="{}", runtime_json="{}"))]
fn configure_telemetry<'py>(
    py: Python<'py>,
    config_json: &str,
    runtime_json: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let config_json = config_json.to_owned();
    let runtime_json = runtime_json.to_owned();
    async_bridge::future_into_py(py, async move {
        telemetry::initialize(&config_json, &runtime_json)
            .await
            .map_err(py_error)
    })
}

#[pyfunction]
#[pyo3(signature = (timeout_ms=None))]
fn shutdown_telemetry<'py>(
    py: Python<'py>,
    timeout_ms: Option<u32>,
) -> PyResult<Bound<'py, PyAny>> {
    async_bridge::future_into_py(py, async move {
        async_bridge::retain_writer_operation(async move {
            telemetry::shutdown(timeout_ms).await.map_err(py_error)
        })
        .await
    })
}

#[pyfunction]
fn start_trace_span(options_json: &str) -> PyResult<String> {
    telemetry::scope_ready().map_err(py_error)?;
    codex_otel::sdk_trace::start_trace_span(options_json).map_err(py_error)
}

#[pyfunction]
fn end_trace_span(handle: &str, outcome_json: &str) -> PyResult<()> {
    codex_otel::sdk_trace::end_trace_span(handle, outcome_json).map_err(py_error)
}

#[pyfunction]
fn retain_trace_context(handle: &str) -> PyResult<String> {
    codex_otel::sdk_trace::retain_trace_context(handle).map_err(py_error)
}

#[pyfunction]
fn trace_span_context(handle: &str) -> PyResult<String> {
    codex_otel::sdk_trace::trace_span_context(handle).map_err(py_error)
}

#[pyfunction]
fn set_trace_routes(options_json: &str) -> PyResult<u64> {
    telemetry::tracing_ready().map_err(py_error)?;
    codex_otel::sdk_trace_routes::set_trace_routes(options_json).map_err(py_error)
}

#[pyfunction]
fn register_trace_credentials(headers_json: &str) -> PyResult<String> {
    telemetry::tracing_ready().map_err(py_error)?;
    codex_otel::sdk_trace_routes::register_trace_credentials(headers_json).map_err(py_error)
}

#[pyfunction]
fn release_trace_credentials(credential_ref: &str) -> PyResult<()> {
    codex_otel::sdk_trace_routes::release_trace_credentials(credential_ref).map_err(py_error)
}

#[pyfunction]
fn ack_trace_events(through_sequence: u64, failed_deliveries: u64) -> PyResult<()> {
    codex_otel::sdk_trace_routes::ack_trace_events(through_sequence, failed_deliveries)
        .map_err(py_error)
}

#[pyfunction]
fn release_trace_context(handle: &str) {
    codex_otel::sdk_trace::release_trace_context(handle);
}

#[pyfunction]
fn release_trace_span(handle: &str) {
    codex_otel::sdk_trace::release_trace_span(handle);
}

#[pyfunction]
#[pyo3(signature = (timeout_ms=None))]
fn force_flush_telemetry<'py>(
    py: Python<'py>,
    timeout_ms: Option<u32>,
) -> PyResult<Bound<'py, PyAny>> {
    async_bridge::future_into_py(py, async move {
        async_bridge::retain_writer_operation(async move {
            telemetry::force_flush(timeout_ms).await.map_err(py_error)
        })
        .await
    })
}
#[pyfunction]
fn drain_trace_events<'py>(py: Python<'py>, max_count: u32) -> PyResult<Bound<'py, PyAny>> {
    async_bridge::future_into_py(py, async move {
        codex_otel::sdk_trace_routes::drain_trace_events(max_count as usize).map_err(py_error)
    })
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let mut runtime = tokio::runtime::Builder::new_multi_thread();
    runtime
        .enable_all()
        .thread_stack_size(codex_async_utils::THREAD_STACK_SIZE_BYTES);
    pyo3_async_runtimes::tokio::init(runtime);
    module.add_class::<Engine>()?;
    module.add_class::<python_tools::ManualToolGrant>()?;
    module.add_class::<NativeChildTurn>()?;
    module.add_class::<NativeThread>()?;
    module.add_class::<live::NativeLiveSession>()?;
    module.add_class::<provider_owner::NativeProviderOwner>()?;
    module.add_class::<provider_owner::NativeProviderLease>()?;
    module.add_class::<provider_owner::NativeProviderHome>()?;
    module.add_function(wrap_pyfunction!(create_engine, module)?)?;
    module.add_function(wrap_pyfunction!(create_checkpoint_engine, module)?)?;
    module.add_function(wrap_pyfunction!(config_schema, module)?)?;
    module.add_function(wrap_pyfunction!(live::_connect_live, module)?)?;
    module.add_function(wrap_pyfunction!(_python_tool_parameters, module)?)?;
    module.add_function(wrap_pyfunction!(
        _validate_python_tool_declarations,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(_read_openai_api_key_from_env, module)?)?;
    module.add_function(wrap_pyfunction!(_read_session_instructions, module)?)?;
    module.add_function(wrap_pyfunction!(_check_session_instructions, module)?)?;
    module.add_function(wrap_pyfunction!(
        _validate_known_root_function_output,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(configure_telemetry, module)?)?;
    module.add_function(wrap_pyfunction!(shutdown_telemetry, module)?)?;
    module.add_function(wrap_pyfunction!(start_trace_span, module)?)?;
    module.add_function(wrap_pyfunction!(end_trace_span, module)?)?;
    module.add_function(wrap_pyfunction!(retain_trace_context, module)?)?;
    module.add_function(wrap_pyfunction!(trace_span_context, module)?)?;
    module.add_function(wrap_pyfunction!(set_trace_routes, module)?)?;
    module.add_function(wrap_pyfunction!(register_trace_credentials, module)?)?;
    module.add_function(wrap_pyfunction!(release_trace_credentials, module)?)?;
    module.add_function(wrap_pyfunction!(ack_trace_events, module)?)?;
    module.add_function(wrap_pyfunction!(release_trace_context, module)?)?;
    module.add_function(wrap_pyfunction!(release_trace_span, module)?)?;
    module.add_function(wrap_pyfunction!(force_flush_telemetry, module)?)?;
    module.add_function(wrap_pyfunction!(drain_trace_events, module)?)?;

    module.add("CODEX_REVISION", include_str!("../CODEX_REVISION").trim())?;
    let mut operations = ops::OPERATIONS.to_vec();
    operations.push("set_instructions");
    module.add("OPERATIONS", operations)?;
    Ok(())
}

mod request_metadata;
