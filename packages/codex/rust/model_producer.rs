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
pub(super) type Reply = std::result::Result<Response, String>;
pub(super) enum Response {
    Complete(ModelReply),
    Stream {
        events: mpsc::Receiver<ResponseEvent>,
        finish: oneshot::Receiver<StreamResult>,
        upstream_request_id: Option<String>,
    },
}
pub(super) enum Delivery {
    Waiting(oneshot::Sender<Reply>),
    Streaming {
        events: mpsc::Sender<ResponseEvent>,
        finish: oneshot::Sender<StreamResult>,
        projection: ApplicationModelStream,
        observe_raw_chat: bool,
        observe_raw_responses_websocket: bool,
        chat_stream: bool,
        provider_metadata: Option<codex_extension_api::CapturedModelProviderOutput>,
    },
}
impl Delivery {
    pub(super) fn is_closed(&self) -> bool {
        match self {
            Self::Waiting(reply) => reply.is_closed(),
            Self::Streaming { events, .. } => events.is_closed(),
        }
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ModelReply {
    response_id: String,
    output: Vec<Value>,
    #[serde(default)]
    usage: Option<codex_protocol::protocol::TokenUsage>,
    #[serde(default)]
    original_usage: Option<OriginalResponsesUsage>,
    #[serde(default)]
    raw_chat_completion: Option<serde_json::Map<String, Value>>,
    #[serde(default)]
    provider_metadata: Vec<codex_extension_api::ModelOutputProviderMetadata>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OriginalResponsesUsage {
    response_id: String,
    #[serde(default)]
    upstream_request_id: Option<String>,
    usage: serde_json::Map<String, Value>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FailedModelReply {
    failed: bool,
    failure: codex_protocol::model_retry::ModelProducerFailure,
}
#[derive(Default)]
pub(super) struct State {
    enabled: bool,
    retired: bool,
}
pub(super) struct Ticket {
    tools: Arc<PythonTools>,
    receipt: Arc<TurnAdmission>,
    generation: Arc<Generation>,
    provider_metadata: Option<codex_extension_api::CapturedModelProviderOutput>,
}
fn model_provider_metadata_item_matches(captured: &ResponseItem, wire: &Value) -> bool {
    let Ok(wire) = serde_json::from_value::<ResponseItem>(wire.clone()) else {
        return false;
    };
    if captured == &wire {
        return true;
    }
    if !captured.has_same_tool_result_metadata(&wire) {
        return false;
    }
    let mut projected = captured.clone();
    // Core removes this field before sending to a non-OpenAI provider. Keep every
    // model-visible identity and payload field in the exact occurrence check.
    projected.clear_internal_chat_message_metadata_passthrough();
    projected == wire
}

fn capture_stream_metadata(
    data: &str,
    controller: Option<&codex_extension_api::CapturedModelProviderOutput>,
) -> std::result::Result<Option<String>, ()> {
    const FIELD: &str = "__sdk_model_provider_metadata";
    if !data.contains(FIELD) {
        return Ok(None);
    }
    if data.len() > 16 * 1024 * 1024 {
        return Err(());
    }
    let mut frame: Value = serde_json::from_str(data).map_err(|_| ())?;
    let Some(annotation) = frame.as_object_mut().and_then(|frame| frame.remove(FIELD)) else {
        return Ok(None);
    };
    let annotation: codex_extension_api::ModelOutputProviderMetadata =
        serde_json::from_value(annotation).map_err(|_| ())?;
    if frame.get("type").and_then(Value::as_str) != Some("response.output_item.done")
        || frame.get("output_index").and_then(Value::as_u64) != Some(annotation.output_index as u64)
    {
        return Err(());
    }
    let item = serde_json::from_value(frame.get("item").cloned().ok_or(())?).map_err(|_| ())?;
    controller
        .ok_or(())?
        .append_stream_output(annotation.output_index, item, annotation.metadata)
        .map_err(|_| ())?;
    serde_json::to_string(&frame).map(Some).map_err(|_| ())
}

impl State {
    pub(super) fn enabled_for_retry(&self) -> bool {
        self.enabled && !self.retired
    }
}

impl Ticket {
    pub(super) fn admits(&self) -> bool {
        self.valid(
            &self
                .tools
                .model_producer
                .lock()
                .expect("Model callback state poisoned"),
        )
    }
    fn valid(&self, state: &State) -> bool {
        state.enabled
            && !state.retired
            && self.receipt.is_current()
            && self.tools.admits_generation(&self.generation)
            && *self.receipt.state.borrow() == AdmissionState::Pending
    }
}
struct StreamCursor {
    events: mpsc::Receiver<ResponseEvent>,
    finish: Option<oneshot::Receiver<StreamResult>>,
    accepted: std::vec::IntoIter<ResponseEvent>,
    status: tokio::sync::watch::Receiver<AdmissionState>,
    ticket: Arc<Ticket>,
    callback: Arc<CallbackRelease>,
    cancellation: Option<tokio_util::sync::CancellationToken>,
    _pending: PendingPreparation,
    upstream_request_id: Option<String>,
}
impl StreamCursor {
    async fn next(&mut self) -> std::result::Result<Option<ResponseEvent>, ApiError> {
        let failure = || ApiError::InvalidRequest {
            message: FAILURE.into(),
        };
        let mut event = if let Some(event) = self.accepted.next() {
            event
        } else if self.finish.is_some() {
            let cancelled = async {
                if let Some(cancellation) = &self.cancellation {
                    cancellation.cancelled().await;
                } else {
                    std::future::pending::<()>().await;
                }
            };
            let received = tokio::select! { biased;
                _ = self.status.wait_for(|status| *status != AdmissionState::Pending) => Err(failure()),
                _ = cancelled => Err(failure()),
                event = self.events.recv() => Ok(event),
            };
            let received = match received {
                Ok(event) => event,
                Err(error) => {
                    self.events.close();
                    self._pending
                        .tools
                        .retire_preparation(self._pending.id, false);
                    self.callback.wait().await;
                    return Err(error);
                }
            };
            match received {
                Some(event) => event,
                None => {
                    let result = self.finish.take().expect("unfinished model stream").await;
                    self.callback.wait().await;
                    if self.callback.cleanup_failed.load(Ordering::Acquire) {
                        return Err(failure());
                    }
                    self.accepted = result.map_err(|_| failure())??.into_iter();
                    self.accepted.next().ok_or_else(failure)?
                }
            }
        } else {
            return Ok(None);
        };
        if !self.ticket.admits() {
            self.events.close();
            self._pending
                .tools
                .retire_preparation(self._pending.id, false);
            self.callback.wait().await;
            return Err(failure());
        }
        if let (Some(request_id), ResponseEvent::ResponseObserved(observation)) =
            (&self.upstream_request_id, &mut event)
        {
            observation.upstream_request_id = Some(request_id.clone());
        }
        Ok(Some(event))
    }
}

impl PythonTools {
    fn cancel_model_producer_preparations(&self) {
        let ids = {
            let replies = self
                .preparation_replies
                .lock()
                .expect("Python preparation replies poisoned");
            replies
                .as_ref()
                .into_iter()
                .flat_map(|pending| pending.iter())
                .filter_map(|(id, slot)| {
                    matches!(
                        slot.reply.as_ref(),
                        Some(PreparationReply::ModelProducer { .. })
                    )
                    .then_some(*id)
                })
                .collect::<Vec<_>>()
        };
        for id in ids {
            self.retire_preparation(id, false);
        }
    }
    pub(crate) fn enable_model_producer(&self, enabled: bool) -> Result<()> {
        {
            let mut state = self.model_producer.lock().expect("Model producer poisoned");
            ensure!(!state.retired, "Model producer owner is retired");
            state.enabled = enabled;
        }
        if !enabled {
            self.cancel_model_producer_preparations();
        }
        Ok(())
    }
    pub(crate) fn check_model_producer(&self, id: u64) -> bool {
        let replies = self
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        replies
            .as_ref()
            .and_then(|pending| pending.get(&id))
            .is_some_and(|slot| {
                !slot.is_closed()
                    && matches!(slot.reply, Some(PreparationReply::ModelProducer { .. }))
            })
    }
    pub(crate) fn start_model_producer_stream(
        &self,
        id: u64,
        observe_raw: bool,
        chat_stream: bool,
        upstream_request_id: Option<String>,
        responses_websocket_stream: bool,
    ) -> bool {
        if chat_stream && responses_websocket_stream {
            return false;
        }
        let mut replies = self
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        let Some(slot) = replies.as_mut().and_then(|pending| pending.get_mut(&id)) else {
            return false;
        };
        if slot.is_closed() {
            return false;
        }
        let Some(PreparationReply::ModelProducer {
            reply: delivery,
            ticket,
        }) = slot.reply.as_mut()
        else {
            return false;
        };
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
                projection: ApplicationModelStream::new(
                    observe_raw && !chat_stream && !responses_websocket_stream,
                ),
                observe_raw_chat: observe_raw && chat_stream,
                observe_raw_responses_websocket: observe_raw && responses_websocket_stream,
                chat_stream,
                provider_metadata: ticket.provider_metadata.clone(),
            },
        ) else {
            unreachable!()
        };
        reply
            .send(Ok(Response::Stream {
                events: receive_events,
                finish: receive_finish,
                upstream_request_id,
            }))
            .is_ok()
    }
    pub(crate) async fn push_model_producer_event(
        &self,
        id: u64,
        data: &str,
        raw_chat_chunk: bool,
        raw_responses_websocket_frame: bool,
    ) -> bool {
        let (projected, sender, continue_stream) = {
            let mut replies = self
                .preparation_replies
                .lock()
                .expect("Python preparation replies poisoned");
            let Some(slot) = replies.as_mut().and_then(|pending| pending.get_mut(&id)) else {
                return false;
            };
            if slot.is_closed() {
                return false;
            }
            let attempt = slot.callback.model_attempt.as_ref();
            let Some(PreparationReply::ModelProducer {
                reply:
                    Delivery::Streaming {
                        events,
                        projection,
                        observe_raw_chat,
                        observe_raw_responses_websocket,
                        provider_metadata,
                        ..
                    },
                ..
            }) = slot.reply.as_mut()
            else {
                return false;
            };
            let (projected, continue_stream) = if raw_chat_chunk || raw_responses_websocket_frame {
                if (raw_chat_chunk && !*observe_raw_chat)
                    || (raw_responses_websocket_frame && !*observe_raw_responses_websocket)
                    || data.len() > 8 * 1024 * 1024
                {
                    return false;
                }
                use codex_protocol::protocol::{
                    ResponsesEventTransport, SdkRawProviderEvent, SdkRawProviderSource,
                };
                let (source, transport) = if raw_chat_chunk {
                    (
                        SdkRawProviderSource::ChatCompletions,
                        ResponsesEventTransport::Json,
                    )
                } else {
                    (
                        SdkRawProviderSource::Responses,
                        ResponsesEventTransport::Websocket,
                    )
                };
                let Ok(payload) = serde_json::from_str::<serde_json::Map<String, Value>>(data)
                else {
                    return false;
                };
                (
                    vec![ResponseEvent::RawProvider(SdkRawProviderEvent {
                        source,
                        transport,
                        payload: Value::Object(payload),
                    })],
                    true,
                )
            } else {
                let Ok(clean) = capture_stream_metadata(data, provider_metadata.as_ref()) else {
                    return false;
                };
                let had_usage = projection.completed_usage().is_some();
                let (projected, continue_stream) =
                    projection.push(clean.as_deref().unwrap_or(data));
                // Retirement can discard the reply before the iterator settles.
                if !had_usage
                    && let (Some(attempt), Some(usage)) = (attempt, projection.completed_usage())
                {
                    let _ = attempt.report_usage(usage.clone());
                }
                (projected, continue_stream)
            };
            (projected, events.clone(), continue_stream)
        };
        for event in projected {
            let Ok(permit) = sender.reserve().await else {
                return false;
            };
            let replies = self
                .preparation_replies
                .lock()
                .expect("Python preparation replies poisoned");
            if !replies
                .as_ref()
                .and_then(|pending| pending.get(&id))
                .is_some_and(|slot| !slot.is_closed())
            {
                return false;
            }
            permit.send(event);
        }
        continue_stream
    }
    pub(crate) fn reply_model_producer(&self, id: u64, data: Option<&str>, error: bool) -> bool {
        let mut replies = self
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        let Some(slot) = replies.as_mut().and_then(|pending| pending.get_mut(&id)) else {
            return false;
        };
        if slot.is_closed() || !matches!(slot.reply, Some(PreparationReply::ModelProducer { .. })) {
            return false;
        }
        let attempt = slot.callback.model_attempt.clone();
        if error {
            if let (Some(attempt), Some(data)) = (&attempt, data) {
                if data.len() > 65_536 {
                    return false;
                }
                let Ok(failure) = serde_json::from_str::<FailedModelReply>(data) else {
                    return false;
                };
                if !failure.failed || attempt.report_failure(failure.failure).is_err() {
                    return false;
                }
            }
        }
        let Some(PreparationReply::ModelProducer {
            reply: delivery, ..
        }) = slot.reply.take()
        else {
            unreachable!()
        };
        let reply = match delivery {
            Delivery::Waiting(reply) => reply,
            Delivery::Streaming {
                projection,
                finish,
                chat_stream,
                ..
            } => {
                let usage = projection.completed_usage().cloned();
                let mut result = projection.finish(error.then_some(FAILURE));
                if chat_stream
                    && let Ok(events) = &mut result
                    && let Some(ResponseEvent::Completed {
                        usage_metadata: Some(metadata),
                        ..
                    }) = events.last_mut()
                {
                    metadata.metadata = None;
                }
                if let (Some(attempt), Some(usage)) = (&attempt, usage)
                    && attempt.report_usage(usage).is_err()
                    && result.is_ok()
                {
                    return false;
                }
                return finish.send(result).is_ok();
            }
        };
        let parsed = if error {
            Err(FAILURE.to_owned())
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
                .map_err(str::to_owned)
        };
        if let (Some(attempt), Ok(reply)) = (&attempt, &parsed)
            && let Some(usage) = &reply.usage
            && attempt.report_usage(usage.clone()).is_err()
        {
            return false;
        }
        reply.send(parsed.map(Response::Complete)).is_ok()
    }
    pub(super) fn retire_model_producer(&self) {
        self.model_producer
            .lock()
            .expect("Model producer poisoned")
            .retired = true;
        self.cancel_model_producer_preparations();
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
        Some(Box::new(Producer(tools, input.model.to_owned())))
    }
}
struct Producer(Arc<PythonTools>, String);
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
        let model = self.1.clone();
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
                provider_metadata: admission.provider_output_metadata().cloned(),
            });
            if !ticket.valid(
                &tools
                    .model_producer
                    .lock()
                    .expect("Model producer poisoned"),
            ) {
                return Err(FAILURE.into());
            }
            let profile = tools
                .source_profile(admission.step_store())
                .map_err(|_| FAILURE)?;
            let attempt = admission.model_attempt();
            let cancellation = attempt.as_ref().map(|attempt| attempt.cancellation());
            let id = tools.family.next_invocation.fetch_add(1, Ordering::Relaxed);
            let (reply, receive) = oneshot::channel();
            let mut event = json!({"id":receipt.turn_id,"msg":{"type":"_sdk_model_produce","turn_id":receipt.turn_id,"thread":admission.thread_id(),"instance":tools.instance,"request_id":id,"generation":ticket.generation.id,"model":model,"data":request}});
            {
                let message = &mut event["msg"];
                let retained = admission.provider_input_metadata();
                if !retained.is_empty() {
                    #[derive(serde::Serialize)]
                    struct InputMetadata<'a> {
                        input_index: usize,
                        item: &'a Value,
                        metadata: &'a codex_history::ModelProviderMetadata,
                    }
                    let mut provider_metadata = Vec::new();
                    let mut budget = codex_extension_api::ModelProviderMetadataBudget::default();
                    let mut by_id = std::collections::HashMap::new();
                    for envelope in retained {
                        if let Some(id) = envelope.item.id() {
                            by_id
                                .entry(id.as_str())
                                .or_insert_with(Vec::new)
                                .push(envelope);
                        }
                    }
                    if let Some(input) = message["data"]["input"].as_array() {
                        let mut counts = std::collections::HashMap::new();
                        for item in input {
                            if let Some(id) = item.get("id").and_then(Value::as_str) {
                                *counts.entry(id).or_insert(0usize) += 1;
                            }
                        }
                        for (input_index, item) in input.iter().enumerate() {
                            let Some(id) = item.get("id").and_then(Value::as_str) else {
                                continue;
                            };
                            let Some(matching) = by_id.get(id) else {
                                continue;
                            };
                            if matching.len() != 1 || counts.get(id) != Some(&1) {
                                return Err(FAILURE.into());
                            }
                            let captured = matching[0];
                            if !model_provider_metadata_item_matches(&captured.item, item) {
                                continue;
                            }
                            if let Some(metadata) = captured
                                .metadata
                                .as_ref()
                                .and_then(|metadata| metadata.sdk_model_provider_metadata.as_ref())
                            {
                                let row = InputMetadata {
                                    input_index,
                                    item,
                                    metadata,
                                };
                                budget.charge(&row).map_err(|_| FAILURE)?;
                                provider_metadata
                                    .push(serde_json::to_value(row).map_err(|_| FAILURE)?);
                            }
                        }
                    }
                    if !provider_metadata.is_empty() {
                        message["provider_metadata"] = json!(provider_metadata);
                    }
                }
            }
            if let Some(transport) = admission.request_transport() {
                event["msg"]["request_transport"] = json!(transport);
            }
            if let Some(profile) = profile {
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
            event["msg"]["root_turn_id"] = json!(admission.root_turn_id());
            if let Some(attempt) = &attempt {
                event["msg"]["attempt_id"] = json!(attempt.id());
                event["msg"]["attempt"] = json!(attempt.attempt());
                event["msg"]["retry_configured"] = json!(attempt.retry_configured());
                event["msg"]["disable_provider_retries"] =
                    json!(attempt.disable_provider_retries());
            }
            let callback = tools.model_preparation_callback(id, &mut event, attempt);
            {
                let mut replies = tools
                    .preparation_replies
                    .lock()
                    .expect("Python preparation replies poisoned");
                if tools.preparations_stopped.load(Ordering::Acquire)
                    || !callback.admits_reply()
                    || !ticket.admits()
                {
                    return Err(FAILURE.into());
                }
                replies.as_mut().ok_or(FAILURE)?.insert(
                    id,
                    PreparationSlot {
                        turn: receipt.turn_id.clone(),
                        reply: Some(PreparationReply::ModelProducer {
                            ticket: ticket.clone(),
                            reply: Delivery::Waiting(reply),
                        }),
                        callback: callback.clone(),
                    },
                );
            }
            let pending = PendingPreparation {
                tools: tools.clone(),
                id,
            };
            if let Some(tracing) = &receipt.tracing {
                event["msg"]["tracing_context"] = json!(tracing.token());
            }
            if !receipt.is_current() {
                return Err(FAILURE.into());
            }
            tools
                .family
                .requests
                .send(QueuedRequest::Step(event))
                .map_err(|_| FAILURE)?;
            let mut status = receipt.state.subscribe();
            let cancelled = async {
                if let Some(cancellation) = &cancellation {
                    cancellation.cancelled().await;
                } else {
                    std::future::pending::<()>().await;
                }
            };
            let result = tokio::select! { biased;
                _ = status.wait_for(|status| *status != AdmissionState::Pending) => Err(FAILURE.to_owned()),
                _ = cancelled => Err(FAILURE.to_owned()),
                reply = receive => reply.map_err(|_| FAILURE.to_owned()).and_then(|reply| reply),
            };
            let response = match result {
                Ok(response) if ticket.admits() => response,
                _ => {
                    tools.retire_preparation(id, false);
                    callback.wait().await;
                    return Err(FAILURE.into());
                }
            };
            match response {
                Response::Complete(reply) => {
                    callback.wait().await;
                    if !ticket.admits() || callback.cleanup_failed.load(Ordering::Acquire) {
                        return Err(FAILURE.into());
                    }
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
                    if !reply.provider_metadata.is_empty() {
                        let output = serde_json::from_value(Value::Array(reply.output.clone()))
                            .map_err(|_| FAILURE)?;
                        admission
                            .provider_output_metadata()
                            .ok_or(FAILURE)?
                            .capture(output, reply.provider_metadata)
                            .map_err(|_| FAILURE)?;
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
                    } else if matches!(
                        events.last(),
                        Some(Ok(codex_extension_api::ResponseEvent::Completed { .. }))
                    ) {
                        events.insert(
                            events.len() - 1,
                            Ok(codex_extension_api::ResponseEvent::ResponseObserved(
                                codex_protocol::protocol::ResponseObservation {
                                    response_id: None,
                                    upstream_request_id: None,
                                    status: codex_protocol::protocol::ResponseObservationStatus::Completed,
                                    incomplete_reason: None,
                                    usage: None,
                                },
                            )),
                        );
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
                Response::Stream {
                    events,
                    finish,
                    upstream_request_id,
                } => {
                    let cursor = StreamCursor {
                        events,
                        finish: Some(finish),
                        accepted: Vec::new().into_iter(),
                        status,
                        ticket,
                        callback,
                        cancellation,
                        _pending: pending,
                        upstream_request_id,
                    };
                    Ok(
                        futures_util::stream::try_unfold(cursor, |mut cursor| async move {
                            let event = cursor.next().await?;
                            Ok(event.map(|event| (event, cursor)))
                        })
                        .boxed(),
                    )
                }
            }
        }))
    }
}
