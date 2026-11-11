//! Retry only the short gap between delivery of a managed terminal and Core idle.
use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use codex_core_api::{Config, WithTurnExtensionData};
use codex_extension_api::{
    ExtensionDataInit, ExtensionFuture, ThreadIdleInput, ThreadLifecycleContributor,
    ThreadStopInput, TurnLifecycleContributor, TurnStartInput,
};
use codex_protocol::protocol::{EventMsg, ThreadSettingsOverrides, W3cTraceContext};
use codex_protocol::turn_input::{
    NotSubmittedReason, StartIfIdleSubmission, TurnInputRequest, TurnInputSubmission,
};
use tokio::sync::{MutexGuard, watch};
use tokio::time::{Instant, timeout_at};

// This bounds settlement waits, not provider work or an accepted native start.
const SETTLEMENT_GRACE: Duration = Duration::from_secs(1);

struct Turn {
    id: String,
    accepted: AtomicBool,
    delivered: AtomicBool,
    successful: AtomicBool,
}

#[derive(Default)]
struct Current {
    turn: Option<Arc<Turn>>,
    retired: bool,
}

pub(crate) struct Settlement {
    current: Mutex<Current>,
    changed: watch::Sender<()>,
    #[cfg(test)]
    pub(crate) waiting: tokio::sync::Semaphore,
}

impl Default for Settlement {
    fn default() -> Self {
        Self {
            current: Mutex::new(Current::default()),
            changed: watch::channel(()).0,
            #[cfg(test)]
            waiting: tokio::sync::Semaphore::new(0),
        }
    }
}

// Core can retain initializers for future turns, including same-ID recovery.
// Only the first native turn can belong to this managed submission.
#[derive(Default)]
pub(crate) struct Seed(OnceLock<Arc<Turn>>);

impl Seed {
    pub(crate) fn attach(init: &mut ExtensionDataInit) -> Arc<Self> {
        let seed = Arc::new(Self::default());
        init.insert(seed.clone());
        seed
    }

    pub(crate) fn confirm(&self, result: &StartIfIdleSubmission) {
        if let StartIfIdleSubmission::Started { turn_id } = result
            && let Some(turn) = self.0.get()
            && turn.id == *turn_id
        {
            turn.accepted.store(true, Ordering::Release);
        }
    }
}

impl Settlement {
    fn completed(&self) -> Option<Arc<Turn>> {
        let current = self.current.lock().expect("turn settlement poisoned");
        current
            .turn
            .as_ref()
            .filter(|turn| {
                !current.retired
                    && turn.accepted.load(Ordering::Acquire)
                    && turn.delivered.load(Ordering::Acquire)
            })
            .cloned()
    }

    fn is_current(&self, expected: &Arc<Turn>) -> bool {
        self.completed()
            .is_some_and(|turn| Arc::ptr_eq(&turn, expected))
    }

    pub fn delivered(&self, event: &EventMsg) {
        let id = match event {
            EventMsg::TurnComplete(event) => Some(event.turn_id.as_str()),
            EventMsg::TurnAborted(event) => event.turn_id.as_deref(),
            _ => None,
        };
        let current = self.current.lock().expect("turn settlement poisoned");
        if let Some(turn) = &current.turn
            && id == Some(turn.id.as_str())
        {
            turn.successful.store(
                matches!(event, EventMsg::TurnComplete(_)),
                Ordering::Release,
            );
            turn.delivered.store(true, Ordering::Release);
        }
    }

    pub fn retire(&self) {
        self.current
            .lock()
            .expect("turn settlement poisoned")
            .retired = true;
        self.changed.send_replace(());
    }
}

pub(crate) struct Contributor;

impl ThreadLifecycleContributor<Config> for Contributor {
    fn on_thread_idle<'a>(&'a self, input: ThreadIdleInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(settlement) = input.thread_store.get::<Settlement>() {
                settlement.changed.send_replace(());
            }
        })
    }

    fn on_thread_stop<'a>(&'a self, input: ThreadStopInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(settlement) = input.thread_store.get::<Settlement>() {
                settlement.retire();
            }
        })
    }
}

impl TurnLifecycleContributor for Contributor {
    fn on_turn_start<'a>(&'a self, input: TurnStartInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            let settlement = input.thread_store.get_or_init(Settlement::default);
            let turn = Arc::new(Turn {
                id: input.turn_id.to_owned(),
                accepted: AtomicBool::new(false),
                delivered: AtomicBool::new(false),
                successful: AtomicBool::new(false),
            });
            if let Some(seed) = input.turn_store.get::<Arc<Seed>>() {
                let _ = seed.0.set(turn.clone());
            }
            settlement
                .current
                .lock()
                .expect("turn settlement poisoned")
                .turn = Some(turn);
            settlement.changed.send_replace(());
        })
    }
}

