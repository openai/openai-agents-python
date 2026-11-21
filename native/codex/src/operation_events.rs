//! Bounded, best-effort delivery on the thread's existing event stream.

use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex as StdMutex};

use codex_core::operation_observation::{
    OperationCaptureMode, OperationEvent, OperationObservation, OperationObserver, OperationPhase,
};
use codex_core_api::CodexThread;
use codex_extension_api::ExtensionDataInit;
use codex_protocol::protocol::EventMsg;
use serde_json::json;
use tokio::sync::{Mutex, mpsc, watch};

const QUEUE_CAPACITY: usize = 64;

struct Sink {
    sender: mpsc::Sender<String>,
    dropped: Arc<AtomicU64>,
    accepting: StdMutex<bool>,
}

impl OperationObserver for Sink {
    fn emit(&self, event: OperationEvent) {
        let method = match event.phase {
            OperationPhase::Started => "turn/operation/started",
            OperationPhase::Completed => "turn/operation/completed",
        };
        let serialized = serde_json::to_string(&json!({"method": method, "params": event}));
        let Ok(accepting) = self.accepting.lock() else {
            return;
        };
        if !*accepting {
            return;
        }
        if serialized.is_err()
            || self
                .sender
                .try_send(serialized.unwrap_or_default())
                .is_err()
        {
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }

    fn turn_finished<'a>(
        &'a self,
        _thread_id: &'a str,
        _turn_id: &'a str,
    ) -> Pin<Box<dyn Future<Output = ()> + Send + 'a>> {
        if let Ok(mut accepting) = self.accepting.lock() {
            *accepting = false;
        }
        Box::pin(async {})
    }
}

struct State {
    receiver: mpsc::Receiver<String>,
    pending: Option<String>,
}

pub(crate) struct OperationEvents {
    sender: mpsc::Sender<String>,
    dropped: Arc<AtomicU64>,
    state: Mutex<State>,
    resolution_finished: watch::Sender<bool>,
}

impl OperationEvents {
    pub(crate) fn new() -> Self {
        let (sender, receiver) = mpsc::channel(QUEUE_CAPACITY);
        let (resolution_finished, _) = watch::channel(false);
        Self {
            sender,
            dropped: Arc::new(AtomicU64::new(0)),
            state: Mutex::new(State {
                receiver,
                pending: None,
            }),
            resolution_finished,
        }
    }

    pub(crate) fn initializer(&self, mode: Option<&str>) -> Result<ExtensionDataInit, String> {
        let mode = match mode {
            None => None,
            Some("metadata") => Some(OperationCaptureMode::Metadata),
            Some("content") => Some(OperationCaptureMode::Content),
            Some(_) => return Err("Invalid Codex operation capture mode.".to_owned()),
        };
        // An empty initializer explicitly clears the previous turn's capture policy.
        let mut init = ExtensionDataInit::default();
        if let Some(mode) = mode {
            let sink = Arc::new(Sink {
                sender: self.sender.clone(),
                dropped: self.dropped.clone(),
                accepting: StdMutex::new(true),
            });
            init.insert(OperationObservation::new(mode, sink));
        }
        Ok(init)
    }

    // Release an idle observation reader before the resolver drains the final queue.
    pub(crate) fn finish_resolution(&self) {
        self.resolution_finished.send_replace(true);
    }

    pub(crate) async fn next_notification(&self, thread_id: &str) -> Option<String> {
        let mut finished = self.resolution_finished.subscribe();
        let mut state = self.state.lock().await;
        if let Ok(event) = state.receiver.try_recv() {
            return Some(event);
        }
        let dropped = self.dropped.swap(0, Ordering::Relaxed);
        if dropped != 0 {
            return Some(
                json!({
                    "method": "turn/operation/dropped",
                    "params": {"threadId": thread_id, "droppedCount": dropped},
                })
                .to_string(),
            );
        }
        if *finished.borrow() {
            return None;
        }
        tokio::select! {
            biased;
            event = state.receiver.recv() => event,
            _ = finished.changed() => None,
        }
    }

    pub(crate) async fn drain_notifications(&self, thread_id: &str) -> Vec<String> {
        let mut state = self.state.lock().await;
        let mut events = Vec::new();
        while let Ok(event) = state.receiver.try_recv() {
            events.push(event);
        }
        let dropped = self.dropped.swap(0, Ordering::Relaxed);
        if dropped != 0 {
            events.push(
                json!({
                    "method": "turn/operation/dropped",
                    "params": {"threadId": thread_id, "droppedCount": dropped},
                })
                .to_string(),
            );
        }
        events
    }

