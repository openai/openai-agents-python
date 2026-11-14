//! Optional header-only facts on the existing managed family inbox.
use super::{PythonTools, QueuedRequest};
use codex_extension_api::{
    BuiltinSandboxOperation, ExtensionData, ToolCallOutcome, ToolFinishInput, ToolStartInput,
};
use serde_json::{Value, json};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

pub(super) struct Notice {
    pub(super) payload: Value,
    _permit: OwnedSemaphorePermit,
}

impl PythonTools {
    pub(crate) fn enable_sandbox_operations(&self) {
        self.family
            .sandbox_operations
            .get_or_init(|| Arc::new(Semaphore::new(64)));
    }
}

fn publish(
    tools: &PythonTools,
    operation: BuiltinSandboxOperation,
    step: Option<&ExtensionData>,
    thread: &str,
    turn: &str,
    call: &str,
    finish: Option<(bool, Duration)>,
) {
    let Some(capacity) = tools.family.sandbox_operations.get() else {
        return;
    };
    // Optional telemetry cannot retain unbounded provider identities or block dispatch.
    if thread.len() > 128 || turn.len() > 128 || call.is_empty() || call.len() > 256 {
        return;
    }
    let Ok(permit) = capacity.clone().try_acquire_owned() else {
        return;
    };
    let Ok(profile) = tools.source_profile(step) else {
        return;
    };
    let operation = match operation {
        BuiltinSandboxOperation::Exec => "exec",
        BuiltinSandboxOperation::WriteStdin => "write_stdin",
        BuiltinSandboxOperation::ViewImage => "view_image",
    };
    let mut message = json!({"type":"_sdk_sandbox_operation", "operation":operation,
        "phase":if finish.is_some() {"finish"} else {"start"},
        "thread":thread,"turn_id":turn,"call_id":call});
    if let Some(profile) = profile {
        message["profile_id"] = json!(profile.profile_id);
        message["profile_epoch"] = json!(profile.epoch);
    }
    if let Some((ok, duration)) = finish {
        message["ok"] = json!(ok);
        message["duration_ms"] = json!(duration.as_secs_f64() * 1000.0);
    }
    let mut payload = json!({"id":turn,"msg":message});
    if let Some(instance) = &tools.instance {
        payload["subagent_id"] = json!(thread);
        payload["subagent_incarnation"] = json!(instance);
    }
    let _ = tools
        .family
        .requests
        .send(QueuedRequest::SandboxOperation(Notice {
            payload,
            _permit: permit,
        }));
}

pub(super) fn on_start(input: &ToolStartInput<'_>) {
    if let Some(operation) = input.builtin_sandbox_operation
        && let Some(tools) = input.thread_store.get::<PythonTools>()
    {
        publish(
            &tools,
            operation,
            input.step_store,
            input.thread_store.level_id(),
            input.turn_id,
            input.call_id,
            None,
        );
    }
}

pub(super) fn on_finish(input: &ToolFinishInput<'_>) {
    if let Some(operation) = input.builtin_sandbox_operation
        && let Some(duration) = input.builtin_sandbox_duration
        && let Some(tools) = input.thread_store.get::<PythonTools>()
    {
        let ok = matches!(input.outcome, ToolCallOutcome::Completed { success: true })
            && input.builtin_sandbox_exit_code.is_none_or(|code| code == 0);
        publish(
            &tools,
            operation,
            input.step_store,
            input.thread_store.level_id(),
            input.turn_id,
            input.call_id,
            Some((ok, duration)),
        );
    }
}
