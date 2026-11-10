//! Captured Code Mode origins and the replay fence for a single SDK recovery.
//! No receipt is inferred here. Python validates each original outcome before
//! attaching the seed. Request interceptors retain the immutable set on retry.
use std::collections::{HashMap, HashSet};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::task::{Context, Poll};

use codex_core_api::Config;
use codex_extension_api::{
    ExtensionDataInit, ExtensionFuture, ModelRequestContributor, ModelRequestInput,
    ModelRequestKind, ModelResponseError, ModelResponseInterceptor, ModelResponseStream,
    ResponseEvent, ThreadLifecycleContributor, ThreadStartInput, ThreadStopInput, ToolCallSource,
    ToolLifecycleContributor, ToolLifecycleFuture, ToolStartInput, TurnLifecycleContributor,
    TurnStartInput,
};
use codex_protocol::models::ResponseItem;
use futures_core::Stream;
use serde::Serialize;

pub struct Seed {
    turn_id: String,
    original_ids: Arc<HashSet<String>>,
    consumed: AtomicBool,
}

impl Seed {
    pub fn attach(init: &mut ExtensionDataInit, turn_id: &str, ids: Vec<String>) {
        init.insert(Self {
            turn_id: turn_id.to_owned(),
            original_ids: Arc::new(ids.into_iter().collect()),
            consumed: AtomicBool::new(false),
        });
    }
}

#[derive(Clone, Serialize)]
pub struct Origin {
    turn_id: String,
    originating_item_id: String,
}

#[derive(Default)]
pub struct Guard {
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    current: Option<Arc<HashSet<String>>>,
    reconciled: Option<Arc<HashSet<String>>>,
    origins: HashMap<String, Origin>,
}

impl Guard {
    pub(crate) fn set_reconciled(&self, ids: HashSet<String>) {
        self.state
            .lock()
            .expect("recovery guard poisoned")
            .reconciled = (!ids.is_empty()).then(|| Arc::new(ids));
    }

    pub(crate) fn has_active_recovery(&self) -> bool {
        self.state
            .lock()
            .expect("recovery guard poisoned")
            .current
            .is_some()
    }

    pub fn origins(&self) -> HashMap<String, Origin> {
        self.state
            .lock()
            .expect("recovery guard poisoned")
            .origins
            .clone()
    }
}

#[derive(Debug, Default)]
pub struct RecoveryContributor {
    threads: Mutex<HashMap<String, Weak<Guard>>>,
}

impl ThreadLifecycleContributor<Config> for RecoveryContributor {
    fn on_thread_start<'a>(
        &'a self,
        input: ThreadStartInput<'a, Config>,
    ) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            let guard = input.thread_store.get_or_init(Guard::default);
            self.threads
                .lock()
                .expect("recovery index poisoned")
                .insert(
                    input.thread_store.level_id().to_owned(),
                    Arc::downgrade(&guard),
                );
        })
    }

    fn on_thread_stop<'a>(&'a self, input: ThreadStopInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            self.threads
                .lock()
                .expect("recovery index poisoned")
                .remove(input.thread_store.level_id());
        })
    }
}

impl TurnLifecycleContributor for RecoveryContributor {
    fn on_turn_start<'a>(&'a self, input: TurnStartInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            let Some(guard) = input.thread_store.get::<Guard>() else {
                return;
            };
            let mut state = guard.state.lock().expect("recovery guard poisoned");
            state.origins.clear();
            // Native can retain the initializer for subsequent turns. It may
            // only authorize the exact recovery submission once, not a new turn.
            state.current = input.turn_store.get::<Seed>().and_then(|seed| {
                (seed.turn_id == input.turn_id && !seed.consumed.swap(true, Ordering::AcqRel))
                    .then(|| seed.original_ids.clone())
            });
        })
    }
}

