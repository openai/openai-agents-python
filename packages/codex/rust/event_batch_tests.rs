use super::*;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use serde_json::json;
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

fn event(kind: &str, sequence: usize) -> Value {
    json!({"id":"turn", "msg":{"type":kind,"sequence":sequence,"delta":"original"}})
}

fn read_queue(
    queue: Arc<Mutex<VecDeque<Value>>>,
) -> impl FnMut() -> std::future::Ready<anyhow::Result<Value>> {
    move || {
        std::future::ready(Ok(queue
            .lock()
            .unwrap()
            .pop_front()
            .expect("unexpected extra read")))
    }
}

fn unpack(value: EventValue) -> (Vec<Value>, Option<PyErr>, bool) {
    let EventValue::Batch {
        events_json,
        following_error,
        barrier,
    } = value
    else {
        panic!("collector returned a single-event result");
    };
    (
        serde_json::from_str(&events_json).unwrap(),
        following_error,
        barrier,
    )
}

#[test]
fn bounded_packets_preserve_full_order_and_the_terminal() {
    let expected: Vec<_> = (0..MAX_RECORDS + 1)
        .map(|i| {
            let kind = match i {
                0 => "user_message",
                1 => "warning",
                _ => "agent_message_content_delta",
            };
            event(kind, i)
        })
        .chain([event("turn_complete", MAX_RECORDS + 1)])
        .collect();
    let queue = Arc::new(Mutex::new(VecDeque::from(expected.clone())));
    let runtime = pyo3_async_runtimes::tokio::get_runtime();
    let (first, error, barrier) =
        unpack(runtime.block_on(collect(read_queue(queue.clone()), "turn", true)));
    assert_eq!(first, expected[..MAX_RECORDS]);
    assert!(error.is_none() && !barrier);
    let (second, error, barrier) =
        unpack(runtime.block_on(collect(read_queue(queue.clone()), "turn", true)));
    assert_eq!(second, expected[MAX_RECORDS..]);
    assert!(error.is_none() && barrier);
    assert!(queue.lock().unwrap().is_empty());
}

#[test]
fn control_unknown_child_and_other_turn_stop_before_later_work() {
    let runtime = pyo3_async_runtimes::tokio::get_runtime();
    let mut barriers: Vec<_> = [
        "_sdk_select_step",
        "_sdk_prepare_request",
        "_sdk_transform_context",
        "dynamic_tool_call_request",
        "exec_approval_request",
        "elicitation_request",
        "_sdk_model_generation_failed",
        "error",
        "thread_settings_applied",
        "session_configured",
        "sdk_subagent_lifecycle",
        "turn_aborted",
        "shutdown_complete",
        "new_unknown_kind",
    ]
    .into_iter()
    .map(|kind| event(kind, 1))
    .collect();
    let mut child = event("agent_message_content_delta", 1);
    child["subagent_id"] = json!("child");
    barriers.push(child);
    let mut other = event("agent_message_content_delta", 1);
    other["id"] = json!("other-turn");
    barriers.push(other);
    for control in barriers {
        let prefix = event("agent_message_content_delta", 0);
        let later = event("agent_message_content_delta", 2);
        let queue = Arc::new(Mutex::new(VecDeque::from([
            prefix.clone(),
            control.clone(),
            later.clone(),
        ])));
        let (records, error, barrier) =
            unpack(runtime.block_on(collect(read_queue(queue.clone()), "turn", true)));
        assert_eq!(records, [prefix, control]);
        assert!(error.is_none() && barrier);
        assert_eq!(*queue.lock().unwrap(), VecDeque::from([later]));
    }
}

#[test]
fn byte_bound_preserves_one_oversized_record_without_reading_the_next() {
    let mut large = event("agent_message_content_delta", 0);
    large["msg"]["delta"] = json!("世".repeat(MAX_ENCODED_BYTES));
    let terminal = event("turn_complete", 1);
    let queue = Arc::new(Mutex::new(VecDeque::from([
        large.clone(),
        terminal.clone(),
    ])));
    let (records, error, barrier) =
        unpack(pyo3_async_runtimes::tokio::get_runtime().block_on(collect(
            read_queue(queue.clone()),
            "turn",
            true,
        )));
    assert_eq!(records, [large]);
    assert!(error.is_none() && !barrier);
    assert_eq!(*queue.lock().unwrap(), VecDeque::from([terminal]));
}

