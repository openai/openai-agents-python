//! Suspend while an actual Core tool handler is between SDK reply acceptance
//! and Core's terminal-outcome commit. The hold exists only in native tests.
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use bytes::Bytes;
use codex_protocol::dynamic_tools::DynamicToolResponse;
use codex_protocol::turn_input::{TurnInput, TurnInputRequest};
use codex_protocol::user_input::UserInput;
use http_body_util::{BodyExt, Full};
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use serde_json::json;
use tokio::sync::Semaphore;

use super::{PendingKey, PythonTools};
use crate::{NativeThread, engine::EngineState, manual_checkpoint};

pub(crate) struct BeforeResult {
    pub(crate) call_id: String,
    pub(crate) entered: Semaphore,
    pub(crate) release: Semaphore,
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn automatic_reply_waits_for_core_terminal_before_sealing() {
    checkpoint_with_completed_sibling(false).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn suspension_joins_a_completed_siblings_result_write() {
    checkpoint_with_completed_sibling(true).await;
}

async fn checkpoint_with_completed_sibling(hold_append: bool) {
    tokio::time::timeout(std::time::Duration::from_secs(15), async {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let requests = Arc::new(AtomicUsize::new(0));
        let received = requests.clone();
        let server = tokio::spawn(async move {
            loop {
                let (socket, _) = listener.accept().await.unwrap();
                let received = received.clone();
                tokio::spawn(async move {
                    hyper::server::conn::http1::Builder::new()
                        .serve_connection(TokioIo::new(socket), service_fn(move |request: hyper::Request<hyper::body::Incoming>| {
                            let received = received.clone();
                            async move {
                                let _body = request.into_body().collect().await.unwrap();
                                received.fetch_add(1, Ordering::SeqCst);
                                let mut body = String::new();
                                for (index, name, call) in [(0, "lookup", "call-A"), (1, "record", "call-B")] {
                                    body.push_str(&format!("data: {}\n\n", json!({
                                        "type":"response.output_item.done", "output_index":index,
                                        "item":{"id":format!("fc-{call}"),"type":"function_call",
                                        "name":name,"arguments":"{}","call_id":call}
                                    })));
                                }
                                body.push_str(&format!("data: {}\n\n", json!({
                                    "type":"response.completed", "response":{"id":"resp-mixed","output":[],"usage":{
                                        "input_tokens":3,"output_tokens":2,"total_tokens":5
                                    }}
                                })));
                                Ok::<_, std::convert::Infallible>(
                                    hyper::Response::builder()
                                        .header("content-type", "text/event-stream")
                                        .body(Full::new(Bytes::from(body))).unwrap()
                                )
                            }
                        })).await.unwrap();
                });
            }
        });
        let home = tempfile::tempdir().unwrap();
        let config = json!({
            "model":"gpt-5.1-codex", "model_provider":"fixture",
            "model_providers.fixture":{
                "name":"fixture", "base_url":format!("http://{addr}/v1"),
                "wire_api":"responses", "supports_websockets":false,
                "requires_openai_auth":false,
                "request_max_retries":0,"stream_max_retries":0
            },
            "features.code_mode":false, "features.shell_snapshot":false,
            "approval_policy":"never", "sandbox_mode":"danger-full-access",
            "web_search":"disabled"
        });
        let runtime = json!({"codex_home":home.path(), "cwd":home.path()});
        let engine = Arc::new(EngineState::new(config.to_string(), runtime.to_string(), false, None)
            .await.unwrap());
        let specs: Vec<codex_core_api::DynamicToolSpec> = ["lookup", "record"].into_iter().map(|name| {
            serde_json::from_value(json!({
                "type":"function", "name":name, "description":name,
                "inputSchema":{"type":"object","properties":{},"additionalProperties":false}
            })).unwrap()
        }).collect();
        let options = serde_json::from_value(json!({
            "_application_instructions":"Run both tools.", "_manual_tool_slots":[0],
            "tool_policy":{"allowed_tools":[
                {"namespace":"functions","name":"lookup"},
                {"namespace":"functions","name":"record"}
            ]}
        })).unwrap();
        let native: NativeThread = engine.start_with_tools(options, None, Some(specs.clone()), false)
            .await.unwrap();
        let state = native.current().unwrap();
        crate::application_instructions::publish(&state.thread, "Run both tools.".into())
            .await.unwrap();
        let tools = state.thread.thread_extension_data().get::<PythonTools>().unwrap();
        let hold = Arc::new(BeforeResult {
            call_id: "call-B".into(), entered: Semaphore::new(0), release: Semaphore::new(0),
        });
        *tools.before_result.lock().unwrap() = Some(hold.clone());
        state.thread.start_turn_if_idle(TurnInputRequest::new(TurnInput::UserInput {
            content: vec![UserInput::Text { text: "go".into(), text_elements: vec![] }],
            client_id: None,
        })).await.unwrap();

        // Receive both notices so A is a live manual grant and B has a live
        // SDK sender. The SDK's successful send is deliberately before Core
        // can commit a terminal outcome.
        for _ in 0..2 {
            let request = super::managed_tests::next_callback_request(&tools).await;
            let key: PendingKey = serde_json::from_value(request["msg"].clone()).unwrap();
            if key.call == "call-B" {
                let sender = tools.take_reply(&state.id.to_string(), &key).unwrap().unwrap();
                let response: DynamicToolResponse = serde_json::from_value(json!({
                    "contentItems":[{"type":"inputText","text":"known-B"}], "success":true
                })).unwrap();
                sender.send(response.into()).unwrap();
            }
        }
        hold.entered.acquire().await.unwrap().forget();
        let version = tools.manual_progress();
        assert!(!manual_checkpoint::ready(&state, &native.manual).await.unwrap());
        assert!(manual_checkpoint::capture(&state, &native.manual, None, false).await.unwrap().is_none());

        hold.release.add_permits(1);
        // This must also work if Core finishes before the waiter subscribes.
        tools.wait_manual_progress(version).await.unwrap();
        assert!(manual_checkpoint::ready(&state, &native.manual).await.unwrap());
        let checkpoint = if hold_append {
            let hold = Arc::new(crate::storage::CompletionHold {
                parent: state.id,
                entered: Semaphore::new(0),
                release: Semaphore::new(0),
            });
            let store = engine.store.as_any()
                .downcast_ref::<crate::storage::MemoryThreadStore>().unwrap();
            *store.tool_output_hold.lock().unwrap() = Some(("call-B".into(), hold.clone()));
            let capture = manual_checkpoint::capture(&state, &native.manual, None, false);
            tokio::pin!(capture);
            tokio::select! {
                entered = hold.entered.acquire() => entered.unwrap().forget(),
                _ = &mut capture => panic!("Capture finished before the exact sibling entered the writer"),
            }
            // The old 100 ms forced abort returned a checkpoint without call-B.
            // Keep the exact append blocked beyond that deadline.
            let early = tokio::time::timeout(std::time::Duration::from_millis(250), &mut capture)
                .await;
            store.tool_output_hold.lock().unwrap().take();
            hold.release.add_permits(1);
            assert!(early.is_err(), "Capture finished without joining the sibling write");
            capture.await.unwrap().unwrap()
        } else {
            manual_checkpoint::capture(&state, &native.manual, None, false).await.unwrap().unwrap()
        };
        assert_eq!(checkpoint["format_version"], 3);
        manual_checkpoint::validate_for_opening(
            &checkpoint, "Run both tools.", &specs, &[0], None,
        ).unwrap();
        assert_eq!(checkpoint["native_continuation"]["pending_manual"][0]["call_id"], "call-A");
        let replies = checkpoint["native_continuation"]["accepted_replies"].as_object().unwrap();
        assert_eq!(replies.len(), 1);
        let reply: codex_history::RolloutItem =
            serde_json::from_value(replies["call-B"].clone()).unwrap();
        let codex_history::RolloutItem::ResponseItem(reply) = reply else {
            panic!("Accepted reply must preserve its canonical envelope");
        };
        assert!(matches!(reply.item,
            codex_protocol::models::ResponseItem::FunctionCallOutput { call_id, .. }
                if call_id.as_deref() == Some("call-B")
        ));
        assert!(replies["call-B"].to_string().contains("known-B"), "{replies:?}");
        assert!(!replies["call-B"].to_string().contains("aborted"), "{replies:?}");
        assert_eq!(requests.load(Ordering::SeqCst), 1);
        state.close().await.unwrap();
        engine.close().await.unwrap();
        server.abort();
        let _ = server.await;
    }).await.expect("manual settlement stalled");
}
