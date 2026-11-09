//! A retained provider terminal is passive history; append failure fails its owning turn.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::Duration;

use bytes::Bytes;
use codex_history::RolloutItem;
use codex_protocol::protocol::Op;
use codex_protocol::turn_input::{TurnInput, TurnInputRequest};
use codex_protocol::user_input::UserInput;
use http_body_util::{BodyExt, Full};
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use serde_json::json;
use tokio::sync::Semaphore;

use super::EngineState;
use crate::storage::MemoryThreadStore;

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn failed_observation_append_fails_turn_without_inference_retry() {
    check_failed_append(false, false).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn failed_compaction_observation_append_does_not_retry_provider() {
    check_failed_append(true, false).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn failed_remote_compaction_observation_append_has_one_error_and_no_retry() {
    check_failed_append(true, true).await;
}

async fn check_failed_append(compaction: bool, remote: bool) {
    tokio::time::timeout(Duration::from_secs(20), async {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/v1", listener.local_addr().unwrap());
        let requests = Arc::new(AtomicUsize::new(0));
        let remote_requested = Arc::new(AtomicBool::new(false));
        let request_arrived = Arc::new(Semaphore::new(0));
        let release = Arc::new(Semaphore::new(0));
        let server = tokio::spawn({
            let requests = requests.clone();
            let remote_requested = remote_requested.clone();
            let request_arrived = request_arrived.clone();
            let release = release.clone();
            async move {
                loop {
                    let (socket, _) = listener.accept().await.unwrap();
                    let requests = requests.clone();
                    let remote_requested = remote_requested.clone();
                    let request_arrived = request_arrived.clone();
                    let release = release.clone();
                    tokio::spawn(async move {
                        let service = service_fn(move |request: hyper::Request<hyper::body::Incoming>| {
                            let requests = requests.clone();
                            let remote_requested = remote_requested.clone();
                            let request_arrived = request_arrived.clone();
                            let release = release.clone();
                            async move {
                                let body = request.into_body().collect().await.unwrap().to_bytes();
                                let request: serde_json::Value = serde_json::from_slice(&body).unwrap();
                                remote_requested.store(request["input"].as_array().unwrap().iter()
                                    .any(|item| item["type"] == "compaction_trigger"), Ordering::SeqCst);
                                requests.fetch_add(1, Ordering::SeqCst);
                                request_arrived.add_permits(1);
                                release.acquire().await.unwrap().forget();
                                let events = [
                                    json!({"type":"response.output_item.done", "item":{
                                        "type":"message", "id":"msg_history", "role":"assistant",
                                        "content":[{"type":"output_text", "text":"done"}]}}),
                                    json!({"type":"response.completed", "response":{
                                        "id":"resp_history", "usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}),
                                ];
                                let body = events.into_iter().map(|event| format!("data: {event}\n\n")).collect::<String>();
                                Ok::<_, std::convert::Infallible>(hyper::Response::builder()
                                    .header("content-type", "text/event-stream")
                                    .body(Full::new(Bytes::from(body))).unwrap())
                            }
                        });
                        let _ = hyper::server::conn::http1::Builder::new()
                            .serve_connection(TokioIo::new(socket), service).await;
                    });
                }
            }
        });
        let home = tempfile::tempdir().unwrap();
        let engine = Arc::new(EngineState::new(
            json!({
                "model":"gpt-5.2", "model_provider":"fixture",
                "model_providers.fixture":{"name":"history fixture", "base_url":url,
                    "wire_api":"responses", "requires_openai_auth":false,
                    "supports_websockets":false, "request_max_retries":2,"stream_max_retries":2,
                    "capabilities":{"remote_compaction":if remote {"v2"} else {"unsupported"}}},
                "approval_policy":"never", "sandbox_mode":"read-only", "web_search":"disabled",
                "agents.enabled":false,"features.code_mode":false,"features.shell_snapshot":false
            }).to_string(),
            json!({"codex_home":home.path(),"cwd":home.path()}).to_string(), false, None,
        ).await.unwrap());
        let native = engine.start(Default::default(), None).await.unwrap();
        let state = native.current().unwrap();
        if compaction {
            state.thread.submit(Op::Compact).await.unwrap();
        } else {
            state.thread.start_turn_if_idle(TurnInputRequest::new(TurnInput::UserInput {
                content:vec![UserInput::Text {text:"go".into(),text_elements:vec![]}],client_id:None,
            })).await.unwrap();
        }
        request_arrived.acquire().await.unwrap().forget();
        let store = engine.store.as_any().downcast_ref::<MemoryThreadStore>().unwrap();
        assert!(!store.fail_appends.load(Ordering::SeqCst));
        assert_eq!(store.failed_response_observation_appends.load(Ordering::SeqCst), 0);
        store.fail_response_observation_appends.store(true, Ordering::SeqCst);
        release.add_permits(1);
        let mut errors = 0;
        let mut error_messages = Vec::new();
        let mut terminals = 0;
        loop {
            let event = state.next_event().await.unwrap();
            if event["msg"]["type"] == "error" {
                assert_eq!(store.failed_response_observation_appends.load(Ordering::SeqCst), 1,
                    "an unrelated append must not trigger the tested failure");
                error_messages.push(event["msg"]["message"].as_str().unwrap().to_string());
                errors += 1;
            }
            if event["msg"]["type"] == "task_complete" {
                terminals += 1;
                break;
            }
        }
        // The native task has settled; inspect any already-following deliveries
        // as well, so a duplicate terminal/error cannot hide behind the first.
        while let Ok(event) = tokio::time::timeout(Duration::from_millis(50), state.next_event()).await {
            let event = event.unwrap();
            if event["msg"]["type"] == "error" {
                errors += 1;
                error_messages.push(event["msg"]["message"].as_str().unwrap().to_string());
            }
            if event["msg"]["type"] == "task_complete" || event["msg"]["type"] == "turn_complete" {
                terminals += 1;
            }
        }
        assert_eq!(errors, 1, "the owning task must publish Fatal exactly once: {error_messages:?}");
        // Patch 0046 fences canonical history after a failed store append.
        // Sampling's durability check or compaction task finalization wraps the
        // original observation error; preserve the current exact public message.
        assert_eq!(error_messages[0], if compaction {
            "Fatal error: Canonical thread persistence failed; turn completion stopped"
        } else {
            "Fatal error: Canonical thread persistence failed; execution stopped pending recovery"
        });
        assert_eq!(terminals, 1, "the owning task must settle once");
        assert_eq!(remote_requested.load(Ordering::SeqCst), remote);
        assert_eq!(store.failed_response_observation_appends.load(Ordering::SeqCst), 1,
            "the intended observation append must actually fail exactly once");
        store.fail_response_observation_appends.store(false, Ordering::SeqCst);
        let history = state.thread.load_history(true).await.unwrap();
        assert!(!history.items.iter().any(|item| matches!(item, RolloutItem::ResponseObservationRecord(_))));
        assert_eq!(requests.load(Ordering::SeqCst), 1);
        engine.close().await.unwrap();
        server.abort();
    }).await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn ephemeral_inference_does_not_claim_retained_observation_history() {
    tokio::time::timeout(Duration::from_secs(20), async {
        let provider = super::settlement_tests::Provider::start().await;
        let home = tempfile::tempdir().unwrap();
        let config = json!({
            "model":"gpt-5.2", "model_provider":"fixture",
            "model_providers.fixture":{"name":"ephemeral fixture", "base_url":provider.url,
                "wire_api":"responses", "requires_openai_auth":false,
                "supports_websockets":false, "request_max_retries":0,"stream_max_retries":0},
            "approval_policy":"never", "sandbox_mode":"read-only", "web_search":"disabled",
            "agents.enabled":false,"features.code_mode":false,"features.shell_snapshot":false
        })
        .to_string();
        let runtime =
            json!({"codex_home":home.path(),"cwd":home.path(),"ephemeral":true}).to_string();
        let engine = Arc::new(
            EngineState::new(config.clone(), runtime.clone(), false, None)
                .await
                .unwrap(),
        );
        let native = engine.start(Default::default(), None).await.unwrap();
        let state = native.current().unwrap();
        state
            .thread
            .start_turn_if_idle(TurnInputRequest::new(TurnInput::UserInput {
                content: vec![UserInput::Text {
                    text: "go".into(),
                    text_elements: vec![],
                }],
                client_id: None,
            }))
            .await
            .unwrap();
        loop {
            let event = state.next_event().await.unwrap();
            assert_ne!(event["msg"]["type"], "error");
            if event["msg"]["type"] == "task_complete" {
                break;
            }
        }
        assert!(state.thread.load_history(true).await.is_err());
        assert_eq!(provider.requests.lock().unwrap().len(), 1);
        engine.close().await.unwrap();
        assert!(EngineState::new(config, runtime, true, None).await.is_err());
    })
    .await
    .unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn failed_observation_append_joins_held_tool_and_preserves_fatal() {
    use crate::python_tools::{PendingKey, PythonTools};
    use futures_util::StreamExt;
    use http_body_util::StreamBody;
    use hyper::body::Frame;

    tokio::time::timeout(Duration::from_secs(20), async {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/v1", listener.local_addr().unwrap());
        let requests = Arc::new(AtomicUsize::new(0));
        let terminal = Arc::new(Semaphore::new(0));
        let server = tokio::spawn({
            let requests = requests.clone();
            let terminal = terminal.clone();
            async move {
                loop {
                    let (socket, _) = listener.accept().await.unwrap();
                    let requests = requests.clone();
                    let terminal = terminal.clone();
                    tokio::spawn(async move {
                        let service = service_fn(move |request: hyper::Request<hyper::body::Incoming>| {
                            let requests = requests.clone();
                            let terminal = terminal.clone();
                            async move {
                                request.into_body().collect().await.unwrap();
                                requests.fetch_add(1, Ordering::SeqCst);
                                let opening = [
                                    json!({"type":"response.output_item.done", "item":{
                                        "type":"function_call", "call_id":"held-call",
                                        "namespace":"_agents_sdk", "name":"dispatch",
                                        "arguments":json!({"original":{
                                            "type":"function_call", "call_id":"held-call",
                                            "name":"held", "arguments":"{}"
                                        }}).to_string()}}),
                                    json!({"type":"response.output_item.added", "item":{
                                        "type":"message", "id":"unfinished", "role":"assistant",
                                        "content":[]}}),
                                ].into_iter().map(|event| format!("data: {event}\n\n")).collect::<String>();
                                let opening = futures_util::stream::iter([Ok::<_, std::convert::Infallible>(
                                    Frame::data(Bytes::from(opening))
                                )]);
                                let completion = futures_util::stream::once(async move {
                                    terminal.acquire().await.unwrap().forget();
                                    let event = json!({"type":"response.completed", "response":{
                                        "id":"rejected-after-retention-failure",
                                        "usage":{"input_tokens":1,"output_tokens":0,"total_tokens":1}}});
                                    Ok::<_, std::convert::Infallible>(Frame::data(Bytes::from(format!("data: {event}\n\n"))))
                                });
                                let body = StreamBody::new(Box::pin(opening.chain(completion)));
                                Ok::<_, std::convert::Infallible>(hyper::Response::builder()
                                    .header("content-type", "text/event-stream").body(body).unwrap())
                            }
                        });
                        let _ = hyper::server::conn::http1::Builder::new()
                            .serve_connection(TokioIo::new(socket), service).await;
                    });
                }
            }
        });
        let home = tempfile::tempdir().unwrap();
        let engine = Arc::new(EngineState::new(
            json!({
                "model":"gpt-5.2", "model_provider":"fixture",
                "model_providers.fixture":{"name":"history fixture", "base_url":url,
                    "wire_api":"responses", "requires_openai_auth":false,
                    "supports_websockets":false,"request_max_retries":2,"stream_max_retries":2},
                "approval_policy":"never", "sandbox_mode":"read-only", "web_search":"disabled",
                "agents.enabled":false,"features.code_mode":false,"features.shell_snapshot":false,
                "features.instant_interrupt":false
            }).to_string(),
            json!({"codex_home":home.path(),"cwd":home.path()}).to_string(), false, None,
        ).await.unwrap());
        let specs = vec![serde_json::from_value(json!({
            "type":"namespace", "name":"_agents_sdk", "description":"SDK managed call",
            "tools":[{"type":"function", "name":"dispatch", "description":"test tool",
                "inputSchema":{"type":"object","properties":{}}}]
        })).unwrap()];
        let options = serde_json::from_value(json!({
            "_application_instructions":"Call held.",
            "tool_policy":{"allowed_tools":[{"namespace":"_agents_sdk","name":"dispatch"}]}
        })).unwrap();
        let native = engine.start_with_tools(options, None, Some(specs), false).await.unwrap();
        let state = native.current().unwrap();
        let tools = state.thread.thread_extension_data().get::<PythonTools>().unwrap();
        let started = crate::ops::submit_replaceable(&state, json!({
            "input":[UserInput::Text {text:"hold the tool and reject the terminal".into(),text_elements:vec![]}]
        })).await.unwrap();
        let (key, invocation): (PendingKey, u64) = loop {
            let event = state.next_session_event().await.unwrap();
            assert_ne!(event["msg"]["type"], "_sdk_model_generation_failed");
            if event["msg"]["type"] == "dynamic_tool_call_request" {
                break (
                    serde_json::from_value(event["msg"].clone()).unwrap(),
                    event["msg"]["_sdk_invocation"].as_u64().unwrap(),
                );
            }
        };
        let owner = state.id.to_string();
        tools.check_pending(&owner, &key).unwrap();
        assert!(tools.admit_callback(&owner, &key, invocation, true).unwrap());
        let store = engine.store.as_any().downcast_ref::<MemoryThreadStore>().unwrap();
        assert!(!store.fail_appends.load(Ordering::SeqCst));
        assert_eq!(store.failed_response_observation_appends.load(Ordering::SeqCst), 0);
        store.fail_response_observation_appends.store(true, Ordering::SeqCst);
        terminal.add_permits(1);
        let mut errors = 0;
        let mut error_messages = Vec::new();
        let mut terminals = 0;
        let errors_at_first_terminal;
        loop {
            let event = state.next_session_event().await.unwrap();
            assert_ne!(event["msg"]["type"], "_sdk_model_generation_failed",
                "generic integrity rejection must not replace the persistence error");
            if event["msg"]["type"] == "error" {
                assert_eq!(store.failed_response_observation_appends.load(Ordering::SeqCst), 1,
                    "an unrelated append must not trigger the tested failure");
                error_messages.push(event["msg"]["message"].as_str().unwrap().to_string());
                errors += 1;
            }
            if matches!(event["msg"]["type"].as_str(), Some("task_complete" | "turn_complete")) {
                assert_eq!(event["id"], started["turn_id"]);
                terminals += 1;
                errors_at_first_terminal = errors;
                break;
            }
            assert_ne!(event["msg"]["type"], "turn_aborted",
                "the sampling child must not cancel the owning turn");
        }
        assert!(tools.check_pending(&owner, &key).is_err());
        assert!(tools.take_reply(&owner, &key).unwrap().is_none(),
            "the joined callback must refuse a late reply");
        // Check joined ownership before waiting: the trailing drain must not
        // hide a callback that retires only after terminal delivery.
        while let Ok(event) = tokio::time::timeout(Duration::from_millis(50), state.next_session_event()).await {
            let event = event.unwrap();
            assert_ne!(event["msg"]["type"], "_sdk_model_generation_failed",
                "a late integrity rejection must not replace the persistence error");
            assert_ne!(event["msg"]["type"], "turn_aborted",
                "the sampling child must not cancel the owning turn after terminal delivery");
            if event["msg"]["type"] == "error" {
                assert_eq!(store.failed_response_observation_appends.load(Ordering::SeqCst), 1,
                    "an unrelated append must not trigger the tested failure");
                errors += 1;
                error_messages.push(event["msg"]["message"].as_str().unwrap().to_string());
            }
            if matches!(event["msg"]["type"].as_str(), Some("task_complete" | "turn_complete")) {
                assert_eq!(event["id"], started["turn_id"]);
                terminals += 1;
            }
        }
        assert_eq!(errors_at_first_terminal, 1,
            "the held tool's Fatal must precede its first terminal: {error_messages:?}");
        assert_eq!(errors, 1, "the held tool's owning task must publish Fatal once: {error_messages:?}");
        assert_eq!(terminals, 1, "the held tool's owning task must settle once");
        assert_eq!(error_messages[0],
            "Fatal error: Canonical thread persistence failed; execution stopped pending recovery");
        assert_eq!(requests.load(Ordering::SeqCst), 1);
        assert_eq!(store.failed_response_observation_appends.load(Ordering::SeqCst), 1,
            "the intended observation append must actually fail exactly once");
        store.fail_response_observation_appends.store(false, Ordering::SeqCst);
        let history = state.thread.load_history(true).await.unwrap();
        assert!(!history.items.iter().any(|item| matches!(item, RolloutItem::ResponseObservationRecord(_))));
        engine.close().await.unwrap();
        server.abort();
        let _ = server.await;
    }).await.unwrap();
}
