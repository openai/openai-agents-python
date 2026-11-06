//! A Provider owns native acquisition slots, homes and uncredentialed HTTP clients, never Python work.
use std::sync::{Arc, Mutex, Weak};
use std::time::Duration;

use futures_util::future::{BoxFuture, FutureExt, Shared};
use pyo3::prelude::*;
use tokio::sync::Mutex as AsyncMutex;

use crate::{async_bridge, py_error};
type Closing = Shared<BoxFuture<'static, std::result::Result<(), String>>>;
type Home = Arc<Mutex<Option<tempfile::TempDir>>>;

/// One exact-recipe entry; all per-Session owners remain outside this entry.
pub(crate) struct SessionTemplate {
    key: String,
    pub(crate) config: codex_core::config::SdkEphemeralConfigTemplate,
    // Environment-key openings cache Config only, never a usable bearer.
    pub(crate) services: Option<(
        Arc<codex_login::AuthManager>,
        codex_models_manager::manager::SharedModelsManager,
    )>,
}
impl SessionTemplate {
    pub(crate) fn key(recipe: &crate::config::SdkConfigRecipe, api_key: &str) -> String {
        use sha2::{Digest, Sha256};
        // Private callers cannot reuse a different credential's services.
        format!(
            "{}:{:x}",
            recipe.key,
            Sha256::digest(api_key.trim().as_bytes())
        )
    }

