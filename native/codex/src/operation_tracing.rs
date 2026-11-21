//! Optional, passive metadata observations for SDK-owned ephemeral root threads.
//! Stock request streams and tool execution remain unchanged. Warmups, child threads,
//! content capture, transport-level timing, and generic tool outputs are out of scope.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::time::{SystemTime, UNIX_EPOCH};

use codex_extension_api::{
    ExtensionData, ModelRequestContributor, ModelRequestInput, ModelRequestKind,
    ModelResponseError, ModelResponseInterceptor, ModelResponseStream, ResponseEvent,
    ToolCallOutcome, ToolFinishInput, ToolLifecycleContributor, ToolLifecycleFuture,
    ToolStartInput,
};
use codex_protocol::ToolName;
use codex_protocol::protocol::TokenUsage;
use futures::StreamExt;
use serde_json::{Value, json};

const MAX_EVENTS: usize = 512;
const MAX_STRING_BYTES: usize = 256;

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| u64::try_from(value.as_millis()).unwrap_or(u64::MAX))
        .unwrap_or_default()
}

#[derive(Debug, Default)]
pub(crate) struct Buffer {
    events: Mutex<Events>,
    sequence: AtomicU64,
}

#[derive(Debug, Default)]
struct Events {
    items: Vec<Value>,
    dropped: bool,
}

impl Buffer {
    fn push(&self, event: Value) {
        let mut events = self
            .events
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if events.items.len() < MAX_EVENTS {
            events.items.push(event);
        } else {
            events.dropped = true;
        }
    }

    fn bounded(&self, value: &str) -> bool {
        let valid = !value.is_empty() && value.len() <= MAX_STRING_BYTES;
        if !valid {
            self.events
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .dropped = true;
        }
        valid
    }

    pub(crate) fn drain(&self) -> Option<String> {
        let events = {
            let mut buffered = self
                .events
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let mut events = std::mem::take(&mut buffered.items);
            if std::mem::take(&mut buffered.dropped) {
                events.push(json!({"kind": "dropped"}));
            }
            events
        };
        if events.is_empty() {
            return None;
        }
        Some(serde_json::to_string(&events).unwrap_or_else(|_| "[{\"kind\":\"dropped\"}]".into()))
    }

    fn tool(&self, phase: &str, call_id: &str, name: &ToolName, outcome: Option<&str>) {
        if !self.bounded(call_id)
            || !self.bounded(&name.name)
            || name
                .namespace
                .as_deref()
                .is_some_and(|value| !self.bounded(value))
        {
            return;
        }
        let mut event = json!({
            "kind": "tool", "phase": phase, "id": call_id, "at": now(),
            "name": name.name, "namespace": name.namespace,
        });
        if let Some(outcome) = outcome {
            event["outcome"] = json!(outcome);
        }
        self.push(event);
    }
}

#[derive(Debug, Default)]
pub(crate) struct Observers {
    roots: Mutex<HashMap<String, Weak<Buffer>>>,
}

impl Observers {
    pub(crate) fn root(self: &Arc<Self>, id: String) -> Arc<Root> {
        Arc::new(Root {
            id,
            observers: self.clone(),
            buffer: Arc::new(Buffer::default()),
            enabled: AtomicBool::new(false),
        })
    }

    fn buffer(&self, id: &str) -> Option<Arc<Buffer>> {
        self.roots
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .get(id)?
            .upgrade()
    }
}

pub(crate) struct Root {
    id: String,
    observers: Arc<Observers>,
    pub(crate) buffer: Arc<Buffer>,
    enabled: AtomicBool,
}

impl Root {
    pub(crate) fn enable(&self, store: &ExtensionData) {
        if !self.enabled.swap(true, Ordering::AcqRel) {
            store.insert_arc(self.buffer.clone());
            self.observers
                .roots
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .insert(self.id.clone(), Arc::downgrade(&self.buffer));
        }
    }

    pub(crate) fn close(&self) {
        let mut roots = self
            .observers
            .roots
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if roots
            .get(&self.id)
            .is_some_and(|buffer| buffer.ptr_eq(&Arc::downgrade(&self.buffer)))
        {
            roots.remove(&self.id);
        }
    }
}

impl Drop for Root {
    fn drop(&mut self) {
        self.close();
    }
}

struct ModelObservation {
    buffer: Arc<Buffer>,
    id: String,
    finished: bool,
}

impl ModelObservation {
    fn finish(&mut self, outcome: &str, usage: Option<&TokenUsage>) {
        if self.finished {
            return;
        }
        self.finished = true;
        let mut event = json!({
            "kind": "model", "phase": "end", "id": self.id,
            "at": now(), "outcome": outcome,
        });
        if let Some(usage) = usage {
            event["usage"] = json!(usage);
        }
        self.buffer.push(event);
    }
}

impl Drop for ModelObservation {
    fn drop(&mut self) {
        self.finish("interrupted", None);
    }
}