#[test]
fn prefix_precedes_the_original_runtime_error_or_panic_class() {
    Python::initialize();
    let runtime = pyo3_async_runtimes::tokio::get_runtime();
    for panic in [false, true] {
        let prefix = event("agent_message_content_delta", 0);
        let expected = prefix.clone();
        let mut first = Some(prefix);
        let (records, error, barrier) = unpack(runtime.block_on(collect(
            || {
                let value = first.take();
                async move {
                    if let Some(value) = value {
                        return Ok(value);
                    }
                    if panic {
                        panic!("original read panic");
                    }
                    anyhow::bail!("original read failure")
                }
            },
            "turn",
            true,
        )));
        assert_eq!(records, [expected]);
        assert!(!barrier);
        let error = error.expect("following error disappeared");
        Python::attach(|py| {
            if panic {
                assert!(error.is_instance_of::<pyo3_async_runtimes::err::RustPanic>(py));
                assert!(
                    error
                        .to_string()
                        .contains("rust future panicked: original read panic")
                );
            } else {
                assert!(error.is_instance_of::<PyRuntimeError>(py));
                assert!(error.to_string().contains("original read failure"));
            }
        });
    }
}

#[test]
fn typed_batch_preserves_the_exception_object_at_python_conversion() {
    Python::initialize();
    Python::attach(|py| {
        let value = EventValue::Batch {
            events_json: "[]".to_owned(),
            following_error: Some(PyRuntimeError::new_err("same failure")),
            barrier: false,
        }
        .into_pyobject(py)
        .unwrap();
        let (_, error, barrier): (String, Py<PyAny>, bool) = value.extract().unwrap();
        assert!(!barrier);
        assert!(error.bind(py).is_instance_of::<PyRuntimeError>());
        assert_eq!(
            error.bind(py).str().unwrap().to_str().unwrap(),
            "same failure"
        );
    });
}

#[test]
fn disabled_collection_returns_one_record_without_draining_a_followup() {
    let first = event("agent_message_content_delta", 0);
    let second = event("agent_message_content_delta", 1);
    let queue = Arc::new(Mutex::new(VecDeque::from([first.clone(), second.clone()])));
    let (records, error, barrier) =
        unpack(pyo3_async_runtimes::tokio::get_runtime().block_on(collect(
            read_queue(queue.clone()),
            "turn",
            false,
        )));
    assert_eq!(records, [first]);
    assert!(barrier && error.is_none());
    assert_eq!(*queue.lock().unwrap(), VecDeque::from([second]));
}

#[test]
fn pending_read_keeps_the_prefix_and_hard_cancellation_drops_the_inflight_read() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    struct ReadDrop(Arc<AtomicUsize>);
    impl Drop for ReadDrop {
        fn drop(&mut self) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
    }
    pyo3_async_runtimes::tokio::get_runtime().block_on(async {
        for cancel in [false, true] {
            let (sender, receiver) = tokio::sync::mpsc::unbounded_channel();
            let receiver = Arc::new(tokio::sync::Mutex::new(receiver));
            let started = Arc::new(AtomicUsize::new(0));
            let dropped = Arc::new(AtomicUsize::new(0));
            let pending = Arc::new(tokio::sync::Notify::new());
            let first = event("agent_message_content_delta", 0);
            sender.send(first.clone()).unwrap();
            let reads = started.clone();
            let drops = dropped.clone();
            let waiting = pending.clone();
            let owner = tokio::spawn(collect(
                move || {
                    let receiver = receiver.clone();
                    let reads = reads.clone();
                    let drops = drops.clone();
                    let waiting = waiting.clone();
                    async move {
                        let _drop = ReadDrop(drops);
                        if reads.fetch_add(1, Ordering::SeqCst) == 1 {
                            waiting.notify_one();
                        }
                        Ok(receiver.lock().await.recv().await.expect("sender retained"))
                    }
                },
                "turn",
                true,
            ));
            tokio::time::timeout(std::time::Duration::from_secs(5), pending.notified())
                .await
                .unwrap();
            assert!(
                !owner.is_finished(),
                "collector must retain a pending read after its prefix"
            );
            if cancel {
                owner.abort();
                let Err(error) = owner.await else {
                    panic!("cancelled owner returned a result");
                };
                assert!(error.is_cancelled());
            } else {
                let terminal = event("turn_complete", 1);
                sender.send(terminal.clone()).unwrap();
                let (records, error, barrier) = unpack(owner.await.unwrap());
                assert_eq!(records, [first, terminal]);
                assert!(error.is_none() && barrier);
            }
            assert_eq!(started.load(Ordering::SeqCst), 2);
            assert_eq!(dropped.load(Ordering::SeqCst), 2);
        }
    });
}
