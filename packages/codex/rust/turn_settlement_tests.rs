//! Real Core terminal/idle ordering, held only in the SDK's test ThreadStore.
use super::*;
use crate::storage::CompletionHold;
use crate::turn_settlement::{self, Settlement};
use bytes::Bytes;
use codex_protocol::protocol::{SubAgentSource, ThreadSource};
use codex_protocol::turn_input::{
    NotSubmittedReason, StartIfIdleSubmission, TurnInput, TurnInputRequest, TurnStartOptions,
};
use codex_protocol::user_input::UserInput;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, Response};
use hyper_util::rt::TokioIo;
use serde_json::{Value, json};
use std::convert::Infallible;
use tokio::net::TcpListener;
use tokio::sync::Semaphore;
use tokio::task::{JoinHandle, JoinSet};

pub(crate) struct Provider {
    pub(crate) url: String,
    pub(crate) requests: Arc<std::sync::Mutex<Vec<Value>>>,
    pub(crate) first_child_sample: Arc<std::sync::Mutex<Option<Arc<ChildSampleHold>>>>,
    task: JoinHandle<()>,
}

pub(crate) struct ChildSampleHold {
    pub(crate) entered: Semaphore,
    pub(crate) release: Semaphore,
}

impl Provider {
    pub(crate) async fn start() -> Self {
        Self::with_first_item(None).await
    }