    /// Failure to prepare the optimization leaves the ordinary loader in charge.
    pub(crate) async fn prepare(
        recipe: &crate::config::SdkConfigRecipe,
        config: &codex_core_api::Config,
        api_key: &str,
        config_only: bool,
    ) -> Option<(
        Arc<Self>,
        codex_core_api::Config,
        codex_config::CloudConfigBundleLoader,
    )> {
        let template = recipe.capture_template(config)?;
        let bound = recipe.bind(&template).await?;
        let services = if config_only {
            None
        } else {
            // The API-key adapter replaces/drops its cwd-bearing command
            // placeholder before returning. These retained services own no home.
            let auth = crate::auth::manager(&bound.0, Some(api_key.to_owned()))
                .await
                .ok()?;
            let provider = codex_model_provider::create_model_provider(
                bound.0.model_provider.clone(),
                Some(auth.clone()),
            );
            let models = provider.models_manager_without_cache(bound.0.model_catalog.clone());
            models.set_api_key_model_discovery_enabled(
                bound
                    .0
                    .features
                    .enabled(codex_core_api::Feature::ApiKeyModelDiscovery),
            );
            Some((auth, models))
        };
        let entry = Arc::new(Self {
            key: Self::key(recipe, api_key),
            config: template,
            services,
        });
        Some((entry, bound.0, bound.1))
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Phase {
    Opening,
    Adopted,
    Orphan,
    Retired,
}
struct Record {
    phase: Phase,
    engines: Vec<Arc<Mutex<crate::EngineOwner>>>,
    threads: Vec<Arc<Mutex<crate::NativeThreadOwner>>>,
    homes: Vec<Home>,
}
#[derive(Default)]
struct State {
    generation: u64,
    leases: Vec<Arc<Lease>>,
    closed: bool,
    closing: Option<Closing>,
    session_template: Option<Arc<SessionTemplate>>,
}
struct ProviderResource {
    state: Mutex<State>,
    model_http_pool: codex_http_client::SdkModelHttpPool,
}
impl ProviderResource {
    fn new() -> Self {
        Self {
            state: Mutex::new(State::default()),
            model_http_pool: codex_http_client::SdkModelHttpPool::default(),
        }
    }
}
pub(crate) struct Lease {
    resource: Weak<ProviderResource>,
    record: Mutex<Record>,
    cleanup: AsyncMutex<()>,
}
impl Lease {
    pub(crate) fn session_template(&self, key: &str) -> Option<Arc<SessionTemplate>> {
        let resource = self.resource.upgrade()?;
        let state = resource.state.lock().ok()?;
        if state.closed || state.closing.is_some() {
            return None;
        }
        state
            .session_template
            .as_ref()
            .filter(|entry| entry.key == key)
            .cloned()
    }
    pub(crate) fn can_prepare_session_template(&self) -> bool {
        self.resource
            .upgrade()
            .and_then(|resource| {
                let state = resource.state.lock().ok()?;
                Some(!state.closed && state.closing.is_none() && state.session_template.is_none())
            })
            .unwrap_or(false)
    }
    pub(crate) fn publish_session_template(&self, entry: &Arc<SessionTemplate>) {
        let Some(resource) = self.resource.upgrade() else {
            return;
        };
        let Ok(mut state) = resource.state.lock() else {
            return;
        };
        let Ok(record) = self.record.lock() else {
            return;
        };
        if !state.closed
            && state.closing.is_none()
            && state.session_template.is_none()
            && matches!(record.phase, Phase::Opening | Phase::Adopted)
        {
            state.session_template = Some(entry.clone());
        }
    }
    pub(crate) fn model_http_pool(&self) -> PyResult<codex_http_client::SdkModelHttpPool> {
        self.check_live()?;
        let resource = self
            .resource
            .upgrade()
            .ok_or_else(|| py_error("Provider is closed"))?;
        Ok(resource.model_http_pool.clone())
    }
    pub(crate) fn check_live(&self) -> PyResult<()> {
        if !matches!(
            self.record.lock().map_err(py_error)?.phase,
            Phase::Opening | Phase::Adopted
        ) {
            return Err(py_error("Provider acquisition is retired"));
        }
        Ok(())
    }
    fn check(&self, resource: &Arc<ProviderResource>) -> PyResult<()> {
        self.check_live()?;
        if !self.resource.ptr_eq(&Arc::downgrade(resource))
            || self.record.lock().map_err(py_error)?.phase == Phase::Retired
        {
            return Err(py_error(
                "Provider lease is retired or belongs to another Provider",
            ));
        }
        Ok(())
    }
    pub(crate) fn keeps_unpublished_owner(&self) -> bool {
        self.record
            .lock()
            .map(|r| matches!(r.phase, Phase::Opening | Phase::Orphan))
            .unwrap_or(true)
    }
    pub(crate) fn register_engine(&self, owner: Arc<Mutex<crate::EngineOwner>>) -> PyResult<()> {
        self.check_live()?;
        self.record.lock().map_err(py_error)?.engines.push(owner);
        Ok(())
    }
    pub(crate) fn register_thread(
        &self,
        owner: Arc<Mutex<crate::NativeThreadOwner>>,
    ) -> PyResult<()> {
        self.check_live()?;
        self.record.lock().map_err(py_error)?.threads.push(owner);
        Ok(())
    }
    fn release(&self) -> PyResult<()> {
        // Normal Session retirement has already joined every registered owner.
        let resource = self
            .resource
            .upgrade()
            .ok_or_else(|| py_error("Provider disappeared"))?;
        let mut state = resource.state.lock().map_err(py_error)?;
        let mut record = self.record.lock().map_err(py_error)?;
        record.phase = Phase::Retired;
        record.engines.clear();
        record.threads.clear();
        record.homes.clear();
        state
            .leases
            .retain(|lease| !std::ptr::eq(lease.as_ref(), self));
        Ok(())
    }
    // Called only while holding cleanup. Joining owners is distinct from
    // deleting their home: an Environment must outlive these users, but not
    // a failed filesystem cleanup after those users have retired.
    async fn close_resources(&self) -> PyResult<()> {
        let (threads, engines) = {
            let r = self.record.lock().map_err(py_error)?;
            if r.phase == Phase::Retired {
                return Ok(());
            }
            if r.phase != Phase::Orphan {
                return Err(py_error("Provider has open or opening Sessions"));
            }
            (r.threads.clone(), r.engines.clone())
        };
        let mut errors = Vec::new();
        for owner in threads.into_iter().rev() {
            if let Err(error) = crate::close_thread_owner(owner, None).await {
                errors.push(error.to_string());
            }
        }
        for owner in engines.into_iter().rev() {
            if let Err(error) = crate::close_engine_owner(owner).await {
                errors.push(error.to_string());
            }
        }
        if !errors.is_empty() {
            return Err(py_error(errors.join("; additional cleanup failure: ")));
        }
        Ok(())
    }
    async fn close_orphan(&self) -> PyResult<()> {
        let _cleanup = self.cleanup.lock().await;
        self.close_resources().await?;
        let homes = {
            let r = self.record.lock().map_err(py_error)?;
            if r.phase == Phase::Retired {
                return Ok(());
            }
            r.homes.clone()
        };
        let mut errors = Vec::new();
        for home in homes {
            // Keep Provider.close's shared future and admission fence in place
            // until the orphan home is gone. A Windows stdio process may still
            // hold its cwd briefly after Core has requested its termination.
            let deadline = tokio::time::Instant::now() + crate::engine::CLOSE_TIMEOUT;
            let mut first = None;
            loop {
                match close_home(&home) {
                    Ok(()) => break,
                    Err(error)
                        if cfg!(windows)
                            && error
                                .downcast_ref::<std::io::Error>()
                                .and_then(std::io::Error::raw_os_error)
                                == Some(32) =>
                    {
                        if first.is_none() {
                            first = Some(error);
                        }
                        if tokio::time::Instant::now() >= deadline {
                            errors.push(first.expect("sharing violation").to_string());
                            break;
                        }
                        tokio::time::sleep(Duration::from_millis(10)).await;
                    }
                    Err(error) => {
                        errors.push(error.to_string());
                        break;
                    }
                }
            }
        }
        if !errors.is_empty() {
            return Err(py_error(errors.join("; additional cleanup failure: ")));
        }
        self.release()
    }
}
fn close_home(home: &Home) -> anyhow::Result<()> {
    let mut slot = home
        .lock()
        .map_err(|error| anyhow::anyhow!(error.to_string()))?;
    if let Some(directory) = slot.as_mut() {
        match std::fs::remove_dir_all(directory.path()) {
            Ok(()) => {
                // Removal succeeded; do not repeat it from TempDir::drop.
                directory.disable_cleanup(true);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        slot.take();
    }
    Ok(())
}
#[pyclass(module = "agents._native")]
pub(crate) struct NativeProviderHome {
    inner: Home,
    name: String,
}
#[pymethods]
impl NativeProviderHome {
    #[getter]
    fn name(&self) -> String {
        self.name.clone()
    }
    fn cleanup(&self) -> PyResult<()> {
        close_home(&self.inner).map_err(py_error)
    }
}
#[pyclass(module = "agents._native")]
pub(crate) struct NativeProviderLease {
    pub(crate) inner: Arc<Lease>,
    // The wrapper keeps the resource alive; registry records only point back weakly.
    resource: Arc<ProviderResource>,
}
#[pymethods]
impl NativeProviderLease {
    fn release(&self) -> PyResult<()> {
        self.inner.release()
    }
    fn adopt(&self) -> PyResult<()> {
        let mut r = self.inner.record.lock().map_err(py_error)?;
        if r.phase != Phase::Opening {
            return Err(py_error("Invalid Provider adoption"));
        }
        r.phase = Phase::Adopted;
        Ok(())
    }
    fn orphan(&self) -> PyResult<()> {
        let mut r = self.inner.record.lock().map_err(py_error)?;
        if r.phase == Phase::Opening {
            r.phase = Phase::Orphan;
        } else if r.phase != Phase::Orphan {
            return Err(py_error("Invalid Provider orphan transition"));
        }
        Ok(())
    }
    fn close_orphan<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let lease = self.inner.clone();
        let resource = self.resource.clone();
        async_bridge::future_into_py(py, async move {
            let result = lease.close_orphan().await;
            drop(resource);
            result
        })
    }
    fn close_orphan_resources<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let lease = self.inner.clone();
        let resource = self.resource.clone();
        async_bridge::future_into_py(py, async move {
            let _cleanup = lease.cleanup.lock().await;
            let result = lease.close_resources().await;
            drop(resource);
            result
        })
    }
    fn home(&self) -> PyResult<NativeProviderHome> {
        self.inner.check(&self.resource)?;
        let directory = tempfile::Builder::new()
            .prefix("native-agent-")
            .tempdir()
            .map_err(py_error)?;
        std::fs::create_dir(directory.path().join("workspace")).map_err(py_error)?;
        let name = directory.path().to_string_lossy().into_owned();
        let inner = Arc::new(Mutex::new(Some(directory)));
        self.inner
            .record
            .lock()
            .map_err(py_error)?
            .homes
            .push(inner.clone());
        Ok(NativeProviderHome { inner, name })
    }
}
#[pyclass(module = "agents._native")]
pub(crate) struct NativeProviderOwner {
    inner: Arc<ProviderResource>,
}
#[pymethods]
impl NativeProviderOwner {
    #[new]
    fn new() -> Self {
        Self {
            inner: Arc::new(ProviderResource::new()),
        }
    }
    fn acquire(&self) -> PyResult<NativeProviderLease> {
        let mut state = self.inner.state.lock().map_err(py_error)?;
        if state.closed || state.closing.is_some() {
            return Err(py_error("NativeProvider is closing or closed"));
        }
        let lease = Arc::new(Lease {
            resource: Arc::downgrade(&self.inner),
            record: Mutex::new(Record {
                phase: Phase::Opening,
                engines: vec![],
                threads: vec![],
                homes: vec![],
            }),
            cleanup: AsyncMutex::new(()),
        });
        state.leases.push(lease.clone());
        Ok(NativeProviderLease {
            inner: lease,
            resource: self.inner.clone(),
        })
    }
    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let (generation, pending) = {
            let mut state = self.inner.state.lock().map_err(py_error)?;
            if state.leases.iter().any(|lease| {
                lease
                    .record
                    .lock()
                    .map(|r| matches!(r.phase, Phase::Opening | Phase::Adopted))
                    .unwrap_or(true)
            }) {
                return Err(py_error("NativeProvider has open or opening Sessions"));
            }
            if state.closed {
                return async_bridge::future_into_py(py, async { Ok(()) });
            }
            if let Some(pending) = &state.closing {
                (state.generation, pending.clone())
            } else {
                let leases = state.leases.clone();
                let model_http_pool = self.inner.model_http_pool.clone();
                let resource = self.inner.clone();
                let task = pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
                    let mut errors = Vec::new();
                    for lease in leases {
                        if let Err(error) = lease.close_orphan().await {
                            errors.push(error.to_string());
                        }
                    }
                    if !errors.is_empty() {
                        return Err(errors.join("; additional cleanup failure: "));
                    }
                    // Scoped owners have joined before cached services retire.
                    let retired = resource
                        .state
                        .lock()
                        .map_err(|error| error.to_string())?
                        .session_template
                        .take();
                    drop(retired);
                    model_http_pool.close();
                    Ok(())
                });
                let pending = async move { task.await.map_err(|error| error.to_string())? }
                    .boxed()
                    .shared();
                state.generation += 1;
                state.closing = Some(pending.clone());
                (state.generation, pending)
            }
        };
        let resource = self.inner.clone();
        async_bridge::future_into_py(py, async move {
            let result = pending.await;
            let mut state = resource.state.lock().map_err(py_error)?;
            if state.generation == generation {
                state.closing = None;
                if result.is_ok() {
                    state.closed = true;
                }
            }
            result.map_err(py_error)
        })
    }
}

