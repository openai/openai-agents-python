//! Native send counts owned by one explicit root submission.
use codex_extension_api::{
    ExtensionData, ExtensionDataInit, ModelAttemptOperation, ModelAttemptRecorder,
    ModelAttemptRoute, ModelRequestTransport,
};
use pyo3::prelude::*;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, OnceLock, Weak};

#[derive(Debug, Default)]
pub(crate) struct Capture {
    turn_id: OnceLock<String>,
    state: Mutex<Counts>,
}

#[derive(Debug, Default)]
struct Counts {
    values: BTreeMap<(&'static str, &'static str, &'static str), u64>,
    unavailable: bool,
    overflowed: bool,
    sealed: bool,
}

#[derive(Debug)]
struct Route {
    capture: Arc<Capture>,
    child: bool,
}

impl ModelAttemptRecorder for Route {
    fn matches_root_turn(&self, root_turn_id: Option<&str>) -> bool {
        !self.child || self.capture.turn_id.get().map(String::as_str) == root_turn_id
    }

    fn unavailable(&self) {
        let mut state = self
            .capture
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if !state.sealed {
            state.unavailable = true;
        }
    }

    fn record(&self, operation: ModelAttemptOperation, transport: ModelRequestTransport) {
        let owner = if self.child { "child" } else { "root" };
        let operation = match operation {
            ModelAttemptOperation::Generation => "generation",
            ModelAttemptOperation::Compaction => "compaction",
            ModelAttemptOperation::Internal => "internal",
        };
        let transport = match transport {
            ModelRequestTransport::HttpSse => "http_sse",
            ModelRequestTransport::HttpJson => "http_json",
            ModelRequestTransport::Websocket => "websocket",
        };
        let mut state = self
            .capture
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if state.sealed {
            return;
        }
        let count = state
            .values
            .entry((owner, operation, transport))
            .or_default();
        if let Some(next) = count.checked_add(1) {
            *count = next;
        } else {
            state.overflowed = true;
        }
    }
}

impl Capture {
    pub(crate) fn seed(self: &Arc<Self>, init: &mut ExtensionDataInit) {
        init.insert(self.clone());
        init.insert(self.route(false));
    }

    fn route(self: &Arc<Self>, child: bool) -> ModelAttemptRoute {
        ModelAttemptRoute(Arc::new(Route {
            capture: self.clone(),
            child,
        }))
    }

    fn seal(&self) {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .sealed = true;
    }

    pub(crate) fn seal_for_turn(&self, turn_id: &str) {
        if self.turn_id.get().map(String::as_str) == Some(turn_id) {
            self.seal();
        }
    }

    fn report(&self) -> Option<Value> {
        let state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        if !state.sealed {
            return None;
        }
        let availability = if state.unavailable && state.values.is_empty() {
            "unavailable"
        } else if state.unavailable || state.overflowed {
            "partial"
        } else {
            "complete"
        };
        let counts: Vec<_> = state.values.iter().map(|((owner, operation, transport), count)| {
            json!({"owner": owner, "operation": operation, "transport": transport, "count": count.to_string()})
        }).collect();
        Some(json!({
            "availability": availability,
            "counts": counts,
            "borrowed_requests_unavailable": state.unavailable,
        }))
    }
}

pub(crate) fn on_turn_start(
    store: &ExtensionData,
    turn_id: &str,
    child: bool,
    root: &Mutex<Weak<Capture>>,
) {
    let mut root = root.lock().unwrap_or_else(|error| error.into_inner());
    if let Some(capture) = store.get::<Arc<Capture>>() {
        let _ = capture.turn_id.set(turn_id.to_owned());
        *root = Arc::downgrade(capture.as_ref());
    } else if child && let Some(capture) = root.upgrade() {
        store.insert(capture.route(true));
    }
}

pub(crate) fn on_turn_end(store: &ExtensionData) {
    if let Some(capture) = store.get::<Arc<Capture>>() {
        capture.seal();
    }
}

#[pyclass(frozen, module = "agents._native")]
pub(crate) struct NativeModelAttemptCapture(pub(crate) Arc<Capture>);

#[pymethods]
impl NativeModelAttemptCapture {
    #[new]
    fn new() -> Self {
        Self(Arc::default())
    }

    fn _snapshot(&self) -> Option<String> {
        self.0.report().map(|report| report.to_string())
    }
}
