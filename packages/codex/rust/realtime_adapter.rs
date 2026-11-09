//! Bounded, Session-bound frame I/O. Native code never retains Python callbacks.
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, Weak};

use anyhow::{Result, ensure};
use codex_api::{
    RealtimeAdapter, RealtimeAdapterConnection, RealtimeAdapterContext, RealtimeAdapterError,
};
use futures_util::future::BoxFuture;
use pyo3::prelude::*;
use serde_json::{Value, json};
use tokio::sync::{
    Mutex as AsyncMutex, Notify, OwnedSemaphorePermit, Semaphore, mpsc, oneshot, watch,
};
use tokio_util::sync::CancellationToken;

use crate::async_bridge;
use crate::engine::ThreadState;
use crate::py_error;

const DATA_CAPACITY: usize = 8;
const INCOMING_CAPACITY: usize = 64;
const BYTE_CAPACITY: usize = 8 * 1024 * 1024;

struct Request {
    event_json: Mutex<String>,
    reply: Mutex<Option<oneshot::Sender<bool>>>,
    bytes: Mutex<Option<OwnedSemaphorePermit>>,
}

impl Request {
    fn event_json(&self) -> String {
        self.event_json
            .lock()
            .expect("adapter event poisoned")
            .clone()
    }

    fn complete(&self, success: bool) -> bool {
        // Settlement releases the budget and credentials even if Python keeps
        // the request or its error traceback alive until a later collection.
        self.bytes.lock().expect("adapter bytes poisoned").take();
        *self.event_json.lock().expect("adapter event poisoned") = String::new();
        self.reply
            .lock()
            .expect("adapter reply poisoned")
            .take()
            .is_some_and(|sender| sender.send(success).is_ok())
    }
}

struct Frame {
    text: String,
    _bytes: OwnedSemaphorePermit,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Incoming {
    Open,
    Eof,
    Failed,
}

struct State {
    data_tx: mpsc::Sender<Arc<Request>>,
    data_rx: AsyncMutex<mpsc::Receiver<Arc<Request>>>,
    close_tx: mpsc::Sender<Arc<Request>>,
    close_rx: AsyncMutex<mpsc::Receiver<Arc<Request>>>,
    incoming_tx: mpsc::Sender<Frame>,
    incoming_rx: AsyncMutex<mpsc::Receiver<Frame>>,
    incoming_state: watch::Sender<Incoming>,
    data_bytes: Arc<Semaphore>,
    incoming_bytes: Arc<Semaphore>,
    data_operation: AsyncMutex<()>,
    close_operation: AsyncMutex<()>,
    cancel_pending: AtomicBool,
    cancel: Notify,
    finished: CancellationToken,
    connected: AtomicBool,
    connection_cancel: Mutex<Option<CancellationToken>>,
    connection_changed: Notify,
    connection_cancel_reported: AtomicBool,
}

impl State {
    fn new() -> Arc<Self> {
        let (data_tx, data_rx) = mpsc::channel(DATA_CAPACITY);
        let (close_tx, close_rx) = mpsc::channel(1);
        let (incoming_tx, incoming_rx) = mpsc::channel(INCOMING_CAPACITY);
        Arc::new(Self {
            data_tx,
            data_rx: AsyncMutex::new(data_rx),
            close_tx,
            close_rx: AsyncMutex::new(close_rx),
            incoming_tx,
            incoming_rx: AsyncMutex::new(incoming_rx),
            incoming_state: watch::channel(Incoming::Open).0,
            data_bytes: Arc::new(Semaphore::new(BYTE_CAPACITY)),
            incoming_bytes: Arc::new(Semaphore::new(BYTE_CAPACITY)),
            data_operation: AsyncMutex::new(()),
            close_operation: AsyncMutex::new(()),
            cancel_pending: AtomicBool::new(false),
            cancel: Notify::new(),
            finished: CancellationToken::new(),
            connected: AtomicBool::new(false),
            connection_cancel: Mutex::new(None),
            connection_changed: Notify::new(),
            connection_cancel_reported: AtomicBool::new(false),
        })
    }

    fn cancel(&self) {
        self.cancel_pending.store(true, Ordering::Release);
        self.cancel.notify_one();
    }

