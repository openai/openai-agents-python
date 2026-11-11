//! Request-scoped application model output; Core remains the turn and tool owner.
use super::*;
use codex_extension_api::{
    ModelRequestContributor, ModelRequestInput, ModelResponseInterceptor, ModelResponseStream,
    ModelToolAdmission,
};
use futures_util::StreamExt;
use futures_util::future::BoxFuture;

const FAILURE: &str = "Native model producer failed or was cancelled";
pub(super) type Reply = std::result::Result<ModelReply, String>;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ModelReply {
    response_id: String,
    output: Vec<Value>,
    #[serde(default)]
    usage: Option<codex_protocol::protocol::TokenUsage>,
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
        let Some(slot) = replies.as_ref().and_then(|pending| pending.get(&id)) else {
            return false;
        };
        let Some(PreparationReply::ModelProducer { ticket, reply }) = &slot.reply else {
            return false;
        };
        !slot.is_closed()
            && !reply.is_closed()
            && ticket.valid(&self.model_producer.lock().expect("Model producer poisoned"))
    }
    pub(crate) fn reply_model_producer(&self, id: u64, data: Option<&str>, error: bool) -> bool {
        let mut replies = self
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        let Some(slot) = replies.as_mut().and_then(|pending| pending.get_mut(&id)) else {
            return false;
        };
        let Some(PreparationReply::ModelProducer { ticket, .. }) = &slot.reply else {
            return false;
        };
        if slot.is_closed()
            || !ticket.valid(&self.model_producer.lock().expect("Model producer poisoned"))
        {
            return false;
        }
        let parsed = if error {
            if let (Some(attempt), Some(data)) = (&slot.callback.model_attempt, data) {
                let Some(data) = Some(data).filter(|data| data.len() <= 65_536) else {
                    return false;
                };
                let Ok(failure) = serde_json::from_str::<FailedModelReply>(data) else {
                    return false;
                };
                if !failure.failed || attempt.report_failure(failure.failure).is_err() {
                    return false;
                }
            }
            Err(FAILURE.into())
        } else {
            let parsed = data
                .ok_or(FAILURE)
                .and_then(|data| serde_json::from_str::<ModelReply>(data).map_err(|_| FAILURE))
                .and_then(|reply| {
                    if reply.response_id.trim().is_empty() {
                        Err(FAILURE)
                    } else {
                        Ok(reply)
                    }
                });
            match parsed {
                Ok(reply) => {
                    if let (Some(attempt), Some(usage)) =
                        (&slot.callback.model_attempt, &reply.usage)
                    {
                        if attempt.report_usage(usage.clone()).is_err() {
                            return false;
                        }
                    }
                    Ok(reply)
                }
                Err(error) => Err(error.to_owned()),
            }
        };
        let Some(PreparationReply::ModelProducer { reply, .. }) = slot.reply.take() else {
            unreachable!("checked model request");
        };
        reply.send(parsed).is_ok()
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
            let reply: ModelReply = tools.exchange_preparation(
                receipt.clone(), attempt.clone(), cancellation,
                [FAILURE, FAILURE, FAILURE],
                |reply| PreparationReply::ModelProducer { ticket: ticket.clone(), reply },
                |id| {
                    let mut event = json!({"id":receipt.turn_id,"msg":{"type":"_sdk_model_produce","turn_id":receipt.turn_id,"thread":admission.thread_id(),"instance":tools.instance,"request_id":id,"generation":ticket.generation.id,"model":model,"data":request}});
                    if let Some(profile) = profile {
                        event["msg"]["profile_id"] = json!(profile.profile_id);
                        event["msg"]["profile_epoch"] = json!(profile.epoch);
                    }
                    event["msg"]["root_turn_id"] = json!(admission.root_turn_id());
                    if let Some(attempt) = &attempt {
                        event["msg"]["attempt_id"] = json!(attempt.id());
                        event["msg"]["attempt"] = json!(attempt.attempt());
                        event["msg"]["retry_configured"] = json!(attempt.retry_configured());
                        event["msg"]["disable_provider_retries"] = json!(attempt.disable_provider_retries());
                    }
                    event
                },
                |reply: Reply| reply.map_err(anyhow::Error::msg),
            ).await.map_err(|_| FAILURE)?;
            if !ticket.valid(
                &tools
                    .model_producer
                    .lock()
                    .expect("Model producer poisoned"),
            ) {
                return Err(FAILURE.into());
            }
            let events = codex_extension_api::completed_model_response(
                reply.response_id,
                reply.output,
                reply.usage,
            );
            Ok(futures_util::stream::iter(events).boxed())
        }))
    }
}