    pub(crate) async fn next_event(
        &self,
        thread: &CodexThread,
        thread_id: &str,
    ) -> Result<String, String> {
        let mut state = self.state.lock().await;
        loop {
            if let Ok(event) = state.receiver.try_recv() {
                return Ok(event);
            }
            let dropped = self.dropped.swap(0, Ordering::Relaxed);
            if dropped != 0 {
                return Ok(json!({
                    "method": "turn/operation/dropped",
                    "params": {"threadId": thread_id, "droppedCount": dropped},
                })
                .to_string());
            }
            if let Some(event) = state.pending.take() {
                return Ok(event);
            }
            tokio::select! {
                biased;
                Some(event) = state.receiver.recv() => return Ok(event),
                event = thread.next_event() => {
                    let event = event.map_err(|_| "The Codex event stream ended unexpectedly.".to_owned())?;
                    match &event.msg {
                        EventMsg::ExecApprovalRequest(_) | EventMsg::ApplyPatchApprovalRequest(_) => {
                            let (request, preconditions) = thread
                                .capture_tool_approval(event.msg)
                                .await
                                .map_err(|_| "This Codex approval cannot be safely resumed.".to_owned())?;
                            state.pending = Some(json!({
                                "id": event.id,
                                "msg": request,
                                "preconditions": preconditions,
                            }).to_string());
                            continue;
                        }
                        EventMsg::RequestPermissions(_)
                        | EventMsg::RequestUserInput(_)
                        | EventMsg::ElicitationRequest(_) => {
                            return Err("Codex requested an unsupported kind of interactive approval.".to_owned());
                        }
                        _ => {}
                    }
                    // Keep the native event across cancellation while draining observations.
                    // Core emits and closes operation admission before its turn terminal.
                    state.pending = Some(serde_json::to_string(&event)
                        .map_err(|_| "Unable to decode the Codex event.".to_owned())?);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_core::operation_observation::{OperationKind, OperationMetadata, OperationOutcome};

    #[tokio::test]
    async fn restoration_delivers_start_before_completion_and_drains_once() {
        let events = OperationEvents::new();
        let init = events.initializer(Some("metadata")).unwrap();
        let observation = init.get::<OperationObservation>().unwrap();
        let guard = observation.start(
            "thread_1".into(),
            "turn_1".into(),
            OperationKind::Tool,
            OperationMetadata::default(),
        );
        let start = events.next_notification("thread_1").await.unwrap();
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&start).unwrap()["method"],
            "turn/operation/started"
        );
        guard.finish(OperationOutcome::Success);
        events.finish_resolution();
        let terminal = events.drain_notifications("thread_1").await;
        assert_eq!(terminal.len(), 1);
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&terminal[0]).unwrap()["method"],
            "turn/operation/completed"
        );
        assert!(events.next_notification("thread_1").await.is_none());
    }

    #[tokio::test]
    async fn restoration_completion_releases_waiting_reader_before_drain() {
        let events = OperationEvents::new();
        let reader = async {
            assert!(events.next_notification("thread_1").await.is_none());
        };
        let finish = async {
            tokio::task::yield_now().await;
            events.finish_resolution();
            assert!(events.drain_notifications("thread_1").await.is_empty());
        };
        tokio::time::timeout(std::time::Duration::from_secs(1), async {
            tokio::join!(reader, finish);
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn delivery_preserves_source_identity_and_per_turn_capture() {
        for mode in ["metadata", "content"] {
            let events = OperationEvents::new();
            let init = events.initializer(Some(mode)).unwrap();
            let observation = init.get::<OperationObservation>().unwrap();
            let guard = observation.start(
                "thread_1".into(),
                "turn_1".into(),
                OperationKind::Tool,
                OperationMetadata {
                    tool_name: Some("lookup".into()),
                    ..Default::default()
                },
            );
            guard.finish_with_output(OperationOutcome::Success, &json!({"text": "visible"}));
            let notifications = events.drain_notifications("thread_1").await;
            assert_eq!(notifications.len(), 2);
            let start: serde_json::Value = serde_json::from_str(&notifications[0]).unwrap();
            let end: serde_json::Value = serde_json::from_str(&notifications[1]).unwrap();
            assert_eq!(start["method"], "turn/operation/started");
            assert_eq!(end["method"], "turn/operation/completed");
            assert_eq!(start["params"]["operationId"], end["params"]["operationId"]);
            assert_eq!(end["params"]["threadId"], "thread_1");
            assert_eq!(end["params"]["turnId"], "turn_1");
            assert_eq!(end["params"]["outcome"], "success");
            assert!(end["params"]["completedAt"].is_string());
            assert_eq!(end["params"].get("output").is_some(), mode == "content");
            assert!(
                events
                    .initializer(None)
                    .unwrap()
                    .get::<OperationObservation>()
                    .is_none()
            );
        }
    }

    #[tokio::test]
    async fn queue_overflow_stays_bounded_and_reports_loss() {
        let events = OperationEvents::new();
        let init = events.initializer(Some("metadata")).unwrap();
        let observation = init.get::<OperationObservation>().unwrap();
        for _ in 0..QUEUE_CAPACITY {
            let guard = observation.start(
                "thread_1".into(),
                "turn_1".into(),
                OperationKind::Model,
                OperationMetadata::default(),
            );
            guard.finish(OperationOutcome::Success);
        }
        let state = events.state.lock().await;
        assert_eq!(state.receiver.len(), QUEUE_CAPACITY);
        assert_eq!(
            events.dropped.load(Ordering::Relaxed),
            QUEUE_CAPACITY as u64
        );
    }

    #[tokio::test]
    async fn closing_turn_admission_suppresses_late_guard_drop() {
        let (sender, mut receiver) = mpsc::channel(QUEUE_CAPACITY);
        let sink = Arc::new(Sink {
            sender,
            dropped: Arc::new(AtomicU64::new(0)),
            accepting: StdMutex::new(true),
        });
        let observation = OperationObservation::new(OperationCaptureMode::Metadata, sink.clone());
        let guard = observation.start(
            "thread_1".into(),
            "turn_1".into(),
            OperationKind::Model,
            OperationMetadata::default(),
        );
        assert!(receiver.try_recv().is_ok());
        sink.turn_finished("thread_1", "turn_1").await;
        drop(guard);
        assert!(receiver.try_recv().is_err());
    }
}
