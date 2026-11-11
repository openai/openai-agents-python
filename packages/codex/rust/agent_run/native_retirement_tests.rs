//! Test-induced EngineOwner mutex poison; no production fault hook or Core change.
use super::*;
use bytes::Bytes;
use futures_util::FutureExt;
use http_body_util::{BodyExt, Full};
use hyper::{Request, Response, body::Incoming, server::conn::http1, service::service_fn};
use hyper_util::rt::TokioIo;
use pyo3::exceptions::PyRuntimeError;
use pyo3::types::PyModule;
use std::convert::Infallible;
use std::panic::AssertUnwindSafe;
use std::sync::atomic::{AtomicUsize, Ordering};
use tokio::net::TcpListener;
use tokio::sync::{Semaphore, oneshot};

type EngineSlot = Arc<Mutex<crate::EngineOwner>>;

#[test]
#[cfg(target_os = "linux")]
fn actual_thread_join_survives_engine_slot_poison_then_retry_releases_lease() {
    Python::initialize();
    let (provider, lease_object) = Python::attach(|py| -> PyResult<_> {
        let provider = py
            .get_type::<provider_owner::NativeProviderOwner>()
            .call0()?
            .extract::<Py<provider_owner::NativeProviderOwner>>()?;
        let lease = provider
            .bind(py)
            .call_method0("acquire")?
            .extract::<Py<provider_owner::NativeProviderLease>>()?;
        Ok((provider, lease))
    })
    .expect("existing Provider constructors");
    let lease = Python::attach(|py| lease_object.borrow(py).inner.clone());
    let fault_slot = Arc::new(Mutex::new(None::<EngineSlot>));
    let release = Arc::new(Semaphore::new(0));
    let stop = CancellationToken::new();
    let request_count = Arc::new(AtomicUsize::new(0));

    // Keep recovery outside the assertion/unwind boundary. Even a failed
    // assertion must clear our poison before existing Provider cleanup runs.
    let outcome = pyo3_async_runtimes::tokio::get_runtime().block_on(async {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (request_seen, admitted) = oneshot::channel();
        let request_seen = Arc::new(Mutex::new(Some(request_seen)));
        let peer_release = release.clone();
        let peer_stop = stop.clone();
        let requests = request_count.clone();
        let peer = tokio::spawn(async move {
            let accepted = tokio::select! {
                _ = peer_stop.cancelled() => return,
                accepted = listener.accept() => accepted.unwrap(),
            };
            let service = service_fn(move |request: Request<Incoming>| {
                let release = peer_release.clone();
                let seen = request_seen.clone();
                let requests = requests.clone();
                async move {
                    assert!(request.uri().path().ends_with("/responses"));
                    let body = request.into_body().collect().await.unwrap().to_bytes();
                    let _: Value = serde_json::from_slice(&body).unwrap();
                    requests.fetch_add(1, Ordering::SeqCst);
                    if let Some(seen) = seen.lock().unwrap().take() {
                        let _ = seen.send(());
                    }
                    release.acquire().await.unwrap().forget();
                    let item = json!({"type":"message", "id":"msg_retirement",
                        "role":"assistant", "status":"completed", "phase":"final_answer",
                        "content":[{"type":"output_text","text":"done","annotations":[]}]});
                    let rows = [
                        json!({"type":"response.created","response":{"id":"resp_retirement"}}),
                        json!({"type":"response.output_item.done","item":item}),
                        json!({"type":"response.completed","response":{
                            "id":"resp_retirement","output":[item],
                            "usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}),
                    ];
                    let body = rows.into_iter().map(|row| format!("data: {row}\n\n"))
                        .collect::<String>();
                    Ok::<_, Infallible>(Response::builder()
                        .header("content-type", "text/event-stream")
                        .body(Full::new(Bytes::from(body))).unwrap())
                }
            });
            tokio::select! {
                _ = peer_stop.cancelled() => {},
                served = http1::Builder::new().serve_connection(TokioIo::new(accepted.0), service) => {
                    served.unwrap();
                },
            }
        });

        let checked = AssertUnwindSafe(async {
            let config = json!({
                "model":"gpt-5.1-codex", "model_provider":"retirement",
                "model_providers.retirement":{
                    "name":"Retirement fixture", "base_url":format!("http://{address}/v1"),
                    "wire_api":"responses", "requires_openai_auth":false,
                    "supports_websockets":false},
                "include_environment_context":false, "include_permissions_instructions":false,
                "agents.enabled":false, "web_search":"disabled",
                "tools.update_plan.enabled":false, "features.code_mode":false,
                "features.goals":false
            }).to_string();
            let runtime = json!({"strict_config":true,"base_instructions":"Reply plainly.",
                "loader":{"ignore_user_config":true,"ignore_project_config":true}}).to_string();
            let scope = crate::conversation_opening(config, runtime,
                "{\"_application_instructions\":\"\"}", "[]", "{}",
                "test-key".into(), lease.clone(), false, false).unwrap().await.unwrap();
            assert!(scope.2.is_none(), "test must use the shared no-home profile");
            assert!(!scope.0.current().unwrap().owns_runtime_services());
            let engine_slot = scope.0.inner.clone();
            let thread_slot = scope.1.inner.clone();
            *fault_slot.lock().unwrap() = Some(engine_slot.clone());
            let native = Python::attach(|py| NativeRun::new(async move { Ok(scope) },
                lease_object.borrow(py), json!({"mode":"start_if_idle",
                    "input":[{"type":"text","text":"hello"}],
                    "observation_submission_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}).to_string()));
            let inputs = native.inputs.lock().unwrap().take().unwrap();
            let poison = async {
                admitted.await.expect("real request proves native admission preceded fault");
                let target = engine_slot.clone();
                let poisoned = std::thread::spawn(move || {
                    let _guard = target.lock().unwrap();
                    panic!("test-induced EngineOwner mutex poison");
                }).join();
                assert!(poisoned.is_err(), "poisoning thread must physically join");
                let expected = match engine_slot.lock() {
                    Ok(_) => panic!("test did not poison EngineOwner"),
                    Err(error) => error.to_string(),
                };
                release.add_permits(1);
                expected
            };
            let (result, expected_error) = tokio::time::timeout(
                std::time::Duration::from_secs(15),
                async { tokio::join!(execute(inputs, native.cancelled.clone()), poison) },
            ).await.expect("bounded native execution and joined fault producer");
            let output = result.expect("owner returns adoptable failure facts");
            assert_eq!(output.info["status"], "retirement_failed");
            assert_eq!(output.info["output"], "done");
            assert_eq!(output.info["retired"], json!({"thread":true,"engine":false}));
            assert_eq!(output.failures.len(), 1);
            assert_eq!(output.failures[0].0, "engine");
            Python::attach(|py| {
                let error = &output.failures[0].1;
                assert!(error.is_instance_of::<PyRuntimeError>(py));
                assert_eq!(error.value(py).str().unwrap().to_str().unwrap(), expected_error);
                let rejected = provider.bind(py).call_method0("close")
                    .err().expect("Provider must still retain the opening lease");
                assert!(rejected.to_string().contains("open or opening Sessions"));
            });
            assert!(thread_slot.lock().unwrap().live.is_none(), "real Thread helper joined/released");
            assert!(engine_slot.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
                .live.is_some());
            assert!(lease.check_live().is_ok());
            assert!(lease.keeps_unpublished_owner());
            assert_eq!(request_count.load(Ordering::SeqCst), 1);

            engine_slot.clear_poison(); // Undo only this test's introduced fault.
            crate::close_thread_owner(thread_slot, None).await.unwrap();
            crate::close_engine_owner(engine_slot.clone()).await.unwrap();
            assert!(engine_slot.lock().unwrap().live.is_none());
            lease.release().unwrap();
            assert!(!lease.keeps_unpublished_owner());
            assert!(lease.check_live().is_err());
            drop(output);
        }).catch_unwind().await;

        if let Some(slot) = fault_slot.lock().unwrap().as_ref() {
            slot.clear_poison();
        }
        release.add_permits(1);
        // Provider cleanup below may still need the peer while joining errors.
        (checked, peer)
    });

    let (checked, peer) = outcome;
    let pending = lease.check_live().is_ok();
    let cleanup = Python::attach(|py| -> PyResult<()> {
        let script = std::ffi::CString::new(
            "import asyncio\nasync def retire(provider, lease, pending):\n    if pending:\n        lease.orphan()\n        await lease.close_orphan()\n    await provider.close()\ndef close(provider, lease, pending):\n    asyncio.run(retire(provider, lease, pending))\n"
        ).unwrap();
        let module =
            PyModule::from_code(py, &script, c"retirement_cleanup.py", c"retirement_cleanup")?;
        module
            .getattr("close")?
            .call1((provider.bind(py), lease_object.bind(py), pending))?;
        Ok(())
    });
    stop.cancel();
    let peer_join = pyo3_async_runtimes::tokio::get_runtime().block_on(peer);
    if let Err(primary) = checked {
        if let Err(error) = cleanup {
            eprintln!("cleanup also failed: {error}");
        }
        if let Err(error) = peer_join {
            eprintln!("peer join also failed: {error}");
        }
        std::panic::resume_unwind(primary);
    }
    cleanup.expect("Provider cleanup physically joins retained runtime");
    peer_join.expect("loopback peer physically joined");
}
