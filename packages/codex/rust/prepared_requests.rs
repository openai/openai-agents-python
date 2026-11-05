//! One-shot prepared sampling replies, scoped to their original operation and turn.
use super::*;
use codex_extension_api::{
    ModelRequestContributor, ModelRequestInput, ModelResponseInterceptor, ModelToolAdmission,
    PreparedRequestAdmission, PreparedRequestData, PreparedRequestInput, PreparedRequestOutput,
};
use futures_util::future::BoxFuture;

const FAILURE: &str = "Native request preparation failed or was cancelled";
type Reply = std::result::Result<Option<PreparedRequestData>, String>;

#[derive(Default)]
pub(super) struct State {
    operation: u64,
    enabled: bool,
    retired: bool,
    pending: HashMap<u64, Pending>,
}

struct Pending {
    ticket: Arc<Ticket>,
    reply: oneshot::Sender<Reply>,
}

struct Ticket {
    tools: Arc<PythonTools>,
    receipt: Arc<TurnAdmission>,
    original: Arc<dyn ModelToolAdmission>,
    operation: u64,
    generation: u64,
}

impl std::fmt::Debug for Ticket {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PreparedRequestAdmission")
            .finish_non_exhaustive()
    }
}

impl Ticket {
    fn valid(&self, state: &State) -> bool {
        state.enabled
            && !state.retired
            && state.operation == self.operation
            && self.tools.instance.is_none()
            && self.tools.snapshot().id == self.generation
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
    pub(crate) fn prepare_requests(&self, enabled: bool) -> Result<()> {
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
        state.pending.clear();
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
        let mut state = self
            .prepared_requests
            .lock()
            .expect("Prepared requests poisoned");
        state.retired = true;
        state.enabled = false;
        state.pending.clear();
    }

    pub(crate) fn check_prepared_request(&self, id: u64) -> bool {
        let state = self
            .prepared_requests
            .lock()
            .expect("Prepared requests poisoned");
        state
            .pending
            .get(&id)
            .is_some_and(|pending| !pending.reply.is_closed() && pending.ticket.valid(&state))
    }

    pub(crate) fn reply_prepared_request(&self, id: u64, data: Option<&str>, error: bool) -> bool {
        let mut state = self
            .prepared_requests
            .lock()
            .expect("Prepared requests poisoned");
        let valid = state
            .pending
            .get(&id)
            .is_some_and(|pending| !pending.reply.is_closed() && pending.ticket.valid(&state));
        if !valid {
            return false;
        }
        // Decode now, before another await. Never expose payload-bearing serde errors.
        let reply = if error {
            Err(FAILURE.into())
        } else {
            data.map(serde_json::from_str::<PreparedRequestData>)
                .transpose()
                .map_err(|_| FAILURE.to_owned())
        };
        state
            .pending
            .remove(&id)
            .expect("checked pending request")
            .reply
            .send(reply)
            .is_ok()
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
                generation: tools.snapshot().id,
                tools: tools.clone(),
                receipt: receipt.clone(),
                original: input.admission,
                operation,
            });
            let id = tools.family.next_invocation.fetch_add(1, Ordering::Relaxed);
            let (reply, receive) = oneshot::channel();
            {
                let mut state = tools
                    .prepared_requests
                    .lock()
                    .expect("Prepared requests poisoned");
                if !ticket.valid(&state) {
                    return Err(FAILURE.into());
                }
                state.pending.insert(
                    id,
                    Pending {
                        ticket: ticket.clone(),
                        reply,
                    },
                );
            }
            struct Lease {
                tools: Arc<PythonTools>,
                id: u64,
            }
            impl Drop for Lease {
                fn drop(&mut self) {
                    self.tools
                        .prepared_requests
                        .lock()
                        .expect("Prepared requests poisoned")
                        .pending
                        .remove(&self.id);
                }
            }
            let _lease = Lease {
                tools: tools.clone(),
                id,
            };
            tools
                .family
                .requests
                .send(QueuedRequest::Prepared(json!({
                    "id": receipt.turn_id,
                    "msg": {"type":"_sdk_prepare_request", "turn_id":receipt.turn_id,
                        "thread":thread, "instance":tools.instance, "request_id":id,
                        "generation":ticket.generation, "recovery_attempt":input.recovery_attempt,
                        "data":input.data}
                })))
                .map_err(|_| FAILURE)?;
            let mut status = receipt.state.subscribe();
            let data = tokio::select! {
                biased;
                _ = status.wait_for(|status| *status != AdmissionState::Pending) => return Err(FAILURE.into()),
                reply = receive => reply.map_err(|_| FAILURE)??,
            };
            ticket.admit_request()?;
            Ok(PreparedRequestOutput {
                data,
                admission: ticket,
            })
        })
    }
}