impl ToolLifecycleContributor for RecoveryContributor {
    fn on_tool_start<'a>(&'a self, input: ToolStartInput<'a>) -> ToolLifecycleFuture<'a> {
        Box::pin(async move {
            // This observation binds a nested action to its real model wrapper;
            // it does not assert success, authorize execution, or supply output.
            if matches!(input.source, ToolCallSource::CodeMode { .. })
                && input.root_turn_id == Some(input.turn_id)
                && let Some(origin) = input.originating_item_id
                && let Some(guard) = input.thread_store.get::<Guard>()
            {
                guard
                    .state
                    .lock()
                    .expect("recovery guard poisoned")
                    .origins
                    .insert(
                        input.call_id.to_owned(),
                        Origin {
                            turn_id: input.turn_id.to_owned(),
                            originating_item_id: origin.to_string(),
                        },
                    );
            }
        })
    }
}

impl ModelRequestContributor for RecoveryContributor {
    fn request(&self, input: ModelRequestInput<'_>) -> Option<Box<dyn ModelResponseInterceptor>> {
        if input.kind != ModelRequestKind::Generation {
            return None;
        }
        let guard = self
            .threads
            .lock()
            .expect("recovery index poisoned")
            .get(input.thread_id)?
            .upgrade()?;
        let state = guard.state.lock().expect("recovery guard poisoned");
        let original_ids = match (&state.current, &state.reconciled) {
            (Some(current), Some(reconciled)) => {
                Arc::new(current.union(reconciled).cloned().collect())
            }
            (Some(ids), None) | (None, Some(ids)) => Arc::clone(ids),
            (None, None) => return None,
        };
        Some(Box::new(Request(original_ids)))
    }
}

struct Request(Arc<HashSet<String>>);

impl ModelResponseInterceptor for Request {
    fn intercept(self: Box<Self>, stream: ModelResponseStream) -> ModelResponseStream {
        Box::pin(GuardedStream {
            original_ids: self.0,
            stream,
            rejected: false,
        })
    }
}

struct GuardedStream {
    original_ids: Arc<HashSet<String>>,
    stream: ModelResponseStream,
    rejected: bool,
}

impl Stream for GuardedStream {
    type Item = Result<ResponseEvent, ModelResponseError>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.get_mut();
        // Core can keep polling after Err. Only our own rejection terminates
        // this response: no later item may reach interception or seal a Step.
        if this.rejected {
            return Poll::Ready(None);
        }
        let next = this.stream.as_mut().poll_next(cx);
        if let Poll::Ready(Some(Ok(
            ResponseEvent::OutputItemAdded(item) | ResponseEvent::OutputItemDone(item),
        ))) = &next
        {
            let id = match item {
                ResponseItem::FunctionCall { call_id, .. }
                | ResponseItem::CustomToolCall { call_id, .. }
                | ResponseItem::CustomToolCallOutput { call_id, .. }
                | ResponseItem::ComputerCall { call_id, .. }
                | ResponseItem::ComputerCallOutput { call_id, .. } => Some(call_id.as_str()),
                ResponseItem::FunctionCallOutput { call_id, .. } => call_id.as_deref(),
                _ => None,
            };
            if id.is_some_and(|id| this.original_ids.contains(id)) {
                this.rejected = true;
                return Poll::Ready(Some(Err(ModelResponseError::InvalidRequest {
                    message: "Cannot reuse an original call identity during recovery".into(),
                })));
            }
        }
        next
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    use futures_util::{StreamExt, stream};
    use serde_json::json;

    fn call(id: &str) -> ResponseItem {
        serde_json::from_value(json!({
            "type":"function_call", "name":"reserve", "call_id":id,
            "arguments":r#"{"quantity":4}"#,
        }))
        .unwrap()
    }

    fn completed() -> ResponseEvent {
        ResponseEvent::Completed {
            response_id: "provider-response".into(),
            token_usage: None,
            usage_metadata: None,
            end_turn: Some(true),
        }
    }

