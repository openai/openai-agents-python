//! One-shot prepared sampling replies, scoped to their original operation and turn.
use super::*;
use codex_extension_api::{
    ModelRequestContributor, ModelRequestInput, ModelResponseInterceptor, ModelToolAdmission,
    PreparedRequestAdmission, PreparedRequestData, PreparedRequestInput, PreparedRequestOutput,
};
use futures_util::future::BoxFuture;

const FAILURE: &str = "Native request preparation failed or was cancelled";
pub(super) type Reply = std::result::Result<Option<PreparedRequestData>, String>;

#[derive(Default)]
pub(super) struct State {
    operation: u64,
    enabled: bool,
    retired: bool,
}

pub(super) struct Ticket {
    tools: Arc<PythonTools>,
    receipt: Arc<TurnAdmission>,
    original: Arc<dyn ModelToolAdmission>,
    operation: u64,
    generation: Arc<Generation>,
    profile: Option<codex_extension_api::AgentProfileReceipt>,
}

impl std::fmt::Debug for Ticket {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PreparedRequestAdmission")
            .finish_non_exhaustive()
    }
}

impl Ticket {
    pub(super) fn admits(&self) -> bool {
        self.valid(
            &self
                .tools
                .prepared_requests
                .lock()
                .expect("Model callback state poisoned"),
        )
    }
    fn valid(&self, state: &State) -> bool {
        state.enabled
            && !state.retired
            && self
                .original
                .model_attempt()
                .is_none_or(|attempt| !attempt.cancellation().is_cancelled())
            && state.operation == self.operation
            && self.tools.instance.is_none()
            && self.tools.admits_generation(&self.generation)
            && self.receipt.prepared_operation == Some(self.operation)
            && self.receipt.is_current()
            && self
                .tools
                .interception
                .get()
                .and_then(Weak::upgrade)
                .is_some_and(|control| {
                    control.admits_prepared_turn(self.original.turn_store(), &self.receipt)
                })
            && self.original.thread_id() == self.receipt.thread_id
            && !self.receipt.graph_fenced
            && *self.receipt.state.borrow() == AdmissionState::Pending
            && self.original.turn_store().level_id() == self.receipt.turn_id
            && self
                .original
                .turn_store()
                .get::<Arc<TurnAdmission>>()
                .is_some_and(|owner| Arc::ptr_eq(owner.as_ref(), &self.receipt))
    }
}

impl PreparedRequestAdmission for Ticket {
    fn admit_request(&self) -> std::result::Result<(), String> {
        let state = self
            .tools
            .prepared_requests
            .lock()
            .expect("Prepared requests poisoned");
        self.valid(&state)
            .then_some(())
            .ok_or_else(|| FAILURE.into())
    }
}

impl PythonTools {
    pub(crate) fn can_run_without_python(&self) -> bool {
        self.current
            .read()
            .expect("Python tools poisoned")
            .entries
            .is_empty()
            && self.fixed.is_empty()
            && self.instance.is_none()
            && !self.suspended.load(Ordering::Acquire)
            && !self.selecting_steps.load(Ordering::Acquire)
            && !self.selecting_children.load(Ordering::Acquire)
            && !self.context_transform_enabled
            && !self.before_compact_enabled
            && !self.before_complete_enabled
            && !self.tool_batch_completion_enabled
            && !self
                .prepared_requests
                .lock()
                .expect("Prepared requests poisoned")
                .enabled
            && self
                .pending
                .lock()
                .expect("Python calls poisoned")
                .is_empty()
            && self
                .admission
                .lock()
                .expect("Python admission poisoned")
                .is_none()
            && self
                .step_defaults
                .read()
                .expect("Step defaults poisoned")
                .as_ref()
                .is_none_or(Vec::is_empty)
    }