    async fn request(
        self: &Arc<Self>,
        event: Value,
        close: bool,
        cancellation: CancellationToken,
    ) -> Result<(), RealtimeAdapterError> {
        // Only one data operation can be issued. Close bypasses this lock and
        // byte budget so shutdown can cancel an unfinished connect or write.
        let _operation = if close {
            self.close_operation.lock().await
        } else {
            self.data_operation.lock().await
        };
        if self.finished.is_cancelled() {
            return Err(RealtimeAdapterError);
        }
        let event_json = serde_json::to_string(&event).map_err(|_| RealtimeAdapterError)?;
        if event_json.len() > BYTE_CAPACITY {
            return Err(RealtimeAdapterError);
        }
        let bytes = if close {
            None
        } else {
            Some(tokio::select! {
                biased;
                _ = self.finished.cancelled() => return Err(RealtimeAdapterError),
                _ = cancellation.cancelled() => return Err(RealtimeAdapterError),
                permit = self.data_bytes.clone().acquire_many_owned(event_json.len() as u32) => {
                    permit.map_err(|_| RealtimeAdapterError)?
                }
            })
        };
        let (reply, result) = oneshot::channel();
        let request = Arc::new(Request {
            event_json: Mutex::new(event_json),
            reply: Mutex::new(Some(reply)),
            bytes: Mutex::new(bytes),
        });
        let mut cancellation_owner = CancelOnDrop {
            state: self.clone(),
            armed: true,
        };
        let sender = if close { &self.close_tx } else { &self.data_tx };
        tokio::select! {
            biased;
            _ = self.finished.cancelled() => return Err(RealtimeAdapterError),
            _ = cancellation.cancelled() => return Err(RealtimeAdapterError),
            sent = sender.send(request) => sent.map_err(|_| RealtimeAdapterError)?,
        }
        let accepted = tokio::select! {
            biased;
            _ = self.finished.cancelled() => return Err(RealtimeAdapterError),
            _ = cancellation.cancelled() => return Err(RealtimeAdapterError),
            result = result => result.map_err(|_| RealtimeAdapterError)?,
        };
        cancellation_owner.armed = false;
        if accepted {
            Ok(())
        } else {
            Err(RealtimeAdapterError)
        }
    }

    async fn next(&self) -> Option<Arc<Request>> {
        let mut data = self.data_rx.lock().await;
        let mut close = self.close_rx.lock().await;
        loop {
            if self.finished.is_cancelled() {
                while let Ok(request) = data.try_recv() {
                    request.complete(false);
                }
                while let Ok(request) = close.try_recv() {
                    request.complete(false);
                }
                return None;
            }
            let connection_cancel = self
                .connection_cancel
                .lock()
                .expect("adapter cancellation poisoned")
                .clone();
            let connection_cancelled = connection_cancel
                .as_ref()
                .is_some_and(CancellationToken::is_cancelled);
            if self.cancel_pending.swap(false, Ordering::AcqRel)
                || (connection_cancelled
                    && !self.connection_cancel_reported.load(Ordering::Acquire))
            {
                if connection_cancelled {
                    self.connection_cancel_reported
                        .store(true, Ordering::Release);
                }
                return Some(Arc::new(Request {
                    event_json: Mutex::new("{\"type\":\"cancel\"}".into()),
                    reply: Mutex::new(None),
                    bytes: Mutex::new(None),
                }));
            }
            let cancellation = if self.connection_cancel_reported.load(Ordering::Acquire) {
                CancellationToken::new()
            } else {
                connection_cancel.unwrap_or_default()
            };
            tokio::select! {
                biased;
                _ = self.finished.cancelled() => {},
                _ = cancellation.cancelled() => {},
                _ = self.connection_changed.notified() => {},
                request = close.recv() => return request,
                _ = self.cancel.notified() => {},
                request = data.recv() => return request,
            }
        }
    }

    fn end(&self, success: bool) {
        // A failure cannot later be downgraded to a clean EOF by cancellation.
        self.incoming_state.send_if_modified(|state| {
            if *state == Incoming::Failed || (*state == Incoming::Eof && success) {
                false
            } else {
                *state = if success {
                    Incoming::Eof
                } else {
                    Incoming::Failed
                };
                true
            }
        });
    }

    fn finish(&self) {
        self.finished.cancel();
        self.data_bytes.close();
        self.incoming_bytes.close();
        self.end(true);
        if let Ok(mut data) = self.data_rx.try_lock() {
            while let Ok(request) = data.try_recv() {
                request.complete(false);
            }
        }
        if let Ok(mut close) = self.close_rx.try_lock() {
            while let Ok(request) = close.try_recv() {
                request.complete(false);
            }
        }
        if let Ok(mut incoming) = self.incoming_rx.try_lock() {
            while incoming.try_recv().is_ok() {}
        }
    }
}

struct CancelOnDrop {
    state: Arc<State>,
    armed: bool,
}

impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        if self.armed {
            self.state.cancel();
        }
    }
}

struct Adapter {
    state: Arc<State>,
}