#[cfg(all(test, windows))]
mod tests {
    use std::fs::OpenOptions;
    use std::os::windows::fs::OpenOptionsExt;
    use std::pin::pin;

    use super::*;

    // A real exclusive Windows file handle rejects removal with ERROR_SHARING_VIOLATION.
    // These tests exercise the same orphan slots Provider.close retires, with no model
    // or process timing dependency. Paused Tokio time makes the full deadline bounded.
    #[tokio::test(start_paused = true)]
    async fn orphan_home_waits_for_windows_lock_before_retiring_lease() {
        Python::initialize();
        let owner = NativeProviderOwner::new();
        let lease = owner.acquire().expect("acquire");
        let home = lease.home().expect("private home");
        let path = std::path::Path::new(&home.name);
        let locked = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .share_mode(0)
            .open(path.join("still-owned"))
            .expect("open exclusive file");
        lease.orphan().expect("unpublished owner");
        let mut closing = pin!(lease.inner.close_orphan());
        assert!(futures_util::poll!(&mut closing).is_pending());
        assert!(path.exists());
        assert!(lease.inner.record.lock().unwrap().phase == Phase::Orphan);
        assert_eq!(owner.inner.state.lock().unwrap().leases.len(), 1);
        drop(locked);
        closing.await.expect("retire after lock release");
        assert!(!path.exists());
        assert!(lease.inner.record.lock().unwrap().phase == Phase::Retired);
        assert!(owner.inner.state.lock().unwrap().leases.is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn expired_windows_orphan_cleanup_keeps_original_failure_and_real_home_for_retry() {
        Python::initialize();
        let owner = NativeProviderOwner::new();
        let lease = owner.acquire().expect("acquire");
        let home = lease.home().expect("private home");
        let path = std::path::Path::new(&home.name);
        let locked = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .share_mode(0)
            .open(path.join("still-owned"))
            .expect("open exclusive file");
        lease.orphan().expect("unpublished owner");
        let original = close_home(&home.inner).expect_err("still locked");
        assert_eq!(
            original
                .downcast_ref::<std::io::Error>()
                .and_then(std::io::Error::raw_os_error),
            Some(32)
        );
        let mut closing = pin!(lease.inner.close_orphan());
        assert!(futures_util::poll!(&mut closing).is_pending());
        tokio::time::advance(crate::engine::CLOSE_TIMEOUT).await;
        let error = closing.await.expect_err("bounded sharing violation");
        assert!(error.to_string().ends_with(&original.to_string()));
        assert!(path.exists());
        assert!(home.inner.lock().unwrap().is_some());
        assert!(lease.inner.record.lock().unwrap().phase == Phase::Orphan);
        assert_eq!(owner.inner.state.lock().unwrap().leases.len(), 1);
        drop(locked);
        lease
            .inner
            .close_orphan()
            .await
            .expect("retry real cleanup");
        assert!(!path.exists());
        assert!(home.inner.lock().unwrap().is_none());
        assert!(lease.inner.record.lock().unwrap().phase == Phase::Retired);
        assert!(owner.inner.state.lock().unwrap().leases.is_empty());
    }
}

#[cfg(all(test, target_os = "linux"))]
mod session_template_tests {
    //! Verify service reuse preserves independently owned Engines.
    use super::*;
    use crate::engine::EngineState;
    use serde_json::json;

    fn inputs(home: &std::path::Path) -> (String, String) {
        let config = json!({
        "include_permissions_instructions": false, "include_environment_context": false,
        "model_provider": "native_agent", "model_providers.native_agent": {
            "name": "Native agent provider", "base_url": "http://127.0.0.1:1/v1",
            "wire_api": "responses", "requires_openai_auth": true,
            "supports_websockets": false, "supports_standalone_web_search": false
        }, "agents.enabled": false, "features.goals": false, "tools.update_plan.enabled": false,
        "mcp_servers": {}, "features.non_prefixed_mcp_tool_names": false,
        "features.code_mode": { "enabled": false, "direct_only_tool_namespaces": ["functions"] },
        "features.code_mode_interrupt": false, "web_search": "disabled", "features.standalone_web_search": false
    }).to_string();
        let runtime = json!({ "strict_config": true,
        "loader": { "ignore_user_config": true, "ignore_project_config": true },
        "base_instructions": "SDK instructions", "codex_home": home,
        "cwd": home.join("workspace") })
        .to_string();
        (config, runtime)
    }

    async fn scope(
        home: &std::path::Path,
        lease: Option<Arc<Lease>>,
        key: &str,
    ) -> Arc<EngineState> {
        scope_with_mode(home, lease, key, false).await
    }

    async fn scope_with_mode(
        home: &std::path::Path,
        lease: Option<Arc<Lease>>,
        key: &str,
        config_only: bool,
    ) -> Arc<EngineState> {
        std::fs::create_dir(home.join("workspace")).expect("private workspace");
        let (config, runtime) = inputs(home);
        Arc::new(
            EngineState::new_with_store(
                config,
                runtime,
                false,
                Some(key.to_owned()),
                None,
                None,
                true,
                lease,
                config_only,
            )
            .await
            .expect("ordinary API-key Engine"),
        )
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn exact_recipe_shares_only_services_and_raw_creation_stays_independent() {
        let owner = NativeProviderOwner::new();
        let a_lease = owner.acquire().expect("first lease");
        let b_lease = owner.acquire().expect("second lease");
        let a_home = tempfile::tempdir().unwrap();
        let b_home = tempfile::tempdir().unwrap();
        let raw_home = tempfile::tempdir().unwrap();
        let a = scope(a_home.path(), Some(a_lease.inner.clone()), "synthetic-key").await;
        let entry = owner
            .inner
            .state
            .lock()
            .unwrap()
            .session_template
            .clone()
            .expect("normal SDK recipe must actually populate the entry");
        let b = scope(b_home.path(), Some(b_lease.inner.clone()), "synthetic-key").await;
        assert!(Arc::ptr_eq(
            &entry.services.as_ref().unwrap().0,
            &a.manager.auth_manager()
        ));
        assert!(Arc::ptr_eq(
            &a.manager.auth_manager(),
            &b.manager.auth_manager()
        ));
        assert!(Arc::ptr_eq(
            &a.manager.get_models_manager(),
            &b.manager.get_models_manager()
        ));
        assert!(!Arc::ptr_eq(&a.manager, &b.manager));
        assert!(!Arc::ptr_eq(&a.store, &b.store));
        let raw = scope(raw_home.path(), None, "synthetic-key").await;
        assert!(!Arc::ptr_eq(
            &raw.manager.auth_manager(),
            &entry.services.as_ref().unwrap().0
        ));
        assert!(!Arc::ptr_eq(
            &raw.manager.get_models_manager(),
            &entry.services.as_ref().unwrap().1
        ));
        a.close().await.unwrap();
        assert!(b.manager.list_thread_ids().await.is_empty());
        b.close().await.unwrap();
        raw.close().await.unwrap();
        a_lease.inner.release().unwrap();
        b_lease.inner.release().unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn different_credentials_fall_back_without_replacing_the_first_entry() {
        let owner = NativeProviderOwner::new();
        let a_lease = owner.acquire().unwrap();
        let b_lease = owner.acquire().unwrap();
        let a_home = tempfile::tempdir().unwrap();
        let b_home = tempfile::tempdir().unwrap();
        let a = scope(a_home.path(), Some(a_lease.inner.clone()), "first-key").await;
        let entry = owner
            .inner
            .state
            .lock()
            .unwrap()
            .session_template
            .clone()
            .expect("entry");
        let b = scope(b_home.path(), Some(b_lease.inner.clone()), "second-key").await;
        assert!(!Arc::ptr_eq(
            &entry.services.as_ref().unwrap().0,
            &b.manager.auth_manager()
        ));
        assert!(!Arc::ptr_eq(
            &entry.services.as_ref().unwrap().1,
            &b.manager.get_models_manager()
        ));
        assert!(Arc::ptr_eq(
            &entry,
            owner
                .inner
                .state
                .lock()
                .unwrap()
                .session_template
                .as_ref()
                .unwrap()
        ));
        a.close().await.unwrap();
        b.close().await.unwrap();
        a_lease.inner.release().unwrap();
        b_lease.inner.release().unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn config_only_openings_bind_the_entry_but_keep_fresh_credential_owners() {
        let owner = NativeProviderOwner::new();
        let mut scopes: Vec<Arc<EngineState>> = Vec::new();
        let mut leases = Vec::new();
        let mut homes = Vec::new();
        let mut first_entry = None;
        let mut credential_owners = Vec::new();
        for key in [
            "environment-A",
            "environment-A",
            "environment-B",
            "environment-A",
        ] {
            let lease = owner.acquire().unwrap();
            let home = tempfile::tempdir().unwrap();
            let engine = scope_with_mode(home.path(), Some(lease.inner.clone()), key, true).await;
            let entry = owner
                .inner
                .state
                .lock()
                .unwrap()
                .session_template
                .clone()
                .unwrap();
            assert!(entry.services.is_none());
            assert!(!entry.key.contains("environment-A"));
            assert!(!entry.key.contains("environment-B"));
            assert!(!format!("{:?}", engine.service_config).contains(key));
            if key == "environment-A" {
                let used = engine
                    .session_template_witness
                    .as_ref()
                    .unwrap()
                    .upgrade()
                    .unwrap();
                assert!(Arc::ptr_eq(&used, &entry));
            } else {
                // The one slot still belongs to A; B uses the ordinary loader.
                assert!(engine.session_template_witness.is_none());
            }
            if let Some(first) = &first_entry {
                assert!(Arc::ptr_eq(first, &entry));
            } else {
                first_entry = Some(entry);
            }
            for earlier in &scopes {
                assert!(!Arc::ptr_eq(
                    &earlier.manager.auth_manager(),
                    &engine.manager.auth_manager()
                ));
                assert!(!Arc::ptr_eq(
                    &earlier.manager.get_models_manager(),
                    &engine.manager.get_models_manager()
                ));
                assert!(!Arc::ptr_eq(&earlier.manager, &engine.manager));
            }
            credential_owners.push((
                Arc::downgrade(&engine.manager.auth_manager()),
                Arc::downgrade(&engine.manager.get_models_manager()),
            ));
            scopes.push(engine);
            leases.push(lease);
            homes.push(home);
        }
        for engine in scopes {
            engine.close().await.unwrap();
        }
        for lease in leases {
            lease.inner.release().unwrap();
        }
        // The Provider and Config entry remain live, but no cached owner can
        // keep a captured environment bearer or model service alive.
        assert!(
            credential_owners
                .iter()
                .all(|(auth, models)| auth.upgrade().is_none() && models.upgrade().is_none())
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn config_only_requests_never_borrow_retained_services_and_do_not_upgrade_the_slot() {
        for initial_config_only in [false, true] {
            let owner = NativeProviderOwner::new();
            let first_lease = owner.acquire().unwrap();
            let second_lease = owner.acquire().unwrap();
            let third_lease = owner.acquire().unwrap();
            let first_home = tempfile::tempdir().unwrap();
            let second_home = tempfile::tempdir().unwrap();
            let third_home = tempfile::tempdir().unwrap();
            let a = scope_with_mode(
                first_home.path(),
                Some(first_lease.inner.clone()),
                "same-captured-key",
                initial_config_only,
            )
            .await;
            let entry = owner
                .inner
                .state
                .lock()
                .unwrap()
                .session_template
                .clone()
                .unwrap();
            let b = scope_with_mode(
                second_home.path(),
                Some(second_lease.inner.clone()),
                "same-captured-key",
                !initial_config_only,
            )
            .await;
            let c = scope_with_mode(
                third_home.path(),
                Some(third_lease.inner.clone()),
                "same-captured-key",
                initial_config_only,
            )
            .await;
            for engine in [&a, &b, &c] {
                let used = engine
                    .session_template_witness
                    .as_ref()
                    .unwrap()
                    .upgrade()
                    .unwrap();
                assert!(Arc::ptr_eq(&used, &entry));
            }
            assert!(!Arc::ptr_eq(
                &a.manager.auth_manager(),
                &b.manager.auth_manager()
            ));
            assert!(!Arc::ptr_eq(
                &a.manager.get_models_manager(),
                &b.manager.get_models_manager()
            ));
            assert_eq!(entry.services.is_none(), initial_config_only);
            if initial_config_only {
                assert!(!Arc::ptr_eq(
                    &a.manager.auth_manager(),
                    &c.manager.auth_manager()
                ));
                assert!(!Arc::ptr_eq(
                    &a.manager.get_models_manager(),
                    &c.manager.get_models_manager()
                ));
            } else {
                assert!(Arc::ptr_eq(
                    &a.manager.auth_manager(),
                    &c.manager.auth_manager()
                ));
                assert!(Arc::ptr_eq(
                    &a.manager.get_models_manager(),
                    &c.manager.get_models_manager()
                ));
            }
            a.close().await.unwrap();
            b.close().await.unwrap();
            c.close().await.unwrap();
            first_lease.inner.release().unwrap();
            second_lease.inner.release().unwrap();
            third_lease.inner.release().unwrap();
        }
    }
}
