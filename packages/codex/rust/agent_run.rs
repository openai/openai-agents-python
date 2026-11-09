//! Keep a fresh plain conversation native until it completes or needs Python.
use std::sync::{Arc, Mutex};

use futures_util::future::BoxFuture;
use pyo3::prelude::*;
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

use crate::{Engine, NativeThread, async_bridge, ops, provider_owner, py_error};

type Scope = (
    Engine,
    NativeThread,
    Option<provider_owner::NativeProviderHome>,
);
const MAX_EVENTS: usize = 256;
const MAX_BYTES: usize = 256 * 1024;

struct Inputs {
    opening: BoxFuture<'static, PyResult<Scope>>,
    lease: Arc<provider_owner::Lease>,
    keepalive: Py<provider_owner::NativeProviderLease>,
    input: String,
}

#[pyclass(frozen, module = "agents._native")]
pub(crate) struct NativeRun {
    inputs: Mutex<Option<Inputs>>,
    cancelled: CancellationToken,
}

impl NativeRun {
    pub(crate) fn new(
        opening: impl std::future::Future<Output = PyResult<Scope>> + Send + 'static,
        lease: PyRef<'_, provider_owner::NativeProviderLease>,
        input: String,
    ) -> Self {
        Self {
            inputs: Mutex::new(Some(Inputs {
                opening: Box::pin(opening),
                lease: lease.inner.clone(),
                keepalive: lease.into(),
                input,
            })),
            cancelled: CancellationToken::new(),
        }
    }
}

#[pymethods]
impl NativeRun {
    fn cancel(&self) {
        self.cancelled.cancel();
    }

    fn run<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inputs = self
            .inputs
            .lock()
            .map_err(py_error)?
            .take()
            .ok_or_else(|| py_error("Native run already started"))?;
        let cancelled = self.cancelled.clone();
        async_bridge::future_factory_into_py(py, move || execute(inputs, cancelled))
    }
}

impl Drop for NativeRun {
    fn drop(&mut self) {
        self.cancelled.cancel();
    }
}

#[pyclass(frozen, module = "agents._native")]
pub(crate) struct NativeRunArchive {
    events: Vec<u8>,
    count: usize,
}

impl Default for NativeRunArchive {
    fn default() -> Self {
        Self {
            events: b"[]".to_vec(),
            count: 0,
        }
    }
}

impl NativeRunArchive {
    fn push(&mut self, event: &Value) -> PyResult<usize> {
        let _ = self.events.pop();
        if self.count != 0 {
            self.events.push(b',');
        }
        let start = self.events.len();
        serde_json::to_writer(&mut self.events, event).map_err(py_error)?;
        let bytes = self.events.len() - start;
        self.events.push(b']');
        self.count += 1;
        Ok(bytes)
    }
}

#[pymethods]
impl NativeRunArchive {
    fn json(&self) -> PyResult<&str> {
        std::str::from_utf8(&self.events).map_err(py_error)
    }
}

struct RunOutput {
    scope: Option<Scope>,
    info: Value,
    archive: NativeRunArchive,
    failures: Vec<(&'static str, PyErr)>,
}

impl<'py> IntoPyObject<'py> for RunOutput {
    type Target = PyAny;
    type Output = Bound<'py, PyAny>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> PyResult<Self::Output> {
        let failures = self
            .failures
            .into_iter()
            .map(|(phase, error)| (phase, error.value(py).clone().unbind()))
            .collect::<Vec<_>>();
        Ok((
            self.scope,
            serde_json::to_string(&self.info).map_err(py_error)?,
            self.archive,
            failures,
        )
            .into_pyobject(py)?
            .into_any())
    }
}