    fn cancel_prepared_request_callbacks(&self) {
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
                        Some(PreparationReply::PreparedRequest { .. })
                    )
                    .then_some(*id)
                })
                .collect::<Vec<_>>()
        };
        for id in ids {
            self.retire_preparation(id, false);
        }
    }
    pub(crate) fn prepare_requests(&self, enabled: bool) -> Result<()> {
        {
            let mut state = self
                .prepared_requests
                .lock()
                .expect("Prepared requests poisoned");
            ensure!(!state.retired, "Request preparation owner is closed");
            ensure!(
                !enabled || self.instance.is_none(),
                "Request preparation requires a root thread"
            );
            state.operation = state
                .operation
                .checked_add(1)
                .ok_or_else(|| anyhow::anyhow!("Request preparation operation exhausted"))?;
            state.enabled = enabled;
        }
        self.cancel_prepared_request_callbacks();
        Ok(())
    }
    pub(super) fn prepared_operation(&self) -> Option<u64> {
        let state = self
            .prepared_requests
            .lock()
            .expect("Prepared requests poisoned");
        (state.enabled && !state.retired).then_some(state.operation)
    }
    pub(super) fn retire_prepared_requests(&self) {
        {
            let mut state = self
                .prepared_requests
                .lock()
                .expect("Prepared requests poisoned");
            state.retired = true;
            state.enabled = false;
        }
        self.cancel_prepared_request_callbacks();
    }
    pub(crate) fn check_prepared_request(&self, id: u64) -> bool {
        let replies = self
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        let Some(slot) = replies.as_ref().and_then(|pending| pending.get(&id)) else {
            return false;
        };
        let Some(PreparationReply::PreparedRequest { ticket, reply }) = &slot.reply else {
            return false;
        };
        !slot.is_closed()
            && !reply.is_closed()
            && ticket.valid(
                &self
                    .prepared_requests
                    .lock()
                    .expect("Prepared requests poisoned"),
            )
    }
    pub(crate) fn reply_prepared_request(&self, id: u64, data: Option<&str>, error: bool) -> bool {
        let mut replies = self
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        let Some(slot) = replies.as_mut().and_then(|pending| pending.get_mut(&id)) else {
            return false;
        };
        let Some(PreparationReply::PreparedRequest { ticket, .. }) = &slot.reply else {
            return false;
        };
        if slot.is_closed()
            || !ticket.valid(
                &self
                    .prepared_requests
                    .lock()
                    .expect("Prepared requests poisoned"),
            )
        {
            return false;
        }
        let reply = if error {
            Err(FAILURE.into())
        } else {
            data.map(serde_json::from_str::<PreparedRequestData>)
                .transpose()
                .map_err(|_| FAILURE.to_owned())
        };
        let Some(PreparationReply::PreparedRequest { reply: sender, .. }) = slot.reply.take()
        else {
            unreachable!("checked prepared request");
        };
        sender.send(reply).is_ok()
    }
}

#[derive(Debug)]
pub(crate) struct Contributor;

impl ModelRequestContributor for Contributor {
    fn request(&self, _: ModelRequestInput<'_>) -> Option<Box<dyn ModelResponseInterceptor>> {
        None
    }

    fn prepares_request(&self, turn: &ExtensionData) -> bool {
        // This marker deliberately survives operation retirement, forcing Core
        // to reject a stale prepared operation rather than silently skip it.
        turn.get::<Arc<TurnAdmission>>()
            .is_some_and(|owner| owner.prepared_operation.is_some())
    }

    fn prepare_request(
        &self,
        input: PreparedRequestInput,
    ) -> BoxFuture<'_, std::result::Result<PreparedRequestOutput, String>> {
        Box::pin(async move {
            let receipt = input
                .admission
                .turn_store()
                .get::<Arc<TurnAdmission>>()
                .map(|owner| Arc::clone(owner.as_ref()))
                .ok_or(FAILURE)?;
            let tools = receipt.owner.upgrade().ok_or(FAILURE)?;
            let operation = receipt.prepared_operation.ok_or(FAILURE)?;
            let thread = input.admission.thread_id().to_owned();
            // Receipt identity comes from Core's retained store, never Python IDs.
            let ticket = Arc::new(Ticket {
                generation: tools
                    .generation_for_step(input.admission.step_store())
                    .map_err(|_| FAILURE)?,
                profile: tools
                    .source_profile(input.admission.step_store())
                    .map_err(|_| FAILURE)?,
                tools: tools.clone(),
                receipt: receipt.clone(),
                original: input.admission.clone(),
                operation,
            });
            let attempt = ticket.original.model_attempt();
            let cancellation = attempt.as_ref().map(|attempt| attempt.cancellation());
            let data = tools.exchange_preparation(
                receipt.clone(), attempt.clone(), cancellation,
                [FAILURE, FAILURE, FAILURE],
                |reply| PreparationReply::PreparedRequest { ticket: ticket.clone(), reply },
                |id| {
                    let mut request = json!({
                        "id": receipt.turn_id,
                        "msg": {"type":"_sdk_prepare_request", "turn_id":receipt.turn_id,
                            "thread":thread, "instance":tools.instance, "request_id":id,
                            "generation":ticket.generation.id, "recovery_attempt":input.recovery_attempt,
                            "data":input.data}
                    });
                    if let Some(profile) = &ticket.profile {
                        request["msg"]["profile_id"] = json!(profile.profile_id);
                        request["msg"]["profile_epoch"] = json!(profile.epoch);
                    }
                    request["msg"]["root_turn_id"] = json!(input.admission.root_turn_id());
                    if let Some(attempt) = &attempt {
                        request["msg"]["attempt_id"] = json!(attempt.id());
                        request["msg"]["attempt"] = json!(attempt.attempt());
                    }
                    request
                },
                |reply: Reply| reply.map_err(anyhow::Error::msg),
            ).await.map_err(|_| FAILURE)?;
            ticket.admit_request()?;
            Ok(PreparedRequestOutput {
                data,
                admission: ticket,
            })
        })
    }
}
