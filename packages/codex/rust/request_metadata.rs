//! Bounded passive metadata delivery, separate from authority-bearing requests.
use codex_extension_api::{
    ExtensionData, ExtensionDataInit, ModelRequestObservation, ModelRequestObservationRoute,
    ModelRequestObserver,
};
use serde_json::{Value, json};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Weak};
use tokio::sync::{Mutex, mpsc};

const CAPACITY: usize = 256;

#[derive(Debug)]
pub(crate) struct Queue {
    sender: mpsc::Sender<Value>,
    receiver: Mutex<mpsc::Receiver<Value>>,
}
impl Default for Queue {
    fn default() -> Self {
        let (sender, receiver) = mpsc::channel(CAPACITY);
        Self {
            sender,
            receiver: Mutex::new(receiver),
        }
    }
}
impl Queue {
    /// Seed only managed root submissions; tokens never enter provider metadata.
    pub(crate) fn seed(
        self: &Arc<Self>,
        init: &mut ExtensionDataInit,
        submission: String,
        thread_id: String,
    ) {
        if submission.is_empty()
            || submission.len() > 64
            || !submission
                .bytes()
                .all(|c| c.is_ascii_hexdigit() || c == b'-')
        {
            return;
        }
        let route = Arc::new(Route {
            queue: Arc::downgrade(self),
            submission,
            thread_id,
            live: AtomicBool::new(true),
        });
        init.insert(ModelRequestObservationRoute(route.clone()));
        init.insert(route);
    }
    pub(crate) fn try_next(&self) -> Option<Value> {
        self.receiver.try_lock().ok()?.try_recv().ok()
    }
    pub(crate) async fn next(&self) -> Value {
        self.receiver
            .lock()
            .await
            .recv()
            .await
            .unwrap_or(Value::Null)
    }
}

#[derive(Debug)]
struct Route {
    queue: Weak<Queue>,
    submission: String,
    thread_id: String,
    live: AtomicBool,
}
impl ModelRequestObserver for Route {
    fn observe(&self, observation: ModelRequestObservation) {
        if !self.live.load(Ordering::Acquire) {
            return;
        }
        let Some(queue) = self.queue.upgrade() else {
            return;
        };
        let value = observation.to_bridge_value();
        if value["thread_id"].as_str() != Some(self.thread_id.as_str()) {
            return;
        }
        let notice = json!({
            "id": value["turn_id"],
            "msg": { "type": "_sdk_request_metadata", "submission_id": self.submission, "observation": value },
        });
        // Full/closed delivery is harmless. No callback, await, current turn or
        // provider-visible metadata participates in this internal route.
        let _ = queue.sender.try_send(notice);
    }
}

pub(crate) fn retire(store: &ExtensionData) {
    if let Some(route) = store.get::<Arc<Route>>() {
        route.live.store(false, Ordering::Release);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_extension_api::{ModelRequestDispatching, ModelRequestTransport, ModelRequestWire};
    fn event() -> ModelRequestObservation {
        ModelRequestObservation::Dispatching(ModelRequestDispatching {
            request_id: "request".into(),
            thread_id: "thread".into(),
            turn_id: "same-turn".into(),
            model: "model".into(),
            reasoning_effort: None,
            reasoning_summary: None,
            service_tier: None,
            wire: ModelRequestWire::Responses,
            transport: ModelRequestTransport::HttpSse,
        })
    }
    #[tokio::test]
    async fn exact_submission_full_closed_and_retired_delivery_is_passive() {
        let queue = Arc::new(Queue::default());
        let mut init = ExtensionDataInit::new();
        queue.seed(
            &mut init,
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            "thread".into(),
        );
        let inherited = ExtensionData::new_with_init("child-turn", init.clone());
        let store = ExtensionData::new_with_init("same-turn", init);
        let route = store.get::<ModelRequestObservationRoute>().unwrap();
        let mut child = event();
        if let ModelRequestObservation::Dispatching(event) = &mut child {
            event.thread_id = "child-thread".into();
        }
        inherited
            .get::<ModelRequestObservationRoute>()
            .unwrap()
            .0
            .observe(child);
        assert!(queue.try_next().is_none());
        for _ in 0..CAPACITY + 1 {
            route.0.observe(event());
        }
        let mut receiver = queue.receiver.lock().await;
        for _ in 0..CAPACITY {
            let notice = receiver.try_recv().unwrap();
            assert_eq!(
                notice["msg"]["submission_id"],
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            );
        }
        assert!(receiver.try_recv().is_err());
        retire(&store);
        route.0.observe(event());
        assert!(receiver.try_recv().is_err());
        let mut later = ExtensionDataInit::new();
        queue.seed(
            &mut later,
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into(),
            "thread".into(),
        );
        let later_route = later.get::<ModelRequestObservationRoute>().unwrap();
        later_route.0.observe(event());
        route.0.observe(event());
        assert_eq!(
            receiver.try_recv().unwrap()["msg"]["submission_id"],
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        );
        assert!(receiver.try_recv().is_err());
        receiver.close();
        drop(receiver);
        later_route.0.observe(event());
        drop(queue);
        route.0.observe(event());
    }
}