fn plain_request(input: &Value) -> bool {
    input.as_object().is_some_and(|fields| {
        fields.keys().all(|key| {
            matches!(
                key.as_str(),
                "mode" | "input" | "observation_submission_id" | "trace"
            )
        })
    }) && input["mode"] == "start_if_idle"
        && input["observation_submission_id"]
            .as_str()
            .is_some_and(|id| !id.is_empty())
        && input["input"].as_array().is_some_and(|parts| {
            parts.len() == 1 && parts[0]["type"] == "text" && parts[0]["text"].is_string()
        })
}

fn passive(event: &Value) -> bool {
    let message = &event["msg"];
    if event.get("subagent_id").is_some()
        || [
            "_sdk_callback",
            "_sdk_python_tool",
            "_sdk_manual",
            "_sdk_policy_failure",
        ]
        .iter()
        .any(|key| message.get(*key).is_some())
        || event.get("_sdk_policy_failure").is_some()
        || (!event["sdk_unsampled_steering"].is_null() && event["sdk_unsampled_steering"] != false)
    {
        return false;
    }
    // Delegate unfamiliar output shapes and blank finals to ordinary validation.
    // This is an admission restriction, not a second output validator.
    if message["type"] == "raw_response_item" {
        let item = &message["item"];
        if !matches!(item["type"].as_str(), Some("message" | "reasoning")) {
            return false;
        }
        if item["type"] == "message"
            && item["role"] == "assistant"
            && !item["content"].as_array().is_some_and(|parts| {
                !parts.is_empty()
                    && parts.iter().all(|part| {
                        matches!(part["type"].as_str(), Some("input_text" | "output_text"))
                            && part["text"]
                                .as_str()
                                .is_some_and(|text| !text.trim().is_empty())
                    })
            })
        {
            return false;
        }
    }
    matches!(
        message["type"].as_str(),
        Some(
            "session_configured"
                | "mcp_startup_complete"
                | "thread_settings_applied"
                | "_sdk_request_metadata"
                | "warning"
                | "deprecation_notice"
                | "raw_response_created"
                | "raw_response_item"
                | "raw_response_completed"
                | "task_started"
                | "turn_started"
                | "user_message"
                | "item_started"
                | "item_completed"
                | "agent_message"
                | "agent_message_content_delta"
                | "response_fact"
                | "token_count"
                | "agent_reasoning"
                | "agent_reasoning_delta"
                | "agent_reasoning_raw_content"
                | "agent_reasoning_raw_content_delta"
                | "agent_reasoning_section_break"
                | "turn_diff"
                | "skills_update_available"
                | "task_complete"
                | "turn_complete"
        )
    )
}