impl ModelResponseInterceptor for ModelObservation {
    fn intercept(self: Box<Self>, stream: ModelResponseStream) -> ModelResponseStream {
        let mut observation = *self;
        // Inspect borrows each event and yields the original event without buffering or rewriting.
        Box::pin(stream.inspect(move |event| match event {
            Ok(ResponseEvent::Completed { token_usage, .. }) => {
                observation.finish("completed", token_usage.as_ref());
            }
            Err(ModelResponseError::ResponseIncomplete(error)) => {
                observation.finish("failed", error.token_usage.as_ref());
            }
            Err(_) => observation.finish("failed", None),
            _ => {}
        }))
    }
}

impl ModelRequestContributor for Observers {
    fn request(&self, input: ModelRequestInput<'_>) -> Option<Box<dyn ModelResponseInterceptor>> {
        if input.kind != ModelRequestKind::Generation {
            return None;
        }
        let buffer = self.buffer(input.thread_id)?;
        if !buffer.bounded(input.model) {
            return None;
        }
        let id = format!("model-{}", buffer.sequence.fetch_add(1, Ordering::Relaxed));
        buffer.push(json!({
            "kind": "model", "phase": "start", "id": id,
            "at": now(), "model": input.model,
        }));
        Some(Box::new(ModelObservation {
            buffer,
            id,
            finished: false,
        }))
    }
}

impl ToolLifecycleContributor for Observers {
    fn on_tool_start<'a>(&'a self, input: ToolStartInput<'a>) -> ToolLifecycleFuture<'a> {
        if let Some(buffer) = input.thread_store.get::<Buffer>() {
            // A handler start can still encounter an approval denial before any effect occurs.
            buffer.tool("start", input.call_id, input.tool_name, None);
        }
        Box::pin(std::future::ready(()))
    }

