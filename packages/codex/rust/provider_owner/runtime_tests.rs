//! Real prepared managers and acquisition retirement; no model request or test-only production hook.
use super::super::{NativeProviderLease, NativeProviderOwner};
use super::*;
use serde_json::json;

async fn opening(key: &str, process: bool) -> (NativeProviderOwner, NativeProviderLease, Engine) {
    let owner = NativeProviderOwner::new();
    let lease = owner.acquire().unwrap();
    let source = json!({
        "model":"gpt-5.1-codex", "model_provider":"pool-test",
        "model_providers.pool-test":{
            "name":"Pool test", "base_url":"http://127.0.0.1:1/v1",
            "wire_api":"responses", "requires_openai_auth":false,
            "supports_websockets":false},
        "include_environment_context":false, "include_permissions_instructions":false,
        "agents.enabled":false, "web_search":"disabled",
        "tools.update_plan.enabled":false, "features.code_mode":false, "features.goals":false
    })
    .to_string();
    let runtime = json!({"strict_config":true,"base_instructions":"Reply plainly.",
        "loader":{"ignore_user_config":true,"ignore_project_config":true}})
    .to_string();
    // This tests the owning layer after lib.rs has validated transport intent.
    let (state, home) = lease
        .inner
        .conversation_engine(source, runtime, key.into(), true, process)
        .await
        .unwrap();
    assert!(home.is_none());
    let engine = Engine::new(state);
    lease.inner.register_engine(engine.inner.clone()).unwrap();
    (owner, lease, engine)
}

async fn release(opened: (NativeProviderOwner, NativeProviderLease, Engine)) {
    let (owner, lease, engine) = opened;
    crate::close_engine_owner(engine.inner.clone())
        .await
        .unwrap();
    lease.inner.release().unwrap();
    owner.inner.close_prepared_runtime().await.unwrap();
    owner.inner.model_http_pool.close();
}

struct BlockedHome {
    path: PathBuf,
    backup: PathBuf,
}

impl BlockedHome {
    fn new(path: PathBuf) -> Self {
        let backup = path.with_extension("held");
        std::fs::rename(&path, &backup).unwrap();
        std::fs::write(&path, "block directory removal").unwrap();
        Self { path, backup }
    }

    fn restore(&self) {
        if self.backup.exists() {
            std::fs::remove_file(&self.path).unwrap();
            std::fs::rename(&self.backup, &self.path).unwrap();
        }
    }
}

impl Drop for BlockedHome {
    fn drop(&mut self) {
        if self.backup.exists() {
            let _ = std::fs::remove_file(&self.path);
            let _ = std::fs::rename(&self.backup, &self.path);
        }
    }
}

#[test]
fn matching_scopes_outlive_idle_interval_and_failed_retirement_stays_owned() {
    Python::initialize();
    pyo3_async_runtimes::tokio::get_runtime().block_on(async {
        close_process_runtime().await.unwrap();
        let first = opening("pool-key-a", true).await;
        let first_state = first.2.current().unwrap();
        let manager = first_state.manager.clone();
        let parent = process_slot()
            .unwrap()
            .read()
            .await
            .as_ref()
            .unwrap()
            .engine
            .as_ref()
            .unwrap()
            .current()
            .unwrap();
        assert!(parent.private_function_http_pool.get().is_none());
        let function_http = first_state.private_function_http_pool();
        assert!(parent.private_function_http_pool.get().is_some());
        drop(parent);
        assert!(first_state.private_function_http_pool.get().is_none());
        let factory = codex_http_client::HttpClientFactory::new(
            codex_http_client::OutboundProxyPolicy::ReqwestDefault,
        );
        assert!(
            function_http
                .client(&factory, "invalid-url", false, http::HeaderMap::new())
                .unwrap()
                .is_none()
        );
        let home = process_slot()
            .unwrap()
            .read()
            .await
            .as_ref()
            .unwrap()
            .home
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .path()
            .to_owned();
        // The timer must not queue a writer behind this active acquisition.
        tokio::time::sleep(PROCESS_IDLE + Duration::from_millis(100)).await;
        let second = opening("pool-key-a", true).await;
        let second_http = second.2.current().unwrap().private_function_http_pool();
        assert!(Arc::ptr_eq(&manager, &second.2.current().unwrap().manager));
        assert!(!Arc::ptr_eq(
            &first.2.current().unwrap().agent_graph,
            &second.2.current().unwrap().agent_graph
        ));
        close_idle_at_exit(process_slot().unwrap());
        assert!(home.exists(), "active exit cleanup must skip the parent");

        let mismatch = opening("pool-key-b", true).await;
        assert!(!Arc::ptr_eq(
            &manager,
            &mismatch.2.current().unwrap().manager
        ));
        assert!(
            mismatch
                .1
                .inner
                .record
                .lock()
                .unwrap()
                .process_runtime
                .is_none()
        );
        release(mismatch).await;
        let explicit = opening("pool-key-a", false).await;
        assert!(!Arc::ptr_eq(
            &manager,
            &explicit.2.current().unwrap().manager
        ));
        assert!(
            explicit
                .2
                .current()
                .unwrap()
                .private_function_http_pool
                .get()
                .is_none()
        );
        release(explicit).await;
        assert!(
            home.exists(),
            "explicit cleanup must not own the process parent"
        );

        let extra_home = first.1.home().unwrap();
        let blocked = BlockedHome::new(PathBuf::from(&extra_home.name));
        first.1.orphan().unwrap();
        assert!(first.1.inner.close_orphan().await.is_err());
        release(second).await;
        // Both short-lived Provider owners have closed; the parent transport stays open.
        assert!(
            function_http
                .client(&factory, "invalid-url", false, http::HeaderMap::new())
                .unwrap()
                .is_none()
        );
        assert!(
            second_http
                .client(&factory, "invalid-url", false, http::HeaderMap::new())
                .unwrap()
                .is_none()
        );
        assert!(
            process_slot().unwrap().clone().try_write_owned().is_err(),
            "failed orphan cleanup must keep its read lease"
        );
        blocked.restore();
        first.1.inner.close_orphan().await.unwrap();
        assert!(process_slot().unwrap().clone().try_write_owned().is_ok());

        let blocked = BlockedHome::new(home.clone());
        assert!(close_process_runtime().await.is_err());
        // Native shutdown succeeds before this deliberately failed home removal.
        for pool in [&function_http, &second_http] {
            assert!(
                pool.client(&factory, "invalid-url", false, http::HeaderMap::new())
                    .is_err()
            );
        }
        assert!(
            process_slot()
                .unwrap()
                .read()
                .await
                .as_ref()
                .unwrap()
                .retiring
                .load(Ordering::Acquire)
        );
        let fallback = opening("pool-key-a", true).await;
        assert!(!Arc::ptr_eq(
            &manager,
            &fallback.2.current().unwrap().manager
        ));
        release(fallback).await;
        blocked.restore();
        close_process_runtime().await.unwrap();
        assert!(!home.exists());
        assert!(process_slot().unwrap().read().await.is_none());
    });
}
