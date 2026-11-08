//! Exact callback ownership for Decisions response accounting.

use std::sync::{Arc, Mutex};

use codex_core::AuxiliaryResponse;
use codex_protocol::ThreadId;
use codex_protocol::protocol::{
    ReceivedResponseUsage, ResponseObservation, ResponseObservationStatus, TokenUsage,
};
use pyo3::prelude::*;
use serde::Deserialize;
use serde_json::Value;

use crate::{RuntimeOwned, async_bridge, engine, py_error, python_tools};

enum Authority {
    Tool(python_tools::PendingKey),
    Prepared {
        id: u64,
        generation: u64,
        thread: String,
        turn: String,
    },
    Interception {
        nonce: u64,
        owner: crate::native_interception::CapturedTurnStop,
        thread: String,
        turn: String,
    },
    Step {
        id: u64,
        thread: String,
        instance: Option<String>,
        turn: String,
    },
}

impl Authority {
    fn parse(raw: &str, root: &codex_core_api::CodexThread) -> PyResult<Self> {
        let value: Value = serde_json::from_str(raw)
            .map_err(|_| py_error("Invalid decision callback authority"))?;
        if value["type"] == "_sdk_native_interception" {
            let nonce = value["nonce"]
                .as_u64()
                .ok_or_else(|| py_error("Invalid decision callback authority"))?;
            let owner = root
                .thread_extension_data()
                .get::<crate::native_interception::Control>()
                .and_then(|control| control.capture_decision_owner(nonce))
                .ok_or_else(|| py_error("Expired decision callback authority"))?;
            let (thread, turn) = owner
                .decision_ids()
                .ok_or_else(|| py_error("Expired decision callback authority"))?;
            return Ok(Self::Interception {
                nonce,
                thread: thread.to_owned(),
                turn: turn.to_owned(),
                owner,
            });
        }
        if value["type"] == "_sdk_prepare_request" {
            let invalid = || py_error("Invalid decision callback authority");
            if !value["instance"].is_null() {
                return Err(invalid());
            }
            return Ok(Self::Prepared {
                id: value["request_id"].as_u64().ok_or_else(invalid)?,
                generation: value["generation"].as_u64().ok_or_else(invalid)?,
                thread: value["thread"].as_str().ok_or_else(invalid)?.to_owned(),
                turn: value["turn_id"].as_str().ok_or_else(invalid)?.to_owned(),
            });
        }
        if value.get("_sdk_python_tool").is_some() {
            return serde_json::from_value(value)
                .map(Self::Tool)
                .map_err(|_| py_error("Invalid decision callback authority"));
        }
        let invalid = || py_error("Invalid decision callback authority");
        Ok(Self::Step {
            id: value["step_id"].as_u64().ok_or_else(invalid)?,
            thread: value["thread"].as_str().ok_or_else(invalid)?.to_owned(),
            instance: value["instance"].as_str().map(str::to_owned),
            turn: value["turn_id"].as_str().ok_or_else(invalid)?.to_owned(),
        })
    }

    fn thread(&self) -> &str {
        match self {
            Self::Tool(key) => &key.reply.thread,
            Self::Step { thread, .. }
            | Self::Prepared { thread, .. }
            | Self::Interception { thread, .. } => thread,
        }
    }

    fn turn(&self) -> &str {
        match self {
            Self::Tool(key) => &key.turn,
            Self::Step { turn, .. }
            | Self::Prepared { turn, .. }
            | Self::Interception { turn, .. } => turn,
        }
    }

    fn check(&self, tools: Option<&Arc<python_tools::PythonTools>>, root: &str) -> PyResult<()> {
        if let Self::Interception { nonce, owner, .. } = self {
            return if owner.decision_pending(*nonce) {
                Ok(())
            } else {
                Err(py_error("Expired decision callback authority"))
            };
        }
        let tools = tools.ok_or_else(|| py_error("Decision requires a managed callback"))?;
        match self {
            Self::Interception { .. } => unreachable!(),
            Self::Prepared {
                id,
                generation,
                thread,
                turn,
            } => {
                if thread != root || !tools.check_prepared_authority(thread, turn, *generation, *id)
                {
                    return Err(py_error("Expired decision callback authority"));
                }
                Ok(())
            }
            Self::Tool(key) => tools
                .owner(root, key)
                .ok_or_else(|| py_error("Expired decision callback authority"))?
                .check_pending(&key.reply.thread, key)
                .map_err(py_error),
            Self::Step {
                id,
                thread,
                instance,
                ..
            } => tools
                .check_step(*id, Some(thread), instance.as_deref())
                .map_err(py_error),
        }
    }
    fn check_thread(&self, thread: &codex_core_api::CodexThread) -> PyResult<()> {
        if let Self::Interception { owner, .. } = self {
            if !owner.decision_registered(thread.thread_extension_data()) {
                return Err(py_error("Expired decision callback authority"));
            }
        }
        Ok(())
    }
}

