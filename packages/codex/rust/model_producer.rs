//! Request-scoped application model output; Core remains the turn and tool owner.
use super::*;
use codex_api::{ApiError, ApplicationModelStream, ResponseEvent};
use codex_extension_api::{
    ModelRequestContributor, ModelRequestInput, ModelResponseInterceptor, ModelResponseStream,
    ModelToolAdmission,
};
use futures_util::StreamExt;
use futures_util::future::BoxFuture;
use tokio::sync::mpsc;

const FAILURE: &str = "Native model producer failed or was cancelled";
const STREAM_CAPACITY: usize = 8;
type StreamResult = std::result::Result<Vec<ResponseEvent>, ApiError>;
type Reply = std::result::Result<Response, String>;

enum Response {
    Complete(ModelReply),
    Stream {
        events: mpsc::Receiver<ResponseEvent>,
        finish: oneshot::Receiver<StreamResult>,
    },
}

enum Delivery {
    Waiting(oneshot::Sender<Reply>),
    Streaming {
        events: mpsc::Sender<ResponseEvent>,
        finish: oneshot::Sender<StreamResult>,
        projection: ApplicationModelStream,
    },
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelReply {
    response_id: String,
    output: Vec<Value>,
    #[serde(default)]
    usage: Option<codex_protocol::protocol::TokenUsage>,
    #[serde(default)]
    original_usage: Option<OriginalResponsesUsage>,
    #[serde(default)]
    raw_chat_completion: Option<serde_json::Map<String, Value>>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OriginalResponsesUsage {
    response_id: String,
    #[serde(default)]
    upstream_request_id: Option<String>,
    usage: serde_json::Map<String, Value>,
}
#[derive(Default)]
pub(super) struct State {
    enabled: bool,
    retired: bool,
    pending: HashMap<u64, (Arc<Ticket>, Delivery)>,
}
impl State {
    fn accepts(&self, id: u64) -> bool {
        self.pending.get(&id).is_some_and(|(ticket, delivery)| {
            ticket.valid(self)
                && match delivery {
                    Delivery::Waiting(reply) => !reply.is_closed(),
                    Delivery::Streaming { events, .. } => !events.is_closed(),
                }
        })
    }
}
struct Ticket {
    tools: Arc<PythonTools>,
    receipt: Arc<TurnAdmission>,
    generation: Arc<Generation>,
}
impl Ticket {
    fn valid(&self, state: &State) -> bool {
        state.enabled
            && !state.retired
            && self.receipt.is_current()
            && self.tools.admits_generation(&self.generation)
            && *self.receipt.state.borrow() == AdmissionState::Pending
    }
}

struct Lease {
    tools: Arc<PythonTools>,
    id: u64,
}

impl Drop for Lease {
    fn drop(&mut self) {
        self.tools
            .model_producer
            .lock()
            .expect("Model producer poisoned")
            .pending
            .remove(&self.id);
    }
}

struct StreamCursor {
    events: mpsc::Receiver<ResponseEvent>,
    finish: Option<oneshot::Receiver<StreamResult>>,
    accepted: std::vec::IntoIter<ResponseEvent>,
    status: tokio::sync::watch::Receiver<AdmissionState>,
    ticket: Arc<Ticket>,
    _lease: Lease,
}

impl StreamCursor {
    async fn next(&mut self) -> std::result::Result<Option<ResponseEvent>, ApiError> {
        let failure = || ApiError::InvalidRequest {
            message: FAILURE.into(),
        };
        let event = if let Some(event) = self.accepted.next() {
            event
        } else if self.finish.is_some() {
            let event = tokio::select! { biased;
                _ = self.status.wait_for(|status| *status != AdmissionState::Pending) => return Err(failure()),
                event = self.events.recv() => event,
            };
            match event {
                Some(event) => event,
                None => {
                    let finish = self.finish.take().expect("unfinished model stream");
                    self.accepted = finish.await.map_err(|_| failure())??.into_iter();
                    self.accepted.next().ok_or_else(failure)?
                }
            }
        } else {
            return Ok(None);
        };
        let state = self
            .ticket
            .tools
            .model_producer
            .lock()
            .expect("Model producer poisoned");
        if !self.ticket.valid(&state) {
            return Err(failure());
        }
        Ok(Some(event))
    }
}

impl PythonTools {
    pub(crate) fn enable_model_producer(&self, enabled: bool) -> Result<()> {
        let mut state = self.model_producer.lock().expect("Model producer poisoned");
        ensure!(!state.retired, "Model producer owner is retired");
        state.enabled = enabled;
        if !enabled {
            state.pending.clear();
        }
        Ok(())
    }
    pub(crate) fn check_model_producer(&self, id: u64) -> bool {
        let state = self.model_producer.lock().expect("Model producer poisoned");
        state.accepts(id)
    }
    pub(crate) fn start_model_producer_stream(&self, id: u64, observe_raw: bool) -> bool {
        let mut state = self.model_producer.lock().expect("Model producer poisoned");
        if !state.accepts(id) {
            return false;
        }
        let (_, delivery) = state.pending.get_mut(&id).expect("checked model request");
        if !matches!(delivery, Delivery::Waiting(_)) {
            return false;
        }
        let (events, receive_events) = mpsc::channel(STREAM_CAPACITY);
        let (finish, receive_finish) = oneshot::channel();
        let Delivery::Waiting(reply) = std::mem::replace(
            delivery,
            Delivery::Streaming {
                events,
                finish,
                projection: ApplicationModelStream::new(observe_raw),
            },
        ) else {
            unreachable!("model request has not started streaming")
        };
        reply
            .send(Ok(Response::Stream {
                events: receive_events,
                finish: receive_finish,
            }))
            .is_ok()
    }
    pub(crate) async fn push_model_producer_event(&self, id: u64, data: &str) -> bool {
        let (projected, sender, continue_stream) = {
            let mut state = self.model_producer.lock().expect("Model producer poisoned");
            if !state.accepts(id) {
                return false;
            }
            let (
                _,
                Delivery::Streaming {
                    events, projection, ..
                },
            ) = state.pending.get_mut(&id).expect("checked model request")
            else {
                return false;
            };
            let (projected, continue_stream) = projection.push(data);
            (projected, events.clone(), continue_stream)
        };
        for event in projected {
            let Ok(permit) = sender.reserve().await else {
                return false;
            };
            let state = self.model_producer.lock().expect("Model producer poisoned");
            if !state.accepts(id) {
                return false;
            }
            permit.send(event);
        }
        continue_stream
    }
    pub(crate) fn reply_model_producer(&self, id: u64, data: Option<&str>, error: bool) -> bool {
        let mut state = self.model_producer.lock().expect("Model producer poisoned");
        if !state.accepts(id) {
            return false;
        }
        let (_, delivery) = state.pending.remove(&id).expect("checked model request");
        let reply = match delivery {
            Delivery::Waiting(reply) => reply,
            Delivery::Streaming {
                projection, finish, ..
            } => {
                return finish
                    .send(projection.finish(error.then_some(FAILURE)))
                    .is_ok();
            }
        };
        let result = if error {
            Err(FAILURE.into())
        } else {
            data.ok_or(FAILURE)
                .and_then(|data| serde_json::from_str::<ModelReply>(data).map_err(|_| FAILURE))
                .and_then(|reply| {
                    if reply.response_id.trim().is_empty() {
                        Err(FAILURE)
                    } else {
                        Ok(reply)
                    }
                })
                .map(Response::Complete)
                .map_err(str::to_owned)
        };
        reply.send(result).is_ok()
    }
    pub(super) fn retire_model_producer(&self) {
        let mut state = self.model_producer.lock().expect("Model producer poisoned");
        state.retired = true;
        state.pending.clear();
    }
}
#[derive(Debug)]
pub(crate) struct Contributor(pub(crate) Arc<crate::native_interception::Boundary>);
impl ModelRequestContributor for Contributor {
    fn request(&self, input: ModelRequestInput<'_>) -> Option<Box<dyn ModelResponseInterceptor>> {
        let tools = self.0.model_producer_tools(input.thread_id)?;
        if !tools
            .model_producer
            .lock()
            .expect("Model producer poisoned")
            .enabled
        {
            return None;
        }
        Some(Box::new(Producer(tools)))
    }
}
struct Producer(Arc<PythonTools>);
impl ModelResponseInterceptor for Producer {
    fn produces_response(&self) -> bool {
        true
    }
    fn intercept(self: Box<Self>, stream: ModelResponseStream) -> ModelResponseStream {
        stream
    }
    fn produce_response<'a>(
        &'a self,
        request: Value,
        admission: Option<Arc<dyn ModelToolAdmission>>,
    ) -> Option<BoxFuture<'a, std::result::Result<ModelResponseStream, String>>> {
        let tools = self.0.clone();
        Some(Box::pin(async move {
            let admission = admission.ok_or(FAILURE)?;
            let receipt = admission
                .turn_store()
                .get::<Arc<TurnAdmission>>()
                .ok_or(FAILURE)?
                .as_ref()
                .clone();
            if !receipt
                .owner
                .upgrade()
                .is_some_and(|owner| Arc::ptr_eq(&owner, &tools))
            {
                return Err(FAILURE.into());
            }
            let ticket = Arc::new(Ticket {
                generation: tools
                    .generation_for_step(admission.step_store())
                    .map_err(|_| FAILURE)?,
                tools: tools.clone(),
                receipt: receipt.clone(),
            });
            let id = tools.family.next_invocation.fetch_add(1, Ordering::Relaxed);
            let (reply, receive) = oneshot::channel();
            {
                let mut state = tools
                    .model_producer
                    .lock()
                    .expect("Model producer poisoned");
                if !ticket.valid(&state) {
                    return Err(FAILURE.into());
                }
                state
                    .pending
                    .insert(id, (ticket.clone(), Delivery::Waiting(reply)));
            }
            let lease = Lease {
                tools: tools.clone(),
                id,
            };
            let mut event = json!({"id":receipt.turn_id,"msg":{"type":"_sdk_model_produce","turn_id":receipt.turn_id,"thread":admission.thread_id(),"instance":tools.instance,"request_id":id,"generation":ticket.generation.id,"data":request}});
            if let Some(profile) = tools
                .source_profile(admission.step_store())
                .map_err(|_| FAILURE)?
            {
                event["msg"]["profile_id"] = json!(profile.profile_id);
                event["msg"]["profile_epoch"] = json!(profile.epoch);
            }
            event["msg"]["capture_raw_usage"] = json!(
                tools
                    .interception
                    .get()
                    .and_then(|control| control.upgrade())
                    .is_some_and(|control| control.raw_usage_active())
            );
            tools
                .family
                .requests
                .send(QueuedRequest::Prepared(event))
                .map_err(|_| FAILURE)?;
            let mut status = receipt.state.subscribe();
            let reply = tokio::select! { biased;
                _ = status.wait_for(|status| *status != AdmissionState::Pending) => return Err(FAILURE.into()),
                reply = receive => reply.map_err(|_| FAILURE)??,
            };
            if !ticket.valid(
                &tools
                    .model_producer
                    .lock()
                    .expect("Model producer poisoned"),
            ) {
                return Err(FAILURE.into());
            }
            match reply {
                Response::Complete(reply) => {
                    if let Some(id) = reply
                        .raw_chat_completion
                        .as_ref()
                        .and_then(|raw| raw.get("id"))
                        .and_then(Value::as_str)
                        .filter(|id| !id.trim().is_empty())
                    {
                        if id != reply.response_id {
                            return Err(FAILURE.into());
                        }
                    }
                    let mut events = codex_extension_api::completed_model_response(
                        reply.response_id,
                        reply.output,
                        reply.usage,
                    );
                    if let Some(original) = reply.original_usage {
                        if let Some(Ok(codex_extension_api::ResponseEvent::Completed {
                            response_id,
                            usage_metadata,
                            ..
                        })) = events.last_mut()
                        {
                            if original.response_id != *response_id {
                                return Err(FAILURE.into());
                            }
                            let raw_usage = Value::Object(original.usage);
                            let count = |value: &Value| value.as_i64().filter(|count| *count >= 0);
                            let received_usage = codex_protocol::protocol::ReceivedResponseUsage {
                                input_tokens: raw_usage.get("input_tokens").and_then(count),
                                cached_input_tokens: raw_usage
                                    .pointer("/input_tokens_details/cached_tokens")
                                    .and_then(count),
                                cache_write_input_tokens: raw_usage
                                    .pointer("/input_tokens_details/cache_write_tokens")
                                    .and_then(count),
                                output_tokens: raw_usage.get("output_tokens").and_then(count),
                                reasoning_output_tokens: raw_usage
                                    .pointer("/output_tokens_details/reasoning_tokens")
                                    .and_then(count),
                                total_tokens: raw_usage.get("total_tokens").and_then(count),
                            };
                            let observation = codex_protocol::protocol::ResponseObservation {
                                response_id: Some(original.response_id),
                                upstream_request_id: original.upstream_request_id,
                                status:
                                    codex_protocol::protocol::ResponseObservationStatus::Completed,
                                incomplete_reason: None,
                                usage: Some(received_usage),
                            };
                            usage_metadata.get_or_insert_default().metadata = Some(raw_usage);
                            events.insert(
                                events.len() - 1,
                                Ok(codex_extension_api::ResponseEvent::ResponseObserved(
                                    observation,
                                )),
                            );
                        }
                    }
                    if let Some(raw) = reply.raw_chat_completion {
                        events.insert(
                            events.len() - 1,
                            Ok(codex_api::ResponseEvent::RawProvider(
                                codex_protocol::protocol::SdkRawProviderEvent {
                                    source: codex_protocol::protocol::SdkRawProviderSource::ChatCompletions,
                                    transport: codex_protocol::protocol::ResponsesEventTransport::Json,
                                    payload: Value::Object(raw),
                                },
                            )),
                        );
                    }
                    Ok(futures_util::stream::iter(events).boxed())
                }
                Response::Stream { events, finish } => {
                    let cursor = StreamCursor {
                        events,
                        finish: Some(finish),
                        accepted: Vec::new().into_iter(),
                        status,
                        ticket,
                        _lease: lease,
                    };
                    let stream =
                        futures_util::stream::try_unfold(cursor, |mut cursor| async move {
                            let event = cursor.next().await?;
                            Ok(event.map(|event| (event, cursor)))
                        });
                    Ok(stream.boxed())
                }
            }
        }))
    }
}
