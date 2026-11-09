//! Request-scoped application model output; Core remains the turn and tool owner.
use super::*;
use codex_extension_api::{
    ModelRequestContributor, ModelRequestInput, ModelResponseInterceptor, ModelResponseStream,
    ModelToolAdmission,
};
use futures_util::StreamExt;
use futures_util::future::BoxFuture;

const FAILURE: &str = "Native model producer failed or was cancelled";
type Reply = std::result::Result<ModelReply, String>;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelReply {
    response_id: String,
    output: Vec<Value>,
    #[serde(default)]
    usage: Option<codex_protocol::protocol::TokenUsage>,
}
#[derive(Default)]
pub(super) struct State {
    enabled: bool,
    retired: bool,
    pending: HashMap<u64, (Arc<Ticket>, oneshot::Sender<Reply>)>,
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
        state
            .pending
            .get(&id)
            .is_some_and(|(ticket, reply)| !reply.is_closed() && ticket.valid(&state))
    }
    pub(crate) fn reply_model_producer(&self, id: u64, data: Option<&str>, error: bool) -> bool {
        let mut state = self.model_producer.lock().expect("Model producer poisoned");
        if !state
            .pending
            .get(&id)
            .is_some_and(|(ticket, reply)| !reply.is_closed() && ticket.valid(&state))
        {
            return false;
        }
        let reply = if error {
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
                .map_err(str::to_owned)
        };
        state
            .pending
            .remove(&id)
            .expect("checked model request")
            .1
            .send(reply)
            .is_ok()
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
                state.pending.insert(id, (ticket.clone(), reply));
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
            let _lease = Lease {
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
            let events = codex_extension_api::completed_model_response(
                reply.response_id,
                reply.output,
                reply.usage,
            );
            Ok(futures_util::stream::iter(events).boxed())
        }))
    }
}