impl RealtimeAdapter for Adapter {
    fn connect(
        &self,
        context: RealtimeAdapterContext,
    ) -> BoxFuture<'_, Result<Arc<dyn RealtimeAdapterConnection>, RealtimeAdapterError>> {
        Box::pin(async move {
            if self.state.connected.swap(true, Ordering::AcqRel) {
                return Err(RealtimeAdapterError);
            }
            let mut headers = BTreeMap::new();
            for (name, value) in &context.headers {
                headers.insert(
                    name.as_str().to_owned(),
                    value.to_str().map_err(|_| RealtimeAdapterError)?.to_owned(),
                );
            }
            *self
                .state
                .connection_cancel
                .lock()
                .expect("adapter cancellation poisoned") = Some(context.cancellation.clone());
            self.state.connection_changed.notify_one();
            self.state.request(
                json!({"type":"connect", "url":context.url, "headers":headers, "model":context.model}),
                false,
                context.cancellation,
            ).await?;
            Ok(Arc::new(Connection {
                state: self.state.clone(),
            }) as Arc<dyn RealtimeAdapterConnection>)
        })
    }
}

struct Connection {
    state: Arc<State>,
}

impl RealtimeAdapterConnection for Connection {
    fn send(&self, frame: String) -> BoxFuture<'_, Result<(), RealtimeAdapterError>> {
        Box::pin(async move {
            let cancellation = self
                .state
                .connection_cancel
                .lock()
                .expect("adapter cancellation poisoned")
                .clone()
                .ok_or(RealtimeAdapterError)?;
            self.state
                .request(json!({"type":"send", "frame":frame}), false, cancellation)
                .await
        })
    }

    fn receive(&self) -> BoxFuture<'_, Result<Option<String>, RealtimeAdapterError>> {
        Box::pin(async move {
            let mut incoming = self.state.incoming_rx.lock().await;
            let mut status = self.state.incoming_state.subscribe();
            loop {
                if self.state.finished.is_cancelled() {
                    while incoming.try_recv().is_ok() {}
                    return Ok(None);
                }
                match *status.borrow() {
                    Incoming::Failed => return Err(RealtimeAdapterError),
                    Incoming::Eof => return Ok(incoming.try_recv().ok().map(|frame| frame.text)),
                    Incoming::Open => {}
                }
                tokio::select! {
                    biased;
                    _ = self.state.finished.cancelled() => {},
                    _ = status.changed() => {},
                    frame = incoming.recv() => return Ok(frame.map(|frame| frame.text)),
                }
            }
        })
    }

    fn close(&self) -> BoxFuture<'_, Result<(), RealtimeAdapterError>> {
        Box::pin(async move {
            // The connection token is already cancelled during native close.
            // Cleanup needs its own request and must remain retryable.
            self.state
                .request(json!({"type":"close"}), true, CancellationToken::new())
                .await
        })
    }
}

#[pyclass(name = "_NativeRealtimeAdapter")]
pub(crate) struct NativeRealtimeAdapter {
    owner: Weak<ThreadState>,
    generation: String,
    attached: AtomicBool,
    state: Arc<State>,
}

impl NativeRealtimeAdapter {
    pub(crate) fn new(owner: &Arc<ThreadState>, generation: String) -> Self {
        Self {
            owner: Arc::downgrade(owner),
            generation,
            attached: AtomicBool::new(false),
            state: State::new(),
        }
    }

    pub(crate) fn attach(
        &self,
        owner: &Arc<ThreadState>,
        generation: &str,
    ) -> Result<Arc<dyn RealtimeAdapter>> {
        ensure!(
            self.owner.ptr_eq(&Arc::downgrade(owner)),
            "Realtime adapter belongs to another Session"
        );
        ensure!(
            !generation.is_empty() && self.generation == generation,
            "Realtime adapter generation differs"
        );
        ensure!(
            !self.state.finished.is_cancelled(),
            "Realtime adapter is retired"
        );
        ensure!(
            self.attached
                .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
                .is_ok(),
            "Realtime adapter was already attached"
        );
        Ok(Arc::new(Adapter {
            state: self.state.clone(),
        }))
    }
}

#[pymethods]
impl NativeRealtimeAdapter {
    fn next<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let state = self.state.clone();
        async_bridge::future_into_py(py, async move {
            Ok(state
                .next()
                .await
                .map(|request| NativeRealtimeAdapterRequest { request }))
        })
    }

    fn receive<'py>(&self, py: Python<'py>, frame: &str) -> PyResult<Bound<'py, PyAny>> {
        if frame.len() > BYTE_CAPACITY {
            return Err(py_error("Realtime adapter incoming frame is oversized"));
        }
        let frame = frame.to_owned();
        let state = self.state.clone();
        async_bridge::future_into_py(py, async move {
            if *state.incoming_state.borrow() != Incoming::Open {
                return Err(py_error(
                    "Realtime adapter incoming stream is closed or oversized",
                ));
            }
            let bytes = tokio::select! {
                biased;
                _ = state.finished.cancelled() => return Err(py_error("Realtime adapter is retired")),
                permit = state.incoming_bytes.clone().acquire_many_owned(frame.len() as u32) => {
                    permit.map_err(|_| py_error("Realtime adapter is retired"))?
                }
            };
            tokio::select! {
                biased;
                _ = state.finished.cancelled() => Err(py_error("Realtime adapter is retired")),
                result = state.incoming_tx.send(Frame { text: frame, _bytes: bytes }) => {
                    result.map_err(|_| py_error("Realtime adapter incoming stream is closed"))
                }
            }
        })
    }

    fn end(&self, success: bool) {
        self.state.end(success);
    }

    fn finish(&self) {
        self.state.finish();
    }
}