struct Captured {
    response: Mutex<Option<AuxiliaryResponse>>,
    completion_id: String,
    thread_id: String,
    turn_id: String,
}

#[pyclass(frozen, module = "agents._native")]
pub(crate) struct NativeDecisionCall {
    state: RuntimeOwned<Captured>,
}

pub(crate) async fn capture(
    inner: RuntimeOwned<engine::ThreadState>,
    request: String,
) -> PyResult<NativeDecisionCall> {
    let _operation = inner.engine.operation(false).map_err(py_error)?;
    let authority = Authority::parse(&request, &inner.thread)?;
    let tools = inner
        .thread
        .thread_extension_data()
        .get::<python_tools::PythonTools>();
    let root = inner.id.to_string();
    authority.check(tools.as_ref(), &root)?;
    let id = ThreadId::from_string(authority.thread())
        .map_err(|_| py_error("Invalid decision callback authority"))?;
    let thread = if id == inner.id {
        Arc::clone(&inner.thread)
    } else {
        inner
            .engine
            .manager
            .get_thread(id)
            .await
            .map_err(py_error)?
    };
    authority.check(tools.as_ref(), &root)?;
    authority.check_thread(&thread)?;
    let response = thread
        .capture_auxiliary_response(authority.turn())
        .await
        .ok_or_else(|| py_error("Expired decision callback authority"))?;
    authority.check(tools.as_ref(), &root)?;
    authority.check_thread(&thread)?;
    let completion_id = response.completion_id().to_owned();
    Ok(NativeDecisionCall {
        state: RuntimeOwned(Some(Arc::new(Captured {
            response: Mutex::new(Some(response)),
            completion_id,
            thread_id: authority.thread().to_owned(),
            turn_id: authority.turn().to_owned(),
        }))),
    })
}

#[derive(Deserialize)]
struct Metadata {
    request_id: Option<String>,
    model: Option<String>,
    usage: Option<Value>,
}

#[pymethods]
impl NativeDecisionCall {
    #[getter]
    fn thread_id(&self) -> String {
        self.state.thread_id.clone()
    }
    #[getter]
    fn turn_id(&self) -> String {
        self.state.turn_id.clone()
    }
    #[getter]
    fn completion_id(&self) -> String {
        self.state.completion_id.clone()
    }

    fn commit<'py>(&self, py: Python<'py>, metadata_json: &str) -> PyResult<Bound<'py, PyAny>> {
        let metadata: Metadata = serde_json::from_str(metadata_json)
            .map_err(|_| py_error("Invalid decision response metadata"))?;
        let state = self.state.clone();
        async_bridge::future_into_py(py, async move {
            let response = state
                .response
                .lock()
                .map_err(py_error)?
                .take()
                .ok_or_else(|| py_error("Decision response was already committed"))?;
            let usage = metadata
                .usage
                .and_then(|value| serde_json::from_value::<TokenUsage>(value).ok());
            let observed = usage.as_ref().map(|usage| ReceivedResponseUsage {
                input_tokens: Some(usage.input_tokens),
                cached_input_tokens: Some(usage.cached_input_tokens),
                cache_write_input_tokens: Some(usage.cache_write_input_tokens),
                output_tokens: Some(usage.output_tokens),
                reasoning_output_tokens: Some(usage.reasoning_output_tokens),
                total_tokens: Some(usage.total_tokens),
            });
            let result = response
                .commit(
                    ResponseObservation {
                        response_id: metadata.request_id,
                        status: ResponseObservationStatus::Completed,
                        incomplete_reason: None,
                        usage: observed,
                    },
                    usage,
                    None,
                    metadata.model,
                )
                .await;
            // Core emitted the completion before returning a retention error.
            // Let the host drain that event before it propagates the failure.
            Ok(result.err().map(|error| error.to_string()))
        })
    }
}
