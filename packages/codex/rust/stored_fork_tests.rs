//! Exact unpublished ownership after a startup wrapper/manager handoff fails.
use super::*;
use serde_json::json;

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn lost_wrapper_without_manager_entry_retains_raw_shutdown_and_rollback_retry() {
    let directory = tempfile::tempdir().unwrap();
    let home = directory.path().join("home");
    let work = directory.path().join("work");
    let storage = directory.path().join("storage");
    for path in [&home, &work, &storage] {
        std::fs::create_dir(path).unwrap();
    }
    let config = json!({
        "model": "receipt-fixture", "model_provider": "fixture",
        "model_providers.fixture": {
            "name": "Receipt fixture", "base_url": "http://127.0.0.1:9/v1",
            "wire_api": "responses", "requires_openai_auth": false,
            "supports_websockets": false, "request_max_retries": 0, "stream_max_retries": 0
        },
        "approval_policy": "never", "sandbox_mode": "read-only", "web_search": "disabled",
        "agents.enabled": false, "features.code_mode": false, "features.instant_interrupt": false
    });
    let runtime = json!({"codex_home":home,"cwd":work,"native_thread_store":storage,
        "codex_self_exe":null,"ephemeral":false});
    let engine = Arc::new(
        EngineState::new(
            config.to_string(),
            runtime.to_string(),
            false,
            Some("fixture-unused-key".into()),
        )
        .await
        .unwrap(),
    );
    let probe = tokio::spawn({
        let engine = Arc::clone(&engine);
        async move {
            let source = engine
                .start_with_tools(
                    options::ThreadOptions::default(),
                    None,
                    Some(Vec::new()),
                    false,
                )
                .await
                .unwrap();
            let source_state = source.current().unwrap();
            let copy = engine
                .fork_stored(&source_state, ForkBoundary::Latest)
                .await
                .unwrap();
            let copy_state = copy.current().unwrap();
            let id = copy_state.id;
            // Publish a referencing branch to make rollback fail after writer retirement.
            let child = engine
                .fork_stored(&copy_state, ForkBoundary::Latest)
                .await
                .unwrap();
            let child_state = child.current().unwrap();
            child_state.close().await.unwrap();
            assert!(engine.finish_stored_fork(child_state.id));
            let raw = Arc::clone(&copy_state.thread);
            let raw_weak = Arc::downgrade(&raw);
            let state_weak = Arc::downgrade(&copy_state);
            // Engine::wrap's historical failure path can remove this entry even
            // when its first shutdown fails. No manager entry proves no shutdown.
            engine.manager.remove_thread_if_matches(&id, &raw).await;
            drop(raw);
            drop(copy_state);
            drop(copy);
            assert!(
                state_weak.upgrade().is_none(),
                "registration must not retain SDK state"
            );
            assert!(
                raw_weak.upgrade().is_some(),
                "raw writer receipt must survive lost wrapper"
            );
            assert!(engine.manager.get_thread(id).await.is_err());
            let _lifecycle = engine.lifecycle.lock().await;
            assert!(
                engine
                    .discard_stored_forks(Some(source_state.id))
                    .await
                    .is_err()
            );
            assert!(
                raw_weak.upgrade().is_some(),
                "failed rollback must retain retry ownership"
            );
            engine
                .rollback_checkpoint_import(&[child_state.id], Some(&child_state))
                .await
                .unwrap();
            engine
                .discard_stored_forks(Some(source_state.id))
                .await
                .unwrap();
            assert!(
                raw_weak.upgrade().is_none(),
                "successful rollback releases raw ownership"
            );
            let read = |thread_id| ReadThreadParams {
                thread_id,
                include_archived: true,
                include_history: false,
            };
            let actual = engine.store.read_thread(read(id)).await.unwrap_err();
            let missing = engine
                .store
                .read_thread(read(ThreadId::new()))
                .await
                .unwrap_err();
            assert_eq!(
                std::mem::discriminant(&actual),
                std::mem::discriminant(&missing)
            );
            assert!(
                engine
                    .store
                    .read_thread(ReadThreadParams {
                        thread_id: source_state.id,
                        include_archived: true,
                        include_history: false,
                    })
                    .await
                    .is_ok(),
                "source record must survive rollback"
            );
            drop(_lifecycle);
            source_state.close().await.unwrap();
        }
    })
    .await;
    engine.close().await.unwrap();
    probe.unwrap();
}