#[pyclass(name = "_NativeRealtimeAdapterRequest")]
pub(crate) struct NativeRealtimeAdapterRequest {
    request: Arc<Request>,
}

#[pymethods]
impl NativeRealtimeAdapterRequest {
    #[getter]
    fn event_json(&self) -> String {
        self.request.event_json()
    }

    fn complete(&self, success: bool) -> bool {
        self.request.complete(success)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn completed_request_releases_allocation_while_request_is_retained() {
        let budget = Arc::new(Semaphore::new(BYTE_CAPACITY));
        let permit = budget
            .clone()
            .acquire_many_owned(BYTE_CAPACITY as u32)
            .await
            .unwrap();
        let (sender, receiver) = oneshot::channel();
        let request = Request {
            event_json: Mutex::new("x".repeat(BYTE_CAPACITY)),
            reply: Mutex::new(Some(sender)),
            bytes: Mutex::new(Some(permit)),
        };
        assert_eq!(budget.available_permits(), 0);
        assert!(request.complete(true));
        assert!(receiver.await.unwrap());
        assert_eq!(budget.available_permits(), BYTE_CAPACITY);
        assert_eq!(request.event_json.lock().unwrap().capacity(), 0);
        assert!(!request.complete(false));
    }

    #[tokio::test]
    async fn finish_retires_waiters_and_drains_after_receiver_lock_is_released() {
        let state = State::new();
        let receiver = state.data_rx.lock().await;
        let sending = {
            let state = state.clone();
            tokio::spawn(async move {
                state
                    .request(
                        json!({"type":"send", "frame":"queued"}),
                        false,
                        CancellationToken::new(),
                    )
                    .await
            })
        };
        // Observing the queued request proves the sender passed admission.
        while state.data_tx.capacity() == DATA_CAPACITY {
            tokio::task::yield_now().await;
        }
        state.finish();
        assert!(sending.await.unwrap().is_err());
        drop(receiver);
        assert!(state.next().await.is_none());
        assert!(state.data_rx.lock().await.is_empty());
    }

    #[tokio::test]
    async fn close_and_cancel_bypass_a_data_operation_waiting_for_python() {
        let state = State::new();
        let sending = {
            let state = state.clone();
            tokio::spawn(async move {
                state
                    .request(
                        json!({"type":"send", "frame":"blocked"}),
                        false,
                        CancellationToken::new(),
                    )
                    .await
            })
        };
        let data = state.next().await.unwrap();
        let closing = {
            let state = state.clone();
            tokio::spawn(async move {
                state
                    .request(json!({"type":"close"}), true, CancellationToken::new())
                    .await
            })
        };
        let close = state.next().await.unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&close.event_json()).unwrap()["type"],
            "close"
        );
        assert!(close.complete(true));
        assert!(closing.await.unwrap().is_ok());
        sending.abort();
        assert!(sending.await.unwrap_err().is_cancelled());
        let cancel = state.next().await.unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&cancel.event_json()).unwrap()["type"],
            "cancel"
        );
        assert!(!data.complete(true));
        state.finish();
    }

    #[tokio::test]
    async fn context_cancellation_is_reported_once_and_does_not_block_close_retry() {
        let state = State::new();
        let cancellation = CancellationToken::new();
        *state.connection_cancel.lock().unwrap() = Some(cancellation.clone());
        cancellation.cancel();
        let cancel = state.next().await.unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&cancel.event_json()).unwrap()["type"],
            "cancel"
        );
        for success in [false, true] {
            let closing = {
                let state = state.clone();
                tokio::spawn(async move {
                    state
                        .request(json!({"type":"close"}), true, CancellationToken::new())
                        .await
                })
            };
            let request = state.next().await.unwrap();
            assert_eq!(
                serde_json::from_str::<Value>(&request.event_json()).unwrap()["type"],
                "close"
            );
            assert!(request.complete(success));
            assert_eq!(closing.await.unwrap().is_ok(), success);
        }
        state.finish();
        assert!(state.next().await.is_none());
    }
}