async fn execute(inputs: Inputs, cancelled: CancellationToken) -> PyResult<RunOutput> {
    let Inputs {
        opening,
        lease,
        keepalive: _keepalive,
        input,
    } = inputs;
    // Opening/admission are never abandoned midway. Python shields this owner
    // and keeps its preparation frame until a result transfers exact ownership.
    let scope = opening.await?;
    let state = scope.1.current()?;
    let mut output = RunOutput {
        scope: Some(scope),
        info: json!({"status": "unsubmitted", "thread_id": state.id.to_string(),
            "receipt": null, "retired": {"thread": false, "engine": false}}),
        archive: NativeRunArchive::default(),
        failures: Vec::new(),
    };
    let (engine, thread, home) = output.scope.as_ref().expect("opened scope is retained");
    let eligible = home.is_none()
        && !engine.current()?.owns_runtime_services()
        && state.initial_turn.lock().map_err(py_error)?.is_some()
        && state
            .thread
            .thread_extension_data()
            .get::<crate::python_tools::PythonTools>()
            .is_some_and(|tools| tools.can_run_without_python());
    if !eligible || cancelled.is_cancelled() {
        return Ok(output);
    }
    let params: Value = serde_json::from_str(&input).map_err(py_error)?;
    if !plain_request(&params) {
        return Ok(output);
    }
    output.info["status"] = json!("live");
    let submitted = async {
        let _operation = state.engine.operation(false)?;
        ops::submit_replaceable(&state, params).await
    }
    .await;
    let receipt = match submitted {
        Ok(receipt) => receipt,
        Err(error) => {
            output.failures.push(("admission", py_error(error)));
            return Ok(output);
        }
    };
    let turn_id = receipt["turn_id"]
        .as_str()
        .filter(|id| !id.is_empty())
        .map(str::to_owned);
    output.info["receipt"] = receipt;
    if output.info["receipt"]["status"] != "started" || turn_id.is_none() {
        return Ok(output);
    }
    let turn_id = turn_id.expect("checked turn identity");
    let mut bytes = 0;
    let text = loop {
        let event = tokio::select! {
            biased;
            _ = cancelled.cancelled() => {
                return Ok(output);
            }
            event = state.next_session_event() => event,
        };
        let event = match event {
            Ok(event) => event,
            Err(error) => {
                output.failures.push(("reader", py_error(error)));
                return Ok(output);
            }
        };
        let message = &event["msg"];
        let terminal = matches!(
            message["type"].as_str(),
            Some("task_complete" | "turn_complete")
        );
        let text = if terminal
            && message["error"].is_null()
            && message["tool_completion"].is_null()
            && message["turn_id"]
                .as_str()
                .or_else(|| message["turnId"].as_str())
                .or_else(|| event["id"].as_str())
                == Some(turn_id.as_str())
        {
            message["last_agent_message"]
                .as_str()
                .filter(|text| !text.is_empty() && text.len() <= 65536)
                .map(str::to_owned)
        } else {
            None
        };
        let supported = passive(&event) && (!terminal || text.is_some());
        // Keep the bytes already needed for the limit, not long-lived event
        // trees that retain allocator space across held Completed results.
        let event_bytes = output.archive.push(&event)?;
        if supported {
            bytes += event_bytes;
        }
        if !supported || bytes > MAX_BYTES || output.archive.count >= MAX_EVENTS {
            return Ok(output);
        }
        if let Some(text) = text {
            break text;
        }
    };
    output.info["output"] = json!(text);
    output.info["usage"] = json!(state.thread.token_usage_info().await);
    // Cancellation before retirement hands the live scope back. Once closing
    // starts, finish both attempts and report their original failures together.
    if cancelled.is_cancelled() {
        return Ok(output);
    }
    drop(state);
    output.info["status"] = json!("retirement_failed");
    match crate::close_thread_owner(thread.inner.clone(), None).await {
        Ok(()) => output.info["retired"]["thread"] = json!(true),
        Err(error) => output.failures.push(("thread", error)),
    }
    match crate::close_engine_owner(engine.inner.clone()).await {
        Ok(()) => output.info["retired"]["engine"] = json!(true),
        Err(error) => output.failures.push(("engine", error)),
    }
    if output.failures.is_empty() {
        match lease.release() {
            Ok(()) => {
                output.info["status"] = json!("completed");
                output.scope = None;
            }
            Err(error) => output.failures.push(("lease", error)),
        }
    }
    Ok(output)
}

#[cfg(test)]
mod archive_tests {
    use super::*;

    #[test]
    fn archive_preserves_events_and_counts_utf8_payload_bytes() {
        let mut archive = NativeRunArchive::default();
        assert_eq!(archive.json().unwrap(), "[]");
        let events = vec![
            json!({"msg": {"type": "warning", "message": "quotes: \"\n☃"}}),
            json!({"msg": {"type": "_sdk_callback", "arguments": [null, 1, true]}}),
        ];
        for event in &events {
            assert_eq!(
                archive.push(event).unwrap(),
                serde_json::to_vec(event).unwrap().len()
            );
        }
        assert_eq!(archive.count, events.len());
        assert_eq!(
            serde_json::from_str::<Vec<Value>>(archive.json().unwrap()).unwrap(),
            events
        );
    }
}

#[cfg(all(test, target_os = "linux"))]
mod native_retirement_tests;
