//! A Provider owns native acquisition slots, homes and uncredentialed HTTP clients, never Python work.
use std::sync::{Arc, Mutex, Weak};
use std::time::Duration;

use futures_util::future::{BoxFuture, FutureExt, Shared};
use pyo3::prelude::*;
use tokio::sync::Mutex as AsyncMutex;

use crate::{async_bridge, py_error};
mod runtime;

type Closing = Shared<BoxFuture<'static, std::result::Result<(), String>>>;
type Home = Arc<Mutex<Option<tempfile::TempDir>>>;

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
}
struct ProviderResource {
    state: Mutex<State>,
    model_http_pool: codex_http_client::SdkModelHttpPool,
    prepared_runtime: AsyncMutex<Option<runtime::PreparedRuntime>>,
}
impl ProviderResource {
    fn new() -> Self {
        Self {
            state: Mutex::new(State::default()),
            model_http_pool: codex_http_client::SdkModelHttpPool::default(),
            prepared_runtime: AsyncMutex::new(None),
        }
    }
}
pub(crate) struct Lease {
    resource: Weak<ProviderResource>,
    record: Mutex<Record>,
    cleanup: AsyncMutex<()>,
}
impl Lease {
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
    pub(crate) fn release(&self) -> PyResult<()> {
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
    if let Some(directory) = slot.as_ref() {
        match std::fs::remove_dir_all(directory.path()) {
            Ok(()) => {}
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
                    let _resource = resource;
                    let mut errors = Vec::new();
                    for lease in leases {
                        if let Err(error) = lease.close_orphan().await {
                            errors.push(error.to_string());
                        }
                    }
                    if !errors.is_empty() {
                        return Err(errors.join("; additional cleanup failure: "));
                    }
                    // All scopes have joined before their shared runtime retires.
                    _resource
                        .close_prepared_runtime()
                        .await
                        .map_err(|error| error.to_string())?;
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