pub(crate) async fn start<'a>(
    state: &'a crate::engine::ThreadState,
    mutation: &mut Option<MutexGuard<'a, ()>>,
    request: TurnInputRequest,
    init: ExtensionDataInit,
    previous: Option<&str>,
) -> anyhow::Result<StartIfIdleSubmission> {
    start_with_settlement(state, mutation, init, previous, |init| {
        let input = WithTurnExtensionData::new(request.clone(), init);
        async move {
            match previous {
                None => state.thread.start_turn_if_idle(input).await,
                Some(previous) => Ok(
                    match state
                        .thread
                        .continue_turn_if_idle(input, previous.into())
                        .await?
                    {
                        TurnInputSubmission::Started { turn_id } => {
                            StartIfIdleSubmission::Started { turn_id }
                        }
                        TurnInputSubmission::NotSubmitted { reason } => {
                            StartIfIdleSubmission::NotSubmitted { reason }
                        }
                        TurnInputSubmission::Steered { .. } => {
                            unreachable!("continuation cannot steer")
                        }
                    },
                ),
            }
        }
    })
    .await
}

pub(crate) async fn start_realtime<'a>(
    state: &'a crate::engine::ThreadState,
    mutation: &mut Option<MutexGuard<'a, ()>>,
    generation: String,
    response_id: String,
    call_ids: Vec<String>,
    disabled_call_ids: Vec<String>,
    settings: ThreadSettingsOverrides,
    trace: Option<W3cTraceContext>,
    init: ExtensionDataInit,
) -> anyhow::Result<StartIfIdleSubmission> {
    start_with_settlement(state, mutation, init, None, |init| {
        let generation = generation.clone();
        let response_id = response_id.clone();
        let call_ids = call_ids.clone();
        let disabled_call_ids = disabled_call_ids.clone();
        let settings = WithTurnExtensionData::new(settings.clone(), init);
        let trace = trace.clone();
        async move {
            Ok(
                match state
                    .thread
                    .start_realtime_tools_if_idle_with_policy(
                        generation,
                        response_id,
                        call_ids,
                        disabled_call_ids,
                        settings,
                        trace,
                    )
                    .await?
                {
                    TurnInputSubmission::Started { turn_id } => {
                        StartIfIdleSubmission::Started { turn_id }
                    }
                    TurnInputSubmission::NotSubmitted { reason } => {
                        StartIfIdleSubmission::NotSubmitted { reason }
                    }
                    TurnInputSubmission::Steered { .. } => {
                        unreachable!("Realtime tool submission cannot steer")
                    }
                },
            )
        }
    })
    .await
}

async fn start_with_settlement<'a, F, Fut>(
    state: &'a crate::engine::ThreadState,
    mutation: &mut Option<MutexGuard<'a, ()>>,
    mut init: ExtensionDataInit,
    previous: Option<&str>,
    submit: F,
) -> anyhow::Result<StartIfIdleSubmission>
where
    F: Fn(ExtensionDataInit) -> Fut,
    Fut: Future<Output = codex_protocol::error::Result<StartIfIdleSubmission>>,
{
    let settlement = state
        .thread
        .thread_extension_data()
        .get_or_init(Settlement::default);
    // Subscribe before the authority check and first native attempt. A native
    // transition between rejection and waiting must remain visible.
    let mut changed = settlement.changed.subscribe();
    let expected = settlement.completed();
    // A retained steer authorizes exactly one automatic successor. Abort, stop,
    // a completed ordinary request or a replaced receipt grants no continuation.
    let authorized = || {
        previous.is_none_or(|id| {
            expected.as_ref().is_some_and(|turn| {
                turn.id == id
                    && turn.successful.load(Ordering::Acquire)
                    && settlement.is_current(turn)
                    && state
                        .thread
                        .thread_extension_data()
                        .get::<crate::python_tools::PythonTools>()
                        .is_some_and(|tools| tools.can_continue_steering(id))
            })
        })
    };
    if !authorized() {
        return Ok(StartIfIdleSubmission::NotSubmitted {
            reason: NotSubmittedReason::Superseded,
        });
    }
    let seed = Seed::attach(&mut init);
    let mut result = submit(init.clone()).await?;
    let deadline = Instant::now() + SETTLEMENT_GRACE;
    loop {
        seed.confirm(&result);
        if result
            != (StartIfIdleSubmission::NotSubmitted {
                reason: NotSubmittedReason::NotIdle,
            })
        {
            return Ok(result);
        }
        let Some(expected) = &expected else {
            return Ok(result);
        };
        if !settlement.is_current(expected) {
            return Ok(result);
        }
        drop(mutation.take());
        #[cfg(test)]
        settlement.waiting.add_permits(1);
        if !matches!(timeout_at(deadline, changed.changed()).await, Ok(Ok(()))) {
            return Ok(result);
        }
        let Ok(guard) = timeout_at(deadline, state.mutations.lock()).await else {
            return Ok(result);
        };
        *mutation = Some(guard);
        if Instant::now() >= deadline || !settlement.is_current(expected) || !authorized() {
            return Ok(result);
        }
        result = submit(init.clone()).await?;
    }
}