    pub(crate) async fn with_first_item(first: Option<Value>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/v1", listener.local_addr().unwrap());
        let requests = Arc::new(std::sync::Mutex::new(Vec::new()));
        let received = requests.clone();
        let first_child_sample = Arc::new(std::sync::Mutex::new(None::<Arc<ChildSampleHold>>));
        let sample = first_child_sample.clone();
        let task = tokio::spawn(async move {
            let mut connections = JoinSet::new();
            loop {
                let (tcp, _) = listener.accept().await.unwrap();
                let received = received.clone();
                let first = first.clone();
                let sample = sample.clone();
                connections.spawn(async move {
                    let service = service_fn(move |request: Request<Incoming>| {
                        let received = received.clone();
                        let first = first.clone();
                        let sample = sample.clone();
                        async move {
                            assert!(request.uri().path().ends_with("/responses"));
                            let raw = request.into_body().collect().await.unwrap().to_bytes();
                            let body: Value = serde_json::from_slice(&raw).unwrap();
                            let is_child = body["client_metadata"]["x-codex-turn-metadata"]
                                .as_str()
                                .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
                                .is_some_and(|metadata| metadata["agent_name"].as_str()
                                    .is_some_and(|name| name.starts_with("/root/")));
                            let index = {
                                let mut requests = received.lock().unwrap();
                                requests.push(body);
                                requests.len()
                            };
                            let hold = if is_child {
                                sample.lock().unwrap().take()
                            } else {
                                None
                            };
                            if let Some(hold) = hold {
                                hold.entered.add_permits(1);
                                hold.release.acquire().await.unwrap().forget();
                            }
                            let id = format!("resp_{index}");
                            let item = first.filter(|_| index == 1).unwrap_or_else(|| json!({"type":"message", "id":format!("msg_{index}"),
                                "role":"assistant", "status":"completed", "phase":"final_answer",
                                "content":[{"type":"output_text", "text":"done", "annotations":[]}]}));
                            let events = [
                                json!({"type":"response.created","response":{"id":id}}),
                                json!({"type":"response.output_item.done","item":item}),
                                json!({"type":"response.completed","response":{"id":id,"output":[item],
                                    "usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}),
                            ];
                            let body = events.into_iter().map(|e| format!("data: {e}\n\n")).collect::<String>();
                            Ok::<_, Infallible>(Response::builder()
                                .header("content-type", "text/event-stream")
                                .body(Full::new(Bytes::from(body))).unwrap())
                        }
                    });
                    let _ = http1::Builder::new().serve_connection(TokioIo::new(tcp), service).await;
                });
            }
        });
        Self {
            url,
            requests,
            first_child_sample,
            task,
        }
    }

    fn child_requests(&self, id: ThreadId) -> usize {
        self.requests
            .lock()
            .unwrap()
            .iter()
            .filter(|request| {
                let metadata: Value = serde_json::from_str(
                    request["client_metadata"]["x-codex-turn-metadata"]
                        .as_str()
                        .unwrap(),
                )
                .unwrap();
                metadata["thread_id"] == id.to_string()
            })
            .count()
    }
}

impl Drop for Provider {
    fn drop(&mut self) {
        self.task.abort();
    }
}

struct Fixture {
    _home: tempfile::TempDir,
    provider: Provider,
    engine: Arc<EngineState>,
    _parent: crate::NativeThread,
    _child: crate::NativeThread,
    state: Arc<ThreadState>,
    hold: Arc<CompletionHold>,
    old_turn: String,
}

pub(crate) fn request(text: &str) -> TurnInputRequest {
    TurnInputRequest {
        input: TurnInput::UserInput {
            content: vec![UserInput::Text {
                text: text.to_owned(),
                text_elements: vec![],
            }],
            client_id: None,
        },
        thread_settings: Default::default(),
        start: TurnStartOptions {
            // A real native child notifies this parent after queuing its terminal.
            parent_turn_id: Some("settlement-parent-turn".to_owned()),
            ..Default::default()
        },
        additional_context: Default::default(),
        responsesapi_client_metadata: None,
        trace: None,
    }
}

async fn managed(state: &ThreadState, text: &str) -> StartIfIdleSubmission {
    let mut mutation = Some(state.mutations.lock().await);
    let init = state.thread.config_snapshot().await.turn_extension_init;
    turn_settlement::start(state, &mut mutation, request(text), init, None)
        .await
        .unwrap()
}

async fn terminal(state: &Arc<ThreadState>, turn_id: &str) {
    loop {
        let event = state.next_event().await.unwrap();
        if event["msg"]["type"] == "task_complete" && event["msg"]["turn_id"] == turn_id {
            assert!(event["subagent_id"].is_null());
            return;
        }
    }
}

impl Fixture {
    async fn held() -> Self {
        let provider = Provider::start().await;
        let home = tempfile::tempdir().unwrap();
        let config = json!({
            "model":"gpt-5.1-codex", "model_provider":"settlement",
            "model_providers.settlement":{
                "name":"Native settlement fixture", "base_url":provider.url,
                "wire_api":"responses", "requires_openai_auth":false,
                "supports_websockets":false,"request_max_retries":0,"stream_max_retries":0,
                "stream_idle_timeout_ms":5000
            },
            "approval_policy":"never", "sandbox_mode":"danger-full-access", "web_search":"disabled",
            "agents.enabled":true, "features.multi_agent_v2":true,
            "features.shell_snapshot":false, "features.code_mode":false, "analytics.enabled":false
        });
        let runtime = json!({"codex_home":home.path(), "cwd":home.path()});
        let engine = Arc::new(
            EngineState::new(config.to_string(), runtime.to_string(), false, None)
                .await
                .unwrap(),
        );
        let parent = engine.start(Default::default(), None).await.unwrap();
        let parent_state = parent.current().unwrap();
        let source = SessionSource::SubAgent(SubAgentSource::ThreadSpawn {
            parent_thread_id: parent_state.id,
            depth: 1,
            agent_path: Some(
                codex_protocol::AgentPath::root()
                    .join("settlement")
                    .unwrap(),
            ),
            agent_nickname: None,
            agent_role: None,
        });
        let options = serde_json::from_value(json!({
            "session_source":source, "thread_source":ThreadSource::Subagent,
        }))
        .unwrap();
        let child = engine.start(options, None).await.unwrap();
        let state = Arc::clone(&child.current().unwrap());
        let hold = Arc::new(CompletionHold {
            parent: parent_state.id,
            entered: Semaphore::new(0),
            release: Semaphore::new(0),
        });
        *engine
            .store
            .as_any()
            .downcast_ref::<crate::storage::MemoryThreadStore>()
            .unwrap()
            .completion_hold
            .lock()
            .unwrap() = Some(hold.clone());
        let StartIfIdleSubmission::Started { turn_id } = managed(&state, "first input").await
        else {
            panic!("first managed input was not accepted");
        };
        hold.entered.acquire().await.unwrap().forget();
        terminal(&state, &turn_id).await;
        // This is the actual unfixed Core result, not an injected NotIdle.
        assert_eq!(
            state
                .thread
                .start_turn_if_idle(request("unaccepted probe"))
                .await
                .unwrap(),
            StartIfIdleSubmission::NotSubmitted {
                reason: NotSubmittedReason::NotIdle
            }
        );
        Self {
            _home: home,
            provider,
            engine,
            _parent: parent,
            _child: child,
            state,
            hold,
            old_turn: turn_id,
        }
    }

    async fn retry(&self) -> JoinHandle<StartIfIdleSubmission> {
        let state = self.state.clone();
        let task = tokio::spawn(async move { managed(&state, "second input").await });
        self.state
            .thread
            .thread_extension_data()
            .get::<Settlement>()
            .unwrap()
            .waiting
            .acquire()
            .await
            .unwrap()
            .forget();
        assert!(!task.is_finished());
        task
    }

    fn release(&self) {
        *self
            .engine
            .store
            .as_any()
            .downcast_ref::<crate::storage::MemoryThreadStore>()
            .unwrap()
            .completion_hold
            .lock()
            .unwrap() = None;
        self.hold.release.add_permits(1);
    }

    async fn close(self) {
        self.release();
        self.state.close().await.unwrap();
        self.engine.close().await.unwrap();
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn delivered_terminal_retries_only_after_core_settles() {
    tokio::time::timeout(Duration::from_secs(15), async {
        let fixture = Fixture::held().await;
        let retry = fixture.retry().await;
        assert_eq!(fixture.provider.child_requests(fixture.state.id), 1);
        fixture.release();
        let StartIfIdleSubmission::Started { turn_id } = retry.await.unwrap() else {
            panic!("settled native slot did not accept the next input");
        };
        assert_ne!(turn_id, fixture.old_turn);
        terminal(&fixture.state, &turn_id).await;
        assert_eq!(fixture.provider.child_requests(fixture.state.id), 2);
        let history = fixture
            .engine
            .load_checkpoint_history(fixture.state.id)
            .await
            .unwrap();
        let items: Vec<_> = history
            .iter()
            .filter_map(|item| match item {
                RolloutItem::ResponseItem(item) => Some(&item.item),
                _ => None,
            })
            .collect();
        let history = serde_json::to_value(items).unwrap().to_string();
        assert!(!history.contains("unaccepted probe"));
        assert_eq!(history.matches("second input").count(), 1);
        fixture.close().await;
    })
    .await
    .unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn missing_idle_returns_original_rejection_without_input() {
    tokio::time::timeout(Duration::from_secs(15), async {
        let fixture = Fixture::held().await;
        assert_eq!(
            fixture.retry().await.await.unwrap(),
            StartIfIdleSubmission::NotSubmitted {
                reason: NotSubmittedReason::NotIdle
            }
        );
        assert_eq!(fixture.provider.child_requests(fixture.state.id), 1);
        fixture.close().await;
    })
    .await
    .unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn close_retires_waiter_without_needing_idle() {
    tokio::time::timeout(Duration::from_secs(15), async {
        let fixture = Fixture::held().await;
        let retry = fixture.retry().await;
        let state = fixture.state.clone();
        let close = tokio::spawn(async move { state.close().await });
        assert_eq!(
            retry.await.unwrap(),
            StartIfIdleSubmission::NotSubmitted {
                reason: NotSubmittedReason::NotIdle
            }
        );
        fixture.release();
        close.await.unwrap().unwrap();
        assert_eq!(fixture.provider.child_requests(fixture.state.id), 1);
        fixture.engine.close().await.unwrap();
    })
    .await
    .unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn competing_native_turn_retires_the_old_retry() {
    tokio::time::timeout(Duration::from_secs(15), async {
        let fixture = Fixture::held().await;
        let retry = fixture.retry().await;
        let mutation = fixture.state.mutations.lock().await;
        // Core interruption can remove the old empty slot. The following
        // acknowledged settings operation orders this test after that command.
        fixture.state.thread.submit(Op::Interrupt).await.unwrap();
        fixture
            .state
            .thread
            .update_thread_settings(codex_protocol::protocol::ThreadSettingsOverrides::default())
            .await
            .unwrap();
        let mut competing = request("competing input");
        competing.start = Default::default();
        let StartIfIdleSubmission::Started { turn_id } = fixture
            .state
            .thread
            .start_turn_if_idle(competing)
            .await
            .unwrap()
        else {
            panic!("competing native turn was not admitted");
        };
        drop(mutation);
        assert_eq!(
            retry.await.unwrap(),
            StartIfIdleSubmission::NotSubmitted {
                reason: NotSubmittedReason::NotIdle,
            }
        );
        terminal(&fixture.state, &turn_id).await;
        assert_eq!(fixture.provider.child_requests(fixture.state.id), 2);
        let history = fixture
            .engine
            .load_checkpoint_history(fixture.state.id)
            .await
            .unwrap();
        assert!(
            !serde_json::to_value(history)
                .unwrap()
                .to_string()
                .contains("second input")
        );
        fixture.close().await;
    })
    .await
    .unwrap();
}