    fn on_tool_finish<'a>(&'a self, input: ToolFinishInput<'a>) -> ToolLifecycleFuture<'a> {
        if let Some(buffer) = input.thread_store.get::<Buffer>() {
            let outcome = match input.outcome {
                ToolCallOutcome::Completed { success: true } => "completed",
                ToolCallOutcome::Aborted => "interrupted",
                _ => "failed",
            };
            // Stock finish callbacks may arrive without start when policy or cancellation wins.
            buffer.tool("end", input.call_id, input.tool_name, Some(outcome));
        }
        Box::pin(std::future::ready(()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use futures::stream;

    fn request(
        observers: &Observers,
        root: &str,
        kind: ModelRequestKind,
    ) -> Option<Box<dyn ModelResponseInterceptor>> {
        observers.request(ModelRequestInput {
            kind,
            thread_id: root,
            client_metadata: &mut None,
            model: "fixture-model",
        })
    }

    fn drain(buffer: &Buffer) -> Vec<Value> {
        buffer
            .drain()
            .map(|value| serde_json::from_str(&value).unwrap())
            .unwrap_or_default()
    }

    #[tokio::test]
    async fn model_observation_forwards_original_events_and_keeps_only_metadata() {
        let observers = Arc::new(Observers::default());
        let root = observers.root("root".into());
        assert!(request(&observers, "root", ModelRequestKind::Generation).is_none());
        root.enable(&ExtensionData::new("root"));
        assert!(request(&observers, "child", ModelRequestKind::Generation).is_none());
        assert!(request(&observers, "root", ModelRequestKind::Warmup).is_none());
        let interceptor = request(&observers, "root", ModelRequestKind::Generation).unwrap();
        let usage = TokenUsage {
            input_tokens: 11,
            output_tokens: 3,
            total_tokens: 14,
            ..Default::default()
        };
        let mut observed = interceptor.intercept(Box::pin(stream::iter([
            Ok(ResponseEvent::Created {
                response_id: Some("private-response-id".into()),
            }),
            Ok(ResponseEvent::Completed {
                response_id: "private-response-id".into(),
                token_usage: Some(usage),
                usage_metadata: None,
                end_turn: Some(true),
            }),
        ])));
        assert!(
            matches!(observed.next().await, Some(Ok(ResponseEvent::Created { response_id: Some(id) })) if id == "private-response-id")
        );
        assert!(
            matches!(observed.next().await, Some(Ok(ResponseEvent::Completed { response_id, .. })) if response_id == "private-response-id")
        );
        assert!(observed.next().await.is_none());
        drop(observed);
        let events = drain(&root.buffer);
        assert_eq!(events.len(), 2);
        assert_eq!(events[0]["id"], events[1]["id"]);
        assert_eq!(events[0]["model"], "fixture-model");
        assert_eq!(events[1]["usage"]["total_tokens"], 14);
        assert_eq!(events[1]["outcome"], "completed");
        assert!(events[0]["at"].as_u64().unwrap() > 1_000_000_000_000);
        assert!(!format!("{events:?}").contains("private-response-id"));
        assert!(root.buffer.drain().is_none());
    }

    #[tokio::test]
    async fn failure_drop_and_close_remain_isolated_and_drainable() {
        let observers = Arc::new(Observers::default());
        let first = observers.root("first".into());
        let second = observers.root("second".into());
        first.enable(&ExtensionData::new("first"));
        second.enable(&ExtensionData::new("second"));
        let pending = request(&observers, "second", ModelRequestKind::Generation).unwrap();
        let interceptor = request(&observers, "first", ModelRequestKind::Generation).unwrap();
        let mut observed = interceptor.intercept(Box::pin(stream::iter([Err(
            ModelResponseError::Stream("private failure detail".into()),
        )])));
        assert!(
            matches!(observed.next().await, Some(Err(ModelResponseError::Stream(message))) if message == "private failure detail")
        );
        drop(observed);
        first.close();
        assert!(request(&observers, "first", ModelRequestKind::Generation).is_none());
        let events = drain(&first.buffer);
        assert_eq!(events.len(), 2);
        assert_eq!(events[1]["outcome"], "failed");
        assert!(!format!("{events:?}").contains("private failure detail"));
        assert_eq!(drain(&second.buffer).len(), 1);
        second.close();
        drop(pending);
        assert_eq!(drain(&second.buffer)[0]["outcome"], "interrupted");
    }

    #[tokio::test]
    async fn tool_outcomes_preserve_namespace_without_payloads_or_false_success() {
        let observers = Observers::default();
        let store = ExtensionData::new("root");
        let buffer = Arc::new(Buffer::default());
        store.insert_arc(buffer.clone());
        let name = ToolName::namespaced("functions", "lookup");
        let outcomes = [
            ToolCallOutcome::Completed { success: true },
            ToolCallOutcome::Completed { success: false },
            ToolCallOutcome::Blocked,
            ToolCallOutcome::Failed {
                handler_executed: false,
            },
            ToolCallOutcome::Aborted,
        ];
        for (index, outcome) in outcomes.into_iter().enumerate() {
            let id = format!("call-{index}");
            buffer.tool("start", &id, &name, None);
            observers
                .on_tool_finish(ToolFinishInput {
                    session_store: &store,
                    thread_store: &store,
                    turn_store: &store,
                    turn_id: "turn",
                    call_id: &id,
                    tool_name: &name,
                    source: codex_extension_api::ToolCallSource::Direct,
                    outcome,
                })
                .await;
        }
        let events = drain(&buffer);
        let outcomes = ["completed", "failed", "failed", "failed", "interrupted"];
        for (pair, outcome) in events.chunks_exact(2).zip(outcomes) {
            assert_eq!(pair[0]["id"], pair[1]["id"]);
            assert_eq!(pair[0]["namespace"], "functions");
            assert_eq!(pair[1]["namespace"], "functions");
            assert_eq!(pair[1]["name"], "lookup");
            assert_eq!(pair[1]["outcome"], outcome);
            assert!(
                pair.iter()
                    .all(|event| event.get("input").is_none() && event.get("output").is_none())
            );
        }
    }

    #[test]
    fn bounds_events_and_omits_oversized_identifiers_without_truncating() {
        let buffer = Buffer::default();
        buffer.tool(
            "start",
            &"x".repeat(MAX_STRING_BYTES + 1),
            &ToolName::plain("tool"),
            None,
        );
        assert_eq!(drain(&buffer), vec![json!({"kind": "dropped"})]);
        for index in 0..MAX_EVENTS + 2 {
            buffer.tool("start", &index.to_string(), &ToolName::plain("tool"), None);
        }
        let events = drain(&buffer);
        assert_eq!(events.len(), MAX_EVENTS + 1);
        assert_eq!(events.last().unwrap(), &json!({"kind": "dropped"}));
        assert!(buffer.drain().is_none());
    }

    #[test]
    fn concurrent_handlers_and_drains_do_not_drop_observations() {
        let buffer = Arc::new(Buffer::default());
        let completed = Arc::new(AtomicU64::new(0));
        let received = std::thread::scope(|scope| {
            for worker in 0..4 {
                let buffer = buffer.clone();
                let completed = completed.clone();
                scope.spawn(move || {
                    for item in 0..32 {
                        buffer.tool(
                            "start",
                            &format!("{worker}-{item}"),
                            &ToolName::plain("tool"),
                            None,
                        );
                    }
                    completed.fetch_add(1, Ordering::Release);
                });
            }
            let mut events = Vec::new();
            while completed.load(Ordering::Acquire) != 4 {
                events.extend(drain(&buffer));
                std::thread::yield_now();
            }
            events.extend(drain(&buffer));
            events
        });
        assert_eq!(received.len(), 128);
        assert!(received.iter().all(|event| event["kind"] == "tool"));
    }
}
