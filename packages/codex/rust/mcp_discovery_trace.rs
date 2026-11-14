//! Managed MCP trace admission over the existing Python completion bridge.

use std::sync::{Arc, Mutex};

use codex_otel::sdk_trace::{ContextLease, McpDiscoverySpanAdmission, McpDiscoverySpanPolicy};
use futures_util::future::BoxFuture;
use pyo3::prelude::*;
use tokio::sync::{Mutex as AsyncMutex, mpsc, oneshot};
use tokio_util::sync::CancellationToken;

use crate::{async_bridge, py_error};

type Reply = Result<McpDiscoverySpanAdmission, String>;

struct HostPolicy {
    requests: mpsc::Sender<DiscoveryRequest>,
    closed: CancellationToken,
}

impl McpDiscoverySpanPolicy for HostPolicy {
    fn admit<'a>(&'a self, parent: &'a ContextLease, server: &'a str) -> BoxFuture<'a, Reply> {
        Box::pin(async move {
            let (reply, result) = oneshot::channel();
            let request = DiscoveryRequest {
                parent: parent.token().to_owned(),
                server: server.to_owned(),
                reply: Mutex::new(Some(reply)),
            };
            tokio::select! {
                biased;
                _ = self.closed.cancelled() => return Ok(McpDiscoverySpanAdmission::Suppress),
                sent = self.requests.send(request) => {
                    if sent.is_err() {
                        return Ok(McpDiscoverySpanAdmission::Suppress);
                    }
                }
            }
            tokio::select! {
                biased;
                // Shared connection startup can outlive a cache-backed roster receipt.
                // Keep discovery running after the publishing operation retires its policy.
                _ = self.closed.cancelled() => Ok(McpDiscoverySpanAdmission::Suppress),
                result = result => result.unwrap_or(Ok(McpDiscoverySpanAdmission::Suppress)),
            }
        })
    }
}

#[pyclass(name = "_McpDiscoverySpanPolicy")]
pub(crate) struct DiscoveryPolicy {
    host: Mutex<Option<Arc<HostPolicy>>>,
    requests: Arc<AsyncMutex<mpsc::Receiver<DiscoveryRequest>>>,
    closed: CancellationToken,
}

impl DiscoveryPolicy {
    pub(crate) fn take(&self) -> PyResult<Arc<dyn McpDiscoverySpanPolicy>> {
        self.host
            .lock()
            .map_err(py_error)?
            .take()
            .map(|host| host as Arc<dyn McpDiscoverySpanPolicy>)
            .ok_or_else(|| py_error("SDK MCP discovery policy already has a publication owner"))
    }
}

impl Drop for DiscoveryPolicy {
    fn drop(&mut self) {
        self.closed.cancel();
    }
}

#[pymethods]
impl DiscoveryPolicy {
    #[new]
    fn new() -> Self {
        let (requests, receiver) = mpsc::channel(8);
        let closed = CancellationToken::new();
        Self {
            host: Mutex::new(Some(Arc::new(HostPolicy {
                requests,
                closed: closed.clone(),
            }))),
            requests: Arc::new(AsyncMutex::new(receiver)),
            closed,
        }
    }

    fn next<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let requests = Arc::clone(&self.requests);
        let closed = self.closed.clone();
        async_bridge::cancel_first_future_into_py(py, async move {
            let mut requests = requests.lock().await;
            tokio::select! {
                biased;
                _ = closed.cancelled() => Ok(None),
                request = requests.recv() => Ok(request),
            }
        })
    }

    fn close(&self) {
        self.closed.cancel();
    }
}

#[pyclass]
struct DiscoveryRequest {
    #[pyo3(get)]
    parent: String,
    #[pyo3(get)]
    server: String,
    reply: Mutex<Option<oneshot::Sender<Reply>>>,
}

#[pymethods]
impl DiscoveryRequest {
    #[pyo3(signature = (record, span_id=None, *, error=false))]
    fn settle(&self, record: bool, span_id: Option<String>, error: bool) -> PyResult<bool> {
        let outcome = if error {
            Err("SDK MCP discovery tracing policy failed".into())
        } else if record {
            Ok(McpDiscoverySpanAdmission::Record(span_id))
        } else {
            Ok(McpDiscoverySpanAdmission::Suppress)
        };
        Ok(self
            .reply
            .lock()
            .map_err(py_error)?
            .take()
            .is_some_and(|reply| reply.send(outcome).is_ok()))
    }

    fn is_live(&self) -> PyResult<bool> {
        Ok(self
            .reply
            .lock()
            .map_err(py_error)?
            .as_ref()
            .is_some_and(|reply| !reply.is_closed()))
    }
}