    #[tokio::test]
    async fn local_rejection_fuses_at_added_or_done_before_fresh_items_and_completed() {
        for added in [true, false] {
            let consumed = Arc::new(AtomicUsize::new(0));
            let counter = consumed.clone();
            let forbidden = if added {
                ResponseEvent::OutputItemAdded(call("original"))
            } else {
                ResponseEvent::OutputItemDone(call("original"))
            };
            let upstream = stream::iter(vec![
                Ok(ResponseEvent::Created {
                    response_id: Some("provider-response".into()),
                }),
                Ok(forbidden),
                // Neither a fresh ID (callback effects) nor Completed (Step)
                // may escape after locally rejecting this response.
                Ok(ResponseEvent::OutputItemDone(call("fresh"))),
                Ok(completed()),
            ])
            .inspect(move |_| {
                counter.fetch_add(1, Ordering::Relaxed);
            });
            let mut guarded = Box::new(Request(Arc::new(HashSet::from(["original".into()]))))
                .intercept(Box::pin(upstream));
            assert!(matches!(
                guarded.next().await,
                Some(Ok(ResponseEvent::Created { .. }))
            ));
            assert!(matches!(
                guarded.next().await,
                Some(Err(ModelResponseError::InvalidRequest { message }))
                    if message == "Cannot reuse an original call identity during recovery"
            ));
            assert_eq!(consumed.load(Ordering::Relaxed), 2);
            // Model Core's continued polling, independent of consumer teardown
            // scheduling. EOF must remain stable and must not poll upstream.
            assert!(guarded.next().await.is_none());
            assert!(guarded.next().await.is_none());
            assert_eq!(consumed.load(Ordering::Relaxed), 2);
        }
    }

    #[tokio::test]
    async fn upstream_error_with_same_type_and_text_keeps_upstream_event_semantics() {
        let upstream = stream::iter(vec![
            Err(ModelResponseError::InvalidRequest {
                message: "Cannot reuse an original call identity during recovery".into(),
            }),
            Ok(ResponseEvent::OutputItemDone(call("fresh"))),
            Ok(completed()),
        ]);
        let mut guarded = Box::new(Request(Arc::new(HashSet::from(["original".into()]))))
            .intercept(Box::pin(upstream));
        assert!(matches!(
            guarded.next().await,
            Some(Err(ModelResponseError::InvalidRequest { message }))
                if message == "Cannot reuse an original call identity during recovery"
        ));
        assert!(matches!(
            guarded.next().await,
            Some(Ok(ResponseEvent::OutputItemDone(ResponseItem::FunctionCall { call_id, .. })))
                if call_id == "fresh"
        ));
        assert!(matches!(
            guarded.next().await,
            Some(Ok(ResponseEvent::Completed { response_id, end_turn: Some(true), .. }))
                if response_id == "provider-response"
        ));
        assert!(guarded.next().await.is_none());
    }
    #[tokio::test]
    async fn recovery_fences_original_computer_calls_and_outputs_before_forwarding() {
        let computer: ResponseItem = serde_json::from_value(json!({
            "type":"computer_call", "call_id":"original", "actions":[{"type":"click","x":1,"y":2}]
        }))
        .unwrap();
        let output: ResponseItem = serde_json::from_value(json!({
            "type":"computer_call_output", "call_id":"original",
            "output":{"type":"computer_screenshot","image_url":"data:image/png;base64,AA=="},
            "acknowledged_safety_checks":[]
        }))
        .unwrap();
        for item in [computer, output] {
            for added in [true, false] {
                let event = if added {
                    ResponseEvent::OutputItemAdded(item.clone())
                } else {
                    ResponseEvent::OutputItemDone(item.clone())
                };
                let upstream = stream::iter(vec![
                    Ok(event),
                    Ok(ResponseEvent::OutputItemDone(call("fresh"))),
                    Ok(completed()),
                ]);
                let mut guarded = Box::new(Request(Arc::new(HashSet::from(["original".into()]))))
                    .intercept(Box::pin(upstream));
                assert!(matches!(
                    guarded.next().await,
                    Some(Err(ModelResponseError::InvalidRequest { .. }))
                ));
                assert!(guarded.next().await.is_none());
                assert!(guarded.next().await.is_none());
            }
        }
    }
}
